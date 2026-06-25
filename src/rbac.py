import json
import logging
from datetime import datetime

logger = logging.getLogger("agent.rbac")


class RBACManager:
    def __init__(self, storage):
        self.storage = storage

    def get_user_role(self, platform: str, platform_uid: str) -> str:
        info = self.resolve_user(platform, platform_uid)
        return info["role"]

    def resolve_user(self, platform: str, platform_uid: str, fallback_name: str = "") -> dict:
        if platform == "cli":
            return {"user_id": None, "user_name": fallback_name or "管理员", "role": "admin"}
        with self.storage.get_connection() as conn:
            row = conn.execute(
                """SELECT u.id, u.name, u.role, u.status FROM rbac_user_identities i
                   JOIN rbac_users u ON i.user_id = u.id
                   WHERE i.platform = ? AND i.platform_uid = ?""",
                (platform, platform_uid)
            ).fetchone()
        if row and row[3] != "disabled":
            return {"user_id": row[0], "user_name": row[1], "role": row[2]}
        return {"user_id": None, "user_name": fallback_name, "role": "default"}

    def check_tool(self, role: str, tool_name: str) -> bool:
        allowed = self._get_allowed(role, "allowed_tools")
        return "*" in allowed or tool_name in allowed

    def check_agent(self, role: str, agent_name: str) -> bool:
        allowed = self._get_allowed(role, "allowed_agents")
        return "*" in allowed or agent_name in allowed

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
                    allowed_tools: list = None, allowed_agents: list = None) -> bool:
        now = datetime.now().isoformat()
        with self.storage.get_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO rbac_roles (name, description, allowed_tools, allowed_agents, created_at) VALUES (?, ?, ?, ?, ?)",
                (name, description, json.dumps(allowed_tools or []), json.dumps(allowed_agents or []), now)
            )
            conn.commit()
        return True

    def create_user(self, name: str, department: str = "", role: str = "default") -> int:
        now = datetime.now().isoformat()
        with self.storage.get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO rbac_users (name, department, role, status, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?)",
                (name, department, role, now, now)
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
                "SELECT id, name, department, role, status, created_at, updated_at FROM rbac_users ORDER BY id"
            ).fetchall()
        result = []
        for r in rows:
            result.append({
                "id": r[0], "name": r[1], "department": r[2],
                "role": r[3], "status": r[4], "created_at": r[5], "updated_at": r[6]
            })
        return result

    def list_users_with_password_flag(self) -> list:
        with self.storage.get_connection() as conn:
            rows = conn.execute(
                "SELECT id, name, department, role, status, created_at, updated_at, "
                "CASE WHEN password_hash IS NOT NULL AND password_hash != '' THEN 1 ELSE 0 END AS has_pw "
                "FROM rbac_users ORDER BY id"
            ).fetchall()
        result = []
        for r in rows:
            result.append({
                "id": r[0], "name": r[1], "department": r[2],
                "role": r[3], "status": r[4], "created_at": r[5], "updated_at": r[6],
                "has_password": bool(r[7])
            })
        return result

    def get_user(self, user_id: int) -> dict | None:
        with self.storage.get_connection() as conn:
            row = conn.execute(
                "SELECT id, name, department, role, status, created_at, updated_at FROM rbac_users WHERE id=?",
                (user_id,)
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "name": row[1], "department": row[2],
            "role": row[3], "status": row[4], "created_at": row[5], "updated_at": row[6]
        }

    def get_user_with_password_flag(self, user_id: int) -> dict | None:
        with self.storage.get_connection() as conn:
            row = conn.execute(
                "SELECT id, name, department, role, status, created_at, updated_at, "
                "CASE WHEN password_hash IS NOT NULL AND password_hash != '' THEN 1 ELSE 0 END AS has_pw "
                "FROM rbac_users WHERE id=?", (user_id,)
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "name": row[1], "department": row[2],
            "role": row[3], "status": row[4], "created_at": row[5], "updated_at": row[6],
            "has_password": bool(row[7])
        }

    def update_user(self, user_id: int, name: str = None, department: str = None, role: str = None) -> bool:
        now = datetime.now().isoformat()
        sets = ["updated_at=?"]
        vals = [now]
        if name is not None:
            sets.append("name=?")
            vals.append(name)
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
                "SELECT name, description, allowed_tools, allowed_agents, created_at FROM rbac_roles ORDER BY name"
            ).fetchall()
        result = []
        for r in rows:
            result.append({
                "name": r[0], "description": r[1],
                "allowed_tools": json.loads(r[2]) if r[2] else [],
                "allowed_agents": json.loads(r[3]) if r[3] else [],
                "created_at": r[4]
            })
        return result

    def get_role(self, name: str) -> dict | None:
        with self.storage.get_connection() as conn:
            row = conn.execute(
                "SELECT name, description, allowed_tools, allowed_agents, created_at FROM rbac_roles WHERE name=?",
                (name,)
            ).fetchone()
        if not row:
            return None
        return {
            "name": row[0], "description": row[1],
            "allowed_tools": json.loads(row[2]) if row[2] else [],
            "allowed_agents": json.loads(row[3]) if row[3] else [],
            "created_at": row[4]
        }

    def update_role(self, name: str, description: str = None,
                    allowed_tools: list = None, allowed_agents: list = None) -> bool:
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
        if not sets:
            return True
        vals.append(name)
        with self.storage.get_connection() as conn:
            conn.execute(f"UPDATE rbac_roles SET {', '.join(sets)} WHERE name=?", vals)
            conn.commit()
        return True

    def delete_role(self, name: str) -> bool:
        if name in ("default", "admin"):
            return False
        with self.storage.get_connection() as conn:
            conn.execute("DELETE FROM rbac_roles WHERE name=?", (name,))
            conn.commit()
        return True

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
