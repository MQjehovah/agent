import os
import json
import logging
from typing import List, Dict, Any, Optional, Sequence
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("agent.session_store")


class SessionStore:
    def __init__(self, workspace: str):
        self.workspace = workspace
        self.sessions_dir = os.path.join(workspace, "sessions")
        self._ensure_dir()
    
    def _ensure_dir(self):
        Path(self.sessions_dir).mkdir(parents=True, exist_ok=True)
    
    def save(self, session_id: str, messages: Sequence[Any]) -> str:
        filepath = os.path.join(self.sessions_dir, f"{session_id}.json")
        data = {
            "session_id": session_id,
            "messages": [dict(m) for m in messages],
            "saved_at": datetime.now().isoformat()
        }
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.debug(f"Session [{session_id}] saved: {len(messages)} messages")
        return filepath
    
    def load(self, session_id: str) -> List[Dict[str, Any]]:
        filepath = os.path.join(self.sessions_dir, f"{session_id}.json")
        if not os.path.exists(filepath):
            return []
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("messages", [])
        except Exception as e:
            logger.error(f"Failed to load session [{session_id}]: {e}")
            return []
    
    def list_sessions(self) -> List[str]:
        if not os.path.exists(self.sessions_dir):
            return []
        return [f.replace(".json", "") for f in os.listdir(self.sessions_dir) 
                if f.endswith(".json")]
    
    def delete(self, session_id: str) -> bool:
        filepath = os.path.join(self.sessions_dir, f"{session_id}.json")
        if os.path.exists(filepath):
            os.remove(filepath)
            logger.debug(f"Session [{session_id}] deleted")
            return True
        return False
    
    def append_message(self, session_id: str, message: Dict[str, Any]) -> str:
        messages = self.load(session_id)
        messages.append(message)
        return self.save(session_id, messages)