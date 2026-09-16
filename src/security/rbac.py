import json
import logging
from datetime import datetime

logger = logging.getLogger("agent.rbac")

BUILTIN_ROLES = ("admin", "default")

# Web 管理端权限目录（前端「角色管理」直接消费；* 为超管通配）。
WEB_PERMISSIONS = [
    {"key": "admin.users", "label": "用户管理", "description": "查看/新增/编辑/禁用用户与身份绑定"},
    {"key": "admin.roles", "label": "角色管理", "description": "查看/新增/编辑/删除角色与权限"},
    {"key": "admin.departments", "label": "部门管理", "description": "查看/新增/编辑/删除部门"},
    {"key": "admin.monitor", "label": "运行监控", "description": "查看会话/用量/运行中/统计(受数据范围约束)"},
    {"key": "admin.logs", "label": "日志查看", "description": "查看实时运行日志"},
    {"key": "admin.memories", "label": "全站记忆", "description": "查看/管理全部用户记忆"},
    {"key": "admin.scheduler", "label": "全站定时任务", "description": "查看/管理全部用户定时任务"},
    {"key": "admin.workspace", "label": "工作区文件", "description": "查看服务端共享工作区文件"},
]

# 数据可见范围：role.data_scope（与本部门隔离配合）。
DATA_SCOPES = [
    {"key": "self", "label": "仅本人", "description": "只可见本人数据"},
    {"key": "department", "label": "本部门", "description": "可见本部门成员数据"},
    {"key": "all", "label": "全站", "description": "可见全部数据"},
]

_VALID_SCOPES = {s["key"] for s in DATA_SCOPES}


class UserNotProvisionedError(Exception):
    """Platform identity has no usable bound agent user (unregistered/disabled).

    Channel handlers catch this to reject the request instead of silently
    falling back to the default role.
    """

    def __init__(self, platform: str, platform_uid: str, message: str = ""):
        self.platform = platform
        self.platform_uid = platform_uid
        detail = message or f"{platform}:{platform_uid} 未开通 AI 数字员工或已被禁用"
        super().__init__(detail)


