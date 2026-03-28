import os
import json
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta

logger = logging.getLogger("agent.memory")


class MemoryManager:
    def __init__(self, workspace: str):
        self.workspace = workspace
        self.memory_dir = os.path.join(workspace, "memory")
        self.daily_dir = os.path.join(self.memory_dir, "daily")
        self.long_term_file = os.path.join(self.memory_dir, "memory.md")
        
        self._ensure_dirs()
    
    def _ensure_dirs(self):
        os.makedirs(self.memory_dir, exist_ok=True)
        os.makedirs(self.daily_dir, exist_ok=True)
    
    def add_preference(self, preference: str):
        pass
    
    def add_key_info(self, info: str):
        pass
    
    def add_todo(self, todo: str):
        pass
    
    def load_memory(self, task: str = "") -> str:
        parts = []
        
        long_term = self._load_long_term(task)
        if long_term:
            parts.append(f"【长期记忆】\n{long_term}")
        
        daily = self._load_recent_daily(days=3)
        if daily:
            parts.append(f"【近期记忆】\n{daily}")
        
        if parts:
            return "\n\n".join(parts)
        return ""
    
    def _load_long_term(self, task: str) -> str:
        if not os.path.exists(self.long_term_file):
            return ""
        
        with open(self.long_term_file, "r", encoding="utf-8") as f:
            content = f.read()
        
        if not task:
            return content
        
        keywords = task.lower().split()
        lines = content.split("\n")
        relevant_lines = []
        current_section = []
        in_relevant_section = False
        
        for line in lines:
            if line.startswith("## "):
                in_relevant_section = any(kw in line.lower() for kw in keywords)
                if in_relevant_section:
                    current_section = [line]
            elif in_relevant_section:
                current_section.append(line)
                if line.startswith("## ") or line.startswith("# "):
                    relevant_lines.extend(current_section[:-1])
                    current_section = [line]
                    in_relevant_section = any(kw in line.lower() for kw in keywords)
        
        if current_section and in_relevant_section:
            relevant_lines.extend(current_section)
        
        return "\n".join(relevant_lines) if relevant_lines else content[:500]
    
    def _load_recent_daily(self, days: int = 3) -> str:
        contents = []
        for i in range(days):
            date = datetime.now() - timedelta(days=i)
            filename = date.strftime("%Y-%m-%d.md")
            filepath = os.path.join(self.daily_dir, filename)
            
            if os.path.exists(filepath):
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
                contents.append(f"### {filename}\n{content}")
        
        return "\n\n".join(contents)
    
    def extract_daily(self, llm_client=None, sessions_dir: str = "") -> bool:
        if not sessions_dir:
            sessions_dir = os.path.join(self.workspace, "sessions")
        
        if not os.path.exists(sessions_dir):
            return False
        
        from .extractor import MemoryExtractor
        extractor = MemoryExtractor(llm_client)
        
        today = datetime.now().strftime("%Y-%m-%d")
        today_session = os.path.join(sessions_dir, f"{today}.json")
        daily_file = os.path.join(self.daily_dir, f"{today}.md")
        
        sessions_content = []
        for f in os.listdir(sessions_dir):
            if f.endswith(".json"):
                filepath = os.path.join(sessions_dir, f)
                try:
                    with open(filepath, "r", encoding="utf-8") as fp:
                        data = json.load(fp)
                    messages = data.get("messages", [])
                    for m in messages:
                        role = m.get("role", "")
                        content = m.get("content", "")
                        if role == "user":
                            sessions_content.append(f"用户: {content}")
                        elif role == "assistant":
                            sessions_content.append(f"助手: {content[:200]}")
                except Exception as e:
                    logger.error(f"Failed to read session {f}: {e}")
        
        if not sessions_content:
            return False
        
        session_text = "\n".join(sessions_content)
        return extractor.extract_to_daily(session_text, daily_file)
    
    def list_daily_files(self) -> List[str]:
        if not os.path.exists(self.daily_dir):
            return []
        return [f for f in os.listdir(self.daily_dir) if f.endswith(".md")]