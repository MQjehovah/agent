"""W3 新增 MCP servers(mcp_time/mcp_fetch/mcp_filesystem) 进程内测试。

- time: 纯离线, 断言字段齐全/跨时区(含夏令时固定日期)/非法时区/时区列表过滤;
- fetch: 纯函数覆盖 SSRF(私网/环回/保留/localhost/非 http scheme/白名单放行)与
  HTML→Markdown 转换, 真实网络调用一律不做, 只验证非法 host 的快速失败;
- filesystem: tmp_path 作受限目录, 覆盖读写 roundtrip/../越界/符号链接逃逸/
  未配置 roots/写开关/max_bytes 截断/search 命中; 符号链接在无权限环境自动跳过。
"""
import gzip
import importlib
import json
import os
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
MCP_SRC = ROOT / "mcp_server" / "src"
for path in (SRC, MCP_SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from mcp import Client  # noqa: E402


def _load(name):
    return importlib.import_module(name)


def _payload(result):
    """把 CallToolResult 还原为 dict/str: 优先 structured_content, 否则解析文本 JSON。"""
    assert result.is_error is not True
    if result.structured_content is not None:
        return result.structured_content
    text = "\n".join(getattr(item, "text", "") or "" for item in (result.content or []))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


# ---------------------------------------------------------------- time

def test_time_current_fields():
    module = _load("mcp_time")
    payload = module.get_current_time()
    assert payload["iso"] and payload["date"] and payload["time"]
    assert payload["weekday"].startswith("星期")
    assert payload["timezone"] == "Asia/Shanghai"
    assert payload["utc_offset"] == "UTC+08:00"


def test_time_convert_cross_timezone_with_dst():
    module = _load("mcp_time")
    winter = module.convert_time("2026-01-15T12:00:00", "Asia/Shanghai", "America/New_York")
    assert winter["iso"].startswith("2026-01-14T23:00:00-05:00")
    assert winter["utc_offset"] == "UTC-05:00"
    assert "慢 13 小时" in winter["note"]

    summer = module.convert_time("2026-07-15T12:00:00", "Asia/Shanghai", "America/New_York")
    assert summer["iso"].startswith("2026-07-15T00:00:00-04:00")
    assert summer["utc_offset"] == "UTC-04:00"
    assert "慢 12 小时" in summer["note"]


def test_time_convert_accepts_explicit_offset():
    module = _load("mcp_time")
    payload = module.convert_time("2026-01-15T12:00:00+08:00", "Asia/Shanghai", "America/New_York")
    assert payload["input_has_timezone"] is True
    assert payload["iso"].startswith("2026-01-14T23:00:00-05:00")


def test_time_invalid_timezone_returns_readable_error():
    module = _load("mcp_time")
    payload = module.get_current_time("Mars/Olympus")
    assert "非法时区" in payload["error"]
    payload = module.convert_time("2026-01-15T12:00:00", "Not/ATimezone", "UTC")
    assert "非法时区" in payload["error"]
    payload = module.convert_time("not-a-time", "UTC", "UTC")
    assert "无法解析时间" in payload["error"]


def test_time_list_timezones_filter_and_limit():
    module = _load("mcp_time")
    payload = module.list_timezones("Shanghai")
    assert "Asia/Shanghai" in payload["timezones"]
    assert payload["returned"] <= 50
    all_tz = module.list_timezones("")
    assert all_tz["matched"] > 50
    assert all_tz["returned"] == 50


async def test_time_server_in_process_client():
    module = _load("mcp_time")
    async with Client(module.mcp, raise_exceptions=True) as client:
        tools = await client.list_tools()
        assert sorted(tool.name for tool in tools.tools) == ["convert_time", "get_current_time", "list_timezones"]
        result = await client.call_tool("get_current_time", {"timezone": "UTC"})
    payload = _payload(result)
    assert payload["timezone"] == "UTC"
    assert payload["utc_offset"] == "UTC+00:00"


# ---------------------------------------------------------------- fetch

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/",
    "http://127.0.0.1:8080/x",
    "http://10.1.2.3/",
    "http://172.16.0.1/",
    "http://172.31.255.255/",
    "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/",
    "http://[fe80::1]/",
    "http://[::ffff:127.0.0.1]/",
    "http://localhost/",
    "http://localhost:8000/x",
    "http://foo.localhost/",
    "http://100.64.0.0/",
    "http://100.64.0.1/",
    "http://100.127.255.255/",
    "ftp://example.com/file",
    "file:///etc/passwd",
    "",
])
def test_fetch_is_safe_url_blocks_private_and_non_http(url):
    module = _load("mcp_fetch")
    ok, reason = module.is_safe_url(url)
    assert ok is False, url
    assert reason


