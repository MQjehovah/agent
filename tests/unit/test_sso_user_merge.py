"""SSO 账号模型: 姓名/工号分离(rbac_users.name=姓名, work_id=工号) + 双账号合并 + 钉钉自动绑定。

覆盖:
- 老库迁移: name=工号 + display_name=姓名 → work_id=工号 / name=姓名(幂等);
  已是姓名(中文/含空白)的 name 保持不动; 纯 ASCII 账号名(admin)视为工号身份
- create_user/update_user/resolve_user/verify_user_password 对 name/work_id 的读写
- _sso_ensure_user 分段: work_id 命中、邮箱优先补工号、姓名合并补工号、全新创建
- 合并保留老账号 id/role/dept/status/钉钉身份
- claims.dingtalk 登录成功后自动 bind_identity(幂等, 不产生重复绑定)
- SSO 权威源回写: name(姓名)/email/dept; 部门注册表缺失自动建档
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
            "SELECT id, name, work_id, department, role, status, email "
            "FROM rbac_users WHERE id=?",
            (uid,),
        ).fetchone()


def _set_email(store, uid, email):
    with store.get_connection() as conn:
        conn.execute("UPDATE rbac_users SET email=? WHERE id=?", (email, uid))
        conn.commit()


# ---- 老库迁移: 姓名/工号分离回填 ----

def test_migration_separates_name_and_work_id(tmp_path):
    """老库(name=工号 + display_name=姓名) → 初始化回填: work_id=工号, name=姓名。

    已是姓名(中文/含空白)的 name 保持不动; 纯 ASCII 账号名(admin)视为工号身份; 幂等。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    db = ws / "data.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE rbac_users (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT NOT NULL, display_name TEXT DEFAULT '', department TEXT DEFAULT '', "
            "role TEXT NOT NULL DEFAULT 'default', status TEXT DEFAULT 'active', "
            "created_at TEXT, updated_at TEXT)"
        )
        conn.executemany(
            "INSERT INTO rbac_users (name, display_name, department, role, status) "
            "VALUES (?,?,?,?,?)",
            [
                ("202202100024", "季明清", "研发部", "admin", "active"),
                ("王祁", "", "平台部", "default", "active"),
                ("James cui", "", "", "fae", "active"),
                ("admin", "", "", "admin", "active"),
            ],
        )
    s1 = Storage(str(ws))
    try:
        with s1.get_connection() as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(rbac_users)").fetchall()]
            assert "work_id" in cols
            rows = [tuple(r) for r in conn.execute(
                "SELECT name, work_id FROM rbac_users ORDER BY id").fetchall()]
    finally:
        s1.close()
    assert rows == [("季明清", "202202100024"), ("王祁", ""), ("James cui", ""), ("admin", "admin")]
    # 二次启动: 幂等, 不回退
    s2 = Storage(str(ws))
    try:
        with s2.get_connection() as conn:
            row = conn.execute("SELECT name, work_id FROM rbac_users WHERE id=1").fetchone()
        assert tuple(row) == ("季明清", "202202100024")
    finally:
        s2.close()


# ---- rbac name(姓名)/work_id(工号) 读写 ----

def test_create_user_roundtrip_name_and_work_id(store):
    rbac = RBACManager(store)
    uid = rbac.create_user(name="季明清", work_id="202202100024",
                           department="研发部", role="admin")
    assert uid > 0
    u = rbac.get_user(uid)
    assert u["name"] == "季明清"
    assert u["work_id"] == "202202100024"
    assert "display_name" not in u
    listed = next(x for x in rbac.list_users() if x["id"] == uid)
    assert listed["name"] == "季明清"
    assert listed["work_id"] == "202202100024"


def test_update_user_supports_name_work_id(store):
    rbac = RBACManager(store)
    uid = rbac.create_user(name="张三")
    rbac.update_user(uid, name="张思", work_id="10086")
    u = rbac.get_user(uid)
    assert u["name"] == "张思"
    assert u["work_id"] == "10086"


def test_resolve_user_uses_name(store):
    rbac = RBACManager(store)
    uid = rbac.create_user(name="张伟", work_id="10086", role="default")
    rbac.bind_identity(uid, "dingtalk", "dt_10086")
    info = rbac.resolve_user("dingtalk", "dt_10086")
    assert info["user_id"] == uid
    assert info["user_name"] == "张伟"
    assert info["work_id"] == "10086"


