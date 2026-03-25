import os
import logging
from datetime import datetime, timedelta
from typing import List

logger = logging.getLogger("agent.memory")


class MemoryArchiver:
    def __init__(self, memory_dir: str, llm_client=None):
        self.memory_dir = memory_dir
        self.daily_dir = os.path.join(memory_dir, "daily")
        self.long_term_file = os.path.join(memory_dir, "memory.md")
        self.llm_client = llm_client
    
    def archive_daily_to_long_term(self, days_threshold: int = 1) -> bool:
        if not os.path.exists(self.daily_dir):
            logger.warning("Daily memory directory not found")
            return False
        
        files_to_archive = self._get_files_to_archive(days_threshold)
        if not files_to_archive:
            logger.info("No files to archive")
            return True
        
        for daily_file in files_to_archive:
            self._archive_single_file(daily_file)
        
        logger.info(f"Archived {len(files_to_archive)} daily memories")
        return True
    
    def _get_files_to_archive(self, days_threshold: int) -> List[str]:
        threshold_date = datetime.now() - timedelta(days=days_threshold)
        threshold_str = threshold_date.strftime("%Y-%m-%d")
        
        files = []
        for filename in os.listdir(self.daily_dir):
            if not filename.endswith(".md"):
                continue
            date_str = filename.replace(".md", "")
            if date_str < threshold_str:
                files.append(os.path.join(self.daily_dir, filename))
        
        return sorted(files)
    
    def _archive_single_file(self, daily_file: str) -> bool:
        with open(daily_file, "r", encoding="utf-8") as f:
            content = f.read()
        
        valuable_content = self._extract_valuable_content(content)
        if not valuable_content:
            return False
        
        existing = ""
        if os.path.exists(self.long_term_file):
            with open(self.long_term_file, "r", encoding="utf-8") as f:
                existing = f.read()
        
        updated = self._merge_to_long_term(existing, valuable_content)
        
        with open(self.long_term_file, "w", encoding="utf-8") as f:
            f.write(updated)
        
        logger.info(f"Archived {daily_file} to long-term memory")
        return True
    
    def _extract_valuable_content(self, content: str) -> str:
        lines = content.split("\n")
        valuable = []
        
        for line in lines:
            if line.startswith("## ") and "洞察" not in line and "偏好" not in line:
                valuable.append(line)
            elif valuable and not line.startswith("# "):
                if line.strip() and not line.startswith("## "):
                    valuable.append(line)
                elif line.startswith("## "):
                    if "洞察" not in line and "偏好" not in line:
                        valuable.append(line)
                    else:
                        break
        
        return "\n".join(valuable) if valuable else ""
    
    def _merge_to_long_term(self, existing: str, new_content: str) -> str:
        if not existing:
            return f"# 长期记忆\n\n{new_content}"
        
        lines = existing.split("\n")
        result = lines.copy()
        
        new_lines = new_content.split("\n")
        for line in new_lines:
            if line.strip() and line not in result:
                result.append(line)
        
        return "\n".join(result)
    
    def cleanup_old_sessions(self, retention_days: int = 7) -> int:
        sessions_dir = os.path.join(self.memory_dir, "sessions")
        if not os.path.exists(sessions_dir):
            return 0
        
        threshold = datetime.now() - timedelta(days=retention_days)
        deleted = 0
        
        for filename in os.listdir(sessions_dir):
            if not filename.endswith(".md"):
                continue
            
            filepath = os.path.join(sessions_dir, filename)
            mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
            
            if mtime < threshold:
                os.remove(filepath)
                deleted += 1
        
        if deleted > 0:
            logger.info(f"Cleaned up {deleted} old session files")
        
        return deleted