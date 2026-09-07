"""钉钉群共享会话模型 Phase1：可见性/渠道解析/记忆隔离(群) 相关测试。

- channel_of_conversation 把 dingtalk_group: 前缀仍判为 dingtalk 渠道
- storage.list_conversations_for_agent_user 并入「群参与者可见」的群根(只读)
- web 群根历史访问控制：参与者(dingtalk:{uid} 出现过的成员) + admin 可见，非参与者 404
- 单聊/群聊前缀隔离：单聊最近根查询不会命中 dingtalk_group 群根
- 记忆隔离：group_context 时不注入触发人私有记忆；记忆工具置空属主后不写/不读私有
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from unittest.mock import MagicMock  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from storage.storage import Storage, channel_of_conversation  # noqa: E402


def _seed_conv(st, conversation_id, user_tag, n=1):
    """向某个对话根写入 n 对 user/assistant 消息(user_id=user_tag)。"""
    for i in range(n):
        st.save_message_sync("main", conversation_id, "user", f"内容{i}",
                             user_id=user_tag, conversation_id=conversation_id)
        st.save_message_sync("main", conversation_id, "assistant", f"回复{i}",
                             user_id=user_tag, conversation_id=conversation_id)


GROUP_ROOT = "dingtalk_group:cidGRPVis:ab12cd34:aa11"


# ---------------- 渠道解析 ----------------

def test_channel_of_conversation_group_prefix_is_dingtalk():
    assert channel_of_conversation(GROUP_ROOT) == "dingtalk"
    assert channel_of_conversation("dingtalk:7:abc") == "dingtalk"
    assert channel_of_conversation("dingtalk_group:" + "x" * 20) == "dingtalk"
    # 群根不影响其它渠道解析
    assert channel_of_conversation("web:7:abc") == "web"


# ---------------- 单/群前缀隔离 ----------------

def test_single_recent_root_query_does_not_match_group_root(tmp_path):
    """单聊最近根 DB 查询(session LIKE 'dingtalk:{uid}:%')不会命中群根。"""
    st = Storage(str(tmp_path))
    try:
        _seed_conv(st, GROUP_ROOT, "dingtalk:7", n=1)
        _seed_conv(st, "dingtalk:7:single-root", "dingtalk:7", n=1)
        with st.get_connection() as conn:
            rows = conn.execute(
                "SELECT session_id FROM messages "
                "WHERE user_id = ? AND session_id = conversation_id "
                "  AND session_id LIKE ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                ("dingtalk:7", "dingtalk:7:%")).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "dingtalk:7:single-root"
        assert not rows[0][0].startswith("dingtalk_group:")
    finally:
        st.close()


def test_single_and_group_prefix_do_not_cross_merge(tmp_path):
    """list_conversations_for_agent_user: 单聊根与群根按不同前缀互不误并。"""
    st = Storage(str(tmp_path))
    try:
        _seed_conv(st, "dingtalk:7:single", "dingtalk:7", n=1)
        _seed_conv(st, GROUP_ROOT, "dingtalk:7", n=1)
        rows = st.list_conversations_for_agent_user(7)
        ids = {r["conversation_id"] for r in rows}
        # 群根需参与者判定才会并入, 单聊根独立存在
        assert "dingtalk:7:single" in ids
    finally:
        st.close()


# ---------------- 群参与者可见性(storage 层) ----------------

def test_list_conversations_merges_group_roots_for_participants_only(tmp_path):
    st = Storage(str(tmp_path))
    try:
        _seed_conv(st, GROUP_ROOT, "dingtalk:7", n=1)
        _seed_conv(st, GROUP_ROOT, "dingtalk:8", n=1)
        _seed_conv(st, "dingtalk:7:single", "dingtalk:7", n=1)

        ids7 = {r["conversation_id"] for r in st.list_conversations_for_agent_user(7)}
        ids8 = {r["conversation_id"] for r in st.list_conversations_for_agent_user(8)}
        # 参与者 7/8 均见群根 + 各自的单聊根
        assert GROUP_ROOT in ids7
        assert GROUP_ROOT in ids8
        assert "dingtalk:7:single" in ids7
        # 非参与者 9 看不到群根
        assert {r["conversation_id"] for r in st.list_conversations_for_agent_user(9)} == set()

        row = next(r for r in st.list_conversations_for_agent_user(7)
                   if r["conversation_id"] == GROUP_ROOT)
        assert row["channel"] == "dingtalk"
        assert row["msg_count"] == 4  # 7+8 各 2 条(整群根)
        assert st.is_dingtalk_group_participant(GROUP_ROOT, 7) is True
        assert st.is_dingtalk_group_participant(GROUP_ROOT, 8) is True
        assert st.is_dingtalk_group_participant(GROUP_ROOT, 9) is False
        assert st.is_dingtalk_group_participant("dingtalk:7:single", 7) is False
    finally:
        st.close()


def test_dingtalk_scope_pointer_upsert(tmp_path):
    """scope→根 持久指针 upsert(幂等建表/读/覆写)。"""
    st = Storage(str(tmp_path))
    try:
        assert st.get_dingtalk_scope_root("g", "pre1") is None
        st.upsert_dingtalk_scope_root("g", "pre1", "dingtalk_group:p:1:r1")
        assert st.get_dingtalk_scope_root("g", "pre1") == "dingtalk_group:p:1:r1"
        # /new 覆写指针
        st.upsert_dingtalk_scope_root("g", "pre1", "dingtalk_group:p:1:r2")
        assert st.get_dingtalk_scope_root("g", "pre1") == "dingtalk_group:p:1:r2"
        # 不同 scope 互不影响; 单聊 scope 亦可登记
        st.upsert_dingtalk_scope_root("s", "7", "dingtalk:7:r9")
        assert st.get_dingtalk_scope_root("s", "7") == "dingtalk:7:r9"
        assert st.get_dingtalk_scope_root("g", "pre1") == "dingtalk_group:p:1:r2"
    finally:
        st.close()


# ---------------- web 群根历史可见性(server 层) ----------------

def _swap_storage(tmp_path):
    import storage.storage as storage_mod
    prev = storage_mod._storage_instance
    s = Storage(str(tmp_path))
    storage_mod._storage_instance = s
    return prev, s


def test_group_history_visible_to_participants_not_outsiders(tmp_path, monkeypatch):
    """群根历史：参与者(7/8)可读、非参与者(9)404；admin 恒可读。"""
    import storage.storage as storage_mod
    from web.server import WebServer, create_jwt

    monkeypatch.setenv("WEBUI_DISABLE_AUTH", "0")
    prev, s = _swap_storage(tmp_path)
    try:
        _seed_conv(s, GROUP_ROOT, "dingtalk:7", n=1)
        _seed_conv(s, GROUP_ROOT, "dingtalk:8", n=1)

        mock_sm = MagicMock()
        mock_sm.sessions = {}
        w = WebServer()
        mock_agent = MagicMock()
        mock_agent.session_manager = mock_sm
        mock_agent.subagent_manager = None
        mock_agent.name = "main"
        w.set_agent(mock_agent)
        client = TestClient(w._app)

        p7 = create_jwt({"id": 7, "name": "张三", "role": "default"})
        p8 = create_jwt({"id": 8, "name": "李四", "role": "default"})
        outsider = create_jwt({"id": 9, "name": "王五", "role": "default"})
        admin = create_jwt({"id": 1, "name": "admin", "role": "admin"})

        def _read(token):
            return client.get("/api/agent/sessions/messages",
                              params={"session_id": GROUP_ROOT},
                              headers={"Authorization": f"Bearer {token}"})

        assert _read(p7).status_code == 200
        assert _read(p8).status_code == 200
        assert _read(outsider).status_code == 404
        assert _read(admin).status_code == 200
        assert any("内容0" in (m.get("content") or "")
                   for m in _read(p7).json()["messages"])
    finally:
        s.close()
        storage_mod._storage_instance = prev


def test_group_roots_appear_in_my_sessions_history(tmp_path, monkeypatch):
    """web「我的会话」并入参与者可见的群根(只读列表)。"""
    import storage.storage as storage_mod
    from web.server import WebServer, create_jwt

    monkeypatch.setenv("WEBUI_DISABLE_AUTH", "0")
    prev, s = _swap_storage(tmp_path)
    try:
        _seed_conv(s, GROUP_ROOT, "dingtalk:7", n=1)
        w = WebServer()
        client = TestClient(w._app)
        token = create_jwt({"id": 7, "name": "张三", "role": "default"})
        resp = client.get("/api/agent/sessions/history?limit=20",
                          headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text
        ids = {x["id"] for x in resp.json()["sessions"]}
        assert GROUP_ROOT in ids
        row = next(x for x in resp.json()["sessions"] if x["id"] == GROUP_ROOT)
        assert row["channel"] == "dingtalk"
    finally:
        s.close()
        storage_mod._storage_instance = prev


# ---------------- 记忆隔离(群) ----------------

def test_group_context_skips_private_memory_injection(tmp_path):
    """group_context=True 时, agent._build_prompt 不注入触发人私有记忆。"""
    from agent.core import Agent, RunContext, _current_run

    agent = Agent(workspace=str(tmp_path), client=MagicMock())
    agent.memory = MagicMock()
    agent.memory.load_memory.return_value = "【记忆】私密记忆ABC"

    def _build(group_context):
        rc = RunContext(user_id="dingtalk:7", group_context=group_context, task="查表")
        tok = _current_run.set(rc)
        try:
            agent._build_prompt(task="查表")
        finally:
            _current_run.reset(tok)
        return rc.system_prompt or ""

    assert "私密记忆ABC" in _build(False)          # 单聊: 注入触发人私有记忆
    agent.memory.load_memory.reset_mock()
    group_prompt = _build(True)                    # 群聊: 跳过
    assert "私密记忆ABC" not in group_prompt
    agent.memory.load_memory.assert_not_called()


def test_group_context_memory_tool_blank_owner_no_private_read(tmp_path):
    """群上下文记忆工具置空属主: 搜索不返回私有记忆(load_memory('') 为空)。"""
    from memory.manager import MemoryManager
    from tools.memory import MemoryTool

    s = Storage(str(tmp_path))
    try:
        m = MemoryManager(storage=s)
        m.add_key_info("dingtalk:7", "仅本人私密")
        s.save_memory(scope="global", owner_id="", category="knowledge",
                      content="公共知识", importance=3)
        tool = MemoryTool(memory_manager=m)
        res = tool._search({"_local_user_id": ""})
        assert "仅本人私密" not in res
        # list 空属主: 只读 global, 不含私有
        lst = tool._list({"_local_user_id": "", "memory_type": "daily"})
        assert "公共知识" in lst
        assert "仅本人私密" not in lst
    finally:
        s.close()