def test_fetch_is_safe_url_allows_public_and_whitelist():
    module = _load("mcp_fetch")
    assert module.is_safe_url("https://example.com/a?b=1")[0] is True
    assert module.is_safe_url("http://93.184.216.34/")[0] is True
    assert module.is_safe_url("http://172.32.0.1/")[0] is True  # 172.16/12 之外
    assert module.is_safe_url("http://100.63.255.255/")[0] is True  # 100.64/10 之外
    assert module.is_safe_url("http://100.128.0.1/")[0] is True  # 100.64/10 之外
    assert module.is_safe_url("http://192.168.1.10:8080/", {"192.168.1.10"})[0] is True
    assert module.is_safe_url("http://localhost/", ["localhost"])[0] is True


def test_fetch_html_to_text_keeps_structure_strips_scripts():
    module = _load("mcp_fetch")
    html = (
        "<html><head><title>测试 页面</title><style>body{color:red}</style>"
        "<script>alert('x')</script></head><body>"
        "<h1>标题</h1><p>段落 <a href=\"/rel\">相对链接</a> 结束</p>"
        "<ul><li>第一项</li><li>第二项</li></ul>"
        "<a href=\"javascript:void(0)\">脚本链接</a><img src=\"/img.png\" alt=\"图\">"
        "</body></html>"
    )
    title, text = module.html_to_text(html, "https://example.com/dir/page")
    assert title == "测试 页面"
    assert "alert" not in text and "color:red" not in text
    assert "# 标题" in text
    assert "[相对链接](https://example.com/rel)" in text
    assert "- 第一项" in text
    assert "脚本链接" in text and "javascript:" not in text
    assert "![图](https://example.com/img.png)" in text


def test_fetch_truncate_text():
    module = _load("mcp_fetch")
    text, truncated = module.truncate_text("abcdef", 3)
    assert text == "abc" and truncated is True
    text, truncated = module.truncate_text("abc", 3)
    assert text == "abc" and truncated is False


def _mock_fetch_transport(monkeypatch, handler):
    """把 mcp_fetch 内新建的 httpx.Client 换成 MockTransport 客户端(测试不出网)。"""
    module = _load("mcp_fetch")
    real_client = module.httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = module.httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(module.httpx, "Client", client_factory)
    return module


def test_fetch_follows_public_redirect_and_reports_final_url(monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if str(request.url) == "http://93.184.216.34/start":
            return httpx.Response(302, headers={"location": "http://93.184.216.34/final"})
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<h1>OK</h1>")

    module = _mock_fetch_transport(monkeypatch, handler)
    payload = module.fetch_url("http://93.184.216.34/start")
    assert payload["url"] == "http://93.184.216.34/final"
    assert "OK" in payload["text"]
    assert seen == ["http://93.184.216.34/start", "http://93.184.216.34/final"]


def test_fetch_redirect_to_private_host_blocked_before_request(monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1:9/secret"})

    module = _mock_fetch_transport(monkeypatch, handler)
    payload = module.fetch_url("http://93.184.216.34/start")
    assert "127.0.0.1" in payload["error"]
    assert seen == ["http://93.184.216.34/start"]  # 重定向目标未被请求


def test_fetch_gzip_large_response_truncated(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/plain", "content-encoding": "gzip"},
            content=gzip.compress(b"a" * 5000),
        )

    module = _mock_fetch_transport(monkeypatch, handler)
    monkeypatch.setenv("MCP_FETCH_MAX_BYTES", "1000")
    payload = module.fetch_url("http://93.184.216.34/big.txt", max_chars=100000)
    assert payload["truncated"] is True
    assert payload["text"] == "a" * 1000


def test_fetch_large_json_rejected(monkeypatch):
    body = b'{"data":"' + b"a" * 300_000 + b'"}'

    def handler(request):
        return httpx.Response(200, headers={"content-type": "application/json"}, content=body)

    module = _mock_fetch_transport(monkeypatch, handler)
    payload = module.fetch_json("http://93.184.216.34/big.json")
    assert payload["error"]
    assert "200KB" in payload["error"]


def test_fetch_non_text_content_type_rejected(monkeypatch):
    def handler(request):
        return httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=b"\x00\x01")

    module = _mock_fetch_transport(monkeypatch, handler)
    payload = module.fetch_url("http://93.184.216.34/blob.bin")
    assert "不支持的内容类型" in payload["error"]


async def test_fetch_rejects_private_host_fast_without_network():
    module = _load("mcp_fetch")
    async with Client(module.mcp, raise_exceptions=True) as client:
        result = await client.call_tool("fetch_url", {"url": "http://127.0.0.1:9/"})
        payload = _payload(result)
        assert "127.0.0.1" in payload["error"]

        result = await client.call_tool("fetch_json", {"url": "file:///etc/passwd"})
        payload = _payload(result)
        assert "仅允许 http/https" in payload["error"]


# ---------------------------------------------------------------- filesystem

@pytest.fixture
def fs_env(tmp_path, monkeypatch):
    """受限目录 = tmp_path/root, 写开关打开; 环境变量在导入后按调用时读取。"""
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("FS_MCP_ROOTS", str(root))
    monkeypatch.setenv("FS_MCP_ALLOW_WRITE", "true")
    monkeypatch.delenv("FS_MCP_MAX_READ_BYTES", raising=False)
    monkeypatch.delenv("FS_MCP_MAX_WRITE_BYTES", raising=False)
    monkeypatch.delenv("FS_MCP_ALLOW_SUFFIXES", raising=False)
    return _load("mcp_filesystem"), root


def test_filesystem_roundtrip(fs_env):
    module, root = fs_env
    assert module.fs_write_text("sub/hello.txt", "你好 world", create_dirs=True)["success"] is True
    assert (root / "sub" / "hello.txt").read_text(encoding="utf-8") == "你好 world"

    payload = module.fs_read_text("sub/hello.txt")
    assert payload["content"] == "你好 world"
    assert payload["truncated"] is False

    payload = module.fs_stat("sub/hello.txt")
    assert payload["type"] == "file" and payload["size"] > 0

    payload = module.fs_list(".")
    assert any(entry["name"] == "sub" and entry["type"] == "dir" for entry in payload["entries"])
    payload = module.fs_list("sub")
    assert [entry["name"] for entry in payload["entries"]] == ["hello.txt"]

    payload = module.fs_read_text(str(root / "sub" / "hello.txt"))
    assert payload["content"] == "你好 world"


def test_filesystem_rejects_traversal_and_outside_absolute(fs_env, tmp_path):
    module, _ = fs_env
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    payload = module.fs_read_text("../outside.txt")
    assert "越界" in payload["error"]
    payload = module.fs_stat(str(outside))
    assert "越界" in payload["error"]
    payload = module.fs_write_text("../escape.txt", "x", create_dirs=True)
    assert "越界" in payload["error"]
    assert not (tmp_path / "escape.txt").exists()


def test_filesystem_rejects_symlink_escape(fs_env, tmp_path):
    module, root = fs_env
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = root / "link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建符号链接")
    payload = module.fs_read_text("link/secret.txt")
    assert "越界" in payload["error"]
    payload = module.fs_stat("link/secret.txt")
    assert "越界" in payload["error"]


