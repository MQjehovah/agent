"""并发安全单元测试

验证并发修复（contextvars 隔离 / Hook run_id 过滤 / 存储写锁）确实生效。
"""
import asyncio
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from agent import RunContext, _current_run, current_run  # noqa: E402
from hooks import HookEvent, HookManager, reset_run_id, set_run_id  # noqa: E402

# ═══════════════════════════════════════════════════════════
#  RunContext / contextvars 并发隔离
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_run_context_isolated_between_concurrent_tasks():
    """两个并发 Task 各自 set 不同的 RunContext，互不串读（核心：消除用户身份串号）。"""
    results = {}

    async def worker(name: str, uid: str):
        token = _current_run.set(RunContext(user_id=uid, task=name))
        try:
            # 在 await 期间让出，模拟并发交错
            await asyncio.sleep(0.05)
            # 关键断言：读到的必须是本 Task 自己 set 的值
            results[name] = current_run().user_id
        finally:
            _current_run.reset(token)

    await asyncio.gather(
        worker("A", "user_A"),
        worker("B", "user_B"),
    )

    assert results["A"] == "user_A"
    assert results["B"] == "user_B"


@pytest.mark.asyncio
async def test_run_context_inherits_in_nested_subagent_task():
    """子代理在同 Task 内 await 执行时，set 前可读到父级 ctx（身份继承）。"""
    seen = {}

    async def parent():
        token = _current_run.set(RunContext(user_id="parent_uid"))
        try:
            inherited_before_child = current_run().user_id
            await child()
            seen["after_child"] = current_run().user_id  # 子代理 reset 后应恢复父级
            return inherited_before_child
        finally:
            _current_run.reset(token)

    async def child():
        # set 前读到父级身份（继承）
        inherited = current_run().user_id
        token = _current_run.set(RunContext(user_id="child_uid"))
        try:
            await asyncio.sleep(0)
            seen["inside_child"] = current_run().user_id
            seen["child_inherited"] = inherited
        finally:
            _current_run.reset(token)

    parent_inherited = await parent()
    assert parent_inherited == "parent_uid"
    assert seen["child_inherited"] == "parent_uid"   # 子代理继承了父级身份
    assert seen["inside_child"] == "child_uid"       # 子代理内部是自己的身份
    assert seen["after_child"] == "parent_uid"       # reset 后父级身份恢复，无泄漏


def test_current_run_returns_empty_outside_run():
    """run() 之外读取返回空上下文，不报错。"""
    rc = current_run()
    assert rc.user_id == ""
    assert rc.session is None


# ═══════════════════════════════════════════════════════════
#  Prompt 局部化：system_prompt / prompt_builder 不再是实例级共享状态
# ═══════════════════════════════════════════════════════════

def _make_bare_agent():
    """构造一个最小可用的 Agent（跳过 initialize 的重依赖）"""
    from agent import Agent

    agent = Agent(workspace=".", client=MagicMock())
    agent.system_prompt_raw = "BASE_PROMPT"
    agent.skill_manager = None
    agent.subagent_manager = None
    agent.memory = None
    # 避免真实 git 命令拖慢测试
    agent._get_env_context = lambda task="": "ENV_CTX"
    return agent


def test_build_prompt_writes_run_context_not_instance():
    """run 内调用 _build_prompt 应写入本次 ctx，不污染实例属性（并发隔离）。"""
    agent = _make_bare_agent()

    ctx = RunContext(user_id="u1")
    token = _current_run.set(ctx)
    try:
        agent._build_prompt("task")
    finally:
        _current_run.reset(token)

    assert ctx.system_prompt  # 写入了 ctx
    assert "BASE_PROMPT" in ctx.system_prompt
    assert ctx.prompt_builder is not None
    # 关键：实例属性未被 run 内的构建覆盖
    assert agent.system_prompt == ""
    assert agent._prompt_builder is None


def test_build_prompt_isolated_between_two_runs():
    """两次独立 run 的 prompt builder / system_prompt 互不覆盖。"""
    agent = _make_bare_agent()

    ctx_a = RunContext(user_id="A")
    token = _current_run.set(ctx_a)
    try:
        agent._build_prompt("a")
    finally:
        _current_run.reset(token)

    ctx_b = RunContext(user_id="B")
    token = _current_run.set(ctx_b)
    try:
        agent._build_prompt("b")
    finally:
        _current_run.reset(token)

    # 两个 run 持有各自的 builder 与 prompt 快照
    assert ctx_a.prompt_builder is not ctx_b.prompt_builder
    assert ctx_a.system_prompt == ctx_a.prompt_builder.build_full()
    assert ctx_b.system_prompt == ctx_b.prompt_builder.build_full()
    # A 的 prompt 不会被 B 覆盖
    assert "BASE_PROMPT" in ctx_a.system_prompt


