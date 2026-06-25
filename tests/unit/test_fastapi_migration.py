"""FastAPI 迁移冒烟测试：验证 web/server 与 webhook 插件的 FastAPI app 可正常工作。"""
import asyncio
import contextlib
import os
import socket
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from unittest.mock import MagicMock  # noqa: E402

import pytest
from fastapi.testclient import TestClient  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_web_server_sessions_route_works_without_agent():
    """web/server.py 的 FastAPI app：/api/sessions 不依赖 agent 即可响应"""
    from web.server import WebServer

    w = WebServer()
    client = TestClient(w._app)
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    assert resp.json() == {"sessions": []}


def test_web_server_status_requires_agent():
    """未注入 agent 时 /api/agent/status 返回 503"""
    from web.server import WebServer

    w = WebServer()
    client = TestClient(w._app)
    resp = client.get("/api/agent/status")
    assert resp.status_code == 503


def test_webhook_health_and_task_listing(tmp_path):
    """webhook 插件 FastAPI app：/health 与任务列表可用"""
    from plugins.webhook import WebhookPlugin

    plugin = WebhookPlugin(config_dir=str(tmp_path))
    app = plugin._build_app()
    client = TestClient(app)

    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "webhook"}

    # 任务列表为空
    resp = client.get("/webhook/tasks")
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


def test_webhook_execute_requires_token_when_configured(tmp_path):
    """配置了 tokens 但请求未带 token → 401"""
    from plugins.webhook import WebhookPlugin

    plugin = WebhookPlugin(config_path=str(tmp_path / "webhook.json"))
    # 写入带 token 的配置
    import json
    with open(tmp_path / "webhook.json", "w", encoding="utf-8") as f:
        json.dump({"tokens": ["secret"]}, f)
    plugin._load_config()

    app = plugin._build_app()
    client = TestClient(app)

    resp = client.post(plugin.config.path, json={"content": "hi"})
    assert resp.status_code == 401