def test_filesystem_unconfigured_roots_rejects_all_tools(monkeypatch):
    module = _load("mcp_filesystem")
    monkeypatch.setenv("FS_MCP_ROOTS", "")
    monkeypatch.setenv("FS_MCP_ALLOW_WRITE", "true")
    calls = {
        "fs_list": (),
        "fs_read_text": ("a.txt",),
        "fs_stat": ("a.txt",),
        "fs_search": ("a.txt", "x"),
        "fs_write_text": ("a.txt", "x"),
        "fs_mkdir": ("d",),
        "fs_move": ("a", "b"),
        "fs_delete": ("a",),
    }
    for name, args in calls.items():
        payload = getattr(module, name)(*args)
        assert "未配置受限目录" in payload["error"], name


def test_filesystem_write_disabled_rejects_writes_but_reads_work(fs_env, monkeypatch):
    module, root = fs_env
    (root / "b.txt").write_text("hi", encoding="utf-8")
    monkeypatch.setenv("FS_MCP_ALLOW_WRITE", "false")

    assert "写操作未开启" in module.fs_write_text("a.txt", "hello")["error"]
    assert "写操作未开启" in module.fs_mkdir("d")["error"]
    assert "写操作未开启" in module.fs_move("b.txt", "c.txt")["error"]
    assert "写操作未开启" in module.fs_delete("b.txt")["error"]
    assert not (root / "a.txt").exists() and (root / "b.txt").exists()

    assert module.fs_read_text("b.txt")["content"] == "hi"


def test_filesystem_read_max_bytes_truncation(fs_env, monkeypatch):
    module, root = fs_env
    (root / "long.txt").write_text("a" * 100, encoding="utf-8")

    payload = module.fs_read_text("long.txt", max_bytes=10)
    assert payload["truncated"] is True
    assert payload["content"] == "a" * 10
    assert payload["size"] == 100

    monkeypatch.setenv("FS_MCP_MAX_READ_BYTES", "5")
    payload = module.fs_read_text("long.txt", max_bytes=100)
    assert payload["truncated"] is True
    assert payload["content"] == "a" * 5
    assert payload["limit_bytes"] == 5


