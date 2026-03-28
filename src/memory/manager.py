import os
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("agent.memory")


class MemoryManager:
    def __init__(self, workspace: str, storage=None, llm_client=None, agent_name: str = ""):
        self.agent_name = agent_name
        self.workspace = workspace
        self.storage = storage
        self.llm_client = llm_client
        self.memory_dir = os.path.join(workspace, "memory")
        self.agent_memory_dir = os.path.join(self.memory_dir, "agents", agent_name) if agent_name else self.memory_dir
        self.daily_dir = os.path.join(self.agent_memory_dir, "daily")
        self.long_term_file = os.path.join(self.agent_memory_dir, "memory.md")
        self._daily_task = None
        
        self._ensure_dirs()
    
    def _ensure_dirs(self):
        os.makedirs(self.memory_dir, exist_ok=True)
        os.makedirs(self.daily_dir, exist_ok=True)
    
    def start_daily_task(self):
        if self._daily_task:
            return
        self._daily_task = asyncio.create_task(self._daily_extract_loop())
        logger.info("每日记忆提取任务已启动")
    
    def stop_daily_task(self):
        if self._daily_task:
            self._daily_task.cancel()
            self._daily_task = None
            logger.info("每日记忆提取任务已停止")
    
    async def _daily_extract_loop(self):
        while True:
            now = datetime.now()
            tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            seconds_until_midnight = (tomorrow - now).total_seconds()
            
            logger.debug(f"下次记忆提取: {tomorrow} ({seconds_until_midnight:.0f}秒后)")
            
            await asyncio.sleep(seconds_until_midnight)
            
            try:
                logger.info("开始每日记忆提取...")
                if self.extract_daily():
                    logger.info("每日记忆提取完成")
                else:
                    logger.debug("无需提取记忆")
            except Exception as e:
                logger.error(f"每日记忆提取失败: {e}")
    
    def _append_to_memory(self, category: str, content: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        
        if not os.path.exists(self.long_term_file):
            with open(self.long_term_file, "w", encoding="utf-8") as f:
                f.write(f"# 长期记忆\n\n## {category}\n\n- [{timestamp}] {content}\n")
            return
        
        with open(self.long_term_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        
        category_header = f"## {category}\n"
        category_idx = -1
        for i, line in enumerate(lines):
            if line.strip() == category_header.strip():
                category_idx = i
                break
        
        if category_idx == -1:
            lines.append(f"\n## {category}\n\n- [{timestamp}] {content}\n")
        else:
            insert_idx = category_idx + 1
            while insert_idx < len(lines) and not lines[insert_idx].startswith("## "):
                insert_idx += 1
            lines.insert(insert_idx, f"- [{timestamp}] {content}\n")
        
        with open(self.long_term_file, "w", encoding="utf-8") as f:
            f.writelines(lines)
        
        logger.debug(f"Memory saved: [{category}] {content}")
    
    def add_preference(self, preference: str):
        self._append_to_memory("用户偏好", preference)
    
    def add_key_info(self, info: str):
        self._append_to_memory("关键信息", info)
    
    def add_todo(self, todo: str):
        self._append_to_memory("待办事项", todo)
    
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
    
    def extract_daily(self, date_str: str = None) -> bool:
        if not self.storage:
            return False
        
        if not date_str:
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            date_str = yesterday
        
        daily_file = os.path.join(self.daily_dir, f"{date_str}.md")
        
        messages = self.storage.get_messages_by_date(date_str, agent_id=self.agent_name)
        if not messages:
            logger.debug(f"No messages found for {date_str}, agent {self.agent_name}")
            return False
        
        from .extractor import MemoryExtractor
        extractor = MemoryExtractor(self.llm_client)
        
        session_text = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user":
                session_text.append(f"用户: {content}")
            elif role == "assistant":
                session_text.append(f"助手: {content[:500]}")
        
        if not session_text:
            return False
        
        return extractor.extract_to_daily("\n".join(session_text), daily_file)
    
    def list_daily_files(self) -> list:
        if not os.path.exists(self.daily_dir):
            return []
        return [f for f in os.listdir(self.daily_dir) if f.endswith(".md")]