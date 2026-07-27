import logging
import re

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

    def add_reflection(self, user_id: str, knowledge: str, category: str = "reflection", importance: int = 4):
        self._add_user(user_id, category, knowledge, source="reflection", importance=importance)

    def _add_global(self, category, content, source="admin", importance=4):
        """仅供审批通过后调用，写入 global"""
        storage = self._get_storage()
        if not storage:
            return
        storage.save_memory(scope="global", owner_id="", agent_id="",
                            category=category, content=content, source=source, importance=importance)

    # ---------------- 读取 ----------------
    def load_memory(self, user_id: str, task: str = "", limit: int = None) -> str:
        storage = self._get_storage()
        if not storage or not user_id:
            return ""

        # 读取注入策略配置（settings 未初始化时回退默认值）
        try:
            from settings import get_settings
            cfg = get_settings().get("memory", {}) or {}
        except RuntimeError:
            cfg = {}
        per_category = int(cfg.get("per_category_limit", 5))
        total_limit = limit or int(cfg.get("injection_limit", 24))
        keyword_filter = bool(cfg.get("keyword_filter", True))

        # 取较多样本，供关键词重排后裁剪到注入上限
        rows = storage.query_memories(user_id=user_id, limit=50)
        if not rows:
            return ""

        if keyword_filter and task:
            rows = self._keyword_rerank(rows, task)
        rows = rows[:total_limit]

        # 按 category 分组，每组去重+限量
        groups = {}
        seen = set()
        for r in rows:
            label = CATEGORY_LABELS.get(r["category"], r["category"])
            content = r["content"]
            # 去重：相同纠正/经验只保留最后一条
            key = content[:60]
            if key in seen:
                continue
            seen.add(key)
            groups.setdefault(label, []).append(content)

        parts = ["【记忆】"]
        for label, items in groups.items():
            parts.append(f"### {label}")
            for it in items[-per_category:]:
                parts.append(f"- {it[:200]}")
        return "\n".join(parts)

    @staticmethod
    def _keyword_rerank(rows: list, task: str) -> list:
        """按任务关键词重叠度对记忆重排；无任何匹配时回退原序（已按 importance/recency）。"""
        keywords = MemoryManager._tokenize(task)
        if not keywords:
            return rows

        def _score(row):
            content = row.get("content", "") or ""
            return sum(1 for kw in keywords if kw in content)

        scored = [(_score(r), r) for r in rows]
        if not any(s > 0 for s, _ in scored):
            return rows
        # 稳定排序：高分在前，同分保持原序
        return [r for _, r in sorted(scored, key=lambda x: -x[0])]

    @staticmethod
    def _tokenize(text: str) -> list:
        """简单分词：ASCII 按词（>=2 字符），CJK 按字，过滤常见停用字。"""
        if not text:
            return []
        tokens = [m.group().lower() for m in re.finditer(r"[A-Za-z0-9_]{2,}", text)]
        cjk_stop = set("的了是在和与及或我你他她它这那有对于为到地把被让给从向之以个也")
        tokens.extend(ch for ch in re.findall(r"[一-鿿]", text) if ch not in cjk_stop)
        return tokens
