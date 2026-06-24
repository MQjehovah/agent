import logging

logger = logging.getLogger("agent.memory")

# category -> 展示分组名
CATEGORY_LABELS = {
    "preference": "用户偏好",
    "key_info": "关键信息",
    "todo": "待办事项",
    "failure_lesson": "避坑经验",
    "correction": "用户纠正",
    "reflection": "自学习",
    "knowledge": "通用知识",
}


class MemoryManager:
    """基于 DB 的记忆管理器。隔离靠 SQL WHERE owner_id，无文件、无共享状态。"""

    def __init__(self, storage=None, agent_id: str = ""):
        self._storage = storage
        self.agent_id = agent_id

    def _get_storage(self):
        if self._storage:
            return self._storage
        from storage import get_storage
        return get_storage()

    # ---------------- 写入（user 私有）----------------
    def _add_user(self, user_id: str, category: str, content: str,
                  source: str = "", importance: int = 3):
        storage = self._get_storage()
        if not storage:
            logger.warning(f"记忆写入跳过（无 storage）: [{category}]")
            return
        if not user_id:
            logger.warning(f"记忆写入跳过（无 user_id）: category={category} content={content[:80]!r}")
            return
        storage.save_memory(
            scope="user", owner_id=user_id, agent_id=self.agent_id,
            category=category, content=content, source=source, importance=importance,
        )

    def add_preference(self, user_id: str, content: str):
        self._add_user(user_id, "preference", content, source="manual")

    def add_key_info(self, user_id: str, content: str):
        self._add_user(user_id, "key_info", content, source="manual")

    def add_todo(self, user_id: str, content: str):
        self._add_user(user_id, "todo", content, source="manual")

    def add_failure_lesson(self, user_id: str, tool_name: str, args_summary: str, error: str):
        self._add_user(user_id, "failure_lesson", f"{tool_name}({args_summary}) 失败: {error}", source="reflection")

    def add_correction(self, user_id: str, context: str, correction: str):
        self._add_user(user_id, "correction", f"场景: {context} | 纠正: {correction}", source="reflection")

    def add_reflection(self, user_id: str, knowledge: str):
        self._add_user(user_id, "reflection", knowledge, source="reflection", importance=4)

    def _add_global(self, category, content, source="admin", importance=4):
        """仅供审批通过后调用，写入 global"""
        storage = self._get_storage()
        if not storage:
            return
        storage.save_memory(scope="global", owner_id="", agent_id="",
                            category=category, content=content, source=source, importance=importance)

    # ---------------- 读取 ----------------
    def load_memory(self, user_id: str, limit: int = 50) -> str:
        storage = self._get_storage()
        if not storage or not user_id:
            return ""
        rows = storage.query_memories(user_id=user_id, limit=limit)
        if not rows:
            return ""
        # 按 category 分组
        groups = {}
        for r in rows:
            label = CATEGORY_LABELS.get(r["category"], r["category"])
            groups.setdefault(label, []).append(r["content"])
        parts = ["【记忆】"]
        for label, items in groups.items():
            parts.append(f"### {label}")
            for it in items:
                parts.append(f"- {it}")
        return "\n".join(parts)
