import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from security.rbac import RBACManager
from storage.storage import Storage


@pytest.fixture
def storage(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    return Storage(str(ws))


@pytest.fixture
def rbac(storage):
    return RBACManager(storage)


def test_rbac_tables_created(storage):
    with storage.get_connection() as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'rbac_%'"
        ).fetchall()]
    assert "rbac_roles" in tables
    assert "rbac_users" in tables
    assert "rbac_user_identities" in tables


def test_default_role_exists(storage):
    with storage.get_connection() as conn:
        row = conn.execute("SELECT name, allowed_tools, allowed_agents FROM rbac_roles WHERE name='default'").fetchone()
    assert row is not None
    assert row[0] == "default"
    assert row[1] == "[]"
    assert row[2] == "[]"


def test_admin_role_exists(storage):
    with storage.get_connection() as conn:
        row = conn.execute("SELECT name, allowed_tools, allowed_agents FROM rbac_roles WHERE name='admin'").fetchone()
    assert row is not None
    assert row[1] == '["*"]'
    assert row[2] == '["*"]'


def test_get_role_for_unknown_user(rbac):
    role = rbac.get_user_role(platform="dingtalk", platform_uid="unknown_id")
    assert role == "default"


def test_check_tool_default_role_denied(rbac):
    assert not rbac.check_tool("default", "shell")


def test_check_tool_admin_allowed(rbac):
    assert rbac.check_tool("admin", "shell")
    assert rbac.check_tool("admin", "any_tool")


def test_check_agent_default_role_denied(rbac):
    assert not rbac.check_agent("default", "设备运维")


def test_check_agent_admin_allowed(rbac):
    assert rbac.check_agent("admin", "设备运维")


def test_create_user_and_bind_identity(rbac):
    user_id = rbac.create_user(name="张三", department="技术部", role="admin")
    assert user_id > 0

    rbac.bind_identity(user_id=user_id, platform="dingtalk", platform_uid="dt_123")

    role = rbac.get_user_role(platform="dingtalk", platform_uid="dt_123")
    assert role == "admin"


def test_create_custom_role_and_check(rbac):
    rbac.create_role(name="developer", description="开发者",
                     allowed_tools=["shell", "file_operation", "search"],
                     allowed_agents=["代码审查"])
    assert rbac.check_tool("developer", "shell")
    assert not rbac.check_tool("developer", "edit")
    assert rbac.check_agent("developer", "代码审查")
    assert not rbac.check_agent("developer", "设备运维")


def test_disabled_user_gets_default_role(rbac):
    user_id = rbac.create_user(name="李四", department="运维部", role="admin")
    rbac.bind_identity(user_id=user_id, platform="dingtalk", platform_uid="dt_456")
    rbac.disable_user(user_id)

    role = rbac.get_user_role(platform="dingtalk", platform_uid="dt_456")
    assert role == "default"


# ===== Web 权限 / 数据范围 =====

def test_builtin_admin_all_permissions_and_scope(rbac):
    assert rbac.get_permissions("admin") == ["*"]
    assert rbac.get_data_scope("admin") == "all"
    assert rbac.has_permission("admin", "admin.users")
    assert rbac.has_permission("admin", "anything")


def test_builtin_default_no_permission_self_scope(rbac):
    assert rbac.get_permissions("default") == []
    assert rbac.get_data_scope("default") == "self"
    assert not rbac.has_permission("default", "admin.users")


def test_custom_role_permissions_and_scope(rbac):
    rbac.create_role(name="dept_manager", description="部门管理员",
                     permissions=["admin.users", "admin.monitor"], data_scope="department")
    assert rbac.has_permission("dept_manager", "admin.users")
    assert not rbac.has_permission("dept_manager", "admin.roles")
    assert rbac.get_data_scope("dept_manager") == "department"

    role = rbac.get_role("dept_manager")
    assert role["permissions"] == ["admin.users", "admin.monitor"]
    assert role["data_scope"] == "department"
    assert role["builtin"] is False


def test_update_role_changes_permissions(rbac):
    rbac.create_role(name="ops", permissions=["admin.monitor"])
    rbac.update_role("ops", permissions=["admin.monitor", "admin.logs"], data_scope="all")
    assert rbac.has_permission("ops", "admin.logs")
    assert rbac.get_data_scope("ops") == "all"


def test_invalid_data_scope_falls_back_to_self(rbac):
    rbac.create_role(name="weird", data_scope="galaxy")
    assert rbac.get_data_scope("weird") == "self"


# ===== 部门管理 =====

def test_department_crud_and_member_count(rbac):
    rbac.create_department("设备运维部", description="设备")
    rbac.create_user(name="u1", department="设备运维部")
    rbac.create_user(name="u2", department="设备运维部")
    rbac.create_user(name="u3", department="售后客服部")

    depts = {d["name"]: d for d in rbac.list_departments()}
    assert depts["设备运维部"]["member_count"] == 2

    members = rbac.list_user_ids_by_department("设备运维部")
    assert len(members) == 2
    assert rbac.get_user_department(members[0]) == "设备运维部"


def test_department_rename_syncs_members(rbac):
    rbac.create_department("旧部门")
    uid = rbac.create_user(name="u1", department="旧部门")
    rbac.update_department("旧部门", new_name="新部门")
    assert rbac.get_user_department(uid) == "新部门"
    assert rbac.get_department("新部门") is not None


def test_department_delete_blocked_when_has_members(rbac):
    rbac.create_department("有人员部")
    rbac.create_user(name="u1", department="有人员部")
    ok, reason = rbac.delete_department("有人员部")
    assert ok is False
    assert "成员" in reason

    rbac.create_department("空部门")
    ok2, _ = rbac.delete_department("空部门")
    assert ok2 is True


def test_migration_adds_permission_columns_to_legacy_db(tmp_path):
    """老库(无 permissions/data_scope 列)升级: 必须先补列再建种子, 否则启动即崩。"""
    import sqlite3

    db = tmp_path / "data.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE rbac_roles (name TEXT PRIMARY KEY, description TEXT DEFAULT '', "
            "allowed_tools TEXT DEFAULT '[]', allowed_agents TEXT DEFAULT '[]', created_at TEXT)")
        conn.execute(
            "INSERT INTO rbac_roles (name, description, allowed_tools, allowed_agents, created_at) "
            "VALUES ('admin', '管理员-全部权限', '[\"*\"]', '[\"*\"]', datetime('now'))")
        conn.execute(
            "INSERT INTO rbac_roles (name, description, allowed_tools, allowed_agents, created_at) "
            "VALUES ('default', '默认角色-只能对话', '[]', '[]', datetime('now'))")
        conn.commit()

    storage = Storage(str(tmp_path))
    try:
        with storage.get_connection() as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(rbac_roles)")]
        assert "permissions" in cols
        assert "data_scope" in cols
        rbac = RBACManager(storage)
        assert rbac.get_permissions("admin") == ["*"]
        assert rbac.get_data_scope("admin") == "all"
        assert rbac.get_data_scope("default") == "self"
    finally:
        storage.close()

