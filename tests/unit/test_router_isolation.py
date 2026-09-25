"""MessageRouter 非 web 单聊按用户隔离(方案A)测试。

覆盖:
- 钉钉/飞书单聊 -> 取 uid worker 执行(工作区隔离), 并释放;
- 群共享根(group_context) -> 不隔离, 走 root;
- 运行期把 root 的 on_confirm/权限模式同步到 worker, 结束后恢复;
- 池容量饱和/分配失败 -> 回退 root(不阻断消息)。
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from channels.router import MessageRouter  # noqa: E402


class _FakeAgent:
    def __init__(self):
        self.on_confirm = None
        self._permission_config = SimpleNamespace(mode="AUTO")
        self.client = SimpleNamespace(model="m")
        self.calls: list[dict] = []

    async def run(self, content, **kwargs):
        self.calls.append({"content": content, **kwargs})
        return SimpleNamespace(result="ok", sensitive_hit=False)


class _FakePool:
    enabled = True

    def __init__(self, worker):
        self.worker = worker
        self.acquired: list[tuple] = []
        self.released: list[str] = []

    async def acquire(self, tag, uid, name=""):
        self.acquired.append((tag, uid))
        return self.worker

    def release(self, tag):
        self.released.append(tag)


def test_isolation_uid_rules():
    assert MessageRouter._isolation_uid("dingtalk", "dingtalk:7", False) == "7"
    assert MessageRouter._isolation_uid("feishu", "feishu:7", False) == "7"
    # 群共享根不隔离
    assert MessageRouter._isolation_uid("dingtalk", "dingtalk:7", True) == ""
    # 非目标渠道/非数字/服务身份不隔离
    assert MessageRouter._isolation_uid("web", "web:7", False) == ""
    assert MessageRouter._isolation_uid("dingtalk", "dingtalk:admin", False) == ""
    assert MessageRouter._isolation_uid("dingtalk", "dingtalk:0", False) == ""


async def test_route_isolates_dingtalk_single_chat():
    root, worker = _FakeAgent(), _FakeAgent()
    pool = _FakePool(worker)
    r = MessageRouter(root, worker_pool=pool)
    out = await r.route("hi", channel="dingtalk", session_id="dingtalk:7:a",
                        user_id="dingtalk:7", user_name="张三")
    assert out == "ok"
    assert pool.acquired == [("dingtalk:7", "7")]
    assert worker.calls and not root.calls
    assert pool.released == ["dingtalk:7"]


async def test_route_group_uses_root_not_isolated():
    root, worker = _FakeAgent(), _FakeAgent()
    pool = _FakePool(worker)
    r = MessageRouter(root, worker_pool=pool)
    await r.route("hi", channel="dingtalk", session_id="dingtalk_group:x:y:z",
                  user_id="dingtalk:7", group_context=True)
    assert pool.acquired == []
    assert root.calls and not worker.calls


async def test_route_syncs_run_scope_to_worker_and_restores():
    root, worker = _FakeAgent(), _FakeAgent()
    sentinel = object()
    root.on_confirm = sentinel
    root._permission_config.mode = "DEFAULT"
    worker.on_confirm = None
    worker._permission_config.mode = "AUTO"

    async def _run(content, **kwargs):
        assert worker.on_confirm is sentinel          # 运行期已同步
        assert worker._permission_config.mode == "DEFAULT"
        return SimpleNamespace(result="ok")

    worker.run = _run
    pool = _FakePool(worker)
    r = MessageRouter(root, worker_pool=pool)
    await r.route("hi", channel="dingtalk", session_id="dingtalk:7:a", user_id="dingtalk:7")

    # 结束后恢复 worker 原值
    assert worker.on_confirm is None
    assert worker._permission_config.mode == "AUTO"


async def test_route_falls_back_to_root_when_pool_busy():
    class _BusyPool(_FakePool):
        async def acquire(self, tag, uid, name=""):
            raise RuntimeError("system busy")

    root, worker = _FakeAgent(), _FakeAgent()
    pool = _BusyPool(worker)
    r = MessageRouter(root, worker_pool=pool)
    out = await r.route("hi", channel="dingtalk", session_id="dingtalk:7:a",
                        user_id="dingtalk:7")
    assert out == "ok"
    assert root.calls and not worker.calls


async def test_route_without_pool_uses_root():
    root = _FakeAgent()
    r = MessageRouter(root)
    await r.route("hi", channel="dingtalk", session_id="dingtalk:7:a", user_id="dingtalk:7")
    assert root.calls
