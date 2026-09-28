"""SSO 账号模型: rbac_users.display_name + 双账号合并 + claims.dingtalk 自动绑定。

覆盖:
- rbac_users.display_name 列迁移幂等(旧库缺列自动补齐, 重启不重复)
- create_user/update_user/resolve_user 对 display_name 的读写
- _sso_ensure_user 三段: name=sub 命中、老账号(name=中文)合并、全新创建
- 合并保留老账号 id/role/dept/status/钉钉身份
- claims.dingtalk 登录成功后自动 bind_identity(幂等, 不产生重复绑定)
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest
from fastapi import HTTPException

import storage.storage as storage_mod
from security.rbac import RBACManager
from storage.storage import Storage
from web.server import _sso_ensure_user, _sso_lookup_user

CLAIMS = {
    "sub": "202202100024",
    "name": "季明清",
    "dept": "研发部",
    "roles": ["user"],
}
DINGTALK = "1642483198771392"


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


def _user_row(store, uid):
    with store.get_connection() as conn:
        return conn.execute(
            "SELECT id, name, display_name, department, role, status, email "
            "FROM rbac_users WHERE id=?",
            (uid,),
        ).fetchone()


# ---- display_name 迁移幂等 ----

def test_migration_adds_display_name_column_idempotent(tmp_path):
    """旧库(无 display_name 列)初始化自动 ALTER 补齐; 重复初始化不报错。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    db = ws / "data.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE rbac_users (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT NOT NULL, department TEXT DEFAULT '', "
            "role TEXT NOT NULL DEFAULT 'default', status TEXT DEFAULT 'active', "
            "created_at TEXT, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO rbac_users (name, department, role, status) "
            "VALUES ('季明清', '研发部', 'admin', 'active')"
        )
    s1 = Storage(str(ws))
    try:
        with s1.get_connection() as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(rbac_users)").fetchall()]
        assert "display_name" in cols
        row = conn.execute("SELECT name, display_name FROM rbac_users WHERE id=1").fetchone()
        assert row["name"] == "季明清"
        assert row["display_name"] == ""
    finally:
        s1.close()
    # 二次启动(模拟重启): ALTER 幂等, 不抛错, 数据不丢
    s2 = Storage(str(ws))
    try:
        with s2.get_connection() as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(rbac_users)").fetchall()]
            assert "display_name" in cols
            row = conn.execute("SELECT name, display_name FROM rbac_users WHERE id=1").fetchone()
        assert row["name"] == "季明清"
    finally:
        s2.close()


# ---- rbac display_name 读写 ----

def test_create_user_roundtrip_display_name(store):
    rbac = RBACManager(store)
    uid = rbac.create_user(name="202202100024", department="研发部",
                           role="admin", display_name="季明清")
    assert uid > 0
    u = rbac.get_user(uid)
    assert u["name"] == "202202100024"
    assert u["display_name"] == "季明清"
    listed = next(x for x in rbac.list_users() if x["id"] == uid)
    assert listed["display_name"] == "季明清"


def test_update_user_supports_display_name(store):
    rbac = RBACManager(store)
    uid = rbac.create_user(name="10086")
    rbac.update_user(uid, display_name="张三")
    assert rbac.get_user(uid)["display_name"] == "张三"


def test_resolve_user_prefers_display_name(store):
    rbac = RBACManager(store)
    uid = rbac.create_user(name="10086", role="default", display_name="张三")
    rbac.bind_identity(uid, "dingtalk", "dt_10086")
    info = rbac.resolve_user("dingtalk", "dt_10086")
    assert info["user_id"] == uid
    assert info["user_name"] == "张三"


# ---- _sso_lookup_user ----

def test_lookup_user_by_workid(store):
    rbac = RBACManager(store)
    rbac.create_user(name="202202100024", display_name="季明清", role="default")
    u = _sso_lookup_user("202202100024")
    assert u is not None
    assert u["name"] == "202202100024"
    assert u["display_name"] == "季明清"


# ---- _sso_ensure_user: 全新创建 ----

def test_ensure_creates_fresh_user(store):
    u = _sso_ensure_user("202202100024", dict(CLAIMS))
    assert u["name"] == "202202100024"
    assert u["display_name"] == "季明清"
    assert u["role"] == "default"
    assert u["status"] == "active"
    row = _user_row(store, u["id"])
    assert row["name"] == "202202100024"
    assert row["display_name"] == "季明清"
    assert row["department"] == "研发部"
    # claims 无 dingtalk → 不产生身份绑定
    assert RBACManager(store).list_user_identities(u["id"]) == []


# ---- _sso_ensure_user: name=sub 命中, 顺手补 display_name ----

def test_ensure_existing_sub_fills_display_name(store):
    rbac = RBACManager(store)
    uid = rbac.create_user(name="202202100024", department="研发部", role="default")
    u = _sso_ensure_user("202202100024", dict(CLAIMS))
    assert u["id"] == uid
    assert _user_row(store, uid)["display_name"] == "季明清"
    # 再次登录: display_name 已填, 不改变
    u2 = _sso_ensure_user("202202100024", dict(CLAIMS))
    assert u2["id"] == uid


