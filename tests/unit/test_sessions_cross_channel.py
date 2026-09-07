"""Task2 跨渠道「我的会话」：web + 钉钉(改后) + 其它 tag:{uid} 渠道合并展示。

- storage.list_conversations_for_agent_user() 按 agent 用户 id 聚合其全部渠道对话根
- 改造前旧格式钉钉会话(dingtalk:{staff_id}/无前缀)对普通用户保持不可见
- channel_of_conversation() 从前缀解析渠道, 供前端徽标
- /api/agent/sessions/history 对普通用户返回带 channel 的跨渠道会话
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from fastapi.testclient import TestClient  # noqa: E402

from storage.storage import Storage, channel_of_conversation  # noqa: E402

# ---------------- 种子数据辅助 ----------------

def _seed_conv(st, conversation_id, user_id, msg_pairs=1, agent_id="main",
               thread_sessions=0):
    """写入一个对话根：N 对 user/assistant 根消息 + 可选内部子会话(thread)。"""
    for i in range(msg_pairs):
        st.save_message_sync(agent_id, conversation_id, "user", f"内容{i}",
                             user_id=user_id, conversation_id=conversation_id)
        st.save_message_sync(agent_id, conversation_id, "assistant", f"回复{i}",
                             user_id=user_id, conversation_id=conversation_id)
    for k in range(thread_sessions):
        st.save_message_sync(agent_id, f"{conversation_id}#thread{k}",
                             "user", f"子会话{k}", user_id=user_id,
                             conversation_id=conversation_id)


def _new_storage(tmp_path):
    return Storage(str(tmp_path))


# ---------------- 渠道解析 helper ----------------

def test_channel_of_conversation_prefix():
    assert channel_of_conversation("web:7:abc") == "web"
    assert channel_of_conversation("dingtalk:7:abc") == "dingtalk"
    assert channel_of_conversation("wecom:7:abc") == "wecom"
    # 老数据无前缀: 回退 user_id 的渠道前缀, 再无则 other
    assert channel_of_conversation("random_no_prefix") == "other"
    assert channel_of_conversation("random_no_prefix", "dingtalk:7") == "dingtalk"
    assert channel_of_conversation("", "") == "other"


# ---------------- 跨渠道合并查询 ----------------

def test_list_conversations_for_agent_user_merges_all_channels(tmp_path):
    st = _new_storage(tmp_path)
    try:
        _seed_conv(st, "web:7:a1", "web:7", thread_sessions=1)
        _seed_conv(st, "dingtalk:7:b2", "dingtalk:7")
        _seed_conv(st, "wecom:7:d4", "wecom:7")  # 未来渠道同约定同样归该用户
        _seed_conv(st, "web:8:c3", "web:8")      # 他人 web 会话不得混入

        rows = st.list_conversations_for_agent_user(7)
        ids = {r["conversation_id"] for r in rows}
        assert ids == {"web:7:a1", "dingtalk:7:b2", "wecom:7:d4"}
        assert "web:8:c3" not in ids

        by_id = {r["conversation_id"]: r for r in rows}
        web_row = by_id["web:7:a1"]
        assert web_row["channel"] == "web"
        assert web_row["user_id"] == "web:7"
        # 根消息 2 条; 子会话计入 thread_count 而不计入主消息数
        assert web_row["msg_count"] == 2
        assert web_row["thread_count"] == 1
        assert by_id["dingtalk:7:b2"]["channel"] == "dingtalk"
        assert by_id["wecom:7:d4"]["channel"] == "wecom"

        # uid 以字符串传入同样命中
        assert {r["conversation_id"] for r in st.list_conversations_for_agent_user("7")} == ids
    finally:
        st.close()


def test_list_conversations_for_agent_user_hides_legacy_dingtalk(tmp_path):
    """改造前旧钉钉会话: 即使 staff id 恰好是数字, 也不因 session 前缀不符而泄漏。"""
    st = _new_storage(tmp_path)
    try:
        # 新格式钉钉会话应可见
        _seed_conv(st, "dingtalk:7:new1", "dingtalk:7")
        # 旧格式: session_id=dingtalk:{cid}:{staff}, user_id=dingtalk:{staff};
        # staff 数字恰等于 agent uid 7 -> 仅后缀匹配会误中, 靠 session LIKE 交叉校验拦截
        _seed_conv(st, "dingtalk:cid-old:7", "dingtalk:7")
        # 旧格式: staff 非数字, 无前缀会话
        _seed_conv(st, "conv_legacy_xyz", "dingtalk:staff_abc")

        rows = st.list_conversations_for_agent_user(7)
        ids = {r["conversation_id"] for r in rows}
        assert ids == {"dingtalk:7:new1"}
    finally:
        st.close()


def test_legacy_web_session_visible_even_without_user_id(tmp_path):
    """老 web 会话(user_id 缺省或截断)也必须仍可见(按 session 前缀兜底)。"""
    st = _new_storage(tmp_path)
    try:
        # user_id 空(历史未回填): 靠 session LIKE web:{uid}:% 兜底可见
        _seed_conv(st, "web:12:legacy", "web:12")
        # 人为把 user_id 改成空, 模拟缺列时期的老数据
        with st.get_connection() as conn:
            conn.execute(
                "UPDATE messages SET user_id = '' WHERE session_id = 'web:12:legacy'")
            conn.commit()
        _seed_conv(st, "web:2:other", "web:2")  # 他人(截断误标的目标)不得抢占

        rows = st.list_conversations_for_agent_user(12)
        assert {r["conversation_id"] for r in rows} == {"web:12:legacy"}
        # uid 2 只看到自己的会话, 不得抢占/混入 uid 12 的 legacy
        assert {r["conversation_id"] for r in st.list_conversations_for_agent_user(2)} == {"web:2:other"}
    finally:
        st.close()


def test_web_user_id_self_heal_backfill(tmp_path):
    """幂等自愈迁移: web 会话的 user_id 按第二段(uid)正确回填/纠正。"""
    st = _new_storage(tmp_path)
    try:
        # 写入缺 user_id 的老数据(模拟列迁移前/截断误标), 再重跑 _init_db 触发自愈
        with st.get_connection() as conn:
            conn.execute(
                "INSERT INTO messages (session_id, agent_id, role, content, conversation_id, created_at)"
                " VALUES ('web:12:a1', 'main', 'user', 'hi', 'web:12:a1', '2026-01-01T00:00:00')")
            conn.commit()
        with st.get_connection() as conn:
            conn.execute(
                "UPDATE messages SET user_id = 'web:1' WHERE session_id = 'web:12:a1'")
            conn.commit()
        st._init_db()
        with st.get_connection() as conn:
            rows = conn.execute(
                "SELECT user_id FROM messages WHERE session_id='web:12:a1'").fetchall()
        assert rows and rows[0][0] == "web:12"
    finally:
        st.close()


def test_list_conversations_tag_filter_unchanged(tmp_path):
    """既有 list_conversations(user_id=tag) 语义不变; 且新增 channel 字段。"""
    st = _new_storage(tmp_path)
    try:
        _seed_conv(st, "web:7:a1", "web:7")
        _seed_conv(st, "dingtalk:7:b2", "dingtalk:7")
        rows = st.list_conversations(user_id="web:7")
        assert {r["conversation_id"] for r in rows} == {"web:7:a1"}
        assert all(r["channel"] for r in rows)
        # 无过滤全量
        all_rows = st.list_conversations()
        assert {r["conversation_id"] for r in all_rows} == {"web:7:a1", "dingtalk:7:b2"}
        assert {r["channel"] for r in all_rows} == {"web", "dingtalk"}
    finally:
        st.close()


# ---------------- /api/agent/sessions/history (普通用户跨渠道) ----------------

def _seed_history_db(st):
    _seed_conv(st, "web:7:a1", "web:7")
    _seed_conv(st, "dingtalk:7:b2", "dingtalk:7")
    _seed_conv(st, "web:9:c3", "web:9")


def test_history_non_admin_merges_own_channels(tmp_path, monkeypatch):
    """普通用户(非 admin): history 返回其 web+钉钉会话并带 channel, 不含他人会话。"""
    import storage.storage as storage_mod
    from web.server import WebServer, create_jwt

    monkeypatch.setenv("WEBUI_DISABLE_AUTH", "0")  # 走真实 JWT 鉴权路径(非 admin)

    prev = storage_mod._storage_instance
    s = _new_storage(tmp_path)
    storage_mod._storage_instance = s
    try:
        _seed_history_db(s)
        w = WebServer()
        client = TestClient(w._app)
        token = create_jwt({"id": 7, "name": "张三", "role": "default"})
        headers = {"Authorization": f"Bearer {token}"}

        resp = client.get("/api/agent/sessions/history?limit=20", headers=headers)
        assert resp.status_code == 200, resp.text
        sessions = resp.json()["sessions"]
        ids = {x["id"] for x in sessions}
        assert ids == {"web:7:a1", "dingtalk:7:b2"}
        assert "web:9:c3" not in ids
        assert {x["channel"] for x in sessions} == {"web", "dingtalk"}
        assert all(x["channel"] != "other" for x in sessions)
    finally:
        s.close()
        storage_mod._storage_instance = prev


def test_history_admin_lists_all_with_channel(tmp_path, monkeypatch):
    """admin(DISABLE_AUTH) 场景保持全量视图语义, 且每条新增 channel。"""
    import storage.storage as storage_mod
    from web.server import WebServer  # noqa: PLC0415

    monkeypatch.setenv("WEBUI_DISABLE_AUTH", "1")
    prev = storage_mod._storage_instance
    s = _new_storage(tmp_path)
    storage_mod._storage_instance = s
    try:
        # 与既有 test_sessions_history_reads_from_db 相同的裸写入
        s.save_message_sync("agentA", "sess1", "user", "hello")
        s.save_message_sync("agentA", "sess1", "assistant", "hi")
        _seed_conv(s, "dingtalk:7:b2", "dingtalk:7")
        w = WebServer()
        client = TestClient(w._app)

        resp = client.get("/api/agent/sessions/history?limit=20")
        assert resp.status_code == 200, resp.text
        sessions = resp.json()["sessions"]
        ids = {x["id"] for x in sessions}
        assert "sess1" in ids and "dingtalk:7:b2" in ids
        s1 = next(x for x in sessions if x["id"] == "sess1")
        assert s1["messages"] == 2
        assert s1["agent_id"] == "agentA"
        assert s1["channel"] == "other"
        dt = next(x for x in sessions if x["id"] == "dingtalk:7:b2")
        assert dt["channel"] == "dingtalk"
    finally:
        s.close()
        storage_mod._storage_instance = prev


def test_history_owner_can_read_dingtalk_messages_via_web(tmp_path, monkeypatch):
    """跨渠道归属: web 用户可读取自己钉钉会话的 DB 历史; 他人不可见(404)。"""
    from unittest.mock import MagicMock  # noqa: PLC0415

    import storage.storage as storage_mod
    from web.server import WebServer, create_jwt  # noqa: PLC0415

    monkeypatch.setenv("WEBUI_DISABLE_AUTH", "0")

    prev = storage_mod._storage_instance
    s = _new_storage(tmp_path)
    storage_mod._storage_instance = s
    try:
        _seed_conv(s, "dingtalk:7:b2", "dingtalk:7")
        w = WebServer()
        mock_sm = MagicMock()
        mock_sm.sessions = {}
        mock_agent = MagicMock()
        mock_agent.session_manager = mock_sm
        mock_agent.subagent_manager = None
        mock_agent.name = "main"
        w.set_agent(mock_agent)
        client = TestClient(w._app)

        owner = create_jwt({"id": 7, "name": "张三", "role": "default"})
        other = create_jwt({"id": 9, "name": "李四", "role": "default"})

        resp = client.get("/api/agent/sessions/messages",
                          params={"session_id": "dingtalk:7:b2"},
                          headers={"Authorization": f"Bearer {owner}"})
        assert resp.status_code == 200, resp.text
        assert any("内容0" in (m.get("content") or "") for m in resp.json()["messages"])

        resp2 = client.get("/api/agent/sessions/messages",
                           params={"session_id": "dingtalk:7:b2"},
                           headers={"Authorization": f"Bearer {other}"})
        assert resp2.status_code == 404
    finally:
        s.close()
        storage_mod._storage_instance = prev
