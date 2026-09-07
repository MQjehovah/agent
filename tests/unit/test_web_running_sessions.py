"""Task3 「运行中」会话接口：判定口径 / worker 池登记 / 路由可见性。

「运行中」= 正在执行（占用 Agent worker）的活跃会话：
- worker 池启用：数据源为池登记 pool.running_sessions()（真实占着 worker 的会话，
  不受内存会话上限淘汰影响），与 metrics agent_running_streams 同源；
- 池关闭（单实例）：数据源 = 内存 ChatSession.is_streaming（同 metrics）。
两种模式语义一致。覆盖：
- WebUserWorkerPool.register_run/unregister_run/running_sessions 纯逻辑单测；
- ChatSession.stage 生命周期；
- 接口层经 TestClient 冒烟：admin 全量、本人过滤（跨渠道同 agent 用户）、池模式兜底。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from fastapi.testclient import TestClient  # noqa: E402

from web.server import ChatSession, WebServer, create_jwt  # noqa: E402
from web.worker_pool import WebUserWorkerPool  # noqa: E402


class _DummyRoot:
    pass


# ---------------- worker 池运行登记（纯逻辑） ----------------

def test_pool_running_sessions_registration_lifecycle():
    pool = WebUserWorkerPool(root_agent=_DummyRoot(), max_workers=2)
    assert pool.enabled is True
    assert pool.running_sessions() == []

    pool.register_run("web:7", "web:7:a1", started_at="2026-09-07T10:00:00", model="qwen-max")
    pool.register_run("web:7", "web:7:b2", started_at="2026-09-07T10:00:01", model="qwen-max")
    pool.register_run("web:9", "web:9:c3", started_at="2026-09-07T10:00:02", model="qwen-max")

    runs = pool.running_sessions()
    assert len(runs) == 3
    by_sid = {r["session_id"]: r for r in runs}
    assert by_sid["web:7:a1"]["uid"] == "7"
    assert by_sid["web:7:a1"]["tag"] == "web:7"
    assert by_sid["web:7:a1"]["model"] == "qwen-max"
    assert by_sid["web:7:a1"]["started_at"] == "2026-09-07T10:00:00"
    assert by_sid["web:9:c3"]["uid"] == "9"

    # 同用户双会话并跑；注销其中一个仍保留另一个，注销干净后 tag 消失
    pool.unregister_run("web:7", "web:7:a1")
    assert {r["session_id"] for r in pool.running_sessions()} == {"web:7:b2", "web:9:c3"}
    pool.unregister_run("web:7", "web:7:b2")
    pool.unregister_run("web:9", "web:9:c3")
    assert pool.running_sessions() == []


def test_pool_running_sessions_noop_when_disabled():
    pool = WebUserWorkerPool(root_agent=None, max_workers=0)
    assert pool.enabled is False
    pool.register_run("web:7", "web:7:a1")
    pool.unregister_run("web:7", "web:7:a1")  # 不抛异常
    assert pool.running_sessions() == []


def test_pool_unregister_run_idempotent_for_missing():
    pool = WebUserWorkerPool(root_agent=_DummyRoot(), max_workers=2)
    pool.register_run("web:7", "web:7:a1")
    pool.unregister_run("web:8", "web:8:none")  # 其它 tag
    pool.unregister_run("web:7", "web:7:nope")  # 已有 tag 下的未知会话
    pool.unregister_run("web:7", "web:7:a1")    # 重复注销
    assert pool.running_sessions() == []


# ---------------- ChatSession stage 生命周期 ----------------

def test_chat_session_stage_lifecycle():
    cs = ChatSession("web:7:s1")
    assert cs.stage == ""
    cs.set_stage("执行工具 bash")
    assert cs.stage == "执行工具 bash"

    cs.start_stream()  # 新一轮开始：清空阶段
    assert cs.is_streaming and cs.stage == ""
    cs.set_stage("子代理 设备运维 处理中")
    assert cs.stage == "子代理 设备运维 处理中"

    cs.stop_stream()  # 执行结束：阶段复位
    assert not cs.is_streaming and cs.stage == ""


# ---------------- 接口冒烟（TestClient） ----------------

def _inject_streaming(server, sid, owner_tag, name="", stage=""):
    """直接注入一个“流式中/正在执行”的内存会话（模拟真实 chat 生命周期）。"""
    cs = ChatSession(sid)
    cs.start_stream()
    if stage:
        cs.set_stage(stage)
    with server._session_lock:
        server._sessions[sid] = cs
        server._session_owners[sid] = owner_tag
        server._session_owner_names[sid] = name or owner_tag


def test_admin_sessions_running_lists_all_with_user(monkeypatch):
    monkeypatch.setenv("WEBUI_DISABLE_AUTH", "1")
    w = WebServer()
    _inject_streaming(w, "web:7:a1", "web:7", name="张三", stage="执行工具 bash")
    _inject_streaming(w, "web:9:c3", "web:9", name="李四")
    # 空闲（非流式）会话不得出现在运行中
    with w._session_lock:
        w._sessions["web:7:idle"] = ChatSession("web:7:idle")
        w._session_owners["web:7:idle"] = "web:7"
    client = TestClient(w._app)

    r = client.get("/api/admin/sessions/running")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total"] == 2
    by = {s["id"]: s for s in data["sessions"]}
    assert set(by) == {"web:7:a1", "web:9:c3"}
    s = by["web:7:a1"]
    assert s["user"]["name"] == "张三"
    assert s["user"]["uid"] == "7"
    assert s["tag"] == "web:7"
    assert s["stage"] == "执行工具 bash"
    assert s["channel"] == "web"
    assert s["is_streaming"] is True
    assert s["conversation_id"] == "web:7:a1"
    # 稳定字段（前端直接消费）
    assert set(s).issuperset({"id", "conversation_id", "channel", "user", "tag",
                              "started_at", "duration_s", "stage", "model",
                              "is_streaming", "worker"})


def test_agent_sessions_running_own_only_and_cross_channel(monkeypatch):
    """普通用户(本人)：仅见自己；同 agent 用户跨渠道运行会话也合并可见。"""
    monkeypatch.setenv("WEBUI_DISABLE_AUTH", "0")
    w = WebServer()
    _inject_streaming(w, "web:7:a1", "web:7", name="张三")
    _inject_streaming(w, "dingtalk:7:b2", "dingtalk:7", name="张三")  # 同 agent 用户(钉钉)
    _inject_streaming(w, "web:9:c3", "web:9", name="李四")            # 他人不得混入
    client = TestClient(w._app)

    tok = create_jwt({"id": 7, "name": "张三", "role": "default"})
    r = client.get("/api/agent/sessions/running",
                   headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    data = r.json()
    by = {s["id"]: s for s in data["sessions"]}
    assert set(by) == {"web:7:a1", "dingtalk:7:b2"}
    assert "web:9:c3" not in by
    assert by["web:7:a1"]["user"]["name"] == "张三"
    assert by["dingtalk:7:b2"]["channel"] == "dingtalk"


def test_agent_sessions_running_admin_sees_all_and_empty(monkeypatch):
    monkeypatch.setenv("WEBUI_DISABLE_AUTH", "1")
    w = WebServer()
    client = TestClient(w._app)

    r = client.get("/api/agent/sessions/running")
    assert r.status_code == 200, r.text
    assert r.json() == {"total": 0, "sessions": []}  # 无运行时为空数组（不崩）

    _inject_streaming(w, "web:7:a1", "web:7")
    _inject_streaming(w, "web:9:c3", "web:9")
    r2 = client.get("/api/agent/sessions/running")
    assert r2.status_code == 200
    assert r2.json()["total"] == 2


def test_running_from_pool_registry_when_pool_enabled(monkeypatch):
    """池启用且内存会话被淘汰时：仍以池登记为准返回运行中（worker=True）。"""
    from unittest.mock import MagicMock  # noqa: PLC0415

    monkeypatch.setenv("WEBUI_DISABLE_AUTH", "1")
    monkeypatch.setenv("AGENT_WEB_POOL_SIZE", "2")
    agent = MagicMock()
    agent.client = MagicMock(model="qwen-max")
    w = WebServer()
    w.set_agent(agent)
    assert w._pool is not None and w._pool.enabled

    # 仅池登记、不在内存 _sessions（模拟内存会话已被淘汰 / 仅 worker 知晓）
    w._pool.register_run("web:7", "web:7:gone1",
                         started_at="2026-09-07T10:00:00", model="qwen-max")
    with w._session_lock:
        w._session_owner_names["web:7:gone1"] = "张三"
    client = TestClient(w._app)

    r = client.get("/api/admin/sessions/running")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total"] == 1
    s = data["sessions"][0]
    assert s["id"] == "web:7:gone1"
    assert s["worker"] is True
    assert s["user"]["name"] == "张三"
    assert s["model"] == "qwen-max"
