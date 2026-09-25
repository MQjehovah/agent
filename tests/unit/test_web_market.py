"""P4 能力市场代理 + 用户级云端托管安装 + 管理端本地 MCP 配置测试。

- 浏览/详情: 代理市场并 merge joined(act-as 已加入)/installed(本地表);
- join/leave 透传(含市场 403 文案); 市场未配置 503;
- install 校验(type=mcp / 非 local / 已加入)与幂等, 写表后刷新 worker;
- 启停/卸载 表变化 + 刷新; 不存在 404;
- 管理端 local-mcp: admin.mcp_local 权限、白名单字段写入、reload_all 调用。
"""
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import storage.storage as storage_mod  # noqa: E402
from security.rbac import RBACManager  # noqa: E402
from storage.storage import Storage  # noqa: E402
from web import security  # noqa: E402
from web.routers import market as market_mod  # noqa: E402

MARKET_WORKID = "202202100024"


class _FakeManager:
    def __init__(self):
        self.reloads = 0

    async def reload_all(self):
        self.reloads += 1
        return {"default": True}


class _Server:
    def __init__(self, config_dir):
        self.agent = SimpleNamespace(config_dir=str(config_dir), mcp=_FakeManager())
        self.refreshed: list[int] = []

    async def refresh_user_platform(self, uid):
        self.refreshed.append(uid)
        return True


@pytest.fixture
def env(tmp_path, monkeypatch):
    """最小 app(仅 market router) + 临时 storage + 市场请求打桩 + admin 身份。"""
    monkeypatch.delenv("AGENT_SERVICE_TOKEN", raising=False)
    prev = storage_mod._storage_instance
    store = Storage(str(tmp_path / "ws"))
    storage_mod._storage_instance = store
    uid = RBACManager(store).create_user(name=MARKET_WORKID, department="研发部")

    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    server = _Server(cfg_dir)
    app = FastAPI()
    app.include_router(market_mod.build_market_router(server))
    client = TestClient(app)

    market = {"calls": [], "routes": {}, "real": market_mod._market_request}

    async def fake_market(method, path, *, act_as="", params=None, json_body=None):
        market["calls"].append({"method": method, "path": path, "act_as": act_as,
                                "params": params, "json": json_body})
        resp = market["routes"].get((method, path))
        if resp is None:
            return 404, {"error": f"unhandled {method} {path}"}
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr(market_mod, "_market_request", fake_market)
    monkeypatch.setattr(security, "get_authz", lambda request: {
        "uid": uid, "role": "admin", "permissions": ["*"],
        "data_scope": "all", "department": ""})
    yield server, store, client, market, uid
    store.close()
    storage_mod._storage_instance = prev


# ===== 1. 浏览 / 详情 / join / leave =====

def test_browse_merges_joined_and_installed(env):
    server, store, client, market, uid = env
    market["routes"][("GET", "/api/capabilities")] = (200, {"items": [
        {"id": "c1", "name": "天气", "type": "mcp"},
        {"id": "c2", "name": "地图", "type": "mcp"}], "total": 2})
    market["routes"][("GET", "/api/my/capabilities")] = (200, [{"name": "天气"}])
    store.upsert_installation(uid, "c2", "地图", "mcp")

    r = client.get("/api/market/capabilities",
                   params={"q": "x", "type": "mcp", "category": "工具"})
    assert r.status_code == 200
    data = r.json()
    assert data["items"][0]["joined"] is True and data["items"][0]["installed"] is False
    assert data["items"][1]["joined"] is False and data["items"][1]["installed"] is True
    assert data["total"] == 2 and data["page"] == 1

    first, second = market["calls"]
    assert first["path"] == "/api/capabilities"
    assert first["params"]["q"] == "x" and first["params"]["type"] == "mcp"
    assert first["act_as"] == MARKET_WORKID            # 浏览也带 act-as
    assert second["path"] == "/api/my/capabilities"
    assert second["params"] == {"scope": "added"}
    assert second["act_as"] == MARKET_WORKID           # 已加入清单用 act-as