class RBACManager:
    def __init__(self, storage):
        self.storage = storage

    def get_user_role(self, platform: str, platform_uid: str) -> str:
        info = self.resolve_user(platform, platform_uid)
        return info["role"]

    def resolve_user(self, platform: str, platform_uid: str, fallback_name: str = "") -> dict:
        if platform == "cli":
            return {"user_id": None, "user_name": fallback_name or "管理员", "display_name": "",
                    "role": "admin"}
        with self.storage.get_connection() as conn:
            row = conn.execute(
                """SELECT u.id, u.name, u.display_name, u.role, u.status FROM rbac_user_identities i
                   JOIN rbac_users u ON i.user_id = u.id
                   WHERE i.platform = ? AND i.platform_uid = ?""",
                (platform, platform_uid)
            ).fetchone()
        if row and row[4] != "disabled":
            return {"user_id": row[0], "user_name": row[2] or row[1],
                    "display_name": row[2] or row[1], "role": row[3]}
        return {"user_id": None, "user_name": fallback_name, "display_name": "",
                "role": "default"}

    def require_user(self, platform: str, platform_uid: str, fallback_name: str = "") -> dict:
        """Resolve a platform identity to a usable agent user, or raise.

        Same lookup as resolve_user, but treats "not bound / disabled" as an
        error so channel handlers can reject the message (never route under a
        synthetic dingtalk:{staff} id or a silent default role). CLI always
        resolves to the admin.
        """
        if platform == "cli":
            return {"user_id": None, "user_name": fallback_name or "管理员", "role": "admin"}
        info = self.resolve_user(platform, platform_uid, fallback_name=fallback_name)
        if not info.get("user_id"):
            raise UserNotProvisionedError(platform, platform_uid)
        return info

    def check_tool(self, role: str, tool_name: str) -> bool:
        allowed = self._get_allowed(role, "allowed_tools")
        return "*" in allowed or tool_name in allowed

    def check_agent(self, role: str, agent_name: str) -> bool:
        allowed = self._get_allowed(role, "allowed_agents")
        return "*" in allowed or agent_name in allowed

    def get_permissions(self, role: str) -> list:
        """角色的 Web 管理权限键列表；内置 admin 恒为全站通配 ``*``。"""
        if role == "admin":
            return ["*"]
        if not role:
            return []
        with self.storage.get_connection() as conn:
            row = conn.execute(
                "SELECT permissions FROM rbac_roles WHERE name = ?", (role,)
            ).fetchone()
        if not row or not row[0]:
            return []
        try:
            perms = json.loads(row[0])
            return perms if isinstance(perms, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    def get_data_scope(self, role: str) -> str:
        """角色数据可见范围: all / department / self（内置 admin 恒 all）。"""
        if role == "admin":
            return "all"
        if not role or role == "default":
            return "self"
        with self.storage.get_connection() as conn:
            row = conn.execute(
                "SELECT data_scope FROM rbac_roles WHERE name = ?", (role,)
            ).fetchone()
        scope = (row[0] if row and row[0] else "self")
        return scope if scope in _VALID_SCOPES else "self"

    def has_permission(self, role: str, perm: str) -> bool:
        perms = self.get_permissions(role)
        return "*" in perms or perm in perms

    def permission_catalog(self) -> dict:
        return {"permissions": WEB_PERMISSIONS, "data_scopes": DATA_SCOPES,
                "builtin_roles": list(BUILTIN_ROLES)}

    def _get_allowed(self, role: str, column: str) -> list:
        with self.storage.get_connection() as conn:
            row = conn.execute(
                f"SELECT {column} FROM rbac_roles WHERE name = ?",
                (role,)
            ).fetchone()
        if not row:
            return []
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return []

    def create_role(self, name: str, description: str = "",
                    allowed_tools: list = None, allowed_agents: list = None,
                    permissions: list = None, data_scope: str = "self") -> bool:
        now = datetime.now().isoformat()
        scope = data_scope if data_scope in _VALID_SCOPES else "self"
        with self.storage.get_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO rbac_roles "
                "(name, description, allowed_tools, allowed_agents, permissions, data_scope, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, description, json.dumps(allowed_tools or []),
                 json.dumps(allowed_agents or []), json.dumps(permissions or []), scope, now)
            )
            conn.commit()
        return True

    def create_user(self, name: str, department: str = "", role: str = "default",
                    display_name: str = "") -> int:
        now = datetime.now().isoformat()
        with self.storage.get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO rbac_users (name, display_name, department, role, status, created_at, updated_at) VALUES (?, ?, ?, ?, 'active', ?, ?)",
                (name, display_name, department, role, now, now)
            )
            conn.commit()
            return cursor.lastrowid

    def bind_identity(self, user_id: int, platform: str, platform_uid: str) -> bool:
        now = datetime.now().isoformat()
        with self.storage.get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO rbac_user_identities (user_id, platform, platform_uid, created_at) VALUES (?, ?, ?, ?)",
                (user_id, platform, platform_uid, now)
            )
            conn.commit()
        return True

    def disable_user(self, user_id: int) -> bool:
        now = datetime.now().isoformat()
        with self.storage.get_connection() as conn:
            conn.execute(
                "UPDATE rbac_users SET status='disabled', updated_at=? WHERE id=?",
                (now, user_id)
            )
            conn.commit()
        return True

    def enable_user(self, user_id: int) -> bool:
        now = datetime.now().isoformat()
        with self.storage.get_connection() as conn:
            conn.execute(
                "UPDATE rbac_users SET status='active', updated_at=? WHERE id=?",
                (now, user_id)
            )
            conn.commit()
        return True

    def list_users(self) -> list:
        with self.storage.get_connection() as conn:
            rows = conn.execute(
                "SELECT id, name, display_name, department, role, status, created_at, updated_at FROM rbac_users ORDER BY id"
            ).fetchall()
        result = []
        for r in rows:
            result.append({
                "id": r[0], "name": r[1], "display_name": r[2] or r[1], "department": r[3],
                "role": r[4], "status": r[5], "created_at": r[6], "updated_at": r[7]
            })
        return result

    def list_users_with_password_flag(self) -> list:
        with self.storage.get_connection() as conn:
            rows = conn.execute(
                "SELECT id, name, display_name, department, role, status, created_at, updated_at, "
                "CASE WHEN password_hash IS NOT NULL AND password_hash != '' THEN 1 ELSE 0 END AS has_pw "
                "FROM rbac_users ORDER BY id"
            ).fetchall()
        result = []
        for r in rows:
            result.append({
                "id": r[0], "name": r[1], "display_name": r[2] or r[1], "department": r[3],
                "role": r[4], "status": r[5], "created_at": r[6], "updated_at": r[7],
                "has_password": bool(r[8])
            })
        return result

    def get_user(self, user_id: int) -> dict | None:
        with self.storage.get_connection() as conn:
            row = conn.execute(
                "SELECT id, name, display_name, department, role, status, created_at, updated_at FROM rbac_users WHERE id=?",
                (user_id,)
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "name": row[1], "display_name": row[2] or row[1], "department": row[3],
            "role": row[4], "status": row[5], "created_at": row[6], "updated_at": row[7]
        }

    def get_user_with_password_flag(self, user_id: int) -> dict | None:
        with self.storage.get_connection() as conn:
            row = conn.execute(
                "SELECT id, name, display_name, department, role, status, created_at, updated_at, "
                "CASE WHEN password_hash IS NOT NULL AND password_hash != '' THEN 1 ELSE 0 END AS has_pw "
                "FROM rbac_users WHERE id=?", (user_id,)
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "name": row[1], "display_name": row[2] or row[1], "department": row[3],
            "role": row[4], "status": row[5], "created_at": row[6], "updated_at": row[7],
            "has_password": bool(row[8])
        }

    def update_user(self, user_id: int, name: str = None, department: str = None,
                    role: str = None, display_name: str = None) -> bool:
        now = datetime.now().isoformat()
        sets = ["updated_at=?"]
        vals = [now]
        if name is not None:
            sets.append("name=?")
            vals.append(name)
        if display_name is not None:
            sets.append("display_name=?")
            vals.append(display_name)
        if department is not None:
            sets.append("department=?")
            vals.append(department)
        if role is not None:
            sets.append("role=?")
            vals.append(role)
        vals.append(user_id)
        with self.storage.get_connection() as conn:
            conn.execute(f"UPDATE rbac_users SET {', '.join(sets)} WHERE id=?", vals)
            conn.commit()
        return True

    def delete_user(self, user_id: int) -> bool:
        with self.storage.get_connection() as conn:
            conn.execute("DELETE FROM rbac_user_identities WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM rbac_users WHERE id=?", (user_id,))
            conn.commit()
        return True

    def list_roles(self) -> list:
        with self.storage.get_connection() as conn:
            rows = conn.execute(
                "SELECT name, description, allowed_tools, allowed_agents, permissions, data_scope, created_at "
                "FROM rbac_roles ORDER BY name"
            ).fetchall()
        result = []
        for r in rows:
            result.append({
                "name": r[0], "description": r[1],
                "allowed_tools": json.loads(r[2]) if r[2] else [],
                "allowed_agents": json.loads(r[3]) if r[3] else [],
                "permissions": json.loads(r[4]) if r[4] else [],
                "data_scope": r[5] or "self",
                "created_at": r[6],
                "builtin": r[0] in BUILTIN_ROLES,
            })
        return result

    def get_role(self, name: str) -> dict | None:
        with self.storage.get_connection() as conn:
            row = conn.execute(
                "SELECT name, description, allowed_tools, allowed_agents, permissions, data_scope, created_at "
                "FROM rbac_roles WHERE name=?",
                (name,)
            ).fetchone()
        if not row:
            return None
        return {
            "name": row[0], "description": row[1],
            "allowed_tools": json.loads(row[2]) if row[2] else [],
            "allowed_agents": json.loads(row[3]) if row[3] else [],
            "permissions": json.loads(row[4]) if row[4] else [],
            "data_scope": row[5] or "self",
            "created_at": row[6],
            "builtin": row[0] in BUILTIN_ROLES,
        }

    def update_role(self, name: str, description: str = None,
                    allowed_tools: list = None, allowed_agents: list = None,
                    permissions: list = None, data_scope: str = None) -> bool:
        sets = []
        vals = []
        if description is not None:
            sets.append("description=?")
            vals.append(description)
        if allowed_tools is not None:
            sets.append("allowed_tools=?")
            vals.append(json.dumps(allowed_tools))
        if allowed_agents is not None:
            sets.append("allowed_agents=?")
            vals.append(json.dumps(allowed_agents))
        if permissions is not None:
            sets.append("permissions=?")
            vals.append(json.dumps(permissions))
        if data_scope is not None:
            sets.append("data_scope=?")
            vals.append(data_scope if data_scope in _VALID_SCOPES else "self")
        if not sets:
            return True
        vals.append(name)
        with self.storage.get_connection() as conn:
            conn.execute(f"UPDATE rbac_roles SET {', '.join(sets)} WHERE name=?", vals)
            conn.commit()
        return True

    def delete_role(self, name: str) -> bool:
        if name in BUILTIN_ROLES:
            return False
        with self.storage.get_connection() as conn:
            conn.execute("DELETE FROM rbac_roles WHERE name=?", (name,))
            conn.commit()
        return True

    # ---------------- 部门管理 ----------------

    def list_departments(self) -> list:
        """部门列表（含成员数；成员数由 rbac_users.department 字符串聚合）。"""
        with self.storage.get_connection() as conn:
            rows = conn.execute(
                "SELECT d.name, d.description, d.manager_id, d.created_at, "
                "(SELECT COUNT(*) FROM rbac_users u WHERE u.department = d.name) AS member_count "
                "FROM rbac_departments d ORDER BY d.name"
            ).fetchall()
            managers = {r["id"]: (r["display_name"] or r["name"]) for r in conn.execute(
                "SELECT id, name, display_name FROM rbac_users").fetchall()}
        return [{
            "name": r[0], "description": r[1] or "", "manager_id": r[2],
            "manager_name": managers.get(r[2], "") if r[2] else "",
            "member_count": r[4] or 0, "created_at": r[3],
        } for r in rows]

    def get_department(self, name: str) -> dict | None:
        with self.storage.get_connection() as conn:
            row = conn.execute(
                "SELECT name, description, manager_id, created_at FROM rbac_departments WHERE name=?",
                (name,)
            ).fetchone()
        if not row:
            return None
        return {"name": row[0], "description": row[1] or "", "manager_id": row[2],
                "created_at": row[3]}

    def create_department(self, name: str, description: str = "",
                          manager_id: int = None) -> bool:
        now = datetime.now().isoformat()
        with self.storage.get_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO rbac_departments (name, description, manager_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, description, manager_id, now, now)
            )
            conn.commit()
        return True

    def update_department(self, name: str, description: str = None,
                          manager_id: int = None, new_name: str = None) -> bool:
        now = datetime.now().isoformat()
        sets = ["updated_at=?"]
        vals: list = [now]
        if description is not None:
            sets.append("description=?")
            vals.append(description)
        if manager_id is not None:
            sets.append("manager_id=?")
            vals.append(manager_id)
        if new_name and new_name != name:
            sets.append("name=?")
            vals.append(new_name)
        vals.append(name)
        with self.storage.get_connection() as conn:
            conn.execute(f"UPDATE rbac_departments SET {', '.join(sets)} WHERE name=?", vals)
            if new_name and new_name != name:
                # 部门改名同步成员归属，避免出现孤儿部门字符串
                conn.execute("UPDATE rbac_users SET department=? WHERE department=?", (new_name, name))
            conn.commit()
        return True

    def delete_department(self, name: str) -> tuple[bool, str]:
        """删除部门。仍有成员时拒绝（返回 (False, 原因)），避免成员悬空。"""
        with self.storage.get_connection() as conn:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM rbac_users WHERE department=?", (name,)
            ).fetchone()[0]
            if cnt:
                return False, f"部门下仍有 {cnt} 名成员，请先调整成员部门"
            conn.execute("DELETE FROM rbac_departments WHERE name=?", (name,))
            conn.commit()
        return True, ""

    def list_user_ids_by_department(self, department: str) -> list:
        if not department:
            return []
        with self.storage.get_connection() as conn:
            rows = conn.execute(
                "SELECT id FROM rbac_users WHERE department=?", (department,)
            ).fetchall()
        return [r[0] for r in rows]

    def get_user_department(self, user_id: int) -> str:
        with self.storage.get_connection() as conn:
            row = conn.execute(
                "SELECT department FROM rbac_users WHERE id=?", (user_id,)
            ).fetchone()
        return (row[0] or "") if row else ""

    def list_user_identities(self, user_id: int) -> list:
        with self.storage.get_connection() as conn:
            rows = conn.execute(
                "SELECT id, platform, platform_uid, created_at FROM rbac_user_identities WHERE user_id=?",
                (user_id,)
            ).fetchall()
        return [{"id": r[0], "platform": r[1], "platform_uid": r[2], "created_at": r[3]} for r in rows]

    def unbind_identity(self, identity_id: int) -> bool:
        with self.storage.get_connection() as conn:
            conn.execute("DELETE FROM rbac_user_identities WHERE id=?", (identity_id,))
            conn.commit()
        return True
