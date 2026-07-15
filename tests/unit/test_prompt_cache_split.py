"""P3 测试：Prompt cache 拆分（静态/动态双 system message）。

验证 PromptBuilder.build() 分离 static/dynamic、_build_prompt 写入 RunContext 的
system_static/system_dynamic、_apply_system_messages 规范化为双 system 前缀（幂等、
重载安全）、_add_prompt_cache 只标记 static 那条、static 跨轮字节稳定可命中缓存。
"""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from agent import Agent, RunContext, _current_run  # noqa: E402
from prompt import PromptBuilder  # noqa: E402


def _make_bare_agent():
    """构造最小 Agent（跳过 initialize 重依赖），环境上下文固定以便断言。"""
    agent = Agent(workspace=".", client=MagicMock())
    agent.name = "测试"
    agent.system_prompt_raw = "BASE_PROMPT"
    agent.skill_manager = None
    agent.subagent_manager = None
    agent.memory = None
    agent._get_env_context = lambda task="": "ENV_CTX"
    return agent


# ── PromptBuilder 分离 ──

def test_prompt_builder_split_returns_static_dynamic():
    builder = PromptBuilder()
    builder.add("角色", "ROLE", is_static=True, priority=0)
    builder.add("环境", "ENV", is_static=False, priority=30)
    static, dynamic = builder.build()
    assert "ROLE" in static
    assert "ENV" not in static
    assert "ENV" in dynamic


def test_build_prompt_sets_static_dynamic_on_run_context():
    agent = _make_bare_agent()
    ctx = RunContext(user_id="u1")
    token = _current_run.set(ctx)
    try:
        agent._build_prompt("任务")
    finally:
        _current_run.reset(token)
    assert "BASE_PROMPT" in ctx.system_static
    assert "ENV_CTX" in ctx.system_dynamic
    # static 与 dynamic 分离
    assert "ENV_CTX" not in ctx.system_static
    # 兼容：system_prompt 仍为二者拼接
    assert ctx.system_prompt == ctx.system_static + ctx.system_dynamic


# ── _apply_system_messages 规范化 ──

def test_apply_system_messages_creates_two_system_prefixes():
    msgs = [{"role": "user", "content": "hi"}]
    result = Agent._apply_system_messages(msgs, "STATIC", "DYNAMIC")
    assert result[0] == {"role": "system", "content": "STATIC"}
    assert result[1] == {"role": "system", "content": "DYNAMIC"}
    assert result[2] == msgs[0]  # 原消息保留


def test_apply_system_messages_replaces_existing_single_system():
    """重载或旧单 system 场景：已有 system 前缀被替换为双 system，不残留旧内容。"""
    msgs = [{"role": "system", "content": "OLD"}, {"role": "user", "content": "hi"}]
    result = Agent._apply_system_messages(msgs, "STATIC", "DYNAMIC")
    system_contents = [m["content"] for m in result if m["role"] == "system"]
    assert system_contents == ["STATIC", "DYNAMIC"]
    assert "OLD" not in system_contents


def test_apply_system_messages_idempotent_dynamic_updates():
    """连续调用（模拟多轮）不累积 system，且 dynamic 每轮可更新。"""
    msgs = Agent._apply_system_messages([{"role": "user", "content": "hi"}], "S", "D1")
    msgs2 = Agent._apply_system_messages(msgs, "S", "D2")
    system_msgs = [m for m in msgs2 if m["role"] == "system"]
    assert len(system_msgs) == 2  # 不累积
    assert system_msgs[0]["content"] == "S"
    assert system_msgs[1]["content"] == "D2"  # dynamic 更新


# ── cache 标记：只标记 static ──

def test_add_prompt_cache_marks_static_only():
    from llm import LLMClient
    messages = [
        {"role": "system", "content": "STATIC"},
        {"role": "system", "content": "DYNAMIC"},
        {"role": "user", "content": "hi"},
    ]
    result = LLMClient._add_prompt_cache(messages, "glm-5")
    assert "cache_control" in result[0]   # static 被标记（可缓存）
    assert "cache_control" not in result[1]  # dynamic 不标记（每轮变）


# ── static 跨轮稳定（可命中缓存）──

def test_static_stable_across_rebuilds():
    """同一 run 内多次重建 prompt，static（角色定义）字节稳定。"""
    agent = _make_bare_agent()
    ctx = RunContext(user_id="u1")
    token = _current_run.set(ctx)
    try:
        agent._build_prompt("任务A")
        static_a = ctx.system_static
        agent._build_prompt("任务B")  # 重建（模拟新一轮）
        static_b = ctx.system_static
    finally:
        _current_run.reset(token)
    assert static_a == static_b  # 字节稳定 → 可被 prompt cache 命中
