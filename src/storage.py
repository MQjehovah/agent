import sqlite3
import json
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("agent.storage")


class Storage:
    def __init__(self, workspace: str):
        self.workspace = workspace
        self.db_path = Path(workspace) / "data.db"
        self._init_db()
    
    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    description TEXT,
                    created_at TEXT
                );
                
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    FOREIGN KEY (agent_id) REFERENCES agents(id)
                );
                
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    role TEXT,
                    content TEXT,
                    tool_calls TEXT,
                    tool_call_id TEXT,
                    name TEXT,
                    created_at TEXT,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                );
                
                CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
                CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
                CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at);
            """)
    
    def register_agent(self, agent_id: str, name: str, description: str = ""):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO agents (id, name, description, created_at)
                VALUES (?, ?, ?, ?)
            """, (agent_id, name, description, datetime.now().isoformat()))
    
    def create_session(self, session_id: str, agent_id: str) -> str:
        now = datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO sessions (id, agent_id, created_at, updated_at)
                VALUES (?, ?, ?, ?)
            """, (session_id, agent_id, now, now))
        return session_id
    
    def save_message(self, session_id: str, role: str, content: str, 
                     tool_calls: List = None, tool_call_id: str = None, name: str = None):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO messages (session_id, role, content, tool_calls, tool_call_id, name, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (session_id, role, content, 
                  json.dumps(tool_calls) if tool_calls else None,
                  tool_call_id, name, datetime.now().isoformat()))
            conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", 
                        (datetime.now().isoformat(), session_id))
    
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
    
    def list_sessions(self, agent_id: str = None) -> List[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if agent_id:
                rows = conn.execute("""
                    SELECT id, agent_id, created_at, updated_at
                    FROM sessions WHERE agent_id = ? ORDER BY updated_at DESC
                """, (agent_id,)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT id, agent_id, created_at, updated_at
                    FROM sessions ORDER BY updated_at DESC
                """).fetchall()
        return [dict(row) for row in rows]
    
    def get_sessions_by_date(self, date_str: str) -> List[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT s.id, s.agent_id, s.created_at, s.updated_at
                FROM sessions s
                WHERE DATE(s.created_at) = ?
                ORDER BY s.created_at
            """, (date_str,)).fetchall()
        return [dict(row) for row in rows]
    
    def get_messages_by_date(self, date_str: str) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT m.session_id, m.role, m.content, m.tool_calls, m.tool_call_id, m.name
                FROM messages m
                WHERE DATE(m.created_at) = ?
                ORDER BY m.session_id, m.id
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
    
    def delete_session(self, session_id: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))