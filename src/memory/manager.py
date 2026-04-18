import os
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("agent.memory")


class MemoryManager:
    def __init__(self, workspace: str, agent_id: str = ""):
        self.workspace = workspace
        self.agent_id = agent_id
        self.memory_dir = os.path.join(workspace, "memory")
        self.daily_dir = os.path.join(self.memory_dir, "daily")
        self.long_term_file = os.path.join(self.memory_dir, "memory.md")
        self.shared_knowledge_file = os.path.join(self.memory_dir, "shared_knowledge.md")
        self._daily_task = None
        self._llm_client = None
        self._writer = None

        self._ensure_dirs()

    def set_llm_client(self, client):
        """注入 LLM 客户端，用于智能记忆提取"""
        self._llm_client = client
    
    def _get_storage(self):
        from storage import get_storage
        return get_storage()
    
    def _ensure_dirs(self):
        os.makedirs(self.memory_dir, exist_ok=True)
        os.makedirs(self.daily_dir, exist_ok=True)
        if not os.path.exists(self.shared_knowledge_file):
            with open(self.shared_knowledge_file, "w", encoding="utf-8") as f:
                f.write("# 共享知识库\n\n跨代理共享的经验和知识。\n\n")
    
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
            try:
                now = datetime.now()
                tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=30, second=0, microsecond=0)
                seconds_until_midnight = (tomorrow - now).total_seconds()
                
                logger.debug(f"下次记忆提取: {tomorrow} ({seconds_until_midnight:.0f}秒后)")
                try:
                    await asyncio.sleep(seconds_until_midnight)
                except asyncio.CancelledError:
                    logger.info("记忆提取循环被取消")
                    return

                logger.info("开始记忆提取...")
                storage = self._get_storage()
                if storage:
                    agent_ids = storage.get_all_agent_ids()
                    for agent_id in agent_ids:
                        if await self.extract_daily_for_agent(agent_id):
                            logger.info(f"Agent [{agent_id}] 记忆提取完成")
                        else:
                            logger.debug(f"Agent [{agent_id}] 无需提取记忆")
                    await self._archive_to_long_term()
                else:
                    logger.debug("Storage未初始化，无法提取记忆")
            except asyncio.CancelledError:
                logger.info("记忆提取循环被取消")
                return
            except Exception as e:
                logger.error(f"记忆提取失败: {e}", exc_info=True)
                try:
                    await asyncio.sleep(300)
                except asyncio.CancelledError:
                    return

    async def _archive_to_long_term(self):
        """将过期的每日记忆归档到长期记忆（主 agent + 所有子 agent），归档后整理"""
        from .archiver import MemoryArchiver

        # 主 agent 归档
        archiver = MemoryArchiver(self.memory_dir)
        archiver.cleanup_old_files(retention_days=7)
        archiver.archive_daily_to_long_term(days_threshold=1)
        await self._consolidate_long_term(self.long_term_file)
        if self._llm_client:
            from .archiver import MemoryArchiver as Arch
            prune_archiver = Arch(self.memory_dir)
            await prune_archiver.score_and_prune(self.long_term_file, self._llm_client)

        # 子 agent 归档
        agents_dir = os.path.join(self.memory_dir, "agents")
        if os.path.exists(agents_dir):
            for agent_name in os.listdir(agents_dir):
                agent_memory_dir = os.path.join(agents_dir, agent_name)
                if os.path.isdir(agent_memory_dir):
                    sub_archiver = MemoryArchiver(agent_memory_dir)
                    sub_archiver.cleanup_old_files(retention_days=7)
                    sub_archiver.archive_daily_to_long_term(days_threshold=1)
                    sub_long_term = os.path.join(agent_memory_dir, "memory.md")
                    await self._consolidate_long_term(sub_long_term)
                    if self._llm_client:
                        await sub_archiver.score_and_prune(sub_long_term, self._llm_client)
                    logger.debug(f"子 agent [{agent_name}] 记忆归档完成")

        logger.info("所有 agent 记忆归档完成")

    async def _consolidate_long_term(self, file_path: str):
        """整理长期记忆：合并重复条目，但保留所有原始条目以防丢失"""
        if not self._llm_client or not os.path.exists(file_path):
            return

        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        if len(content) < 100:
            return

        # 先备份
        backup_path = file_path + ".bak"
        with open(backup_path, "w", encoding="utf-8") as f:
            f.write(content)

        prompt = (
            "请整理以下长期记忆，要求：\n"
            "1. 合并重复或高度相似的信息\n"
            "2. 删除明显已过时的信息（如已完成的待办）\n"
            "3. 保持分类结构，无内容的分类省略\n"
            "4. 每条信息保持简洁，用一句话概括\n"
            "5. 不要删除任何有效的经验、偏好或关键信息\n\n"
            f"原始记忆：\n{content}\n\n"
            "只输出整理后的结果，保持 markdown 格式。"
        )

        try:
            response = await self._llm_client.chat(
                messages=[
                    {"role": "system", "content": "你是记忆整理助手。保持信息完整，去除冗余。不要删除任何有用的信息。"},
                    {"role": "user", "content": prompt}
                ],
                tools=None, stream=False, use_cache=False
            )
            result = response.choices[0].message.content or ""
            if result.strip():
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(result)
                    f.flush()
                    os.fsync(f.fileno())
                logger.info(f"长期记忆整理完成: {file_path}")
        except Exception as e:
            # 整理失败时从备份恢复
            if os.path.exists(backup_path):
                with open(backup_path, "r", encoding="utf-8") as bf:
                    original = bf.read()
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(original)
            logger.warning(f"长期记忆整理失败（已恢复备份）: {e}")
        finally:
            if os.path.exists(backup_path):
                try:
                    os.remove(backup_path)
                except Exception:
                    pass

    def add_preference(self, preference: str):
        self._write("用户偏好", preference)

    def add_key_info(self, info: str):
        self._write("关键信息", info)

    def add_todo(self, todo: str):
        self._write("待办事项", todo)

    def add_failure_lesson(self, tool_name: str, args_summary: str, error: str):
        self._write("避坑经验", f"{tool_name}({args_summary}) 失败: {error}")

    def add_correction(self, context: str, correction: str):
        self._write("用户纠正", f"场景: {context} | 纠正: {correction}")

    def add_reflection(self, knowledge: str):
        self._write("自学习", knowledge)

    def share_knowledge(self, from_agent: str, knowledge: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"- [{timestamp}][{from_agent}] {knowledge}\n"
        with open(self.shared_knowledge_file, "a", encoding="utf-8") as f:
            f.write(entry)
        logger.debug(f"Shared knowledge from [{from_agent}]: {knowledge[:80]}")

    def _write(self, category: str, content: str):
        """写入记忆 — 如果有 MemoryWriter 走新路径，否则走本地写入"""
        if self._writer:
            from learning.categories import MemoryCategory
            cat_map = {
                "用户偏好": MemoryCategory.PREFERENCE,
                "关键信息": MemoryCategory.KEY_INFO,
                "待办事项": MemoryCategory.TODO,
                "避坑经验": MemoryCategory.FAILURE_LESSON,
                "用户纠正": MemoryCategory.CORRECTION,
                "自学习": MemoryCategory.REFLECTION,
            }
            self._writer.write(cat_map.get(category, MemoryCategory.KEY_INFO), content)
            return
        self._append_to_memory(category, content)

    def _append_to_memory(self, category: str, content: str):
        """本地写入（当 MemoryWriter 不可用时的后备路径）"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        try:
            self._ensure_dirs()
            if not os.path.exists(self.long_term_file):
                with open(self.long_term_file, "w", encoding="utf-8") as f:
                    f.write(f"# 长期记忆\n\n## {category}\n\n- [{timestamp}] {content}\n")
                logger.info(f"Memory created: [{category}] {content[:80]}")
                return
            with open(self.long_term_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            category_header = f"## {category}"
            category_idx = -1
            for i, line in enumerate(lines):
                if line.strip() == category_header:
                    category_idx = i
                    break
            if category_idx == -1:
                lines.append(f"\n## {category}\n\n- [{timestamp}] {content}\n")
            else:
                insert_idx = category_idx + 1
                while insert_idx < len(lines) and not lines[insert_idx].strip().startswith("## "):
                    insert_idx += 1
                lines.insert(insert_idx, f"- [{timestamp}] {content}\n")
            with open(self.long_term_file, "w", encoding="utf-8") as f:
                f.writelines(lines)
            logger.info(f"Memory saved: [{category}] {content[:80]}")
        except Exception as e:
            logger.error(f"Memory write error: [{category}] {e}")

    def set_writer(self, writer):
        """注入 MemoryWriter，接管写入职责"""
        self._writer = writer

    def load_shared_knowledge(self) -> str:
        if not os.path.exists(self.shared_knowledge_file):
            return ""
        with open(self.shared_knowledge_file, "r", encoding="utf-8") as f:
            content = f.read()
        if content.strip() == "# 共享知识库\n\n跨代理共享的经验和知识。\n":
            return ""
        return content
    
    def load_memory(self, task: str = "") -> str:
        parts = []
        
        long_term = self._load_long_term(task)
        if long_term:
            parts.append(f"【长期记忆】\n{long_term}")
        
        daily = self._load_recent_daily(days=3)
        if daily:
            parts.append(f"【近期记忆】\n{daily}")
        
        shared = self.load_shared_knowledge()
        if shared:
            parts.append(f"【共享知识】\n{shared}")
        
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
    
    async def extract_daily(self, date_str: str = None) -> bool:
        storage = self._get_storage()
        if not storage:
            return False

        if not date_str:
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            date_str = yesterday

        agent_ids = storage.get_all_agent_ids()
        success = False
        for agent_id in agent_ids:
            if await self.extract_daily_for_agent(agent_id, date_str):
                success = True
        return success
    
    async def extract_daily_for_agent(self, target_agent_id: str, date_str: str = None) -> bool:
        storage = self._get_storage()
        if not storage:
            return False

        if not date_str:
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            date_str = yesterday

        # 主 agent 存到自己的 daily/ 目录，子 agent 存到 agents/<id>/daily/
        if target_agent_id == self.agent_id:
            daily_dir = self.daily_dir
        else:
            daily_dir = os.path.join(self.memory_dir, "agents", target_agent_id, "daily")
        os.makedirs(daily_dir, exist_ok=True)
        daily_file = os.path.join(daily_dir, f"{date_str}.md")

        messages = storage.get_messages_by_date(date_str, agent_id=target_agent_id)
        if not messages:
            logger.debug(f"No messages found for {date_str}, agent {target_agent_id}")
            return False

        # 组装原始对话文本
        session_lines = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user":
                session_lines.append(f"用户: {content[:500]}")
            elif role == "assistant":
                session_lines.append(f"助手: {content[:500]}")
            elif role == "tool":
                session_lines.append(f"工具结果: {content[:200]}")

        if not session_lines:
            return False

        session_text = "\n".join(session_lines)

        # 优先 LLM 提取，失败则简单保存
        extracted = await self._llm_extract_daily(session_text, target_agent_id)
        if not extracted:
            extracted = self._simple_extract_daily(session_text)

        header = f"# 每日记忆 - {date_str}\n\n"
        content = header + extracted

        with open(daily_file, "w", encoding="utf-8") as f:
            f.write(content)

        logger.info(f"Daily memory saved: {daily_file}")
        return True

    CHUNK_SIZE = 50000  # 每个分片的最大字符数（留余量给 prompt 模板）

    async def _llm_extract_daily(self, session_text: str, agent_id: str) -> str:
        """用 LLM 从对话中提取关键信息，超长文本分片提取后直接拼接"""
        if not self._llm_client:
            return ""

        # 短文本直接提取
        if len(session_text) <= self.CHUNK_SIZE:
            return await self._llm_extract_chunk(session_text, agent_id)

        # 长文本按消息边界分片
        chunks = self._split_by_messages(session_text)
        logger.info(f"对话过长 ({len(session_text)} 字符)，分为 {len(chunks)} 片提取")

        results = []
        for i, chunk in enumerate(chunks):
            extracted = await self._llm_extract_chunk(chunk, agent_id)
            if extracted:
                results.append(extracted)

        if not results:
            return ""

        return "\n\n".join(results)

    async def _llm_extract_chunk(self, chunk: str, agent_id: str) -> str:
        """提取单个分片的关键信息"""
        prompt = (
            f"请从以下 Agent [{agent_id}] 的对话片段中提取关键信息。\n"
            f"要求：\n"
            f"1. 只保留有价值的信息，过滤掉闲聊、重复、工具调用细节\n"
            f"2. 每条信息用一句话概括\n"
            f"3. 按以下分类输出（某分类无内容则省略）\n\n"
            f"## 关键决策\n- ...\n\n"
            f"## 用户偏好\n- ...\n\n"
            f"## 重要事实\n- ...\n\n"
            f"## 待办事项\n- ...\n\n"
            f"对话片段：\n{chunk}\n\n"
            f"只输出提取结果，不要额外说明。如果对话无有价值信息，输出「无关键信息」。"
        )

        try:
            response = await self._llm_client.chat(
                messages=[
                    {"role": "system", "content": "你是记忆提取助手。输出简洁的结构化摘要。"},
                    {"role": "user", "content": prompt}
                ],
                tools=None, stream=False, use_cache=False
            )
            result = response.choices[0].message.content or ""
            if "无关键信息" in result:
                return ""
            return result
        except Exception as e:
            logger.warning(f"LLM 记忆提取失败: {e}")
            return ""

    def _split_by_messages(self, text: str) -> list[str]:
        """按消息边界分割文本，每片不超过 CHUNK_SIZE"""
        lines = text.split("\n")
        chunks = []
        current = []
        current_len = 0

        for line in lines:
            line_len = len(line) + 1  # +1 for \n
            # 单条消息超长时单独成片
            if current_len + line_len > self.CHUNK_SIZE and current:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            current.append(line)
            current_len += line_len

        if current:
            chunks.append("\n".join(current))

        return chunks

    def _simple_extract_daily(self, session_text: str) -> str:
        """无 LLM 时的简单提取：截断保留要点"""
        lines = session_text.split("\n")
        trimmed = [line[:200] + "..." if len(line) > 200 else line for line in lines]
        return f"## 会话摘要\n\n" + "\n".join(trimmed[:50])
    
    def list_daily_files(self) -> list:
        if not os.path.exists(self.daily_dir):
            return []
        return [f for f in os.listdir(self.daily_dir) if f.endswith(".md")]