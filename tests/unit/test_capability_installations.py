"""P3 用户级「云端托管安装」: 存储 CRUD + Agent 平台轨过滤装配/刷新。

- Storage: upsert 幂等(保留 enabled)/启停/移除/列表/enabled_only/空名过滤;
- Agent._platform_install_filter: root/非 worker 为 None, worker 读已安装且启用集合,
  存储异常 fail-closed 空集;
- Agent._load_mcp_servers: worker 构造 PlatformMCPClient 时传入过滤闭包, root 不传;
- Agent.refresh_platform_installations: 调 platform.refresh_once(异常安全)。
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest  # noqa: E402

import storage.storage as storage_mod  # noqa: E402
from agent.core import Agent  # noqa: E402
from storage.storage import Storage  # noqa: E402

UID = 7
WORKID = "202202100024"


@pytest.fixture
def store(tmp_path):
    """临时 Storage 并替换全局单例(get_storage 读它)。"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    prev = storage_mod._storage_instance
    s = Storage(str(ws))
    storage_mod._storage_instance = s
    yield s
    s.close()
    storage_mod._storage_instance = prev


def _agent(tmp_path) -> Agent:
    return Agent(workspace=str(tmp_path / "ws_agent"), client=MagicMock())


# ===== 1. 存储 CRUD =====

def test_upsert_insert_and_idempotent_update_preserves_enabled(store):
    store.upsert_installation(UID, "cap-1", "天气", "mcp")
    rows = store.list_installations(UID)
    assert len(rows) == 1
    assert rows[0]["capability_id"] == "cap-1"
    assert rows[0]["capability_name"] == "天气"
    assert rows[0]["kind"] == "mcp"
    assert rows[0]["enabled"] is True
    assert rows[0]["installed_at"]

    store.set_installation_enabled(UID, "cap-1", False)
    store.upsert_installation(UID, "cap-1", "天气2", "plugin")
    rows = store.list_installations(UID)
    assert len(rows) == 1                                   # 幂等: 不新增行
    assert rows[0]["capability_name"] == "天气2"
    assert rows[0]["kind"] == "plugin"
    assert rows[0]["enabled"] is False                      # 保留启停状态
    assert rows[0]["updated_at"] >= rows[0]["installed_at"]


def test_set_installation_enabled_returns_false_when_missing(store):
    assert store.set_installation_enabled(UID, "nope", False) is False
    store.upsert_installation(UID, "cap-1", "天气", "mcp")
    assert store.set_installation_enabled(UID, "cap-1", False) is True
    assert store.list_installations(UID)[0]["enabled"] is False
    assert store.set_installation_enabled(UID, "cap-1", True) is True


def test_remove_installation(store):
    assert store.remove_installation(UID, "nope") is False
    store.upsert_installation(UID, "cap-1", "天气", "mcp")
    assert store.remove_installation(UID, "cap-1") is True
    assert store.list_installations(UID) == []


def test_list_installations_enabled_only_and_user_isolation(store):
    store.upsert_installation(UID, "cap-1", "天气", "mcp")
    store.upsert_installation(UID, "cap-2", "地图", "mcp")
    store.set_installation_enabled(UID, "cap-2", False)
    store.upsert_installation(8, "cap-3", "翻译", "mcp")

    assert [r["capability_id"] for r in store.list_installations(UID)] == ["cap-1", "cap-2"]
    assert [r["capability_id"] for r in store.list_installations(UID, enabled_only=True)] == ["cap-1"]
    assert [r["capability_id"] for r in store.list_installations(8)] == ["cap-3"]


def test_installed_capability_names_filters_empty_and_disabled(store):
    store.upsert_installation(UID, "cap-1", "天气", "mcp")
    store.upsert_installation(UID, "cap-2", "地图", "mcp")
    store.set_installation_enabled(UID, "cap-2", False)
    store.upsert_installation(UID, "cap-3", "", "mcp")       # 空名

    assert store.installed_capability_names(UID) == {"天气"}
    assert store.installed_capability_names(UID, enabled_only=False) == {"天气", "地图"}


# ===== 2. Agent 过滤闭包 =====