def test_verify_user_password_by_name_or_work_id(store):
    """密码登录用户名: name(姓名) 与 work_id(工号) 均可命中, 返回两个字段本身。"""
    rbac = RBACManager(store)
    uid = rbac.create_user(name="季明清", work_id="202202100024", role="default")
    store.set_user_password(uid, "pw-123456")
    by_wid = store.verify_user_password("202202100024", "pw-123456")
    assert by_wid and by_wid["id"] == uid
    assert by_wid["name"] == "季明清" and by_wid["work_id"] == "202202100024"
    assert "display_name" not in by_wid
    by_name = store.verify_user_password("季明清", "pw-123456")
    assert by_name and by_name["id"] == uid


# ---- _sso_lookup_user ----

def test_lookup_user_by_workid(store):
    rbac = RBACManager(store)
    rbac.create_user(name="季明清", work_id="202202100024", role="default")
    u = _sso_lookup_user("202202100024")
    assert u is not None
    assert u["name"] == "季明清"
    assert u["work_id"] == "202202100024"


# ---- _sso_ensure_user: 全新创建 ----

def test_ensure_creates_fresh_user(store):
    u = _sso_ensure_user("202202100024", dict(CLAIMS))
    assert u["name"] == "季明清"
    assert u["work_id"] == "202202100024"
    assert u["role"] == "default"
    assert u["status"] == "active"
    row = _user_row(store, u["id"])
    assert row["name"] == "季明清"
    assert row["work_id"] == "202202100024"
    assert row["department"] == "研发部"
    # claims 无 dingtalk → 不产生身份绑定
    assert RBACManager(store).list_user_identities(u["id"]) == []


# ---- _sso_ensure_user: work_id 命中, 姓名按 SSO 权威源回写 ----

def test_ensure_existing_workid_syncs_name(store):
    rbac = RBACManager(store)
    uid = rbac.create_user(name="旧名", work_id="202202100024",
                           department="研发部", role="default")
    u = _sso_ensure_user("202202100024", dict(CLAIMS))
    assert u["id"] == uid
    assert _user_row(store, uid)["name"] == "季明清"
    # 再次登录: 姓名已一致, 不改变
    assert _sso_ensure_user("202202100024", dict(CLAIMS))["id"] == uid


def test_ensure_empty_claim_name_keeps_name(store):
    """claims 缺 name → 保留现有姓名, 不清空、不拿工号顶。"""
    rbac = RBACManager(store)
    uid = rbac.create_user(name="手工名", work_id="202202100024", role="default")
    without = {k: v for k, v in CLAIMS.items() if k != "name"}
    assert _sso_ensure_user("202202100024", without)["name"] == "手工名"
    assert _user_row(store, uid)["name"] == "手工名"


# ---- _sso_ensure_user: 部门以 SSO 为权威源回写 ----

def test_ensure_writes_back_changed_department(store):
    """登录 dept 与当前不同 → 回写; 二次登录取新值(与 market 侧同语义)。"""
    rbac = RBACManager(store)
    uid = rbac.create_user(name="季明清", work_id="202202100024",
                           department="老部门", role="default")
    u = _sso_ensure_user("202202100024", dict(CLAIMS))  # dept=研发部
    assert u["id"] == uid and u["department"] == "研发部"
    assert _user_row(store, uid)["department"] == "研发部"

    u2 = _sso_ensure_user("202202100024", dict(CLAIMS, dept="研发二部"))
    assert u2["department"] == "研发二部"
    assert _user_row(store, uid)["department"] == "研发二部"


def test_ensure_empty_department_keeps_manual_value(store):
    """claims 缺/空 dept → 保留管理员手工填写的部门(不覆盖为空)。"""
    rbac = RBACManager(store)
    uid = rbac.create_user(name="季明清", work_id="202202100024",
                           department="手工部门", role="default")
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
    """老账号(姓名合并)补工号后同样以 SSO 部门为权威源回写。"""
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
    assert rbac.get_department("研发部")["description"] == "原描述"


def test_ensure_registers_department_even_when_user_dept_unchanged(store):
    """用户部门已一致但注册表缺失 → 登录时也补建档。"""
    rbac = RBACManager(store)
    uid = rbac.create_user(name="季明清", work_id="202202100024",
                           department="研发部", role="default")
    assert rbac.get_department("研发部") is None
    _sso_ensure_user("202202100024", dict(CLAIMS))
    assert rbac.get_department("研发部") is not None
    assert _user_row(store, uid)["department"] == "研发部"


