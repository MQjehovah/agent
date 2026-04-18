import os
import re
import logging
from datetime import datetime, timedelta
from typing import List, Optional

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
            if date_str <= threshold_str:
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
        """提取有价值的内容 — 保留所有分类，只排除空段落"""
        lines = content.split("\n")
        valuable = []
        current_section_has_content = False

        for line in lines:
            if line.startswith("# ") and not line.startswith("## "):
                # 顶级标题，保留
                valuable.append(line)
                current_section_has_content = False
            elif line.startswith("## "):
                # 二级分类标题，保留
                valuable.append(line)
                current_section_has_content = False
            elif line.strip():
                valuable.append(line)
                current_section_has_content = True

        # 移除没有内容的空段落（标题后无实质内容）
        result = []
        i = 0
        while i < len(valuable):
            line = valuable[i]
            if line.startswith("## ") and i + 1 < len(valuable) and valuable[i + 1].startswith("## "):
                # 空段落：分类标题后紧跟下一个分类标题
                pass  # skip empty section
            else:
                result.append(line)
            i += 1

        return "\n".join(result) if result else ""
    
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
    
    def cleanup_old_files(self, retention_days: int = 7) -> int:
        """清理超过保留天数的 sessions/ 和 daily/ 文件"""
        threshold = datetime.now() - timedelta(days=retention_days)
        deleted = 0

        for subdir in ["sessions", "daily"]:
            dir_path = os.path.join(self.memory_dir, subdir)
            if not os.path.exists(dir_path):
                continue

            for filename in os.listdir(dir_path):
                if not filename.endswith(".md"):
                    continue

                filepath = os.path.join(dir_path, filename)
                # 从文件名解析日期（格式：YYYY-MM-DD.md）
                date_str = filename.replace(".md", "")
                try:
                    file_date = datetime.strptime(date_str, "%Y-%m-%d")
                except ValueError:
                    # 非日期格式文件，按修改时间判断
                    file_date = datetime.fromtimestamp(os.path.getmtime(filepath))

                if file_date < threshold:
                    os.remove(filepath)
                    deleted += 1

        if deleted > 0:
            logger.info(f"Cleaned up {deleted} old files (older than {retention_days} days)")

        return deleted

    async def score_and_prune(self, long_term_file: str, llm_client) -> int:
        if not os.path.exists(long_term_file):
            return 0

        with open(long_term_file, "r", encoding="utf-8") as f:
            content = f.read()

        if len(content) < 200:
            return 0

        lines = content.split("\n")
        entries = []
        current_section = ""
        current_entry_lines = []

        for line in lines:
            if line.startswith("# ") and not line.startswith("## "):
                if current_entry_lines:
                    entries.append((current_section, "\n".join(current_entry_lines)))
                current_section = line
                current_entry_lines = [line]
            elif line.startswith("## "):
                if current_entry_lines:
                    entries.append((current_section, "\n".join(current_entry_lines)))
                current_section = line
                current_entry_lines = [line]
            else:
                current_entry_lines.append(line)

        if current_entry_lines:
            entries.append((current_section, "\n".join(current_entry_lines)))

        if len(entries) <= 3:
            return 0

        # 保护最近24小时内的条目（含当天日期的条目不删除）
        from datetime import date
        today_str = date.today().strftime("%Y-%m-%d")
        protected_indices = set()
        for i, (_, text) in enumerate(entries):
            if today_str in text:
                protected_indices.add(i)

        numbered = ""
        for i, (section, text) in enumerate(entries):
            preview = text[:80].replace("\n", " ")
            numbered += f"[{i}] {preview}\n"

        prompt = (
            f"对以下 {len(entries)} 条记忆逐条评分(1-5分):\n"
            f"5=关键不可删(用户偏好/核心知识/重要纠正)\n"
            f"3=一般(普通事实/可能过时的待办)\n"
            f"1=可删除(已完成待办/重复/过时/低价值闲聊)\n\n"
            f"记忆条目:\n{numbered}\n"
            f"只输出: 编号=分数, 如 0=5 1=3 2=1"
        )

        try:
            response = await llm_client.chat(
                messages=[
                    {"role": "system", "content": "你是记忆质量评估助手。严格评分，低价值条目给低分。"},
                    {"role": "user", "content": prompt}
                ],
                tools=None, stream=False, use_cache=False
            )
            text = response.choices[0].message.content or ""

            scores = {}
            for match in re.finditer(r'(\d+)\s*=\s*(\d)', text):
                idx, score = int(match.group(1)), int(match.group(2))
                if 0 <= idx < len(entries) and 1 <= score <= 5:
                    scores[idx] = score

            if not scores:
                return 0

            # 保护最近条目：最近条目最低3分
            pruned = []
            for i in range(len(entries)):
                final_score = scores.get(i, 3)
                if i in protected_indices:
                    final_score = max(final_score, 3)
                if final_score >= 2:
                    pruned.append(entries[i])

            pruned_count = len(entries) - len(pruned)
            if pruned_count == 0:
                return 0

            # 备份后再写入
            backup_path = long_term_file + ".bak"
            with open(backup_path, "w", encoding="utf-8") as f:
                f.write(content)

            new_content = "\n".join(text for _, text in pruned)
            with open(long_term_file, "w", encoding="utf-8") as f:
                f.write(new_content)
                f.flush()
                os.fsync(f.fileno())

            logger.info(f"记忆质量淘汰: 共{len(entries)}条, 删除{pruned_count}条低价值条目")
            return pruned_count

        except Exception as e:
            logger.warning(f"记忆质量淘汰失败: {e}")
            return 0