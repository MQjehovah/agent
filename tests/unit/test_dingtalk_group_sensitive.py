"""钉钉群模型 Phase2：群内敏感工具结果私聊送达。

- 敏感清单：mysql_query 三工具(list_tables/describe_table/execute_query)，集中配置可覆盖
- 工具执行层：命中清单 → 给当前 run 打敏感标记(ctx.sensitive_hit)
- 嵌套 run(子代理调用链) 敏感标记汇聚到父级 → 顶层 AgentResult.sensitive_hit
- 群共享会话(dingtalk_group:) + 结果含敏感 → 不把最终文本发群，
  私聊(单聊回执)发给触发人，群内仅发固定占位提示
- 单聊 / 非敏感工具群内 照常群/对话直接回复
"""
import os
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from agent.core import AgentResult  # noqa: E402
from channels.router import MessageRouter  # noqa: E402
from plugins.dingtalk import (  # noqa: E402
    GROUP_SENSITIVE_PRIVATE_NOTICE,
    AgentChatbotHandler,
    DingTalkPlugin,
    dingtalk_group_root_prefix,
)
from security.rbac import RBACManager  # noqa: E402
from storage.storage import Storage  # noqa: E402

# ---------------- dingtalk_stream 桩(测试不安装 SDK) ----------------

class _FakeText:
    def __init__(self, content=""):
        self.content = content


class _FakeIncomingMessage:
    def __init__(self, data):
        d = data or {}
        self.sender_id = d.get("sender_id", "")
        self.sender_staff_id = d.get("sender_staff_id", "")
        self.staff_id = d.get("staff_id", "")
        self.sender_nick = d.get("sender_nick", "")
        self.conversation_id = d.get("conversation_id", "")
        self.robot_code = d.get("robot_code", "")
        self.text = _FakeText(d.get("content", ""))


class _FakeChatbotMessage:
    TOPIC = "/chat/bot"

    @staticmethod
    def from_dict(data):
        return _FakeIncomingMessage(data)


class _FakeAckMessage:
    STATUS_OK = 200


@pytest.fixture(autouse=True)
def _fake_dingtalk_stream():
    mod = types.ModuleType("dingtalk_stream")
    mod.ChatbotMessage = _FakeChatbotMessage
    mod.AckMessage = _FakeAckMessage
    sys.modules["dingtalk_stream"] = mod
    yield mod