def test_webhook_execute_async_returns_pending(tmp_path):
    """合法请求（已注册 executor）异步模式立即返回 pending"""
    from plugins.webhook import WebhookPlugin

    plugin = WebhookPlugin(config_dir=str(tmp_path))

    async def fake_executor(sid, content, uid="", uname=""):
        return "done"

    plugin.agent_executor = fake_executor
    app = plugin._build_app()
    client = TestClient(app)

    resp = client.post(plugin.config.path, json={"content": "hello"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert "task_id" in body


@pytest.mark.asyncio
async def test_uvicorn_embedded_in_agent_loop_serves_real_http():
    """端到端：uvicorn 以 task 形式跑在当前事件循环内，真实 HTTP 请求可达成。

    这是本次“进事件循环”架构的核心验证——证明 web server 不再需要独立线程，
    uvicorn.serve() 作为后台 task 与其它协程共享同一 loop。
    """
    import httpx

    from web.server import WebServer

    port = _free_port()
    w = WebServer(host="127.0.0.1", port=port)
    w.start()
    server = w._server
    assert server is not None

    # 轮询等待 uvicorn 就绪
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.1)
    assert server.started, "uvicorn 未在事件循环内启动"

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://127.0.0.1:{port}/api/sessions")
            assert resp.status_code == 200
            assert resp.json() == {"sessions": []}
    finally:
        w.stop()
        # 给 uvicorn 优雅退出的时间
        for _ in range(30):
            if not server.started:
                break
            await asyncio.sleep(0.1)


# ===== Sessions 从数据库查询 / 日志流 =====

def test_sessions_history_reads_from_db(tmp_path):
    """Sessions 标签页应从数据库聚合历史会话，而非仅内存活跃 session"""
    import storage as storage_mod
    from storage import Storage
    from web.server import WebServer

    prev = storage_mod._storage_instance
    s = Storage(str(tmp_path))
    storage_mod._storage_instance = s
    s.save_message_sync("agentA", "sess1", "user", "hello")
    s.save_message_sync("agentA", "sess1", "assistant", "hi")
    s.save_message_sync("agentB", "sess2", "user", "test")

    try:
        w = WebServer()
        client = TestClient(w._app)
        resp = client.get("/api/agent/sessions/history?limit=20")
        assert resp.status_code == 200
        sessions = resp.json()["sessions"]
        ids = [x["id"] for x in sessions]
        assert "sess1" in ids and "sess2" in ids
        s1 = next(x for x in sessions if x["id"] == "sess1")
        assert s1["messages"] == 2
        assert s1["agent_id"] == "agentA"
    finally:
        s.close()
        storage_mod._storage_instance = prev


def test_session_messages_falls_back_to_db(tmp_path):
    """内存未命中的历史会话，应从数据库恢复消息"""
    import storage as storage_mod
    from storage import Storage
    from web.server import WebServer

    prev = storage_mod._storage_instance
    s = Storage(str(tmp_path))
    storage_mod._storage_instance = s
    s.save_message_sync("agentX", "hist_sess", "user", "old message")

    try:
        w = WebServer()
        # 模拟 agent 已初始化但内存 session 为空（重启后的典型场景）
        mock_sm = MagicMock()
        mock_sm.sessions = {}
        mock_agent = MagicMock()
        mock_agent.session_manager = mock_sm
        mock_agent.subagent_manager = None
        mock_agent.name = "main"
        w.set_agent(mock_agent)

        client = TestClient(w._app)
        resp = client.get("/api/agent/sessions/hist_sess/messages")
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "(history)"
        assert any("old message" in (m.get("content") or "") for m in data["messages"])
    finally:
        s.close()
        storage_mod._storage_instance = prev


@pytest.mark.asyncio
async def test_log_stream_handler_broadcasts_across_threads():
    """LogStreamHandler 应把日志广播给订阅者（跨线程安全）"""
    import logging

    from web.server import LogStreamHandler

    loop = asyncio.get_running_loop()
    h = LogStreamHandler(loop)
    q = h.subscribe()

    # 模拟来自其它线程的日志（存储写线程 / 钉钉 stream 等）
    import threading
    rec = logging.LogRecord("agent.test", logging.INFO, "", 0, "hello-stream", None, None)

    def emit_from_thread():
        h.emit(rec)

    t = threading.Thread(target=emit_from_thread)
    t.start()
    t.join()

    line = await asyncio.wait_for(q.get(), timeout=2)
    assert "hello-stream" in line
    h.unsubscribe(q)


@pytest.mark.asyncio
async def test_logs_stream_end_to_end():
    """端到端：真实 uvicorn + agent 日志 -> SSE 客户端收到（补全此前缺失的集成验证）"""
    import logging

    import httpx

    from web.server import WebServer

    logging.getLogger("agent").setLevel(logging.INFO)
    port = _free_port()
    w = WebServer(host="127.0.0.1", port=port)
    w.start()
    srv = w._server
    for _ in range(50):
        if srv.started:
            break
        await asyncio.sleep(0.1)
    assert srv.started, "uvicorn 未启动"

    received = []

    async def reader():
        async with httpx.AsyncClient(timeout=10) as client, client.stream("GET", f"http://127.0.0.1:{port}/api/logs/stream") as r:
            async for line in r.aiter_lines():
                received.append(line)
                if any("E2E_LOG_TOKEN_X" in x for x in received):
                    break

    try:
        rt = asyncio.create_task(reader())
        await asyncio.sleep(1.0)  # 等 SSE 连接建立
        logging.getLogger("agent.e2e_test").info("E2E_LOG_TOKEN_X marked")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(rt), timeout=5)
        assert any("E2E_LOG_TOKEN_X" in x for x in received), f"未收到日志，got: {received}"
    finally:
        logging.getLogger("agent").removeHandler(w._log_handler)
        w.stop()
        await asyncio.sleep(0.3)


# ===== Memory 管理 CRUD =====

def test_memories_crud(tmp_path):
    """记忆管理：新增 / 查询(筛选) / 编辑 / 删除 全链路"""
    import storage as storage_mod
    from storage import Storage
    from web.server import WebServer

    prev = storage_mod._storage_instance
    s = Storage(str(tmp_path))
    storage_mod._storage_instance = s
    try:
        w = WebServer()
        client = TestClient(w._app)

        # 新增 global 记忆
        r = client.post("/api/memories", json={
            "scope": "global", "category": "knowledge",
            "content": "测试知识XYZ", "importance": 4,
        })
        assert r.status_code == 200, r.text
        mid = r.json()["id"]
        assert mid

        # 关键词查询应命中
        r = client.get("/api/memories?q=知识XYZ")
        assert r.status_code == 200
        assert any(m["id"] == mid for m in r.json()["memories"])

        # scope 筛选
        r = client.get("/api/memories?scope=user")
        assert all(m["scope"] == "user" for m in r.json()["memories"])

        # 编辑
        r = client.put(f"/api/memories/{mid}", json={"content": "已修改内容", "importance": 5})
        assert r.status_code == 200 and r.json()["success"] is True

        # 删除
        r = client.delete(f"/api/memories/{mid}")
        assert r.status_code == 200 and r.json()["success"] is True

        # 删除后查不到
        r = client.get("/api/memories?q=已修改内容")
        assert not any(m["id"] == mid for m in r.json()["memories"])
    finally:
        s.close()
        storage_mod._storage_instance = prev