def test_detail_merges_statuses_without_extra_components_call(env):
    server, store, client, market, uid = env
    market["routes"][("GET", "/api/capabilities/c1")] = (200, {
        "id": "c1", "name": "天气", "type": "mcp", "components": [{"name": "comp"}]})
    market["routes"][("GET", "/api/my/capabilities")] = (200, [{"name": "天气"}])
    store.upsert_installation(uid, "c1", "天气", "mcp")

    r = client.get("/api/market/capabilities/c1")
    assert r.status_code == 200
    data = r.json()
    assert data["joined"] is True and data["installed"] is True
    assert data["components"] == [{"name": "comp"}]    # 详情自带, 不再辅助调用
    assert [c["path"] for c in market["calls"]] == ["/api/capabilities/c1",
                                                    "/api/my/capabilities"]
    assert all(c["act_as"] == MARKET_WORKID for c in market["calls"])


def test_browse_passes_through_runtime(env):
    """runtime 契约原样透传(顶层与条目内均不解析)。"""
    server, store, client, market, uid = env
    runtime = {"cloud": True, "version": 2}
    market["routes"][("GET", "/api/capabilities")] = (200, {
        "items": [{"id": "c1", "name": "天气", "runtime": runtime}],
        "total": 1, "runtime": {"api": 3}})
    market["routes"][("GET", "/api/my/capabilities")] = (200, [])
    r = client.get("/api/market/capabilities")
    assert r.status_code == 200
    body = r.json()
    assert body["runtime"] == {"api": 3}
    assert body["items"][0]["runtime"] == runtime


def test_browse_503_when_market_not_configured(env, monkeypatch):
    server, store, client, market, uid = env
    monkeypatch.setattr(market_mod, "_market_request", market["real"])
    monkeypatch.delenv("MARKET_BASE_URL", raising=False)
    monkeypatch.delenv("MARKET_SERVICE_TOKEN", raising=False)
    r = client.get("/api/market/capabilities")
    assert r.status_code == 503
    assert r.json()["error"] == "能力市场未配置"


# ===== 1b. C1: act-as 解析失败 fail-closed(禁止静默降级服务身份) =====

def test_user_endpoints_fail_closed_when_act_as_unresolved(env, monkeypatch):
    server, store, client, market, uid = env
    monkeypatch.setattr(security, "resolve_market_act_as", lambda owner: "")
    cases = [
        ("get", "/api/market/capabilities", None),
        ("get", "/api/market/capabilities/c1", None),
        ("post", "/api/market/capabilities/c1/join", {}),
        ("post", "/api/market/capabilities/c1/leave", {}),
        ("get", "/api/market/installations", None),
        ("post", "/api/market/installations", {"capability_id": "c1"}),
        ("patch", "/api/market/installations/c1", {"enabled": False}),
        ("delete", "/api/market/installations/c1", None),
    ]
    for method, path, body in cases:
        fn = getattr(client, method)
        r = fn(path, json=body) if body is not None else fn(path)
        assert r.status_code == 403, (method, path)
        assert "市场用户身份" in r.json()["error"], (method, path)
    assert market["calls"] == []          # 未发出任何市场请求


def test_service_identity_uid0_rejected_for_user_endpoints(env, monkeypatch):
    server, store, client, market, uid = env
    monkeypatch.setattr(security, "get_authz", lambda request: {
        "uid": 0, "role": "admin", "permissions": ["*"], "data_scope": "all"})
    assert client.get("/api/market/capabilities").status_code == 403
    assert client.get("/api/market/installations").status_code == 403
    assert market["calls"] == []