# ---- _sso_ensure_user: 邮箱以 SSO 为权威源回写 ----

def test_ensure_syncs_changed_email(store):
    """email 非空但不同 → 登录时按 claims.email 反写(归一化小写)。"""
    rbac = RBACManager(store)
    uid = rbac.create_user(name="季明清", work_id="202202100024",
                           department="研发部", role="default")
    _set_email(store, uid, "old@x.com")
    u = _sso_ensure_user("202202100024", dict(CLAIMS, email="Ji.MingQing@XZRobot.com"))
    assert u["email"] == "ji.mingqing@xzrobot.com"
    assert _user_row(store, uid)["email"] == "ji.mingqing@xzrobot.com"


def test_ensure_empty_claim_email_keeps_existing(store):
    """claims 缺/空 email → 保留现有邮箱, 不清空。"""
    rbac = RBACManager(store)
    uid = rbac.create_user(name="季明清", work_id="202202100024", role="default")
    _set_email(store, uid, "keep@x.com")
    assert _sso_ensure_user("202202100024", dict(CLAIMS))["email"] == "keep@x.com"
    assert _sso_ensure_user("202202100024", dict(CLAIMS, email="   "))["email"] == "keep@x.com"
    assert _user_row(store, uid)["email"] == "keep@x.com"


def test_ensure_first_login_sets_email(store):
    """首登建号: claims.email 直接写入。"""
    u = _sso_ensure_user("202202100024", dict(CLAIMS, email="Ji@xzrobot.com"))
    assert u["email"] == "ji@xzrobot.com"
    assert _user_row(store, u["id"])["email"] == "ji@xzrobot.com"


# ---- _sso_ensure_user: 老账号合并(姓名命中 → 补工号) ----

def test_ensure_merges_old_name_account(store):
    """老账号(name=中文姓名, 无工号) → 补 work_id=sub, 保留其余列与历史。"""
    rbac = RBACManager(store)
    uid = rbac.create_user(name="季明清", department="研发部", role="admin")
    rbac.bind_identity(uid, "dingtalk", DINGTALK)

    u = _sso_ensure_user("202202100024", dict(CLAIMS))
    assert u["id"] == uid  # 保留老账号 id
    assert u["name"] == "季明清"  # 姓名不变
    assert u["work_id"] == "202202100024"  # 工号补齐
    assert u["role"] == "admin"  # 保留 role
    row = _user_row(store, uid)
    assert row["status"] == "active"
    # 钉钉绑定仍在(合并不丢)
    ids = rbac.list_user_identities(uid)
    assert any(i["platform"] == "dingtalk" and i["platform_uid"] == DINGTALK for i in ids)
    # 按工号可查
    assert _sso_lookup_user("202202100024") is not None


def test_ensure_email_priority_fills_workid(store):
    """邮箱命中(系统自建账号未记工号) → 复用账号并补 work_id=sub。"""
    rbac = RBACManager(store)
    uid = rbac.create_user(name="季明清", role="default")
    _set_email(store, uid, "ji@xzrobot.com")
    u = _sso_ensure_user("202202100024", dict(CLAIMS, email="ji@xzrobot.com"))
    assert u["id"] == uid
    assert u["work_id"] == "202202100024"
    assert u["name"] == "季明清"


def test_ensure_disabled_old_account_raises_403(store):
    rbac = RBACManager(store)
    uid = rbac.create_user(name="季明清", role="admin")
    rbac.disable_user(uid)
    with pytest.raises(HTTPException) as ei:
        _sso_ensure_user("202202100024", dict(CLAIMS))
    assert ei.value.status_code == 403
    # 合并失败不影响老账号原状(不补工号)
    row = _user_row(store, uid)
    assert row["name"] == "季明清" and (row["work_id"] or "") == ""


def test_ensure_disabled_workid_account_raises_403(store):
    rbac = RBACManager(store)
    uid = rbac.create_user(name="季明清", work_id="202202100024", role="default")
    rbac.disable_user(uid)
    with pytest.raises(HTTPException) as ei:
        _sso_ensure_user("202202100024", dict(CLAIMS))
    assert ei.value.status_code == 403


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