def test_permission_hint_does_not_accumulate():
    """权限提示改用 PromptBuilder 动态区块后，多次构建不累积重复文本。"""
    from prompt import PromptBuilder

    text = "当前用户权限有限，部分工具和子代理可能无法使用。"
    builder = PromptBuilder()
    builder.add("角色定义", "BASE", is_static=True, priority=0)

    # 模拟多次 run / 多轮都重新 add 同名“权限提示”区块
    for _ in range(5):
        builder.add("权限提示", text, is_static=False, priority=70)

    full = builder.build_full()
    # 仅出现一次（同名区块被替换，而非 += 累积）
    assert full.count(text) == 1


# ═══════════════════════════════════════════════════════════
#  Hook run_id 作用域过滤（消除流式 cross-talk）
# ═══════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_hook_run_id_filtering_prevents_cross_talk():
    """两个流式请求各自注册带 run_id 的回调，只收到属于自己 run 的事件。"""
    manager = HookManager()
    stream_a, stream_b = [], []

    async def handler_a(ctx):
        stream_a.append(ctx.token)

    async def handler_b(ctx):
        stream_b.append(ctx.token)

    rid_a, rid_b = "run_A", "run_B"
    manager.register(HookEvent.CHAT_EVENT, handler_a, run_id=rid_a)
    manager.register(HookEvent.CHAT_EVENT, handler_b, run_id=rid_b)

    # 模拟 run A 触发 token
    token = set_run_id(rid_a)
    try:
        await manager.fire(HookEvent.CHAT_EVENT, token="hello-A")
    finally:
        reset_run_id(token)

    # 模拟 run B 触发 token
    token = set_run_id(rid_b)
    try:
        await manager.fire(HookEvent.CHAT_EVENT, token="hello-B")
    finally:
        reset_run_id(token)

    assert stream_a == ["hello-A"]   # A 只收到自己的
    assert stream_b == ["hello-B"]   # B 只收到自己的（无串流）


@pytest.mark.asyncio
async def test_global_hook_always_fires_regardless_of_run_id():
    """run_id 为 None 的全局回调（如插件）始终触发，不受作用域过滤影响。"""
    manager = HookManager()
    fired = []

    async def global_handler(ctx):
        fired.append(ctx.token)

    manager.register(HookEvent.TOOL_START, global_handler)  # 无 run_id = 全局

    token = set_run_id("some_run")
    try:
        await manager.fire(HookEvent.TOOL_START, tool_name="t", token="x")
    finally:
        reset_run_id(token)

    assert fired == ["x"]


@pytest.mark.asyncio
async def test_existing_hook_callers_still_work_without_run_id():
    """未设置 run 作用域时（如原有非流式调用），注册的回调正常触发（向后兼容）。"""
    manager = HookManager()
    events = []

    async def hook(ctx):
        events.append(ctx.tool_name)

    manager.register(HookEvent.PRE_TOOL_USE, hook)
    await manager.fire(HookEvent.PRE_TOOL_USE, tool_name="my_tool")

    assert events == ["my_tool"]


# ═══════════════════════════════════════════════════════════
#  存储并发写入：busy_timeout + 写锁，不抛 database is locked
# ═══════════════════════════════════════════════════════════

def test_storage_concurrent_writes_do_not_lock(tmp_path):
    """多线程并发同步写入不应抛 database is locked（busy_timeout + 写锁兜底）。"""
    import threading

    from storage import Storage

    db = tmp_path / "ws"
    db.mkdir()
    storage = Storage(str(db), config_dir=str(tmp_path))

    errors = []

    def writer(thread_id: int):
        try:
            for i in range(20):
                storage.save_message_sync(
                    agent_id="a", session_id=f"s{thread_id}",
                    role="user", content=f"msg-{thread_id}-{i}",
                )
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    storage.close()

    assert not errors, f"并发写入出现错误: {errors}"

    # 校验数据完整：8 线程 × 20 条 = 160 条
    storage2 = Storage(str(db), config_dir=str(tmp_path))
    with storage2.get_connection() as conn:
        count = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    storage2.close()
    assert count == 160, f"期望 160 条消息，实际 {count}（可能静默丢失）"


def test_storage_batch_flush_requeues_on_failure(tmp_path, monkeypatch):
    """_flush_batch 失败时应重新入队而非静默丢弃。"""
    from storage import Storage

    storage = Storage(str(tmp_path), config_dir=str(tmp_path))

    call_count = {"n": 0}

    real_flush = storage._flush_batch

    def failing_flush(batch):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated transient failure")
        real_flush(batch)

    monkeypatch.setattr(storage, "_flush_batch", failing_flush)
    monkeypatch.setattr(storage, "_safe_flush", lambda b: storage.__class__._safe_flush(storage, b))

    # 通过队列投递后触发一次 safe_flush（首次失败会重试入队）
    storage._write_queue.put({
        "agent_id": "a", "session_id": "s", "role": "user",
        "content": "x", "tool_calls": None, "tool_call_id": "",
        "name": "", "reasoning_content": "", "created_at": "2024-01-01",
    })

    # 手动驱动一次 flush（首次失败 → 重新入队）
    batch = [storage._write_queue.get_nowait()]
    storage._safe_flush(batch)  # 内部第一次失败，重试入队

    # 队列里应仍有这条消息（被重新入队）
    assert not storage._write_queue.empty(), "失败批次应被重新入队而非丢弃"
    storage.close()
