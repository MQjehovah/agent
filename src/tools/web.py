import html
import json
import logging
import os
import re
from typing import Dict, Any
from urllib.parse import unquote

import httpx

from . import BuiltinTool

logger = logging.getLogger("agent.tools")

_SEARCH_BACKENDS = [
    "tavily",
    "serper",
    "bing",
    "searxng",
    "duckduckgo",
    "duckduckgo_api",
]


def _get_env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _get_env_int(key: str, default: int = 0) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


def _get_ordered_backends() -> list:
    order_str = _get_env("SEARCH_BACKENDS", "").strip()
    if not order_str:
        backends = []
        if _get_env("TAVILY_API_KEY"):
            backends.append("tavily")
        if _get_env("SERPER_API_KEY"):
            backends.append("serper")
        if _get_env("BING_SEARCH_API_KEY"):
            backends.append("bing")
        if _get_env("SEARXNG_URL"):
            backends.append("searxng")
        backends.extend(["duckduckgo", "duckduckgo_api"])
        return backends
    configured = [b.strip() for b in order_str.split(",") if b.strip()]
    return [b for b in configured if b in _SEARCH_BACKENDS] or ["duckduckgo", "duckduckgo_api"]


class WebSearchTool(BuiltinTool):
    """网络搜索工具 — 多后端容错，按优先级依次尝试"""

    DUCKDUCKGO_HTML_URL = "https://html.duckduckgo.com/html/"
    TAVILY_URL = "https://api.tavily.com/search"
    SERPER_URL = "https://google.serper.dev/search"
    BING_URL = "https://api.bing.microsoft.com/v7.0/search"

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return """搜索互联网获取最新信息。返回搜索结果列表，包含标题、摘要和链接。

使用场景：
- 查询技术文档和最新API信息
- 搜索解决方案和最佳实践
- 获取最新的新闻和动态
- 查找开源项目和库的使用方法"""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词"
                },
                "max_results": {
                    "type": "integer",
                    "description": "最大返回结果数",
                    "default": 10
                }
            },
            "required": ["query"]
        }

    async def execute(self, **kwargs) -> str:
        query = kwargs.get("query", "")
        max_results = kwargs.get("max_results", 10)

        if not query:
            return json.dumps({"success": False, "error": "搜索关键词不能为空"}, ensure_ascii=False)

        backends = _get_ordered_backends()
        results = []
        backend_name = ""

        for backend in backends:
            method = getattr(self, f"_search_{backend}", None)
            if method is None:
                continue
            try:
                if backend == "searxng":
                    results = await method(_get_env("SEARXNG_URL"), query, max_results)
                elif backend == "tavily":
                    results = await method(_get_env("TAVILY_API_KEY"), query, max_results)
                elif backend == "serper":
                    results = await method(_get_env("SERPER_API_KEY"), query, max_results)
                elif backend == "bing":
                    results = await method(_get_env("BING_SEARCH_API_KEY"), query, max_results)
                else:
                    results = await method(query, max_results)
                if results:
                    backend_name = backend
                    break
            except Exception as e:
                logger.warning(f"搜索后端 {backend} 失败: {e}")
                continue

        if not results:
            return json.dumps({
                "success": False,
                "error": f"所有搜索后端均未返回结果: {query}",
                "query": query,
            }, ensure_ascii=False)

        logger.info(f"搜索成功，后端: {backend_name}，结果数: {len(results)}")
        return json.dumps({
            "success": True,
            "query": query,
            "count": len(results[:max_results]),
            "results": results[:max_results]
        }, ensure_ascii=False)

    # ── Tavily ──────────────────────────────────────────

    async def _search_tavily(self, api_key: str, query: str, max_results: int) -> list:
        """Tavily 搜索 API — 专为 AI Agent 设计"""
        if not api_key:
            return []
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10),
            ) as client:
                resp = await client.post(
                    self.TAVILY_URL,
                    json={
                        "api_key": api_key,
                        "query": query,
                        "max_results": max_results,
                        "search_depth": "basic",
                        "include_answer": False,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            results = []
            for item in data.get("results", [])[:max_results]:
                title = (item.get("title") or "").strip()
                snippet = (item.get("content") or "").strip()
                link = (item.get("url") or "").strip()
                if not title or not link:
                    continue
                results.append({
                    "title": title[:200],
                    "snippet": snippet[:500],
                    "url": link,
                })
            return results
        except Exception as e:
            logger.warning(f"Tavily 搜索失败: {e}")
            return []

    # ── Serper (Google) ─────────────────────────────────

    async def _search_serper(self, api_key: str, query: str, max_results: int) -> list:
        """Serper.dev 搜索 API — Google 搜索结果"""
        if not api_key:
            return []
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10, read=15, write=10, pool=10),
            ) as client:
                resp = await client.post(
                    self.SERPER_URL,
                    headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                    json={"q": query, "num": max_results, "gl": "cn", "hl": "zh-cn"},
                )
                resp.raise_for_status()
                data = resp.json()

            results = []
            for item in data.get("organic", [])[:max_results]:
                title = (item.get("title") or "").strip()
                snippet = (item.get("snippet") or "").strip()
                link = (item.get("link") or "").strip()
                if not title or not link:
                    continue
                results.append({
                    "title": title[:200],
                    "snippet": snippet[:500],
                    "url": link,
                })

            for item in data.get("knowledgeGraph", {}).get("description", []):
                pass

            answer_box = data.get("answerBox")
            if answer_box and answer_box.get("snippet"):
                link = answer_box.get("link", "")
                if link:
                    results.insert(0, {
                        "title": (answer_box.get("title") or "Answer Box")[:200],
                        "snippet": (answer_box.get("snippet") or "")[:500],
                        "url": link,
                    })

            return results[:max_results]
        except Exception as e:
            logger.warning(f"Serper 搜索失败: {e}")
            return []

    # ── Bing Search API ────────────────────────────────

    async def _search_bing(self, api_key: str, query: str, max_results: int) -> list:
        """Bing Web Search API — 微软官方"""
        if not api_key:
            return []
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10, read=15, write=10, pool=10),
            ) as client:
                resp = await client.get(
                    self.BING_URL,
                    headers={"Ocp-Apim-Subscription-Key": api_key},
                    params={
                        "q": query,
                        "count": max_results,
                        "responseFilter": "Webpages",
                        "setLang": "zh-Hans",
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            results = []
            for item in data.get("webPages", {}).get("value", [])[:max_results]:
                title = (item.get("name") or "").strip()
                snippet = (item.get("snippet") or "").strip()
                link = (item.get("url") or "").strip()
                if not title or not link:
                    continue
                results.append({
                    "title": title[:200],
                    "snippet": snippet[:500],
                    "url": link,
                })
            return results
        except Exception as e:
            logger.warning(f"Bing Search API 搜索失败: {e}")
            return []

    # ── SearXNG ─────────────────────────────────────────

    async def _search_searxng(self, base_url: str, query: str, max_results: int) -> list:
        """SearXNG 自建搜索引擎 JSON API"""
        if not base_url:
            return []
        try:
            url = base_url.rstrip("/") + "/search"
            params = {
                "q": query,
                "format": "json",
                "engines": _get_env("SEARXNG_ENGINES", "google,bing,duckduckgo"),
            }
            timeout = _get_env_int("SEARXNG_TIMEOUT", 10)
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10, read=timeout, write=10, pool=10),
                follow_redirects=True,
            ) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()

            results = []
            for item in data.get("results", [])[:max_results]:
                title = (item.get("title") or "").strip()
                snippet = (item.get("content") or "").strip()
                link = (item.get("url") or "").strip()
                if not title or not link:
                    continue
                results.append({
                    "title": title[:200],
                    "snippet": snippet[:500],
                    "url": link,
                })
            return results
        except Exception as e:
            logger.warning(f"SearXNG 搜索失败: {e}")
            return []

    # ── DuckDuckGo HTML ─────────────────────────────────

    async def _search_duckduckgo(self, query: str, max_results: int) -> list:
        """DuckDuckGo HTML 爬虫搜索"""
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10),
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                }
            ) as client:
                resp = await client.post(
                    self.DUCKDUCKGO_HTML_URL,
                    data={"q": query, "b": "", "kl": "wt-wt"},
                )
                resp.raise_for_status()
                return self._parse_ddg_html(resp.text, max_results)
        except Exception as e:
            logger.warning(f"DuckDuckGo HTML搜索失败: {e}")
            return []

    # ── DuckDuckGo Instant Answer API ───────────────────

    async def _search_duckduckgo_api(self, query: str, max_results: int) -> list:
        """DuckDuckGo Instant Answer API（结果有限）"""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    "https://api.duckduckgo.com/",
                    params={
                        "q": query,
                        "format": "json",
                        "no_html": 1,
                        "skip_disambig": 1,
                    }
                )
                data = resp.json()

                results = []
                abstract = data.get("Abstract")
                if abstract:
                    results.append({
                        "title": data.get("Heading", ""),
                        "snippet": abstract,
                        "url": data.get("AbstractURL", ""),
                    })

                for topic in (data.get("RelatedTopics") or [])[:max_results]:
                    if isinstance(topic, dict) and "Text" in topic:
                        results.append({
                            "title": topic.get("Text", "")[:80],
                            "snippet": topic.get("Text", ""),
                            "url": topic.get("FirstURL", ""),
                        })

                return results
        except Exception as e:
            logger.warning(f"DuckDuckGo Instant API 搜索失败: {e}")
            return []

    # ── DuckDuckGo HTML 解析 ────────────────────────────

    def _parse_ddg_html(self, html_text: str, max_results: int) -> list:
        """解析 DuckDuckGo HTML 搜索结果"""
        results = []
        seen_urls = set()

        result_blocks = re.findall(
            r'<a[^>]+class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
            html_text, re.DOTALL
        )
        snippet_blocks = re.findall(
            r'<a[^>]+class="result__snippet"[^>]*href="[^"]*"[^>]*>(.*?)</a>',
            html_text, re.DOTALL
        )

        for i, (url, title) in enumerate(result_blocks):
            url = self._decode_ddg_redirect(url)
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            title = html.unescape(re.sub(r'<[^>]+>', '', title).strip())
            snippet = ""
            if i < len(snippet_blocks):
                snippet = html.unescape(re.sub(r'<[^>]+>', '', snippet_blocks[i]).strip())

            if not title:
                continue

            results.append({
                "title": title[:200],
                "snippet": snippet[:500],
                "url": url,
            })

            if len(results) >= max_results:
                break

        if not results:
            results = self._parse_ddg_html_fallback(html_text, max_results)

        return results

    def _parse_ddg_html_fallback(self, html_text: str, max_results: int) -> list:
        """备用解析：从 HTML 中提取链接和文本"""
        results = []
        seen_urls = set()

        for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"[^>]*>([^<]+)</a>', html_text):
            url = m.group(1)
            title = m.group(2).strip()

            if url in seen_urls or not title or len(title) < 5:
                continue
            if any(skip in url for skip in ['duckduckgo.com', 'duck.co', 'javascript:', '.ddg']):
                continue

            seen_urls.add(url)
            results.append({
                "title": title[:200],
                "snippet": "",
                "url": url,
            })

            if len(results) >= max_results:
                break

        return results

    @staticmethod
    def _decode_ddg_redirect(url: str) -> str:
        """解码 DuckDuckGo 重定向 URL"""
        redirect_prefix = "https://duckduckgo.com/l/?uddg="
        if url.startswith(redirect_prefix):
            encoded = url[len(redirect_prefix):]
            amp_idx = encoded.find("&")
            if amp_idx > 0:
                encoded = encoded[:amp_idx]
            try:
                return unquote(encoded)
            except Exception:
                return url
        return url


