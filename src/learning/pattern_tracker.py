import json
import logging
import os
from datetime import datetime
from typing import Any

from .categories import (
    PATTERN_CLASSIFY_PROMPT,
    PATTERN_CLASSIFY_SYSTEM_PROMPT,
    PATTERN_FILE,
    PATTERN_MAX_EXAMPLES,
    PATTERN_TRIGGER_THRESHOLD,
)

logger = logging.getLogger("agent.learning.pattern")

_PATTERN_ENTRY_DEFAULTS = {
    "count": 0,
    "category": "skill",
    "suggested_name": "",
    "description": "",
    "examples": [],
    "created": False,
    "first_seen": "",
}


class PatternTracker:
    """任务模式追踪器 — 识别重复出现的任务模式"""

    def __init__(self, memory_dir: str, llm_client=None):
        self.memory_dir = memory_dir
        self.llm_client = llm_client
        self.patterns_file = os.path.join(memory_dir, PATTERN_FILE)
        self._patterns: dict[str, dict[str, Any]] = {}
        self._load()

    def set_llm_client(self, client):
        self.llm_client = client

    def _load(self):
        if os.path.exists(self.patterns_file):
            try:
                with open(self.patterns_file, encoding="utf-8") as f:
                    raw = json.load(f)
                if not isinstance(raw, dict):
                    logger.warning("任务模式文件格式错误，重置为空")
                    self._patterns = {}
                    return
                for key, entry in raw.items():
                    if not isinstance(entry, dict):
                        continue
                    defaults = dict(_PATTERN_ENTRY_DEFAULTS)
                    defaults.update(entry)
                    defaults["examples"] = entry.get("examples", defaults["examples"] or [])
                    try:
                        defaults["count"] = int(defaults["count"])
                    except (ValueError, TypeError):
                        defaults["count"] = 0
                    self._patterns[key] = defaults
                logger.debug(f"已加载 {len(self._patterns)} 个任务模式")
            except Exception as e:
                logger.warning(f"加载任务模式文件失败: {e}")
                self._patterns = {}

    def _save(self):
        os.makedirs(os.path.dirname(self.patterns_file), exist_ok=True)
        with open(self.patterns_file, "w", encoding="utf-8") as f:
            json.dump(self._patterns, f, ensure_ascii=False, indent=2)

    async def record_task(self, task: str, summary: str) -> dict[str, Any] | None:
        """
        记录一个完成的任务，进行模式分类。

        返回:
            如果触发了阈值，返回待创建的模式信息；否则返回 None
        """
        if not self.llm_client:
            return None

        try:
            classification = await self._classify_task(task, summary)
            if not classification or not isinstance(classification, dict):
                return None

            pattern_key = classification.get("pattern_key", "")
            if not pattern_key:
                return None

            category = classification.get("category", "skill")
            suggested_name = classification.get("suggested_name", "")
            description = classification.get("description", "")

            if category not in ("skill", "subagent"):
                category = "skill"

            return self._update_pattern(
                pattern_key, category, suggested_name, description, task
            )
        except Exception as e:
            logger.warning(f"任务模式记录失败: {e}", exc_info=True)
            return None

    def _update_pattern(
        self,
        pattern_key: str,
        category: str,
        suggested_name: str,
        description: str,
        task: str,
    ) -> dict[str, Any] | None:
        """更新模式计数，达到阈值时返回创建信息"""
        if pattern_key in self._patterns:
            entry = self._patterns[pattern_key]
            if entry.get("created"):
                return None

            count = entry.get("count", 0) + 1
            entry["count"] = count
            examples = entry.get("examples", [])
            if len(examples) < PATTERN_MAX_EXAMPLES:
                examples.append(task)
                entry["examples"] = examples
            if suggested_name:
                entry["suggested_name"] = suggested_name
            if description:
                entry["description"] = description
        else:
            count = 1
            self._patterns[pattern_key] = {
                "count": count,
                "category": category,
                "suggested_name": suggested_name,
                "description": description,
                "examples": [task],
                "created": False,
                "first_seen": datetime.now().isoformat(),
            }

        self._save()

        entry = self._patterns[pattern_key]
        entry_count = entry.get("count", 0)
        if entry_count >= PATTERN_TRIGGER_THRESHOLD and not entry.get("created"):
            logger.info(
                f"[模式检测] 模式 '{pattern_key}' 已达阈值 "
                f"({entry_count}/{PATTERN_TRIGGER_THRESHOLD})，建议创建为 {category}"
            )
            return {
                "pattern_key": pattern_key,
                "category": entry.get("category", category),
                "suggested_name": entry.get("suggested_name", suggested_name),
                "description": entry.get("description", description),
                "examples": entry.get("examples", []),
                "count": entry_count,
            }

        return None

    def mark_created(self, pattern_key: str):
        """标记模式已创建，避免重复触发"""
        if pattern_key in self._patterns:
            self._patterns[pattern_key]["created"] = True
            self._save()

    async def _classify_task(self, task: str, summary: str) -> dict[str, Any] | None:
        """使用 LLM 对任务进行模式分类"""
        prompt = PATTERN_CLASSIFY_PROMPT.format(
            task=task[:300],
            summary=summary[:1000],
        )

        response = await self._call_llm(PATTERN_CLASSIFY_SYSTEM_PROMPT, prompt)
        if not response:
            return None

        return self._parse_classification(response)

    def _parse_classification(self, text: str) -> dict[str, Any] | None:
        """解析 LLM 返回的分类 JSON（复用 parse_llm_json，处理代码块/混杂文本）"""
        from autonomous import parse_llm_json

        text = (text or "").strip()
        if not text:
            return None

        result = parse_llm_json(text)
        if result and isinstance(result, dict) and "pattern_key" in result:
            if result.get("category") not in ("skill", "subagent"):
                result["category"] = "skill"
            return result

        logger.warning(f"无法解析分类结果: {text[:200]}")
        return None

    def get_all_patterns(self) -> dict[str, dict[str, Any]]:
        return dict(self._patterns)

    def get_pending_patterns(self) -> list[dict[str, Any]]:
        """获取所有已达阈值但未创建的模式"""
        return [
            {"pattern_key": k, **v}
            for k, v in self._patterns.items()
            if v.get("count", 0) >= PATTERN_TRIGGER_THRESHOLD and not v.get("created")
        ]

    def get_stats(self) -> dict[str, Any]:
        total = len(self._patterns)
        created = sum(1 for p in self._patterns.values() if p.get("created"))
        pending = len(self.get_pending_patterns())
        return {
            "total_patterns": total,
            "created": created,
            "pending_creation": pending,
        }

    async def _call_llm(self, system_prompt: str, user_prompt: str) -> str:
        response = await self.llm_client.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            tools=None,
            stream=False,
            use_cache=False,
        )
        content = response.choices[0].message.content
        if not content:
            return ""
        return content.strip()
