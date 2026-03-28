import sqlite3
import json
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("agent.storage")

_storage_instance: Optional["Storage"] = None


def get_storage() -> Optional["Storage"]:
    return _storage_instance


def init_storage(workspace: str) -> "Storage":
    global _storage_instance
    _storage_instance = Storage(workspace)
    return _storage_instance


class Storage:
    def __init__(self, workspace: str):
        self.workspace = workspace
        self.db_path = Path(workspace) / "data.db"
        self._init_db()
    
    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    agent_name TEXT,
                    role TEXT,
                    content TEXT,
                    tool_calls TEXT,
                    tool_call_id TEXT,
                    name TEXT,
                    created_at TEXT
                );
                
                CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
                CREATE INDEX IF NOT EXISTS idx_messages_agent ON messages(agent_name);
                CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
            """)
    
    def save_message(self, session_id: str, role: str, content: str, 
                     agent_name: str = "", tool_calls: Optional[List] = None, 
                     tool_call_id: str = "", name: str = ""):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO messages (session_id, agent_name, role, content, tool_calls, tool_call_id, name, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (session_id, agent_name, role, content or "",
                  json.dumps(tool_calls) if tool_calls else None,
                  tool_call_id, name, datetime.now().isoformat()))
    
    def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT role, content, tool_calls, tool_call_id, name
                FROM messages WHERE session_id = ? ORDER BY id
            """, (session_id,)).fetchall()
        
        messages = []
        for row in rows:
            msg = {"role": row["role"], "content": row["content"] or ""}
            if row["tool_calls"]:
                msg["tool_calls"] = json.loads(row["tool_calls"])
            if row["tool_call_id"]:
                msg["tool_call_id"] = row["tool_call_id"]
            if row["name"]:
                msg["name"] = row["name"]
            messages.append(msg)
        return messages
    
    def get_messages_by_date(self, date_str: str, agent_name: Optional[str] = None) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if agent_name:
                rows = conn.execute("""
                    SELECT session_id, role, content, tool_calls, tool_call_id, name
                    FROM messages
                    WHERE DATE(created_at) = ? AND agent_name = ?
                    ORDER BY session_id, id
                """, (date_str, agent_name)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT session_id, role, content, tool_calls, tool_call_id, name
                    FROM messages
                    WHERE DATE(created_at) = ?
                    ORDER BY session_id, id
                """, (date_str,)).fetchall()
        
        messages = []
        for row in rows:
            msg = {"session_id": row["session_id"], "role": row["role"], "content": row["content"] or ""}
            if row["tool_calls"]:
                msg["tool_calls"] = json.loads(row["tool_calls"])
            if row["tool_call_id"]:
                msg["tool_call_id"] = row["tool_call_id"]
            if row["name"]:
                msg["name"] = row["name"]
            messages.append(msg)
        return messages