class WebFetchTool(BuiltinTool):
    """网页内容获取工具 — 获取指定 URL 的网页内容"""

    @property
    def name(self) -> str:
        return "web_fetch"

    @property
    def description(self) -> str:
        return """获取指定URL的网页内容，返回纯文本格式。可用于获取文档页面、API说明、网页内容等。

使用场景：
- 获取在线文档内容
- 读取网页中的信息
- 获取API响应数据"""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要获取的网页URL"
                },
                "max_length": {
                    "type": "integer",
                    "description": "返回内容的最大字符数",
                    "default": 10000
                }
            },
            "required": ["url"]
        }

    async def execute(self, **kwargs) -> str:
        url = kwargs.get("url", "")
        max_length = kwargs.get("max_length", 10000)

        if not url:
            return json.dumps({"success": False, "error": "URL不能为空"}, ensure_ascii=False)

        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                })
                resp.raise_for_status()

                content = resp.text
                if "<html" in content.lower():
                    content = self._html_to_text(content)

                if len(content) > max_length:
                    content = content[:max_length] + "\n... [内容已截断]"

                return json.dumps({
                    "success": True,
                    "url": url,
                    "status_code": resp.status_code,
                    "content_length": len(content),
                    "content": content
                }, ensure_ascii=False)

        except httpx.HTTPError as e:
            return json.dumps({"success": False, "error": f"HTTP请求失败: {e}"}, ensure_ascii=False)

    @staticmethod
    def _html_to_text(html: str) -> str:
        html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
        html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL)
        html = re.sub(r'<[^>]+>', ' ', html)
        html = re.sub(r'\s+', ' ', html).strip()
        return html
