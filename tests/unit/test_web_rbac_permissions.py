"""Web 角色/部门权限控制：细粒度权限守卫 + 部门数据隔离。

覆盖：
- RBAC 管理端点需 admin.roles/users/departments 权限(修复此前无守卫的越权)；
- 自定义角色授予 admin.roles 后可访问对应接口；
- 部门范围(data_scope=department)管理员仅见本部门成员，不可跨部门；
- /api/memories view=all、/api/admin/* 由权限而非硬编码 role==admin 决定。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest
from fastapi.testclient import TestClient

import storage.storage as storage_mod
from security.rbac import RBACManager
from storage.storage import Storage
from web.server import WebServer, create_jwt


@pytest.fixture
def env(tmp_path, monkeypatch):
    """真实 JWT 鉴权 + 临时 storage 单例 + WebServer。"""
    monkeypatch.setenv("WEBUI_DISABLE_AUTH", "0")
    prev = storage_mod._storage_instance
    s = Storage(str(tmp_path))
    storage_mod._storage_instance = s
    w = WebServer()
    client = TestClient(w._app)
    yield w, s, client
    s.close()
    storage_mod._storage_instance = prev


def _token(uid: int, role: str = "default", name: str = "用户") -> dict:
    return {"Authorization": f"Bearer {create_jwt({'id': uid, 'name': name, 'role': role})}"}


# ===== 越权修复：RBAC 端点需权限 =====

def test_rbac_endpoints_reject_default_user(env):
    w, st, client = env
    rbac = RBACManager(st)
    rbac.create_role("custom", permissions=[])

    assert client.get("/api/rbac/roles", headers=_token(7)).status_code == 403
    assert client.get("/api/rbac/roles/custom", headers=_token(7)).status_code == 403
    assert client.delete("/api/rbac/roles/custom", headers=_token(7)).status_code == 403
    assert client.get("/api/rbac/users", headers=_token(7)).status_code == 403
    assert client.get("/api/rbac/departments", headers=_token(7)).status_code == 403
    assert client.get("/api/rbac/permissions", headers=_token(7)).status_code == 403


def test_admin_role_manages_roles_and_catalog(env):
    w, st, client = env
    h = _token(1, role="admin", name="管理员")

    r = client.get("/api/rbac/permissions", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    keys = {p["key"] for p in body["permissions"]}
    assert "admin.users" in keys and "admin.roles" in keys
    assert {s["key"] for s in body["data_scopes"]} == {"self", "department", "all"}

    assert client.get("/api/rbac/roles", headers=h).status_code == 200

    r = client.post("/api/rbac/roles", headers=h, json={
        "name": "auditor", "description": "审计", "permissions": ["admin.logs"],
        "data_scope": "all", "allowed_tools": [], "allowed_agents": []})
    assert r.status_code == 200, r.text

    role = client.get("/api/rbac/roles/auditor", headers=h).json()
    assert role["permissions"] == ["admin.logs"]
    assert role["data_scope"] == "all"

    r = client.put("/api/rbac/roles/auditor", headers=h, json={
        "permissions": ["admin.logs", "admin.monitor"], "data_scope": "department"})
    assert r.status_code == 200, r.text
    assert client.get("/api/rbac/roles/auditor", headers=h).json()["data_scope"] == "department"

    assert client.delete("/api/rbac/roles/auditor", headers=h).status_code == 200


def test_builtin_roles_cannot_be_deleted_or_recreated(env):
    w, st, client = env
    h = _token(1, role="admin")
    assert client.delete("/api/rbac/roles/admin", headers=h).status_code == 400
    assert client.delete("/api/rbac/roles/default", headers=h).status_code == 400
    assert client.post("/api/rbac/roles", headers=h, json={"name": "admin"}).status_code == 400


def test_custom_role_with_admin_roles_permission_can_list(env):
    w, st, client = env
    RBACManager(st).create_role("roleviewer", permissions=["admin.roles"])
    h = _token(7, role="roleviewer")
    assert client.get("/api/rbac/roles", headers=h).status_code == 200
    # 未授予 admin.users → 用户管理仍拒绝
    assert client.get("/api/rbac/users", headers=h).status_code == 403


# ===== 部门数据隔离 =====

def _seed_dept_users(st):
    rbac = RBACManager(st)
    rbac.create_department("设备部")
    rbac.create_department("售后部")
    rbac.create_role("deptmgr", permissions=["admin.users"], data_scope="department")
    mgr = rbac.create_user(name="mgr", department="设备部", role="deptmgr", display_name="设备主管")
    rbac.create_user(name="a1", department="设备部", display_name="设备甲")
    rbac.create_user(name="b1", department="售后部", display_name="售后乙")
    return rbac, mgr


def test_department_scope_filters_user_list(env):
    w, st, client = env
    rbac, mgr = _seed_dept_users(st)
    h = _token(mgr, role="deptmgr", name="设备主管")

    r = client.get("/api/rbac/users", headers=h)
    assert r.status_code == 200, r.text
    depts = {u["department"] for u in r.json()["users"]}
    assert depts == {"设备部"}

    # 跨部门目标用户不可读(403)
    others = [u for u in rbac.list_users() if u["department"] == "售后部"]
    assert client.get(f"/api/rbac/users/{others[0]['id']}", headers=h).status_code == 403


def test_department_scope_create_forced_into_own_department(env):
    w, st, client = env
    _rbac, mgr = _seed_dept_users(st)
    h = _token(mgr, role="deptmgr")
    r = client.post("/api/rbac/users", headers=h, json={
        "name": "newbie", "department": "售后部", "role": "default", "password": "1234"})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["department"] == "设备部"


def test_department_scope_cannot_assign_privileged_role(env):
    w, st, client = env
    _rbac, mgr = _seed_dept_users(st)
    h = _token(mgr, role="deptmgr")
    r = client.post("/api/rbac/users", headers=h, json={
        "name": "evil", "department": "设备部", "role": "admin", "password": "1234"})
    assert r.status_code == 403


def test_role_options_available_to_user_managers(env):
    """仅 admin.users 的管理员也能取角色下拉选项(用户管理必需)。"""
    w, st, client = env
    _rbac, mgr = _seed_dept_users(st)
    h = _token(mgr, role="deptmgr")
    r = client.get("/api/rbac/roles/options", headers=h)
    assert r.status_code == 200, r.text
    names = {x["name"] for x in r.json()["roles"]}
    assert "default" in names and "deptmgr" in names
    # 仍不可读取完整角色定义(需 admin.roles)
    assert client.get("/api/rbac/roles", headers=h).status_code == 403


# ===== 记忆 / 监控权限 =====

def test_memories_view_all_requires_permission(env):
    w, st, client = env
    st.save_memory(scope="global", owner_id="", category="knowledge", content="公共", source="admin")
    st.save_memory(scope="user", owner_id="web:7", category="key_info", content="私有", source="agent")

    # 普通用户 view=all 被强制个人口径，不含 global
    r = client.get("/api/memories?view=all", headers=_token(7))
    assert {m["content"] for m in r.json()["memories"]} == {"私有"}

    # 授予 admin.memories 后可看全量
    RBACManager(st).create_role("memadmin", permissions=["admin.memories"])
    r = client.get("/api/memories?view=all", headers=_token(7, role="memadmin"))
    assert {m["content"] for m in r.json()["memories"]} == {"公共", "私有"}


def test_admin_stats_requires_monitor_permission(env):
    w, st, client = env
    assert client.get("/api/admin/stats", headers=_token(7)).status_code == 403
    # admin 通过权限校验(后续可能因 agent 未初始化返回 503，但不再是 403)
    assert client.get("/api/admin/stats", headers=_token(1, role="admin")).status_code != 403