def test_join_and_leave_forward_market_errors(env):
    server, store, client, market, uid = env
    market["routes"][("POST", "/api/my/capabilities")] = (
        403, {"detail": "没有加入权限（部门限制）"})
    r = client.post("/api/market/capabilities/c1/join")
    assert r.status_code == 403
    assert "没有加入权限" in r.text
    assert market["calls"][0]["act_as"] == MARKET_WORKID
    assert market["calls"][0]["json"] == {"capability_id": "c1"}

    market["routes"][("DELETE", "/api/my/capabilities/c1")] = (
        200, {"message": "已移出我的能力"})
    r = client.post("/api/market/capabilities/c1/leave")
    assert r.status_code == 200
    assert r.json()["message"] == "已移出我的能力"
    assert market["calls"][1]["method"] == "DELETE"


# ===== 2. 安装校验 / 幂等 / 刷新 =====

def test_install_rejects_non_mcp_local_and_not_joined(env):
    server, store, client, market, uid = env
    market["routes"][("GET", "/api/capabilities/c-skill")] = (
        200, {"id": "c-skill", "name": "技能", "type": "skill", "distribution": "remote"})
    r = client.post("/api/market/installations", json={"capability_id": "c-skill"})
    assert r.status_code == 422 and "mcp" in r.json()["error"]

    market["routes"][("GET", "/api/capabilities/c-local")] = (
        200, {"id": "c-local", "name": "本地", "type": "mcp", "distribution": "local"})
    r = client.post("/api/market/installations", json={"capability_id": "c-local"})
    assert r.status_code == 422 and "local" in r.json()["error"]

    market["routes"][("GET", "/api/capabilities/c-remote")] = (
        200, {"id": "c-remote", "name": "天气", "type": "mcp", "distribution": "remote"})
    market["routes"][("GET", "/api/my/capabilities")] = (200, [{"name": "别的"}])
    r = client.post("/api/market/installations", json={"capability_id": "c-remote"})
    assert r.status_code == 403 and "加入" in r.json()["error"]

    # 市场详情缺失 → 404 透传; 未安装记录/刷新均不发生
    r = client.post("/api/market/installations", json={"capability_id": "c-missing"})
    assert r.status_code == 404
    assert store.list_installations(uid) == []
    assert server.refreshed == []


def test_install_runtime_cloud_false_rejected(env):
    server, store, client, market, uid = env
    market["routes"][("GET", "/api/capabilities/c-rt")] = (200, {
        "id": "c-rt", "name": "天气", "type": "mcp", "distribution": "remote",
        "runtime": {"cloud": False}})
    r = client.post("/api/market/installations", json={"capability_id": "c-rt"})
    assert r.status_code == 422
    assert "runtime.cloud" in r.json()["error"]
    assert store.list_installations(uid) == [] and server.refreshed == []


def test_install_runtime_cloud_true_overrides_distribution(env):
    """runtime 存在时以其为准: cloud=true 时不再看 distribution。"""
    server, store, client, market, uid = env
    market["routes"][("GET", "/api/capabilities/c-rt2")] = (200, {
        "id": "c-rt2", "name": "天气", "type": "mcp", "distribution": "local",
        "runtime": {"cloud": True}})
    market["routes"][("GET", "/api/my/capabilities")] = (200, [{"name": "天气"}])
    r = client.post("/api/market/installations", json={"capability_id": "c-rt2"})
    assert r.status_code == 200 and r.json()["success"] is True


def test_install_success_idempotent_and_refresh(env):
    server, store, client, market, uid = env
    market["routes"][("GET", "/api/capabilities/c-remote")] = (
        200, {"id": "c-remote", "name": "天气", "type": "mcp", "distribution": "both"})
    market["routes"][("GET", "/api/my/capabilities")] = (200, [{"name": "天气"}])

    body = {"capability_id": "c-remote", "capability_name": "天气", "kind": "mcp"}
    r = client.post("/api/market/installations", json=body)
    assert r.status_code == 200
    assert r.json()["success"] is True and r.json()["refreshed"] is True
    rows = store.list_installations(uid)
    assert len(rows) == 1
    assert rows[0]["capability_id"] == "c-remote"
    assert rows[0]["capability_name"] == "天气" and rows[0]["kind"] == "mcp"
    assert server.refreshed == [uid]

    r = client.post("/api/market/installations", json=body)     # 幂等
    assert r.status_code == 200 and len(store.list_installations(uid)) == 1


