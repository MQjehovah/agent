"""ask 链路加固回归: bridge 三条路径(回答/超时/取消) + /api/chat/answer 分支。

线上问题背景: 在线会话 ask_user 提问后回答未生效, 恰好 180s 以空文案失败。
本文件锁定加固后的语义:
- bridge 正常回答/超时/取消均有日志, 取消必须向上传播(re-raise)且清理 pending;
- deliver_ask_answer 跨事件循环线程安全投递回答(唤醒目标循环);
- answer 端点: 缺参/过期 404、越权 403、成功投递、投递失败(重复/循环关闭) 410;
- 对外返回码: 成功 200、过期/缺失 404、越权 403、不可投递 410。
"""
import asyncio
import logging
import os
import sys
import threading
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import httpx  # noqa: E402
import pytest  # noqa: E402

from web.server import WebServer, create_jwt, deliver_ask_answer  # noqa: E402

LOGGER_NAME = "agent.web"


def _register_pending(w: WebServer, tag: str = "web:7", question: str = "要继续吗?"):
    """在当前事件循环登记一个挂起中的 ask（模拟 bridge 已把问题发出）。"""
    fut = asyncio.get_running_loop().create_future()
    ask_id = uuid.uuid4().hex
    w._pending_asks[ask_id] = {"future": fut, "tag": tag, "question": question}
    return ask_id, fut


