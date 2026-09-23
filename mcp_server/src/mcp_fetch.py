import html.parser
import ipaddress
import json
import logging
import os
import re
import socket
import urllib.parse

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from rich.console import Console
from rich.logging import RichHandler

# 无密钥依赖: 不使用 env_guard(该守卫用于 PG_MCP_DSN/DB_PASSWORD 等密钥类 server)

console = Console(stderr=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True, show_time=True, show_path=False)]
)

logger = logging.getLogger("mcp.fetch")

mcp = MCPServer("Rosiwit MCP Server")

DEFAULT_MAX_CHARS = 40000
MAX_MAX_CHARS = 200000
JSON_MAX_BYTES = 200 * 1024
MAX_REDIRECTS = 5
USER_AGENT = "RosiwitMCPFetch/1.0"

_TEXT_CONTENT_TYPES = {
    "application/json",
    "application/javascript",
    "application/x-javascript",
    "application/xml",
    "application/xhtml+xml",
    "application/x-ndjson",
}

_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "canvas", "iframe"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

# 运营商级 NAT(CGNAT) 段: 并非全球可路由公网, 显式拒绝(部分 Python 版本 is_private 不含它)
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


class FetchError(Exception):
    """抓取失败, message 为可读中文原因。"""


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(str(os.getenv(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def _max_bytes() -> int:
    """单次下载上限(字节), 环境变量 MCP_FETCH_MAX_BYTES, 默认 2MB。"""
    return _env_int("MCP_FETCH_MAX_BYTES", 2 * 1024 * 1024)


def _timeout() -> float:
    """请求超时(秒), 环境变量 MCP_FETCH_TIMEOUT, 默认 20s。"""
    return float(_env_int("MCP_FETCH_TIMEOUT", 20, minimum=1))


def _allow_hosts() -> set[str]:
    """环境变量 MCP_FETCH_ALLOW_HOSTS 的逗号白名单(命中后放行内网地址)。"""
    raw = os.getenv("MCP_FETCH_ALLOW_HOSTS", "")
    return {_normalize_host(part) for part in raw.split(",") if part.strip()}


def _normalize_host(host: str) -> str:
    return (host or "").strip().lower().rstrip(".")


def _check_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> tuple[bool, str]:
    """拒绝私有/环回/链路本地/保留/组播/未指定地址(含 IPv4 映射与 6to4 形式)与 CGNAT。"""
    candidates = [ip]
    mapped = getattr(ip, "ipv4_mapped", None)
    sixtofour = getattr(ip, "sixtofour", None)
    if mapped is not None:
        candidates.append(mapped)
    if sixtofour is not None:
        candidates.append(sixtofour)
    for item in candidates:
        if isinstance(item, ipaddress.IPv4Address) and item in _CGNAT_NETWORK:
            return False, f"禁止访问运营商级 NAT(CGNAT) 地址: {ip}"
        if (
            item.is_private
            or item.is_loopback
            or item.is_link_local
            or item.is_reserved
            or item.is_multicast
            or item.is_unspecified
        ):
            return False, f"禁止访问内网/保留地址: {ip}"
    return True, ""


def is_safe_url(url: str, allow_hosts: set[str] | list[str] | tuple[str, ...] | None = None) -> tuple[bool, str]:
    """SSRF 防护纯函数: 仅允许 http/https, 且 host 不能是内网/保留地址。

    allow_hosts 命中的主机名直接放行(用于显式放行内网指定主机)。域名(非字面 IP)的
    解析结果在真实抓取时另行校验(_ensure_public_host), 防止 DNS 指向内网。
    返回 (ok, reason); reason 为可读中文原因, 通过时为 ""。
    """
    allow = {_normalize_host(item) for item in (allow_hosts or []) if str(item).strip()}
    try:
        parsed = urllib.parse.urlsplit(str(url or "").strip())
    except ValueError as exc:
        return False, f"URL 解析失败: {exc}"

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, f"仅允许 http/https URL, 当前 scheme: {scheme or '(空)'}"

    host = _normalize_host(parsed.hostname or "")
    if not host:
        return False, "URL 缺少主机名"
    if host in allow:
        return True, ""
    if host == "localhost" or host.endswith(".localhost"):
        return False, f"禁止访问本机地址: {host}"

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True, ""
    return _check_ip(ip)


def _ensure_public_host(url: str, allow_hosts: set[str] | None = None) -> None:
    """域名解析后逐个校验 IP, 拒绝解析到内网的域名(DNS rebinding 兜底)。

    说明: 此处的解析复检只能**缓解**、不能完全防止 DNS rebinding —— 校验与随后
    httpx 实际连接之间存在 TOCTOU 时间窗, 且 httpx 会自行再次解析域名。企业部署
    建议用 MCP_FETCH_ALLOW_HOSTS 固化可信主机, 或经出网代理/防火墙限制可达网段。
    """
    host = _normalize_host(urllib.parse.urlsplit(url).hostname or "")
    if host in {_normalize_host(item) for item in (allow_hosts or set())}:
        return
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise FetchError(f"域名解析失败: {host} ({exc})") from exc
    resolved = {info[4][0].split("%")[0] for info in infos}
    for text in sorted(resolved):
        try:
            ip = ipaddress.ip_address(text)
        except ValueError:
            raise FetchError(f"域名 {host} 解析出非法地址: {text}") from None
        ok, reason = _check_ip(ip)
        if not ok:
            raise FetchError(f"{reason}(域名 {host} 解析到内网/保留地址)")


def truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    """按字符数截断文本, 返回 (文本, 是否被截断)。"""
    try:
        limit = max(0, int(max_chars))
    except (TypeError, ValueError):
        limit = 0
    value = text or ""
    if len(value) <= limit:
        return value, False
    return value[:limit], True


class _MarkdownHTMLParser(html.parser.HTMLParser):
    """把 HTML 转成保留标题/链接/列表的 Markdown 风格文本, 丢弃 script/style。"""

    def __init__(self, base_url: str = ""):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url or ""
        self._parts: list[str] = []
        self._title_parts: list[str] = []
        self._in_title = False
        self._skip_depth = 0
        self._link_stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        params = {key.lower(): (value or "") for key, value in attrs}
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return

        if tag == "title":
            self._in_title = True
        elif tag == "br":
            self._emit("\n")
        elif tag == "hr":
            self._emit("\n\n---\n\n")
        elif tag in _HEADING_TAGS:
            self._emit("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self._emit("\n- ")
        elif tag in ("ul", "ol", "p", "div", "tr", "blockquote", "section", "article", "table"):
            self._emit("\n")
        elif tag == "pre":
            self._emit("\n```\n")
        elif tag == "a":
            target = self._absolute_url(params.get("href", ""))
            self._link_stack.append(target)
            if target:
                self._emit("[")
        elif tag == "img":
            src = self._absolute_url(params.get("src", ""))
            if src:
                self._emit(f"![{params.get('alt', '')}]({src})")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return

        if tag == "title":
            self._in_title = False
        elif tag in _HEADING_TAGS or tag in ("ul", "ol", "table", "blockquote"):
            self._emit("\n\n")
        elif tag in ("p", "div", "tr", "section", "article"):
            self._emit("\n")
        elif tag == "pre":
            self._emit("\n```\n")
        elif tag == "a":
            target = self._link_stack.pop() if self._link_stack else ""
            if target:
                self._emit(f"]({target})")

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
        else:
            self._parts.append(data)

    def _emit(self, text: str) -> None:
        self._parts.append(text)

    def _absolute_url(self, href: str) -> str:
        href = (href or "").strip()
        if not href:
            return ""
        try:
            target = urllib.parse.urljoin(self.base_url, href)
        except ValueError:
            return ""
        scheme = urllib.parse.urlsplit(target).scheme.lower()
        if scheme not in ("http", "https", "mailto"):
            return ""
        return target

    def result(self) -> tuple[str, str]:
        raw = "".join(self._parts).replace("\xa0", " ")
        raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
        raw = re.sub(r" *\n *", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        title = re.sub(r"\s+", " ", "".join(self._title_parts)).strip()
        return title, raw.strip()


def html_to_text(html: str, base_url: str = "") -> tuple[str, str]:
    """把 HTML 转为 Markdown 风格纯文本, 返回 (title, text); base_url 用于补全相对链接。"""
    parser = _MarkdownHTMLParser(base_url=base_url)
    try:
        parser.feed(html or "")
        parser.close()
    except Exception as exc:  # HTMLParser 对畸形标记一般容错, 兜底退回原文
        logger.warning(f"HTML 解析失败, 退回原始文本: {exc}")
        return "", (html or "").strip()
    return parser.result()


def _is_text_content_type(content_type: str) -> bool:
    media = (content_type or "").split(";")[0].strip().lower()
    if not media:
        return True
    if media.startswith("text/"):
        return True
    if media in _TEXT_CONTENT_TYPES:
        return True
    return media.endswith("+json") or media.endswith("+xml")


def _is_html_content_type(content_type: str) -> bool:
    media = (content_type or "").split(";")[0].strip().lower()
    return media in ("text/html", "application/xhtml+xml")


def _decode_body(body: bytes, content_type: str) -> str:
    match = re.search(r"charset\s*=\s*[\"']?([\w\-]+)", content_type or "", flags=re.IGNORECASE)
    encodings = [match.group(1)] if match else []
    encodings += ["utf-8", "gb18030"]
    for encoding in encodings:
        try:
            return body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


def _fetch_bytes(
    url: str, *, max_bytes: int, timeout: float, allow_hosts: set[str] | None
) -> tuple[bytes, int, str, str, bool]:
    """手动跟随重定向(逐跳校验 SSRF), 流式读取并限制下载字节数。

    返回 (body, status, content_type, final_url, truncated); 超过 max_bytes 时截断并
    置 truncated=True(不抛异常), 由调用方决定是否接受不完整数据。
    """
    current = url
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json,text/plain;q=0.8,*/*;q=0.5",
    }
    with httpx.Client(timeout=timeout, follow_redirects=False, headers=headers) as client:
        for _ in range(MAX_REDIRECTS + 1):
            ok, reason = is_safe_url(current, allow_hosts)
            if not ok:
                raise FetchError(reason)
            _ensure_public_host(current, allow_hosts)
            with client.stream("GET", current) as response:
                if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("location"):
                    current = urllib.parse.urljoin(current, response.headers["location"])
                    continue
                if response.status_code >= 400:
                    raise FetchError(f"HTTP {response.status_code} {response.reason_phrase}")
                content_type = response.headers.get("content-type", "")
                chunks: list[bytes] = []
                total = 0
                truncated = False
                for chunk in response.iter_bytes():
                    if not chunk:
                        continue
                    if total + len(chunk) > max_bytes:
                        chunks.append(chunk[: max_bytes - total])
                        total = max_bytes
                        truncated = True
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                return b"".join(chunks), response.status_code, content_type, str(response.url), truncated
        raise FetchError(f"重定向超过 {MAX_REDIRECTS} 次")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True))
def fetch_url(url: str, max_chars: int = DEFAULT_MAX_CHARS, raw: bool = False) -> dict:
    """抓取网页并转为纯文本/Markdown 风格文本(只读, 会出网)。

    参数:
    - url: http/https 地址; 私有/环回/链路本地/保留/运营商级 NAT(100.64.0.0/10) 地址
      一律拒绝(可通过环境变量 MCP_FETCH_ALLOW_HOSTS 配置逗号白名单显式放行内网指定主机)
    - max_chars: 返回文本的最大字符数(默认 40000, 上限 200000)
    - raw: true 返回原始文本(不转 Markdown), 仍受 max_chars 与下载上限约束

    返回 url(最终地址)/status/content_type/title(HTML 有标题时)/text/truncated。
    下载上限由 MCP_FETCH_MAX_BYTES 控制(默认 2MB), 超时由 MCP_FETCH_TIMEOUT 控制
    (默认 20s), 重定向最多 5 跳(逐跳复用 SSRF 校验); 非文本 content-type 或失败时
    返回 {"error": "..."}。
    说明: 本服务不解析/不强制 robots.txt(不代替授权判断), 请仅抓取已获授权的页面;
    企业部署建议用 MCP_FETCH_ALLOW_HOSTS 限定可访问主机。
    """
    logger.info(f"抓取 URL: {url} (raw={raw}, max_chars={max_chars})")
    allow_hosts = _allow_hosts()
    ok, reason = is_safe_url(url, allow_hosts)
    if not ok:
        logger.warning(f"URL 校验拒绝: {reason}")
        return {"error": reason}
    try:
        limit = int(max_chars) if max_chars and int(max_chars) > 0 else DEFAULT_MAX_CHARS
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_CHARS
    limit = min(limit, MAX_MAX_CHARS)

    try:
        body, status, content_type, final_url, byte_truncated = _fetch_bytes(
            url, max_bytes=_max_bytes(), timeout=_timeout(), allow_hosts=allow_hosts
        )
    except FetchError as exc:
        return {"error": str(exc)}
    except httpx.HTTPError as exc:
        return {"error": f"抓取失败: {type(exc).__name__}: {exc}"}

    if not _is_text_content_type(content_type):
        return {"error": f"不支持的内容类型: {content_type or '(未知)'}, 仅支持文本类响应"}

    decoded = _decode_body(body, content_type)
    title = ""
    if raw:
        text = decoded
    elif _is_html_content_type(content_type):
        title, text = html_to_text(decoded, base_url=final_url)
    else:
        text = decoded
    text, char_truncated = truncate_text(text, limit)

    payload = {
        "url": final_url,
        "status": status,
        "content_type": content_type,
        "text": text,
        "truncated": byte_truncated or char_truncated,
    }
    if title:
        payload["title"] = title
    logger.info(f"抓取完成: {final_url} status={status} chars={len(text)} truncated={payload['truncated']}")
    return payload


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True))
def fetch_json(url: str) -> dict:
    """抓取 JSON 接口并返回解析后的结构化数据(只读, 会出网, 限 200KB)。

    参数:
    - url: http/https 地址, 与 fetch_url 相同的 SSRF 防护(私有/环回/保留/CGNAT 拒绝,
      仅 MCP_FETCH_ALLOW_HOSTS 白名单可放行内网主机; 重定向逐跳校验)

    返回 url(最终地址)/status/data(解析后的 JSON)。
    响应超过 200KB、内容类型非文本或 JSON 非法时返回 {"error": "..."}。
    """
    logger.info(f"抓取 JSON: {url}")
    allow_hosts = _allow_hosts()
    ok, reason = is_safe_url(url, allow_hosts)
    if not ok:
        logger.warning(f"URL 校验拒绝: {reason}")
        return {"error": reason}

    try:
        body, status, content_type, final_url, byte_truncated = _fetch_bytes(
            url, max_bytes=JSON_MAX_BYTES, timeout=_timeout(), allow_hosts=allow_hosts
        )
    except FetchError as exc:
        return {"error": str(exc)}
    except httpx.HTTPError as exc:
        return {"error": f"抓取失败: {type(exc).__name__}: {exc}"}

    if byte_truncated:
        return {"error": f"JSON 响应超过 {JSON_MAX_BYTES // 1024}KB 上限, 拒绝返回不完整数据"}
    if not _is_text_content_type(content_type):
        return {"error": f"不支持的内容类型: {content_type or '(未知)'}"}
    try:
        data = json.loads(_decode_body(body, content_type))
    except (json.JSONDecodeError, ValueError) as exc:
        return {"error": f"响应不是合法 JSON: {exc}"}
    return {"url": final_url, "status": status, "data": data}


if __name__ == "__main__":
    logger.info("启动 MCP Server")
    mcp.run()