def test_platform_install_filter_root_vs_worker(tmp_path):
    """过滤仅由 owner_uid 决定(与 MARKET_ACT_AS/platform_act_as 解耦)。"""
    agent = _agent(tmp_path)
    assert agent._platform_install_filter() is None          # root 默认
    agent.owner_uid = UID
    assert agent._platform_install_filter() is not None      # act_as 未开仍注入(解耦)
    agent.owner_uid = 0
    agent.platform_act_as = WORKID
    assert agent._platform_install_filter() is None          # root 仍不过滤


def test_platform_install_filter_reads_enabled_installations(store, tmp_path):
    store.upsert_installation(UID, "cap-1", "天气", "mcp")
    store.upsert_installation(UID, "cap-2", "地图", "mcp")
    store.set_installation_enabled(UID, "cap-2", False)

    agent = _agent(tmp_path)
    agent.owner_tag = f"web:{UID}"
    agent.owner_uid = UID                                    # 不设 platform_act_as
    loader = agent._platform_install_filter()
    assert callable(loader)
    assert loader() == {"天气"}                              # 停用/空名不出


def test_platform_install_filter_storage_error_fail_closed(monkeypatch, tmp_path):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(storage_mod, "get_storage", _boom)
    agent = _agent(tmp_path)
    agent.platform_act_as = WORKID
    agent.owner_uid = UID
    assert agent._platform_install_filter()() == set()


# ===== 3. 平台客户端装配(root/worker 差异) =====

def _fake_platform_client(monkeypatch):
    import mcps.platform as platform_mod

    captured = {}

    class _FakePlatformClient:
        def __init__(self, config, *, act_as="", install_filter=None):
            captured["act_as"] = act_as
            captured["install_filter"] = install_filter
            self.tool_defs = []

        def start(self):
            pass

    monkeypatch.setattr(platform_mod, "PlatformMCPClient", _FakePlatformClient)
    monkeypatch.setenv("MARKET_BASE_URL", "http://market.test")
    monkeypatch.setenv("MARKET_SERVICE_TOKEN", "tok")
    return captured


async def test_worker_platform_client_gets_install_filter(store, monkeypatch, tmp_path):
    captured = _fake_platform_client(monkeypatch)
    store.upsert_installation(UID, "cap-1", "天气", "mcp")
    store.upsert_installation(UID, "cap-2", "地图", "mcp")
    store.set_installation_enabled(UID, "cap-2", False)

    agent = _agent(tmp_path)
    monkeypatch.setattr(agent, "_read_mcp_config_file", lambda: [])
    agent.platform_act_as = WORKID
    agent.owner_tag = f"web:{UID}"
    agent.owner_uid = UID
    await agent._load_mcp_servers()
    agent.mcp.stop_health_check()

    assert captured["act_as"] == WORKID
    assert captured["install_filter"] is not None
    assert captured["install_filter"]() == {"天气"}


async def test_root_platform_client_has_no_install_filter(monkeypatch, tmp_path):
    captured = _fake_platform_client(monkeypatch)
    agent = _agent(tmp_path)
    monkeypatch.setattr(agent, "_read_mcp_config_file", lambda: [])
    await agent._load_mcp_servers()
    agent.mcp.stop_health_check()

    assert captured["install_filter"] is None                # root 服务全量视角


# ===== 4. 安装/启停后立即刷新 =====

async def test_refresh_platform_installations_calls_refresh_once(tmp_path, caplog):
    agent = _agent(tmp_path)
    calls = []

    class _Platform:
        async def refresh_once(self):
            calls.append(1)
            return True

    agent.mcp = SimpleNamespace(platform=_Platform())
    assert await agent.refresh_platform_installations() is True
    assert calls == [1]


async def test_refresh_platform_installations_without_platform(tmp_path):
    agent = _agent(tmp_path)
    assert await agent.refresh_platform_installations() is False


async def test_refresh_platform_installations_error_is_safe(tmp_path, caplog):
    import logging

    agent = _agent(tmp_path)

    class _Platform:
        async def refresh_once(self):
            raise RuntimeError("market down")

    agent.mcp = SimpleNamespace(platform=_Platform())
    with caplog.at_level(logging.WARNING, logger="agent.agent"):
        assert await agent.refresh_platform_installations() is False
    assert any("平台安装刷新失败" in r.getMessage() for r in caplog.records)
