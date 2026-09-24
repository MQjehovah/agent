"""P6 worker 平台轨按用户身份(act-as): uid → market 用户名解析 + worker 透传。

- `web.security.market_act_as_enabled`: MARKET_ACT_AS=1/true/yes 开, 缺省/其它关;
- `web.security.resolve_market_act_as`: 数字 uid(rbac_users.id)→name(工号);
  非数字(已是工号/SSO sub)→原样; 查不到/存储不可用→空串 + WARNING(回退服务视角);
- `WebUserWorkerPool._create_worker`: 开关 + 平台轨齐备时给 worker 设 platform_act_as;
  开关关闭/平台轨关闭/无 worker 池(root 保持服务令牌全量)时为空。
"""

import asyncio
import logging
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest  # noqa: E402

import storage.storage as storage_mod  # noqa: E402
from security.rbac import RBACManager  # noqa: E402
from storage.storage import Storage  # noqa: E402
from web.security import market_act_as_enabled, resolve_market_act_as  # noqa: E402
from web.worker_pool import WebUserWorkerPool  # noqa: E402

WORKID = "202202100024"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """临时 Storage 并替换全局单例(get_storage 读它)。"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    prev = storage_mod._storage_instance
    s = Storage(str(ws))
    storage_mod._storage_instance = s
    yield s
    s.close()
    storage_mod._storage_instance = prev


def _make_user(store, name=WORKID, department="研发部") -> int:
    return RBACManager(store).create_user(name=name, department=department)


class _DummyRoot:
    def __init__(self, workspace):
        self.workspace = str(workspace)
        self.config_dir = ""
        self.client = None
        self.name = "root"
        self.plugin_manager = None
        self._permission_config = None


class _FakeWorker:
    """替身 Agent: 只关心 _create_worker 的属性写入与 initialize 调用。"""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.platform_act_as = None
        self.initialized = False

    async def initialize(self):
        self.initialized = True


@pytest.fixture
def fake_agent(monkeypatch):
    import agent.core as agent_core

    monkeypatch.setattr(agent_core, "Agent", _FakeWorker)
    return _FakeWorker


def _enable_market(monkeypatch, act_as="1"):
    """打开 MARKET_ACT_AS 与平台轨 env(act_as=None 表示不设开关)。"""
    if act_as is None:
        monkeypatch.delenv("MARKET_ACT_AS", raising=False)
    else:
        monkeypatch.setenv("MARKET_ACT_AS", act_as)
    monkeypatch.setenv("MARKET_BASE_URL", "http://market.test")
    monkeypatch.setenv("MARKET_SERVICE_TOKEN", "tok")


# ---------------- 开关解析 ----------------

@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", " yes "])
def test_market_act_as_enabled_truthy(value):
    assert market_act_as_enabled({"MARKET_ACT_AS": value}) is True


@pytest.mark.parametrize("value", ["", "0", "no", "off", "2", "  "])
def test_market_act_as_enabled_falsy(value):
    assert market_act_as_enabled({"MARKET_ACT_AS": value}) is False


def test_market_act_as_enabled_missing_is_off():
    assert market_act_as_enabled({}) is False


# ---------------- uid → market 用户名 ----------------

def test_resolve_act_as_numeric_uid_uses_rbac_name(store):
    """数字 uid(rbac_users.id) → 查表取 name(= 工号/SSO sub)。"""
    uid = _make_user(store)
    assert resolve_market_act_as(f"web:{uid}") == WORKID
    assert resolve_market_act_as(str(uid)) == WORKID  # 兼容裸 uid


def test_resolve_act_as_non_numeric_is_workid_passthrough(store):
    """非数字(SSO sub/工号兜底形态) → 原样使用, 不查库。"""
    assert resolve_market_act_as("web:s_software") == "s_software"
    assert resolve_market_act_as(" s_software ") == "s_software"


def test_resolve_act_as_unknown_uid_warns_and_returns_empty(store, caplog):
    """数字 uid 查不到 → 空串 + WARNING(回退服务令牌视角须日志可见)。"""
    with caplog.at_level(logging.WARNING, logger="agent.web.security"):
        assert resolve_market_act_as("web:999999") == ""
    assert any("999999" in r.getMessage() for r in caplog.records
               if r.levelno == logging.WARNING)


def test_resolve_act_as_empty_input_returns_empty(store):
    assert resolve_market_act_as("") == ""
    assert resolve_market_act_as("web:") == ""


# ---------------- worker 透传(每 worker 恒定 act-as) ----------------

def _pool(tmp_path) -> WebUserWorkerPool:
    return WebUserWorkerPool(root_agent=_DummyRoot(tmp_path), max_workers=1)


def test_create_worker_sets_act_as_when_switch_and_platform_on(
        store, monkeypatch, tmp_path, fake_agent):
    _enable_market(monkeypatch)
    uid = _make_user(store)
    pool = _pool(tmp_path)

    worker = asyncio.run(pool._create_worker(f"web:{uid}", str(tmp_path / "ws")))

    assert worker.platform_act_as == WORKID
    assert worker.platform_mcp_enabled is True
    assert worker.initialized is True  # act-as 在 initialize(建平台连接)前写入


def test_create_worker_act_as_empty_when_switch_off(store, monkeypatch, tmp_path, fake_agent):
    """MARKET_ACT_AS 未开(默认): 不解析, worker 保持服务令牌全量视角。"""
    _enable_market(monkeypatch, act_as=None)
    uid = _make_user(store)
    worker = asyncio.run(_pool(tmp_path)._create_worker(f"web:{uid}", str(tmp_path / "ws")))
    assert worker.platform_act_as == ""


def test_create_worker_act_as_empty_when_platform_disabled(store, monkeypatch, tmp_path, fake_agent):
    """开关开了但平台轨未配置: 无平台连接可言, 不解析(空)。"""
    _enable_market(monkeypatch)
    monkeypatch.delenv("MARKET_BASE_URL", raising=False)
    monkeypatch.delenv("MARKET_SERVICE_TOKEN", raising=False)
    uid = _make_user(store)
    worker = asyncio.run(_pool(tmp_path)._create_worker(f"web:{uid}", str(tmp_path / "ws")))
    assert worker.platform_act_as == ""


def test_create_worker_act_as_falls_back_when_user_missing(
        store, monkeypatch, tmp_path, fake_agent, caplog):
    """数字 uid 无对应用户 → 空串回退服务视角 + WARNING。"""
    _enable_market(monkeypatch)
    pool = _pool(tmp_path)
    with caplog.at_level(logging.WARNING, logger="agent.web.security"):
        worker = asyncio.run(pool._create_worker("web:999999", str(tmp_path / "ws")))
    assert worker.platform_act_as == ""
    assert any("999999" in r.getMessage() and r.levelno == logging.WARNING
               for r in caplog.records)


# ---------------- 无 worker 池: root 保持服务令牌全量 ----------------

def test_set_agent_warns_when_act_as_enabled_without_pool(monkeypatch, caplog):
    _enable_market(monkeypatch)
    monkeypatch.delenv("AGENT_WEB_POOL_SIZE", raising=False)

    from web.server import WebServer  # noqa: PLC0415

    with caplog.at_level(logging.WARNING, logger="agent.web.server"):
        w = WebServer()
        w.set_agent(SimpleNamespace(workspace=".", client=None))
    assert w._pool is None
    assert any("worker 池" in r.getMessage() and "AGENT_WEB_POOL_SIZE" in r.getMessage()
               for r in caplog.records if r.levelno == logging.WARNING)
