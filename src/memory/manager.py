import os
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime
from dataclasses import dataclass, field

logger = logging.getLogger("agent.memory")


@dataclass
class SessionMemory:
    user_preferences: List[str] = field(default_factory=list)
    key_info: List[str] = field(default_factory=list)
    todos: List[str] = field(default_factory=list)
    summary: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


class MemoryManager:
    def __init__(self, workspace: str):
        self.workspace = workspace
        self.memory_dir = os.path.join(workspace, "memory")
        self.sessions_dir = os.path.join(self.memory_dir, "sessions")
        self.daily_dir = os.path.join(self.memory_dir, "daily")
        self.long_term_file = os.path.join(self.memory_dir, "memory.md")
        
        self.session_memory: Optional[SessionMemory] = None
        
        self._ensure_dirs()
    
    def _ensure_dirs(self):
        os.makedirs(self.sessions_dir, exist_ok=True)
        os.makedirs(self.daily_dir, exist_ok=True)
        logger.debug(f"Memory directories initialized at {self.memory_dir}")
    
    def start_session(self):
        self.session_memory = SessionMemory()
        logger.info("Session memory started")
    
    def add_preference(self, preference: str):
        if self.session_memory:
            self.session_memory.user_preferences.append(preference)
    
    def add_key_info(self, info: str):
        if self.session_memory:
            self.session_memory.key_info.append(info)
    
    def add_todo(self, todo: str):
        if self.session_memory:
            self.session_memory.todos.append(todo)
    
    def set_summary(self, summary: str):
        if self.session_memory:
            self.session_memory.summary = summary