async def _post_answer(w: WebServer, payload: dict, token: str = ""):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    transport = httpx.ASGITransport(app=w._app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/api/chat/answer", json=payload, headers=headers)


def _has_log(caplog, text: str) -> bool:
    return any(text in rec.getMessage() for rec in caplog.records)


# ---------------- deliver_ask_answer 跨事件循环投递 ----------------


def test_deliver_ask_answer_across_event_loops():
    """核心用例: 主线程投递 → 另一线程独立事件循环中等待的 future 被唤醒。"""
    box: dict = {}
    started = threading.Event()

    def worker():
        async def wait_future():
            fut = asyncio.get_running_loop().create_future()
            box["future"] = fut
            started.set()
            return await fut

        box["answer"] = asyncio.run(wait_future())

    t = threading.Thread(target=worker)
    t.start()
    try:
        assert started.wait(5), "worker 事件循环未就绪"
        assert deliver_ask_answer(box["future"], "答案") is True
        t.join(5)
        assert not t.is_alive(), "future 未被唤醒(跨循环投递失败)"
        assert box["answer"] == "答案"
    finally:
        t.join(1)


def test_deliver_ask_answer_false_cases():
    """None / 已完成 / 所属循环已关闭 → False(调用方按已过期处理)。"""
    assert deliver_ask_answer(None, "x") is False

    loop = asyncio.new_event_loop()
    try:
        done_fut = loop.create_future()
        done_fut.set_result("already")
        assert deliver_ask_answer(done_fut, "x") is False
    finally:
        loop.close()

    dead_loop = asyncio.new_event_loop()
    pending = dead_loop.create_future()
    dead_loop.close()
    assert deliver_ask_answer(pending, "x") is False


# ---------------- bridge（真实 _make_ask_bridge） ----------------


async def test_ask_bridge_timeout_returns_default(monkeypatch, caplog):
    """超时路径: 返回默认值、SSE 已推送 ask、pending 清理、warning 日志。"""
    monkeypatch.setenv("AGENT_WEB_ASK_TIMEOUT", "0.05")
    w = WebServer()
    q: asyncio.Queue = asyncio.Queue()
    bridge = w._make_ask_bridge(q, "web:7")

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        answer = await bridge("要继续吗?", ["是", "否"], "否")

    assert answer == "否"
    assert w._pending_asks == {}
    etype, payload = q.get_nowait()
    assert etype == "sse" and payload[0] == "ask"
    assert payload[1]["question"] == "要继续吗?" and payload[1]["options"] == ["是", "否"]
    assert _has_log(caplog, "等待回答超时")


async def test_ask_bridge_answer_logs_and_returns(monkeypatch, caplog):
    """正常回答路径: wait_for 返回后记录 info 日志并原样返回答案。"""
    monkeypatch.setenv("AGENT_WEB_ASK_TIMEOUT", "5")
    w = WebServer()
    q: asyncio.Queue = asyncio.Queue()
    bridge = w._make_ask_bridge(q, "web:7")

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        task = asyncio.create_task(bridge("要继续吗?", ["是", "否"], "否"))
        await asyncio.sleep(0.01)
        assert len(w._pending_asks) == 1
        ask_id = next(iter(w._pending_asks))
        w._pending_asks[ask_id]["future"].set_result("是")
        assert await task == "是"

    assert w._pending_asks == {}
    assert _has_log(caplog, "已收到回答")


async def test_ask_bridge_cancel_re_raises_and_logs(monkeypatch, caplog):
    """取消路径: CancelledError 必须向上抛(不吞), pending 清理, warning 日志。"""
    monkeypatch.setenv("AGENT_WEB_ASK_TIMEOUT", "30")
    w = WebServer()
    q: asyncio.Queue = asyncio.Queue()
    bridge = w._make_ask_bridge(q, "web:7")

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        task = asyncio.create_task(bridge("要继续吗?", [], ""))
        await asyncio.sleep(0.01)
        assert len(w._pending_asks) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert w._pending_asks == {}
    assert _has_log(caplog, "被取消")


async def test_ask_bridge_resumes_via_deliver_helper(monkeypatch):
    """进程内端到端: bridge 挂起 → deliver_ask_answer 投递 → 返回用户答案。"""
    monkeypatch.setenv("AGENT_WEB_ASK_TIMEOUT", "5")
    w = WebServer()
    q: asyncio.Queue = asyncio.Queue()
    bridge = w._make_ask_bridge(q, "web:7")

    task = asyncio.create_task(bridge("要继续吗?", ["是", "否"], "否"))
    await asyncio.sleep(0.01)
    ask_id = next(iter(w._pending_asks))
    assert deliver_ask_answer(w._pending_asks[ask_id]["future"], "是") is True

    assert await task == "是"
    assert w._pending_asks == {}


async def test_ask_bridge_across_thread_loop_wakes_main_queue(monkeypatch):
    """跨线程事件循环: bridge 在 worker 线程循环调用, 主循环 q.get() 立即可得。

    回归线上问题: bridge 里 await q.put 不唤醒 q 所属主循环的 selector,
    问题会被 SSE 读取侧的 15s 超时兜底拖住。本用例不依赖任何超时兜底:
    1s 内必须取到 ask 事件(asyncio.wait_for 直接失败), 否则判定回归。
    """
    monkeypatch.setenv("AGENT_WEB_ASK_TIMEOUT", "1")
    w = WebServer()
    q: asyncio.Queue = asyncio.Queue()
    bridge = w._make_ask_bridge(q, "web:7")
    started = threading.Event()
    box: dict = {}

    def worker():
        async def run_bridge():
            started.set()
            return await bridge("要继续吗?", ["是"], "否")

        box["answer"] = asyncio.run(run_bridge())

    t = threading.Thread(target=worker)
    t.start()
    try:
        assert started.wait(5), "worker 线程未启动"
        event_type, content = await asyncio.wait_for(q.get(), timeout=1.0)
        assert event_type == "sse" and content[0] == "ask"
        ask_id = content[1]["ask_id"]
        assert w._pending_asks[ask_id]["tag"] == "web:7"
        assert deliver_ask_answer(w._pending_asks[ask_id]["future"], "是") is True
        t.join(5)
        assert not t.is_alive(), "worker 线程未收到回答"
        assert box["answer"] == "是"
    finally:
        t.join(1)


async def test_ask_bridge_invalid_timeout_falls_back(monkeypatch, caplog):
    """AGENT_WEB_ASK_TIMEOUT 非法值 → 回退 300s 并告警, 不影响 bridge 主流程。"""
    monkeypatch.setenv("AGENT_WEB_ASK_TIMEOUT", "abc")
    w = WebServer()
    q: asyncio.Queue = asyncio.Queue()
    bridge = w._make_ask_bridge(q, "web:7")

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        task = asyncio.create_task(bridge("要继续吗?", [], "否"))
        await asyncio.sleep(0.01)
        ask_id = next(iter(w._pending_asks))
        w._pending_asks[ask_id]["future"].set_result("是")
        assert await task == "是"

    assert _has_log(caplog, "AGENT_WEB_ASK_TIMEOUT")


# ---------------- /api/chat/answer ----------------


async def test_answer_endpoint_sets_result_and_logs(monkeypatch, caplog):
    monkeypatch.setenv("WEBUI_DISABLE_AUTH", "0")
    w = WebServer()
    ask_id, fut = _register_pending(w, tag="web:7")
    tok = create_jwt({"id": 7, "name": "张三", "role": "default"})

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        r = await _post_answer(w, {"ask_id": ask_id, "answer": "允许"}, tok)

    assert r.status_code == 200 and r.json()["success"] is True
    await asyncio.sleep(0)  # call_soon_threadsafe 投递在下一轮循环生效
    assert fut.done() and fut.result() == "允许"
    assert _has_log(caplog, f"answer {ask_id[:8]} by uid=7")


async def test_answer_endpoint_duplicate_returns_410(monkeypatch, caplog):
    """重复回答: future 已完成 → 投递失败 410, 原答案不被覆盖。"""
    monkeypatch.setenv("WEBUI_DISABLE_AUTH", "0")
    w = WebServer()
    ask_id, fut = _register_pending(w, tag="web:7")
    tok = create_jwt({"id": 7, "name": "张三", "role": "default"})

    first = await _post_answer(w, {"ask_id": ask_id, "answer": "允许"}, tok)
    assert first.status_code == 200
    await asyncio.sleep(0)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        second = await _post_answer(w, {"ask_id": ask_id, "answer": "拒绝"}, tok)

    assert second.status_code == 410
    assert second.json()["error"] == "该问题已过期或不存在"
    assert fut.result() == "允许"
    assert _has_log(caplog, "投递失败")
    assert _has_log(caplog, "重复回答")


async def test_answer_endpoint_closed_loop_future_410(monkeypatch, caplog):
    """future 所属事件循环已关闭(worker 退出) → 投递失败 410 + warning。"""
    monkeypatch.setenv("WEBUI_DISABLE_AUTH", "1")
    w = WebServer()
    dead_loop = asyncio.new_event_loop()
    dead_fut = dead_loop.create_future()
    dead_loop.close()
    ask_id = uuid.uuid4().hex
    w._pending_asks[ask_id] = {"future": dead_fut, "tag": "web:1", "question": "q"}

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        r = await _post_answer(w, {"ask_id": ask_id, "answer": "x"})

    assert r.status_code == 410
    assert _has_log(caplog, "投递失败")


async def test_answer_endpoint_expired_or_missing_404(monkeypatch, caplog):
    monkeypatch.setenv("WEBUI_DISABLE_AUTH", "0")
    w = WebServer()
    tok = create_jwt({"id": 7, "name": "张三", "role": "default"})

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        expired = await _post_answer(w, {"ask_id": uuid.uuid4().hex, "answer": "x"}, tok)
        missing = await _post_answer(w, {"answer": "x"}, tok)

    assert expired.status_code == 404 and missing.status_code == 404
    assert _has_log(caplog, "已过期或不存在")
    assert _has_log(caplog, "缺少 ask_id")


async def test_answer_endpoint_foreign_uid_403(monkeypatch, caplog):
    """非 admin 越权回答: 403, future 不被 set。"""
    monkeypatch.setenv("WEBUI_DISABLE_AUTH", "0")
    w = WebServer()
    ask_id, fut = _register_pending(w, tag="web:7")
    tok = create_jwt({"id": 9, "name": "李四", "role": "default"})

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        r = await _post_answer(w, {"ask_id": ask_id, "answer": "允许"}, tok)

    assert r.status_code == 403
    assert not fut.done()
    assert _has_log(caplog, "越权")


async def test_answer_endpoint_admin_can_answer_others(monkeypatch):
    monkeypatch.setenv("WEBUI_DISABLE_AUTH", "0")
    w = WebServer()
    ask_id, fut = _register_pending(w, tag="web:7")
    tok = create_jwt({"id": 1, "name": "管理员", "role": "admin"})

    r = await _post_answer(w, {"ask_id": ask_id, "answer": "允许"}, tok)

    assert r.status_code == 200
    await asyncio.sleep(0)
    assert fut.done() and fut.result() == "允许"