def test_enable_disable_remove_and_404(env):
    server, store, client, market, uid = env
    store.upsert_installation(uid, "c1", "天气", "mcp")

    r = client.patch("/api/market/installations/c1", json={"enabled": False})
    assert r.status_code == 200
    assert store.list_installations(uid)[0]["enabled"] is False
    assert server.refreshed == [uid]

    r = client.patch("/api/market/installations/c1", json={"enabled": "yes"})
    assert r.status_code == 422

    r = client.patch("/api/market/installations/nope", json={"enabled": False})
    assert r.status_code == 404

    r = client.delete("/api/market/installations/c1")
    assert r.status_code == 200 and store.list_installations(uid) == []
    assert server.refreshed == [uid, uid]
    r = client.delete("/api/market/installations/c1")
    assert r.status_code == 404


def test_list_installations_is_local_only(env):
    server, store, client, market, uid = env
    store.upsert_installation(uid, "c1", "天气", "mcp")
    r = client.get("/api/market/installations")
    assert r.status_code == 200
    assert r.json()["installations"][0]["capability_id"] == "c1"
    assert market["calls"] == []                       # 不依赖市场


# ===== 3. 管理端本地 MCP 配置 =====

def test_admin_local_mcp_requires_perm(env, monkeypatch):
    server, store, client, market, uid = env
    monkeypatch.setattr(security, "get_authz", lambda request: {
        "uid": uid, "role": "default", "permissions": [], "data_scope": "self"})
    assert client.get("/api/admin/local-mcp").status_code == 403
    assert client.put("/api/admin/local-mcp/demo",
                      json={"command": "python"}).status_code == 403
    assert client.delete("/api/admin/local-mcp/demo").status_code == 403


def test_admin_local_mcp_upsert_get_delete(env):
    server, store, client, market, uid = env
    path = os.path.join(server.agent.config_dir, "mcp_servers.json")

    r = client.put("/api/admin/local-mcp/demo", json={
        "name": "ignored", "command": "python", "args": ["x.py"], "enabled": True,
        "description": "示例", "env": {"A": "1"}, "risk_overrides": {"t": "read"},
        "evil": "drop"})
    assert r.status_code == 200
    entry = r.json()["server"]
    assert entry["name"] == "demo" and "evil" not in entry
    assert entry["risk_overrides"] == {"t": "read"}
    assert server.agent.mcp.reloads == 1
    with open(path, encoding="utf-8") as f:
        stored = json.load(f)
    assert stored == [entry]

    r = client.get("/api/admin/local-mcp/demo")
    assert r.status_code == 200 and r.json()["server"]["command"] == "python"
    r = client.get("/api/admin/local-mcp")
    assert r.status_code == 200 and len(r.json()["servers"]) == 1
    assert client.get("/api/admin/local-mcp/nope").status_code == 404

    r = client.put("/api/admin/local-mcp/demo", json={"command": "node"})
    assert r.status_code == 200
    with open(path, encoding="utf-8") as f:
        stored = json.load(f)
    assert len(stored) == 1 and stored[0]["name"] == "demo" and stored[0]["command"] == "node"
    assert "args" not in stored[0]                     # 覆盖更新: 未提供字段不再保留

    r = client.delete("/api/admin/local-mcp/demo")
    assert r.status_code == 200
    assert server.agent.mcp.reloads == 3
    assert client.get("/api/admin/local-mcp/demo").status_code == 404
    assert client.delete("/api/admin/local-mcp/demo").status_code == 404