@pytest.fixture
def storage(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    return Storage(str(ws))


def _plugin():
    return DingTalkPlugin(config_path="/nonexistent/dingtalk.json")


def _handler(plugin, router=None):
    handler = AgentChatbotHandler(plugin)
    if router is not None:
        plugin.plugin_manager = MagicMock()
        plugin.plugin_manager.router = router
    return handler


def _bind(storage, name, staff):
    rbac = RBACManager(storage)
    uid = rbac.create_user(name=name, department="技术部", role="admin")
    rbac.bind_identity(uid, "dingtalk", staff)
    return uid


def _msg_data(**overrides):
    data = {
        "sender_id": "sender-x",
        "sender_staff_id": "staff-1",
        "sender_nick": "张三",
        "conversation_id": "conv-1",
        "robot_code": "rc-1",
        "content": "查一下 2025 年销售总额",
    }
    data.update(overrides)
    return data


# ---------------- 敏感清单(默认 + 可配置覆盖) ----------------

def test_sensitive_default_tool_list():
    """默认敏感清单含 mysql_query 三工具，其它工具不敏感。"""
    from agent.sensitive import DEFAULT_SENSITIVE_TOOLS, get_sensitive_tools, is_sensitive_tool
    assert set(DEFAULT_SENSITIVE_TOOLS) == {"list_tables", "describe_table", "execute_query"}
    tools = get_sensitive_tools()
    assert {"list_tables", "describe_table", "execute_query"} <= tools
    assert is_sensitive_tool("execute_query") is True
    assert is_sensitive_tool("list_tables") is True
    assert is_sensitive_tool("describe_table") is True
    assert is_sensitive_tool("shell") is False
    assert is_sensitive_tool("send_message_to_dingtalk") is False


def test_sensitive_tools_override_from_config(monkeypatch):
    """config.json sensitive.tools 覆盖默认清单(读取集中在 get_sensitive_tools)。"""
    import settings as settings_mod

    class _FakeSettings:
        def get(self, path, default=None):
            assert path == "sensitive.tools"
            return ["custom_report_tool"]

    monkeypatch.setattr(settings_mod, "_settings_instance", _FakeSettings())

    from agent.sensitive import get_sensitive_tools, is_sensitive_tool
    assert get_sensitive_tools() == frozenset({"custom_report_tool"})
    assert is_sensitive_tool("custom_report_tool") is True
    assert is_sensitive_tool("list_tables") is False


# ---------------- 工具执行层标记敏感 ----------------

def _fake_safe_agent():
    agent = MagicMock()
    agent._circuit_breaker = None
    agent.rbac = None
    agent.sandbox = None
    agent.plugin_manager = None
    agent.mcp = None
    agent.skill_manager = None
    agent.tool_registry = None
    agent.tracer = MagicMock()
    agent.hooks.fire = AsyncMock()
    agent.permission.check.return_value = SimpleNamespace(reason="")
    return agent


async def test_executor_marks_sensitive_hit_on_sensitive_tool_call():
    """命中敏感清单的工具名被调用 → 当前 run ctx.sensitive_hit 置 True。"""
    from agent.core import RunContext, _current_run
    from agent.executor import execute_tool_safe

    agent = _fake_safe_agent()
    rc = RunContext(user_id="x", run_id="rid-mark")
    tok = _current_run.set(rc)
    try:
        out = await execute_tool_safe(agent, "execute_query", {"query": "SELECT 1"})
    finally:
        _current_run.reset(tok)
    assert rc.sensitive_hit is True
    assert "不存在" in out  # 桩工具不真的执行, 仅证明走完了执行路径


async def test_executor_does_not_mark_non_sensitive_tool():
    from agent.core import RunContext, _current_run
    from agent.executor import execute_tool_safe

    agent = _fake_safe_agent()
    rc = RunContext(user_id="x", run_id="rid-nosens")
    tok = _current_run.set(rc)
    try:
        await execute_tool_safe(agent, "shell", {"command": "echo hi"})
    finally:
        _current_run.reset(tok)
    assert rc.sensitive_hit is False


# ---------------- 顶层结果标记 + 嵌套 run 汇聚(子代理调用链) ----------------

async def test_run_agent_result_carries_sensitive_hit(tmp_path, monkeypatch):
    """本轮 run 调过敏感工具 → 顶层 AgentResult.sensitive_hit=True。"""
    from agent.core import Agent, current_run

    agent = Agent(workspace=str(tmp_path), client=MagicMock())

    async def fake_dispatch(self_, task, session_id, user_id, user_name, inherited):
        current_run().sensitive_hit = True
        return AgentResult(agent_id="", status="completed", result="回复内容")

    monkeypatch.setattr("agent.runner.dispatch", fake_dispatch)
    res = await agent.run("查销售", run_id="t1")
    assert res.sensitive_hit is True
    assert res.result == "回复内容"


async def test_run_agent_result_not_sensitive_when_untouched(tmp_path, monkeypatch):
    from agent.core import Agent

    agent = Agent(workspace=str(tmp_path), client=MagicMock())

    async def fake_dispatch(self_, task, session_id, user_id, user_name, inherited):
        return AgentResult(agent_id="", status="completed", result="普通回复")

    monkeypatch.setattr("agent.runner.dispatch", fake_dispatch)
    res = await agent.run("普通问题", run_id="t2")
    assert res.sensitive_hit is False


async def test_nested_run_propagates_sensitive_to_parent_context(tmp_path, monkeypatch):
    """子代理(run 嵌套)在自身轮次命中敏感 → 汇聚到父级 run 上下文。

    对应数字中台子代理在主 agent run 内执行敏感 mysql_query 工具的场景：
    子 run 结束时把 sensitive_hit 上抛父级 ctx。
    """
    from agent.core import Agent, RunContext, _current_run, current_run

    parent = Agent(workspace=str(tmp_path), client=MagicMock())
    child = Agent(workspace=str(tmp_path), client=MagicMock(), parent_agent=parent)
    parent_ctx = RunContext(user_id="dingtalk:7", group_context=True, task="父任务")

    async def fake_dispatch(self_, task, session_id, user_id, user_name, inherited):
        current_run().sensitive_hit = True
        return AgentResult(agent_id="数字中台", status="completed", result="子代理返回")

    monkeypatch.setattr("agent.runner.dispatch", fake_dispatch)
    tok = _current_run.set(parent_ctx)
    try:
        res = await child.run("子任务", run_id="c1")
    finally:
        _current_run.reset(tok)
    assert res.sensitive_hit is True
    assert parent_ctx.sensitive_hit is True


# ---------------- route 透传敏感标记 ----------------

async def test_route_return_result_keeps_agent_result_meta():
    """return_result=True 时 route 返回 AgentResult(含 sensitive_hit); 默认仍解包为 str。"""

    class _Run:
        client = SimpleNamespace(model="qwen-max")

        async def run(self, content, **kwargs):
            return AgentResult(agent_id="", status="completed",
                               result="明细", sensitive_hit=True)

    router = MessageRouter(_Run())
    res = await router.route("hi", channel="dingtalk", return_result=True)
    assert res.result == "明细"
    assert res.sensitive_hit is True

    res2 = await router.route("hi", channel="dingtalk")
    assert res2 == "明细"


# ---------------- 群敏感出口改道: 私聊送达 + 群占位 ----------------

async def test_group_sensitive_result_rerouted_to_private_and_placeholder(storage):
    """群共享会话 + 敏感工具命中 → 最终文本不发群、私聊给触发人、群内发占位。"""
    uid = _bind(storage, "张三", "staff-g1")
    cid = "cidGRPsens+A=="

    router = MagicMock()
    router.route = AsyncMock(return_value=SimpleNamespace(
        result="销售总额: 1.2 亿", sensitive_hit=True))
    with patch("storage.storage.get_storage", return_value=storage):
        plugin = _plugin()
        handler = _handler(plugin, router=router)
        handler.reply_text = MagicMock()
        plugin._send_text = AsyncMock(return_value="消息已发送")

        code, _ = await handler.process(SimpleNamespace(data=_msg_data(
            sender_staff_id="staff-g1", sender_nick="张三", conversation_id=cid,
            content="查一下销售总额")))

    assert code == 200
    # 群内只发占位提示, 绝不发敏感最终文本
    args, _ = handler.reply_text.call_args
    assert args[0] == GROUP_SENSITIVE_PRIVATE_NOTICE
    assert args[0] != "销售总额: 1.2 亿"
    # 私聊(单聊回执)发给触发人 dingtalk:{uid}, 内容为最终回复文本
    send_args, send_kwargs = plugin._send_text.await_args
    assert send_args[0] == "销售总额: 1.2 亿"
    assert send_kwargs.get("local_user_id") == f"dingtalk:{uid}"
    # 群上下文信号照常传递
    assert router.route.await_args.kwargs["group_context"] is True


async def test_single_chat_sensitive_not_rerouted(storage):
    """单聊即使本轮敏感, 也正常在对话中回复(不私聊、不占位)。"""
    _bind(storage, "张三", "staff-s1")
    router = MagicMock()
    router.route = AsyncMock(return_value=SimpleNamespace(
        result="单聊敏感明细", sensitive_hit=True))
    with patch("storage.storage.get_storage", return_value=storage):
        plugin = _plugin()
        handler = _handler(plugin, router=router)
        handler.reply_text = MagicMock()
        plugin._send_text = AsyncMock(return_value="消息已发送")

        code, _ = await handler.process(SimpleNamespace(data=_msg_data(
            sender_staff_id="staff-s1", content="查我的数据")))

    assert code == 200
    args, _ = handler.reply_text.call_args
    assert args[0] == "单聊敏感明细"
    plugin._send_text.assert_not_awaited()


async def test_group_non_sensitive_result_replied_in_group(storage):
    """群共享会话 + 非敏感工具 → 照常群内回复(不私聊、不占位)。"""
    _bind(storage, "张三", "staff-g2")
    cid = "cidGRPnormal+A=="
    router = MagicMock()
    router.route = AsyncMock(return_value=SimpleNamespace(
        result="文档已生成", sensitive_hit=False))
    with patch("storage.storage.get_storage", return_value=storage):
        plugin = _plugin()
        handler = _handler(plugin, router=router)
        handler.reply_text = MagicMock()
        plugin._send_text = AsyncMock(return_value="消息已发送")

        code, _ = await handler.process(SimpleNamespace(data=_msg_data(
            sender_staff_id="staff-g2", conversation_id=cid, content="生成周报")))

    assert code == 200
    args, _ = handler.reply_text.call_args
    assert args[0] == "文档已生成"
    plugin._send_text.assert_not_awaited()
    assert handler.reply_text.call_count == 1


async def test_group_sensitive_uses_shared_group_root(storage):
    """敏感改道不改变群共享根/会话解析: 仍路由到 dingtalk_group: 根。"""
    uid = _bind(storage, "张三", "staff-g3")
    cid = "cidGRPsensB+A=="
    router = MagicMock()
    router.route = AsyncMock(return_value=SimpleNamespace(
        result="表列表", sensitive_hit=True))
    with patch("storage.storage.get_storage", return_value=storage):
        plugin = _plugin()
        handler = _handler(plugin, router=router)
        handler.reply_text = MagicMock()
        plugin._send_text = AsyncMock(return_value="消息已发送")

        await handler.process(SimpleNamespace(data=_msg_data(
            sender_staff_id="staff-g3", conversation_id=cid, content="看看有哪些表")))

    sid = router.route.await_args.kwargs["session_id"]
    assert sid.startswith(dingtalk_group_root_prefix(cid))
    assert sid.startswith("dingtalk_group:")
    assert router.route.await_args.kwargs["user_id"] == f"dingtalk:{uid}"
    assert plugin._send_text.await_args.kwargs["local_user_id"] == f"dingtalk:{uid}"


# ---------------- 落库改道: 敏感轮不落群根, 存触发人私有旁路(Phase2 收尾) ----------------

def _swap_global_storage(storage):
    """临时替换 storage 单例(AgentSession.add_message 经 get_storage() 落库)。"""
    import storage.storage as storage_mod
    prev = storage_mod._storage_instance
    storage_mod._storage_instance = storage
    return prev, storage_mod


def _boot_group_agent(storage, workspace):
    """最小可跑 run 的 Agent：注入 storage + session_manager(不 initialize)。"""
    from agent.core import Agent
    from agent.session import AgentSessionManager
    agent = Agent(workspace=workspace, client=MagicMock())
    agent.storage = storage
    agent.session_manager = AgentSessionManager()
    return agent


def _conv_rows(st, conversation_id):
    with st.get_connection() as conn:
        rows = conn.execute(
            "SELECT role, content, session_id FROM messages "
            "WHERE conversation_id = ? ORDER BY id", (conversation_id,)).fetchall()
    return [dict(r) for r in rows]


async def test_group_sensitive_round_not_in_root_history_but_in_private(tmp_path, monkeypatch):
    """群敏感轮：群根 messages 无敏感正文(整轮搬离)，私有旁路按触发人有，他人查不到。

    先跑一轮非敏感群轮 → 正常留群根；再跑敏感轮(工具产出含敏感经营数据) → 该轮整轮
    不落群根：敏感工具产出 + 含敏感结果的最终回复 + 该轮触发问题均改存触发人私有侧。
    """
    from agent.core import AgentResult, current_run

    ws = tmp_path / "ws"
    ws.mkdir()
    st = Storage(str(ws))
    prev, storage_mod = _swap_global_storage(st)
    try:
        agent = _boot_group_agent(st, str(tmp_path / "agent"))
        group = "dingtalk_group:cidsensX:abc12345:r1"

        async def fake_dispatch(self_, task, session_id, user_id, user_name, inherited):
            sess = current_run().session
            if task.startswith("正常"):
                sess.add_message("assistant", "正常回复: 周报已生成")
                return AgentResult(agent_id="", status="completed", result="正常回复: 周报已生成")
            # 模拟敏感轮: 命中 list_tables 且最终回复含敏感经营数据
            current_run().sensitive_hit = True
            sess.add_message("assistant", "", tool_calls=[{
                "id": "c1", "type": "function",
                "function": {"name": "list_tables", "arguments": "{}"}}])
            sess.add_message("tool", "客户表|订单表|销售明细表(含渠道/金额)",
                             name="list_tables", tool_call_id="c1")
            sess.add_message("assistant", "2025 年销售总额 1.2 亿（敏感经营数据）")
            return AgentResult(agent_id="", status="completed",
                               result="2025 年销售总额 1.2 亿（敏感经营数据）")

        monkeypatch.setattr("agent.runner.dispatch", fake_dispatch)

        r1 = await agent.run("正常帮我生成周报", session_id=group,
                             user_id="dingtalk:7", group_context=True)
        st.flush_messages()
        r2 = await agent.run("查一下 2025 年销售总额", session_id=group,
                             user_id="dingtalk:7", group_context=True)
        st.flush_messages()
        assert r1.sensitive_hit is False
        assert r2.sensitive_hit is True

        # 群根历史: 只含非敏感轮, 绝无敏感工具产出/含敏感结果的最终回复
        rows = _conv_rows(st, group)
        root_text = "\n".join((r["content"] or "") for r in rows)
        assert "正常回复: 周报已生成" in root_text
        assert "客户表" not in root_text
        assert "1.2 亿" not in root_text
        assert "2025 年销售总额" not in root_text

        # 私有旁路: 触发人 dingtalk:7 可见整轮(问题+工具产出+最终回复)
        priv = st.list_dingtalk_private_messages("dingtalk:7", source_conversation=group)
        priv_text = "\n".join((m.get("content") or "") for m in priv)
        assert "查一下 2025 年销售总额" in priv_text
        assert "客户表|订单表|销售明细表" in priv_text
        assert "1.2 亿" in priv_text
        rounds = st.list_dingtalk_private_rounds("dingtalk:7", source_conversation=group)
        assert len(rounds) == 1
        assert rounds[0]["msg_count"] == 4  # user + assistant(tool_calls) + tool + assistant
        # 他人(非触发人)私有侧/群根均不可见
        assert st.list_dingtalk_private_messages("dingtalk:8", source_conversation=group) == []
        assert st.list_dingtalk_private_rounds("dingtalk:8") == []

        # 群共享内存上下文不含敏感轮(后续群轮/上下文不会串)
        sess = agent.session_manager.sessions.get(group)
        mem_text = "\n".join((m.get("content") or "") for m in sess.messages)
        assert "正常回复: 周报已生成" in mem_text
        assert "1.2 亿" not in mem_text
    finally:
        st.close()
        storage_mod._storage_instance = prev


async def test_group_sensitive_round_not_restored_into_context(tmp_path, monkeypatch):
    """上下文恢复不含敏感轮: DB 恢复群根历史只有非敏感轮, 无敏感正文。"""
    from agent.core import AgentResult, current_run

    ws = tmp_path / "ws"
    ws.mkdir()
    st = Storage(str(ws))
    prev, storage_mod = _swap_global_storage(st)
    try:
        agent = _boot_group_agent(st, str(tmp_path / "agent"))
        group = "dingtalk_group:cidsensY:abc12345:r2"

        async def fake_dispatch(self_, task, session_id, user_id, user_name, inherited):
            sess = current_run().session
            if task.startswith("正常"):
                sess.add_message("assistant", "普通话题的回复")
                return AgentResult(agent_id="", status="completed", result="普通话题的回复")
            current_run().sensitive_hit = True
            sess.add_message("tool", "SELECT 工资 8000 元/月", name="execute_query",
                             tool_call_id="c1")
            sess.add_message("assistant", "工资明细已私聊")
            return AgentResult(agent_id="", status="completed", result="工资明细已私聊")

        monkeypatch.setattr("agent.runner.dispatch", fake_dispatch)
        await agent.run("正常话题", session_id=group, user_id="dingtalk:7", group_context=True)
        st.flush_messages()
        await agent.run("查工资", session_id=group, user_id="dingtalk:7", group_context=True)
        st.flush_messages()

        # 模拟进程重启后首次承接: 会话内仅剩 system, 从 DB 恢复
        from agent.session import AgentSession
        fresh = AgentSession(session_id=group, system_prompt="你是助手")
        n = agent._restore_db_session_history(fresh)
        assert n > 0  # 非敏感轮可恢复
        texts = "\n".join((m.get("content") or "") for m in fresh.messages)
        assert "普通话题的回复" in texts
        assert "工资" not in texts
        assert "8000" not in texts
    finally:
        st.close()
        storage_mod._storage_instance = prev


async def test_non_sensitive_group_round_stays_in_group_root(tmp_path, monkeypatch):
    """非敏感群轮不受影响: 照常实时落群根, 不进私有旁路, 共享上下文不截断。"""
    from agent.core import AgentResult, current_run

    ws = tmp_path / "ws"
    ws.mkdir()
    st = Storage(str(ws))
    prev, storage_mod = _swap_global_storage(st)
    try:
        agent = _boot_group_agent(st, str(tmp_path / "agent"))
        group = "dingtalk_group:cidnonsens:abc12345:r3"

        async def fake_dispatch(self_, task, session_id, user_id, user_name, inherited):
            current_run().session.add_message("assistant", "已生成文档给到大家")
            return AgentResult(agent_id="", status="completed", result="已生成文档给到大家")

        monkeypatch.setattr("agent.runner.dispatch", fake_dispatch)
        res = await agent.run("生成周报", session_id=group,
                              user_id="dingtalk:7", group_context=True)
        st.flush_messages()
        assert res.sensitive_hit is False
        rows = _conv_rows(st, group)
        root_text = "\n".join((r["content"] or "") for r in rows)
        assert "生成周报" in root_text
        assert "已生成文档给到大家" in root_text
        assert st.list_dingtalk_private_messages("dingtalk:7", source_conversation=group) == []
        sess = agent.session_manager.sessions.get(group)
        assert any("已生成文档给到大家" in (m.get("content") or "") for m in sess.messages)
    finally:
        st.close()
        storage_mod._storage_instance = prev


async def test_single_chat_sensitive_stays_in_single_root(tmp_path, monkeypatch):
    """单聊即使命中敏感: 仍照常落自己单聊根(不私有旁路、不截断), 与群轮互不影响。"""
    from agent.core import AgentResult, current_run

    ws = tmp_path / "ws"
    ws.mkdir()
    st = Storage(str(ws))
    prev, storage_mod = _swap_global_storage(st)
    try:
        agent = _boot_group_agent(st, str(tmp_path / "agent"))
        sid = "dingtalk:7:single-root-abc"

        async def fake_dispatch(self_, task, session_id, user_id, user_name, inherited):
            current_run().sensitive_hit = True
            current_run().session.add_message("assistant", "单聊里的敏感明细")
            return AgentResult(agent_id="", status="completed", result="单聊里的敏感明细")

        monkeypatch.setattr("agent.runner.dispatch", fake_dispatch)
        res = await agent.run("查我的数据", session_id=sid,
                              user_id="dingtalk:7", group_context=False)
        st.flush_messages()
        assert res.sensitive_hit is True
        rows = _conv_rows(st, sid)
        text = "\n".join((r["content"] or "") for r in rows)
        assert "查我的数据" in text
        assert "单聊里的敏感明细" in text
        assert st.list_dingtalk_private_messages("dingtalk:7") == []
        # 单聊共享上下文不截断
        sess = agent.session_manager.sessions.get(sid)
        assert any("单聊里的敏感明细" in (m.get("content") or "") for m in sess.messages)
    finally:
        st.close()
        storage_mod._storage_instance = prev


# ---------------- 私有旁路读取: 本人可查 / 他人不可见(web API) ----------------

def _seed_private_round(st, conversation_id, round_id, owner_tag):
    """往 messages 写入一轮消息后经 relocate 搬到私有旁路(等价于真实敏感轮落库路径)。"""
    for role, content, extra in [
        ("user", "查一下 2025 年销售总额", {}),
        ("assistant", "", {"tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "execute_query", "arguments": "{}"}}]}),
        ("tool", "销售明细: 2025 年总额 1.2 亿", {"name": "execute_query", "tool_call_id": "c1"}),
        ("assistant", "2025 年销售总额 1.2 亿（敏感经营数据）", {}),
    ]:
        st.save_message_sync("main", conversation_id, role, content,
                             user_id=owner_tag, conversation_id=conversation_id,
                             round_id=round_id, **extra)
    return st.relocate_round_to_dingtalk_private(conversation_id, round_id, owner_tag)


def test_private_rounds_endpoint_owner_only_visible(tmp_path, monkeypatch):
    """触发人本人(web uid=7)经 API 可查自己的群敏感轮; 他人(8)查不到; 按群过滤可用。"""
    from fastapi.testclient import TestClient

    from web.server import WebServer, create_jwt

    monkeypatch.setenv("WEBUI_DISABLE_AUTH", "0")
    group = "dingtalk_group:cidsensZ:abc12345:r9"
    st = Storage(str(tmp_path / "db"))
    prev, storage_mod = _swap_global_storage(st)
    try:
        assert _seed_private_round(st, group, "round-z", "dingtalk:7") == 4
        assert st.list_dingtalk_private_messages("dingtalk:7", source_conversation=group) != []
        # 群根 messages 已无该轮(搬运即删除)
        assert _conv_rows(st, group) == []

        w = WebServer()
        client = TestClient(w._app)
        p7 = create_jwt({"id": 7, "name": "张三", "role": "default"})
        p8 = create_jwt({"id": 8, "name": "李四", "role": "default"})

        def _get(token):
            return client.get(
                "/api/my/dingtalk/private-rounds",
                params={"conversation_id": group},
                headers={"Authorization": f"Bearer {token}"})

        r7 = _get(p7)
        assert r7.status_code == 200
        rounds = r7.json()["rounds"]
        assert len(rounds) == 1
        assert rounds[0]["conversation_id"] == group
        texts = "\n".join((m.get("content") or "") for m in rounds[0]["messages"])
        assert "查一下 2025 年销售总额" in texts
        assert "1.2 亿" in texts
        assert rounds[0]["msg_count"] == 4
        # 他人不可见
        r8 = _get(p8)
        assert r8.status_code == 200
        assert r8.json()["rounds"] == []
    finally:
        st.close()
        storage_mod._storage_instance = prev


