"""范围修正后端行为：定时任务个人/全量、记忆个人默认不含 global、
外部渠道(钉钉)会话在 web /api/chat(stream) 的续聊写回被拒绝。

- /api/scheduler/tasks: 默认 scope=mine(仅本人创建, 排除 static), admin scope=all 含 static+全部
- /api/memories: 默认 view=mine 只含本人私有(不含 global), admin view=all 含 global+全部
- /api/chat(/stream): 携带非 web 前缀 session_id 续聊 → 400; 他人会话仍 404
"""
import os
import sys
from datetime import datetime
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest
from fastapi.testclient import TestClient

import storage.storage as storage_mod
from storage.storage import Storage
from web.server import WebServer, create_jwt


@pytest.fixture
def env(tmp_path, monkeypatch):
    """真实 JWT 鉴权 + 临时 storage 单例 + WebServer。"""
    monkeypatch.setenv("WEBUI_DISABLE_AUTH", "0")
    prev = storage_mod._storage_instance
    s = Storage(str(tmp_path))
    storage_mod._storage_instance = s
    w = WebServer()
    client = TestClient(w._app)
    yield w, s, client
    s.close()
    storage_mod._storage_instance = prev


def _token(uid: int, role: str = "default", name: str = "用户") -> dict:
    return {"Authorization": f"Bearer {create_jwt({'id': uid, 'name': name, 'role': role})}"}


def _seed_msg(st, conv_id, user_tag, pairs: int = 1):
    for i in range(pairs):
        st.save_message_sync("main", conv_id, "user", f"消息{i}", user_id=user_tag,
                             conversation_id=conv_id)
        st.save_message_sync("main", conv_id, "assistant", f"回复{i}", user_id=user_tag,
                             conversation_id=conv_id)


def _seed_task_row(st, task_id: str, user_id: str, name: str = "任务"):
    """保留为辅助（真实 DB 持久化形态，便于扩展为真插件集成测试）。"""
    now = datetime.now().isoformat(timespec="seconds")
    with st.get_connection() as conn:
        conn.execute(
            """INSERT INTO scheduled_tasks
               (id, name, cron, task, user_id, user_name, session_id, enabled,
                last_run_at, last_result, last_error, run_count, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,1,NULL,NULL,NULL,0,?,?)""",
            (task_id, name, "0 9 * * *", f"执行{name}", user_id, "", "",
             now, now),
        )
        conn.commit()


# ===== 定时任务：个人 vs 全量 =====

def test_scheduler_default_mine_excludes_static_and_others(env):
    w, st, client = env
    sp = MagicMock()
    rows = [
        {"id": "t7", "name": "七的任务", "cron": "0 9 * * *", "task": "执行七的任务",
         "enabled": 1, "user_id": "web:7", "user_name": "", "last_run_at": None,
         "last_result": None, "last_error": None, "run_count": 0,
         "created_at": "", "updated_at": ""},
        {"id": "t9", "name": "九的任务", "cron": "0 9 * * *", "task": "执行九的任务",
         "enabled": 1, "user_id": "web:9", "user_name": "", "last_run_at": None,
         "last_result": None, "last_error": None, "run_count": 0,
         "created_at": "", "updated_at": ""},
    ]
    sp.schedules = [{"id": "st1", "name": "系统报表", "cron": "0 10 * * 0",
                     "task": "静态任务", "enabled": True}]
    sp.list_db_tasks.side_effect = lambda user_id="": (
        rows if not user_id else [r for r in rows if r["user_id"] == user_id])

    mock_pm = MagicMock()
    mock_pm.get_plugin.return_value = sp
    mock_agent = MagicMock()
    mock_agent.plugin_manager = mock_pm
    w.set_agent(mock_agent)

    # 普通用户：默认只看到自己创建的，不含 static / 他人
    r = client.get("/api/scheduler/tasks", headers=_token(7))
    assert r.status_code == 200, r.text
    ids = {t["id"] for t in r.json()["tasks"]}
    assert ids == {"t7"}

    # 普通用户显式 scope=all 仍被忽略（无静态/他人）
    r = client.get("/api/scheduler/tasks?scope=all", headers=_token(7))
    assert {t["id"] for t in r.json()["tasks"]} == {"t7"}

    # admin scope=all：static + 全部用户
    r = client.get("/api/scheduler/tasks?scope=all", headers=_token(1, role="admin"))
    assert r.status_code == 200, r.text
    assert {t["id"] for t in r.json()["tasks"]} == {"st1", "t7", "t9"}