# ---- _sso_ensure_user: 部门以 SSO 为权威源回写 ----

def test_ensure_writes_back_changed_department(store):
    """登录 dept 与当前不同 → 回写; 二次登录取新值(与 market 侧 G1 同语义)。"""
    rbac = RBACManager(store)
    uid = rbac.create_user(name="202202100024", department="老部门", role="default")
    u = _sso_ensure_user("202202100024", dict(CLAIMS))  # dept=研发部
    assert u["id"] == uid and u["department"] == "研发部"
    assert _user_row(store, uid)["department"] == "研发部"

    u2 = _sso_ensure_user("202202100024", dict(CLAIMS, dept="研发二部"))
    assert u2["department"] == "研发二部"
    assert _user_row(store, uid)["department"] == "研发二部"


def test_ensure_empty_department_keeps_manual_value(store):
    """claims 缺/空 dept → 保留管理员手工填写的部门(不覆盖为空)。"""
    rbac = RBACManager(store)
    uid = rbac.create_user(name="202202100024", department="手工部门", role="default")
    without = {k: v for k, v in CLAIMS.items() if k != "dept"}
    assert _sso_ensure_user("202202100024", without)["department"] == "手工部门"
    assert _sso_ensure_user("202202100024", dict(CLAIMS, dept="   "))["department"] == "手工部门"
    assert _user_row(store, uid)["department"] == "手工部门"


def test_ensure_first_login_writes_stripped_department(store):
    """首登建号: department 取 claims.dept 去首尾空白后的值。"""
    u = _sso_ensure_user("202202100024", dict(CLAIMS, dept=" 研发一部 "))
    assert u["department"] == "研发一部"
    assert _user_row(store, u["id"])["department"] == "研发一部"


def test_ensure_merge_writes_back_department(store):
    """老账号(中文名)合并后同样以 SSO 部门为权威源回写。"""
    rbac = RBACManager(store)
    uid = rbac.create_user(name="季明清", department="老部门", role="admin")
    u = _sso_ensure_user("202202100024", dict(CLAIMS))
    assert u["id"] == uid and u["department"] == "研发部"
    assert _user_row(store, uid)["department"] == "研发部"


# ---- _sso_ensure_user: 部门注册表缺失自动建档 ----

def test_ensure_auto_registers_missing_department(store):
    """登录带来的部门在 rbac_departments 缺失 → 自动建档(幂等)。"""
    u = _sso_ensure_user("202202100024", dict(CLAIMS))
    rbac = RBACManager(store)
    d = rbac.get_department("研发部")
    assert d is not None and d["name"] == "研发部"
    # 再次登录不报错/不重复
    _sso_ensure_user("202202100024", dict(CLAIMS))
    assert rbac.get_department("研发部") is not None
    assert _user_row(store, u["id"])["department"] == "研发部"


def test_ensure_department_registration_keeps_existing_meta(store):
    """已建档部门(有描述)不被登录建档覆盖。"""
    rbac = RBACManager(store)
    rbac.create_department("研发部", description="原描述")
    _sso_ensure_user("202202100024", dict(CLAIMS))
    d = rbac.get_department("研发部")
    assert d["description"] == "原描述"


def test_ensure_registers_department_even_when_user_dept_unchanged(store):
    """用户部门已一致但注册表缺失 → 登录时也补建档。"""
    rbac = RBACManager(store)
    uid = rbac.create_user(name="202202100024", department="研发部", role="default")
    assert rbac.get_department("研发部") is None
    _sso_ensure_user("202202100024", dict(CLAIMS))
    assert rbac.get_department("研发部") is not None
    assert _user_row(store, uid)["department"] == "研发部"


# ---- _sso_ensure_user: 姓名以 SSO 为权威源反写 ----

def test_ensure_syncs_changed_display_name(store):
    """display_name 非空但不同 → 登录时按 claims.name 反写(旧名不保留)。"""
    rbac = RBACManager(store)
    uid = rbac.create_user(name="202202100024", department="研发部",
                           role="default", display_name="旧显示名")
    u = _sso_ensure_user("202202100024", dict(CLAIMS))
    assert u["id"] == uid and u["display_name"] == "季明清"
    assert _user_row(store, uid)["display_name"] == "季明清"


def test_ensure_empty_claim_name_keeps_display_name(store):
    """claims 缺 name → 保留现有 display_name, 不清空。"""
    rbac = RBACManager(store)
    uid = rbac.create_user(name="202202100024", display_name="手工名", role="default")
    without = {k: v for k, v in CLAIMS.items() if k != "name"}
    assert _sso_ensure_user("202202100024", without)["display_name"] == "手工名"
    assert _user_row(store, uid)["display_name"] == "手工名"


def test_ensure_merge_prefers_claim_name(store):
    """老账号合并: display_name 以 claims.name 为准(不同也反写)。"""
    rbac = RBACManager(store)
    uid = rbac.create_user(name="季明清", display_name="旧显示名", role="admin")
    u = _sso_ensure_user("202202100024", dict(CLAIMS))
    assert u["id"] == uid and u["display_name"] == "季明清"