def test_filesystem_search_matches_name_and_content(fs_env):
    module, root = fs_env
    (root / "alpha_report.md").write_text("hello world", encoding="utf-8")
    (root / "notes.txt").write_text("first line\ncontains needle here", encoding="utf-8")

    payload = module.fs_search(".", "report")
    assert any(hit["kind"] == "name" and hit["path"].endswith("alpha_report.md") for hit in payload["matches"])

    payload = module.fs_search(".", "needle")
    hits = [hit for hit in payload["matches"] if hit["kind"] == "content"]
    assert hits and hits[0]["path"].endswith("notes.txt")
    assert hits[0]["line"] == 2


def test_filesystem_write_tools_mkdir_move_delete(fs_env):
    module, root = fs_env
    assert module.fs_mkdir("newdir")["success"] is True
    assert (root / "newdir").is_dir()
    assert module.fs_mkdir("newdir")["created"] is False

    module.fs_write_text("newdir/a.txt", "x")
    assert module.fs_move("newdir/a.txt", "newdir/b.txt")["success"] is True
    assert (root / "newdir" / "b.txt").exists()

    assert "error" in module.fs_delete("newdir")
    assert module.fs_delete("newdir", recursive=True)["success"] is True
    assert not (root / "newdir").exists()


def test_filesystem_refuses_root_delete_and_move(fs_env):
    module, _ = fs_env
    assert "拒绝删除受限目录根" in module.fs_delete(".", recursive=True)["error"]
    assert "拒绝移动受限目录根" in module.fs_move(".", "moved")["error"]


def test_filesystem_delete_and_move_do_not_follow_symlink(fs_env, tmp_path):
    """删除/移动 symlink 必须只作用于链接本身, 目标目录与其内容保持不动。"""
    module, root = fs_env
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    link = root / "link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建符号链接")

    payload = module.fs_delete("link")
    assert payload["success"] is True and payload["kind"] == "symlink"
    assert not link.is_symlink()
    assert secret.read_text(encoding="utf-8") == "secret"

    os.symlink(outside, link, target_is_directory=True)
    payload = module.fs_move("link", "moved_link")
    assert payload["success"] is True
    assert not link.is_symlink()
    assert (root / "moved_link").is_symlink()
    assert secret.read_text(encoding="utf-8") == "secret"
    assert outside.is_dir()


def test_filesystem_write_bytes_limit(fs_env, monkeypatch):
    module, root = fs_env
    monkeypatch.setenv("FS_MCP_MAX_WRITE_BYTES", "5")

    assert module.fs_write_text("a.txt", "12345")["success"] is True
    payload = module.fs_write_text("b.txt", "123456")
    assert "超过上限" in payload["error"]
    assert not (root / "b.txt").exists()


def test_filesystem_write_suffix_whitelist(fs_env, monkeypatch):
    module, root = fs_env
    monkeypatch.setenv("FS_MCP_ALLOW_SUFFIXES", "md,.txt")

    assert module.fs_write_text("a.md", "x")["success"] is True
    assert module.fs_write_text("a.txt", "x")["success"] is True
    payload = module.fs_write_text("b.log", "x")
    assert "后缀未允许" in payload["error"]
    assert not (root / "b.log").exists()

    (root / "c.log").write_text("x", encoding="utf-8")
    payload = module.fs_move("c.log", "d.log")
    assert "后缀未允许" in payload["error"]
    assert module.fs_move("c.log", "d.md")["success"] is True


def test_filesystem_write_defaults_no_limit_and_all_suffixes(fs_env):
    module, _ = fs_env
    assert module._max_write_bytes() == 1024 * 1024
    assert module._allowed_suffixes() == set()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 保留名/ADS 校验仅 win32 生效")
def test_filesystem_rejects_windows_device_names_and_ads(fs_env):
    module, _ = fs_env
    for name in ("NUL", "con.txt", "COM1", "LPT9", "data.txt:secret"):
        payload = module.fs_write_text(name, "x")
        assert "error" in payload, name
        assert "Windows" in payload["error"], name
    assert "Windows" in module.fs_stat("NUL")["error"]
    assert "Windows" in module.fs_mkdir("aux")["error"]


def test_filesystem_depth_listing(fs_env):
    module, root = fs_env
    (root / "a").mkdir()
    (root / "a" / "c.txt").write_text("x", encoding="utf-8")

    shallow = module.fs_list(".", depth=1)
    assert not any(entry["name"] == "c.txt" for entry in shallow["entries"])
    deep = module.fs_list(".", depth=2)
    assert any(entry["name"] == "c.txt" for entry in deep["entries"])


async def test_filesystem_in_process_client(fs_env):
    module, _ = fs_env
    async with Client(module.mcp, raise_exceptions=True) as client:
        tools = await client.list_tools()
        assert len(tools.tools) == 8
        result = await client.call_tool("fs_write_text", {"path": "client.txt", "content": "ok"})
        assert _payload(result)["success"] is True
        result = await client.call_tool("fs_read_text", {"path": "client.txt"})
        assert _payload(result)["content"] == "ok"
