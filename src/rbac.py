import json
import logging
from datetime import datetime

logger = logging.getLogger("agent.rbac")


class RBACManager:
    def __init__(self, storage):
        self.storage = storage

    def get_user_role(self, platform: str, platform_uid: str) -> str:
        with self.storage.get_connection() as conn:
            row = conn.execute(
                """SELECT u.role, u.status FROM rbac_user_identities i
                   JOIN rbac_users u ON i.user_id = u.id
                   WHERE i.platform = ? AND i.platform_uid = ?""",
                (platform, platform_uid)
            ).fetchone()
        if not row or row[1] == "disabled":
            return "default"
        return row[0]

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