# ---- _sso_ensure_user: 邮箱以 SSO 为权威源反写 ----

def _set_email(store, uid, email):
    with store.get_connection() as conn:
        conn.execute("UPDATE rbac_users SET email=? WHERE id=?", (email, uid))
        conn.commit()


def test_ensure_syncs_changed_email(store):
    """email 非空但不同 → 登录时按 claims.email 反写(归一化小写)。"""
    rbac = RBACManager(store)
    uid = rbac.create_user(name="202202100024", department="研发部", role="default")
    _set_email(store, uid, "old@x.com")
    u = _sso_ensure_user("202202100024", dict(CLAIMS, email="Ji.MingQing@XZRobot.com"))
    assert u["email"] == "ji.mingqing@xzrobot.com"
    assert _user_row(store, uid)["email"] == "ji.mingqing@xzrobot.com"


def test_ensure_empty_claim_email_keeps_existing(store):
    """claims 缺/空 email → 保留现有邮箱, 不清空。"""
    rbac = RBACManager(store)
    uid = rbac.create_user(name="202202100024", role="default")
    _set_email(store, uid, "keep@x.com")
    assert _sso_ensure_user("202202100024", dict(CLAIMS))["email"] == "keep@x.com"
    assert _sso_ensure_user("202202100024", dict(CLAIMS, email="   "))["email"] == "keep@x.com"
    assert _user_row(store, uid)["email"] == "keep@x.com"


def test_ensure_merge_writes_back_email(store):
    """老账号(中文名)合并后同样以 SSO 邮箱为权威源回写。"""
    rbac = RBACManager(store)
    uid = rbac.create_user(name="季明清", display_name="季明清", role="admin")
    _set_email(store, uid, "old@x.com")
    u = _sso_ensure_user("202202100024", dict(CLAIMS, email="ji@xzrobot.com"))
    assert u["id"] == uid and u["email"] == "ji@xzrobot.com"
    assert _user_row(store, uid)["email"] == "ji@xzrobot.com"


# ---- _sso_ensure_user: 老账号合并 ----

def test_ensure_merges_old_zh_name_account(store):
    rbac = RBACManager(store)
    uid = rbac.create_user(name="季明清", department="研发部", role="admin")
    rbac.bind_identity(uid, "dingtalk", DINGTALK)
    rbac.disable_user(uid)
    rbac.enable_user(uid)

    u = _sso_ensure_user("202202100024", dict(CLAIMS))
    assert u["id"] == uid  # 保留老账号 id
    assert u["name"] == "202202100024"  # name 改为工号
    assert u["display_name"] == "季明清"
    assert u["role"] == "admin"  # 保留 role
    assert u["department"] == "研发部"  # 保留 dept
    row = _user_row(store, uid)
    assert row["name"] == "202202100024"
    assert row["status"] == "active"
    # 钉钉绑定仍在(合并不丢)
    ids = rbac.list_user_identities(uid)
    assert any(i["platform"] == "dingtalk" and i["platform_uid"] == DINGTALK for i in ids)
    # 老中文名不再可查(已改名工号)
    assert _sso_lookup_user("季明清") is None
    assert _sso_lookup_user("202202100024") is not None


def test_ensure_disabled_old_account_raises_403(store):
    rbac = RBACManager(store)
    uid = rbac.create_user(name="季明清", role="admin")
    rbac.disable_user(uid)
    with pytest.raises(HTTPException) as ei:
        _sso_ensure_user("202202100024", dict(CLAIMS))
    assert ei.value.status_code == 403
    # 合并失败不影响老账号原状
    assert _user_row(store, uid)["name"] == "季明清"


def test_ensure_disabled_sub_account_raises_403(store):
    rbac = RBACManager(store)
    uid = rbac.create_user(name="202202100024", role="default")
    rbac.disable_user(uid)
    with pytest.raises(HTTPException) as ei:
        _sso_ensure_user("202202100024", dict(CLAIMS))
    assert ei.value.status_code == 403
    assert _user_row(store, uid)["display_name"] == ""


# ---- claims.dingtalk 自动绑定 ----

def test_ensure_binds_dingtalk_when_claim_present(store):
    rbac = RBACManager(store)
    claims = dict(CLAIMS, dingtalk=DINGTALK)
    u = _sso_ensure_user(claims["sub"], claims)
    ids = rbac.list_user_identities(u["id"])
    assert len(ids) == 1
    assert ids[0]["platform"] == "dingtalk"
    assert ids[0]["platform_uid"] == DINGTALK
    # 幂等: 再次登录不新增重复绑定
    _sso_ensure_user(claims["sub"], claims)
    assert len(rbac.list_user_identities(u["id"])) == 1
    # resolve_user 直接命中 → 钉钉无需人工开号即可用
    info = rbac.resolve_user("dingtalk", DINGTALK)
    assert info["user_id"] == u["id"]
    assert info["user_name"] == "季明清"


def test_ensure_skips_empty_dingtalk_claim(store):
    rbac = RBACManager(store)
    u = _sso_ensure_user("202202100024", dict(CLAIMS, dingtalk="   "))
    assert rbac.list_user_identities(u["id"]) == []
