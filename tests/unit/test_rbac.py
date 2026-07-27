import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from rbac import RBACManager
from storage import Storage


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