# ===== 记忆：个人默认不含 global，view=all 才含 =====

def test_memories_mine_excludes_global_view_all_includes(env):
    w, st, client = env
    st.save_memory(scope="global", owner_id="", category="knowledge",
                   content="公共记忆X", source="admin")
    st.save_memory(scope="user", owner_id="web:7", category="key_info",
                   content="七的私有", source="agent")
    st.save_memory(scope="user", owner_id="web:9", category="key_info",
                   content="九的私有", source="agent")

    # 普通用户 mine：仅本人私有
    r = client.get("/api/memories", headers=_token(7))
    contents = {m["content"] for m in r.json()["memories"]}
    assert contents == {"七的私有"}

    # 普通用户即使 view=all 也被强制为个人口径
    r = client.get("/api/memories?view=all", headers=_token(7))
    assert {m["content"] for m in r.json()["memories"]} == {"七的私有"}

    # admin mine（个人空间视角）同样不含 global/他人
    r = client.get("/api/memories", headers=_token(1, role="admin"))
    assert {m["content"] for m in r.json()["memories"]} == set()

    # admin view=all：global + 全部用户私有
    r = client.get("/api/memories?view=all", headers=_token(1, role="admin"))
    contents = {m["content"] for m in r.json()["memories"]}
    assert contents == {"公共记忆X", "七的私有", "九的私有"}


# ===== 续聊写回：非 web 渠道只读 =====

def _make_agent_for(w):
    mock_agent = MagicMock()
    mock_agent.plugin_manager = MagicMock()
    w.set_agent(mock_agent)


def test_chat_rejects_foreign_channel_session_write(env):
    w, st, client = env
    _seed_msg(st, "dingtalk:7:b1", "dingtalk:7")
    _make_agent_for(w)

    # 属主在 web 续聊钉钉会话 → 400(只读)
    r = client.post("/api/chat/stream", json={
        "message": "继续", "session_id": "dingtalk:7:b1"}, headers=_token(7))
    assert r.status_code == 400, r.text
    assert "外部渠道" in r.text

    # 非流式 /api/chat 同样拒绝
    r = client.post("/api/chat", json={
        "message": "继续", "session_id": "dingtalk:7:b1"}, headers=_token(7))
    assert r.status_code == 400, r.text

    # 他人无法触碰 → 404(不泄漏会话存在性)
    r = client.post("/api/chat/stream", json={
        "message": "继续", "session_id": "dingtalk:7:b1"}, headers=_token(9))
    assert r.status_code == 404, r.text


def test_chat_still_accepts_web_session_write_shape(env):
    """web 前缀会话不被误杀：非流式 /api/chat 走到创建会话分支而非 400。"""
    w, st, client = env
    _seed_msg(st, "web:7:a1", "web:7")
    _make_agent_for(w)

    # 属主 web 会话放行(非 400)；此处返回 processing 即证明通过 web 前缀校验
    r = client.post("/api/chat", json={
        "message": "继续", "session_id": "web:7:a1"}, headers=_token(7))
    assert r.status_code == 200, r.text
    assert r.json().get("session_id") == "web:7:a1"
    # 他人 web 会话 → 404
    r = client.post("/api/chat", json={
        "message": "继续", "session_id": "web:7:a1"}, headers=_token(9))
    assert r.status_code == 404
