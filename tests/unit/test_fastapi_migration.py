"""FastAPI 迁移冒烟测试：验证 web/server 与 webhook 插件的 FastAPI app 可正常工作。"""
import asyncio
import os
import socket
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

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
