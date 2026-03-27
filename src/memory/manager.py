import os
import uuid
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from dataclasses import dataclass, field

logger = logging.getLogger("agent.memory")


@dataclass
class SessionMemory:
    session_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    user_preferences: List[str] = field(default_factory=list)
    key_info: List[str] = field(default_factory=list)
    todos: List[str] = field(default_factory=list)
    summaries: List[Dict[str, str]] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())


class MemoryManager:
    def __init__(self, workspace: str):
        self.workspace = workspace
        self.memory_dir = os.path.join(workspace, "memory")
        self.sessions_dir = os.path.join(self.memory_dir, "sessions")
        self.daily_dir = os.path.join(self.memory_dir, "daily")
        self.long_term_file = os.path.join(self.memory_dir, "memory.md")
        
        self.session_memory: Optional[SessionMemory] = None
        self.current_session_id: Optional[str] = None
        
        self._ensure_dirs()
    
    def _ensure_dirs(self):
        os.makedirs(self.sessions_dir, exist_ok=True)
        os.makedirs(self.daily_dir, exist_ok=True)
    
    def start_session(self, session_id: str = None) -> str:
        if session_id:
            self.current_session_id = session_id
            existing = self._load_session_file(session_id)
            if existing:
                self.session_memory = existing
                logger.info(f"Session [{session_id}] resumed")
                return session_id
        
        self.current_session_id = session_id or str(uuid.uuid4())[:8]
        self.session_memory = SessionMemory(session_id=self.current_session_id)
        logger.info(f"Session [{self.current_session_id}] started")
        return self.current_session_id
    
    def add_preference(self, preference: str):
        if self.session_memory:
            if preference not in self.session_memory.user_preferences:
                self.session_memory.user_preferences.append(preference)
    
    def add_key_info(self, info: str):
        if self.session_memory:
            if info not in self.session_memory.key_info:
                self.session_memory.key_info.append(info)
    
    def add_todo(self, todo: str):
        if self.session_memory:
            if todo not in self.session_memory.todos:
                self.session_memory.todos.append(todo)
    
    def add_summary(self, task: str, result: str):
        if self.session_memory:
            self.session_memory.summaries.append({
                "time": datetime.now().strftime("%H:%M"),
                "task": task,
                "result": result
            })
            self.session_memory.updated_at = datetime.now().isoformat()
    
    def _load_session_file(self, session_id: str) -> Optional[SessionMemory]:
        filepath = os.path.join(self.sessions_dir, f"{session_id}.md")
        if not os.path.exists(filepath):
            return None
        
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            
            session = SessionMemory(session_id=session_id)
            lines = content.split("\n")
            current_section = None
            current_summary = {}
            
            for line in lines:
                if line.startswith("## 用户偏好"):
                    current_section = "preferences"
                elif line.startswith("## 关键信息"):
                    current_section = "key_info"
                elif line.startswith("## 待办事项"):
                    current_section = "todos"
                elif line.startswith("## 对话记录"):
                    current_section = "summaries"
                elif line.startswith("## "):
                    if current_section == "summary_content" and current_summary:
                        session.summaries.append(current_summary)
                        current_summary = {}
                    current_section = None
                elif line.startswith("### [") and current_section == "summaries":
                    if current_summary:
                        session.summaries.append(current_summary)
                    import re
                    match = re.match(r"### \[(\d+:\d+)\]\s*(.*)", line)
                    if match:
                        current_summary = {
                            "time": match.group(1),
                            "task": match.group(2),
                            "result": ""
                        }
                        current_section = "summary_content"
                elif current_section == "summary_content" and line.strip():
                    if current_summary.get("result"):
                        current_summary["result"] += "\n" + line
                    else:
                        current_summary["result"] = line
                elif line.startswith("- ") and current_section in ["preferences", "key_info", "todos"]:
                    item = line[2:].strip()
                    if current_section == "preferences" and item not in session.user_preferences:
                        session.user_preferences.append(item)
                    elif current_section == "key_info" and item not in session.key_info:
                        session.key_info.append(item)
                    elif current_section == "todos" and item not in session.todos:
                        session.todos.append(item)
            
            if current_summary:
                session.summaries.append(current_summary)
            
            return session
        except Exception as e:
            logger.error(f"Failed to load session file: {e}")
            return None
    
    def save_session(self) -> Optional[str]:
        if not self.session_memory:
            logger.warning("No session memory to save")
            return None
        
        if not self.current_session_id:
            logger.warning("No session_id set")
            return None
        
        filename = f"{self.current_session_id}.md"
        filepath = os.path.join(self.sessions_dir, filename)
        
        content = self._format_session_content()
        
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        
        logger.info(f"Session [{self.current_session_id}] saved")
        return filepath
    
    def _format_session_content(self) -> str:
        if not self.session_memory:
            return ""
        
        lines = [
            f"# 会话记录 - {self.current_session_id}",
            f"",
            f"创建时间: {self.session_memory.created_at}",
            f"更新时间: {self.session_memory.updated_at}",
            f""
        ]
        
        if self.session_memory.user_preferences:
            lines.append("## 用户偏好")
            for p in self.session_memory.user_preferences:
                lines.append(f"- {p}")
            lines.append("")
        
        if self.session_memory.key_info:
            lines.append("## 关键信息")
            for info in self.session_memory.key_info:
                lines.append(f"- {info}")
            lines.append("")
        
        if self.session_memory.todos:
            lines.append("## 待办事项")
            for todo in self.session_memory.todos:
                lines.append(f"- {todo}")
            lines.append("")
        
        if self.session_memory.summaries:
            lines.append("## 对话记录")
            for s in self.session_memory.summaries:
                lines.append(f"")
                lines.append(f"### [{s.get('time', '')}] {s.get('task', '')}")
                lines.append(s.get('result', ''))
            lines.append("")
        
        return "\n".join(lines)
    
    def load_memory(self, task: str = "") -> str:
        parts = []
        
        long_term = self._load_long_term(task)
        if long_term:
            parts.append(f"【长期记忆】\n{long_term}")
        
        daily = self._load_recent_daily(days=3)
        if daily:
            parts.append(f"【近期记忆】\n{daily}")
        
        sessions = self._load_recent_sessions(count=5)
        if sessions:
            parts.append(f"【会话历史】\n{sessions}")
        
        current = self._load_current_session()
        if current:
            parts.append(f"【当前会话】\n{current}")
        
        if parts:
            return "\n\n".join(parts)
        return ""
    
    def _load_current_session(self) -> str:
        if not self.session_memory or not self.session_memory.summaries:
            return ""
        
        lines = []
        for s in self.session_memory.summaries[-3:]:
            lines.append(f"- [{s.get('time', '')}] {s.get('task', '')[:30]}")
        return "\n".join(lines)
    
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
    
    def _load_recent_sessions(self, count: int = 5) -> str:
        if not os.path.exists(self.sessions_dir):
            return ""
        
        files = sorted(
            [f for f in os.listdir(self.sessions_dir) if f.endswith(".md")],
            key=lambda x: os.path.getmtime(os.path.join(self.sessions_dir, x)),
            reverse=True
        )[:count]
        
        contents = []
        for filename in files:
            filepath = os.path.join(self.sessions_dir, filename)
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            summary = self._extract_summary(content)
            session_id = filename.replace(".md", "")
            contents.append(f"### {session_id}\n{summary}")
        
        return "\n\n".join(contents)
    
    def _extract_summary(self, content: str) -> str:
        lines = content.split("\n")
        summary_lines = []
        in_summaries = False
        
        for line in lines:
            if line.startswith("## 对话记录"):
                in_summaries = True
                continue
            if in_summaries and line.startswith("## "):
                break
            if in_summaries and line.startswith("### "):
                if len(summary_lines) < 5:
                    summary_lines.append(line)
        
        return "\n".join(summary_lines) if summary_lines else content[:200]
    
    def save_session_and_extract(self, llm_client=None) -> Optional[str]:
        filepath = self.save_session()
        if not filepath:
            return None
        
        self.extract_daily(llm_client, filepath)
        return filepath
    
    def extract_daily(self, llm_client=None, filepath: str = None) -> bool:
        if not filepath:
            if not self.current_session_id:
                return False
            filepath = os.path.join(self.sessions_dir, f"{self.current_session_id}.md")
        
        if not os.path.exists(filepath):
            return False
        
        from .extractor import MemoryExtractor
        extractor = MemoryExtractor(llm_client)
        
        with open(filepath, "r", encoding="utf-8") as f:
            session_content = f.read()
        
        today = datetime.now().strftime("%Y-%m-%d.md")
        daily_file = os.path.join(self.daily_dir, today)
        
        return extractor.extract_to_daily(session_content, daily_file)
    
    def end_session(self):
        if self.session_memory:
            self.save_session()
            self.session_memory = None
            self.current_session_id = None
            logger.info("Session ended")