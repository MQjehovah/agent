"""Web worker 平台轨 provider 注入: 平台轨启用即按用户 token, 关闭则为 None。

- `WebUserWorkerPool._resolve_platform_token_provider`: 平台轨启用(MARKET_BASE_URL +
  MARKET_SERVICE_TOKEN 齐备)时恒定注入该用户 provider(lambda: get_downstream_token(
  uid, "gateway")); 平台轨关闭 → None(worker 保持服务令牌视角);
- `_create_worker`: provider 在 initialize(建平台连接)之前写入 worker;
  uid 不可解析或该用户无托管 token 时 provider 返回空串, 由平台轨 fail-closed
  置空平台能力(不回退服务令牌);
- 归属身份 owner_tag/owner_uid 注入回归。
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest  # noqa: E402

from web.worker_pool import WebUserWorkerPool  # noqa: E402


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
        self.platform_token_provider = None
        self.provider_at_init = None
        self.owner_tag = None
        self.owner_uid = None
        self.owner_at_init = None
        self.initialized = False

    async def initialize(self):
        # 捕获初始化时刻的身份: 证明写入发生在 initialize(建平台连接)之前
        self.provider_at_init = self.platform_token_provider
        self.owner_at_init = (self.owner_tag, self.owner_uid)
        self.initialized = True


@pytest.fixture
def fake_agent(monkeypatch):
    import agent.core as agent_core

    monkeypatch.setattr(agent_core, "Agent", _FakeWorker)
    return _FakeWorker


def _enable_market(monkeypatch):
    """平台轨 env 齐备(MARKET_BASE_URL + MARKET_SERVICE_TOKEN)。"""
    monkeypatch.setenv("MARKET_BASE_URL", "http://market.test")
    monkeypatch.setenv("MARKET_SERVICE_TOKEN", "tok")


def _pool(tmp_path) -> WebUserWorkerPool:
    return WebUserWorkerPool(root_agent=_DummyRoot(tmp_path), max_workers=1)


def _create(tmp_path, tag: str):
    return asyncio.run(_pool(tmp_path)._create_worker(tag, str(tmp_path / "ws")))


# ---------------- provider 注入(平台轨启用即按用户 token) ----------------

def test_create_worker_sets_user_token_provider_when_platform_on(
        monkeypatch, tmp_path, fake_agent):
    _enable_market(monkeypatch)
    uid = 7
    calls = []
    from web import sso_tokens

    def _fake_token(user_uid, audience=""):
        calls.append((user_uid, audience))
        return f"utok-{user_uid}"

    monkeypatch.setattr(sso_tokens, "get_downstream_token", _fake_token)

    worker = _create(tmp_path, f"web:{uid}")

    assert callable(worker.platform_token_provider)
    assert worker.platform_mcp_enabled is True
    assert worker.initialized is True
    assert worker.provider_at_init is worker.platform_token_provider  # 时序: initialize 前注入
    assert worker.platform_token_provider() == f"utok-{uid}"
    assert calls == [(uid, "gateway")]  # audience 恒为 gateway


def test_create_worker_provider_none_when_platform_disabled(
        monkeypatch, tmp_path, fake_agent):
    """平台轨未配置: 无平台连接可言, 不注入 provider(worker 保持服务令牌视角)。"""
    monkeypatch.delenv("MARKET_BASE_URL", raising=False)
    monkeypatch.delenv("MARKET_SERVICE_TOKEN", raising=False)
    worker = _create(tmp_path, "web:7")
    assert worker.platform_token_provider is None
    assert worker.provider_at_init is None


def test_create_worker_provider_returns_empty_when_token_missing(
        monkeypatch, tmp_path, fake_agent):
    """平台轨开但该用户无托管 token: provider 注入且返回空串(由平台轨 fail-closed 置空)。"""
    _enable_market(monkeypatch)
    from web import sso_tokens
    monkeypatch.setattr(sso_tokens, "get_downstream_token",
                        lambda user_uid, audience="": "")
    worker = _create(tmp_path, "web:999999")
    assert callable(worker.platform_token_provider)
    assert worker.platform_token_provider() == ""


def test_create_worker_provider_returns_empty_when_owner_uid_empty(
        monkeypatch, tmp_path, fake_agent):
    """owner_uid 不可解析(非数字 tag → 0): provider 仍注入, 真实现返回空串(fail-closed)。"""
    _enable_market(monkeypatch)
    worker = _create(tmp_path, "web:s_software")
    assert worker.owner_uid == 0
    assert callable(worker.platform_token_provider)
    assert worker.platform_token_provider() == ""  # get_downstream_token(uid<=0) 恒空串


# ---------------- worker 归属身份(用户级云托管安装过滤) ----------------

def test_create_worker_injects_owner_identity(monkeypatch, tmp_path, fake_agent):
    _enable_market(monkeypatch)
    uid = 7
    worker = _create(tmp_path, f"web:{uid}")
    assert worker.owner_tag == f"web:{uid}"
    assert worker.owner_uid == uid
    assert worker.owner_at_init == (f"web:{uid}", uid)  # 时序: initialize 前注入


def test_create_worker_non_numeric_uid_owner_uid_zero(monkeypatch, tmp_path, fake_agent):
    """非数字 uid(SSO sub 形态): owner_uid=0(无用户级安装可查)。"""
    _enable_market(monkeypatch)
    worker = _create(tmp_path, "web:s_software")
    assert worker.owner_tag == "web:s_software"
    assert worker.owner_uid == 0
