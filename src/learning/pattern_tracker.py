import os
import json
import logging
import re
from datetime import datetime
from typing import Dict, Any, Optional, List

from .categories import (
    PATTERN_TRIGGER_THRESHOLD, PATTERN_MAX_EXAMPLES,
    PATTERN_FILE, PATTERN_CLASSIFY_PROMPT, PATTERN_CLASSIFY_SYSTEM_PROMPT,
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
        self._patterns: Dict[str, Dict[str, Any]] = {}
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

    async def record_task(self, task: str, summary: str) -> Optional[Dict[str, Any]]:
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
    ) -> Optional[Dict[str, Any]]:
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

    async def _classify_task(self, task: str, summary: str) -> Optional[Dict[str, Any]]:
        """使用 LLM 对任务进行模式分类"""
        prompt = PATTERN_CLASSIFY_PROMPT.format(
            task=task[:300],
            summary=summary[:1000],
        )

        response = await self._call_llm(PATTERN_CLASSIFY_SYSTEM_PROMPT, prompt)
        if not response:
            return None

        return self._parse_classification(response)

    def _parse_classification(self, text: str) -> Optional[Dict[str, Any]]:
        """解析 LLM 返回的分类 JSON"""
        text = text.strip()
        if not text:
            return None

        text = self._strip_code_block(text)

        result = self._extract_json(text)
        if result and isinstance(result, dict) and "pattern_key" in result:
            if result.get("category") not in ("skill", "subagent"):
                result["category"] = "skill"
            return result

        logger.warning(f"无法解析分类结果: {text[:200]}")
        return None

    def _strip_code_block(self, text: str) -> str:
        """从 LLM 输出中移除 markdown 代码块标记"""
        text = text.strip()
        pattern = re.compile(r'^```(?:\w+)?\s*\n(.*?)\n\s*```', re.DOTALL)
        match = pattern.search(text)
        if match:
            return match.group(1).strip()
        if text.startswith("```"):
            lines = text.split("\n")
            if len(lines) >= 3:
                return "\n".join(lines[1:-1]).strip()
        return text

    def _extract_json(self, text: str) -> Optional[Dict[str, Any]]:
        """从文本中提取 JSON 对象，尝试多种方式"""
        try:
            result = json.loads(text)
            if isinstance(result, dict):
                return result
            if isinstance(result, list) and len(result) > 0 and isinstance(result[0], dict):
                return result[0]
        except json.JSONDecodeError:
            pass

        json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
        if json_match:
            try:
                result = json.loads(json_match.group())
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                pass

        for match in re.finditer(r'\{[^{}]+\}', text):
            try:
                result = json.loads(match.group())
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                continue

        return None

    def get_all_patterns(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._patterns)

    def get_pending_patterns(self) -> List[Dict[str, Any]]:
        """获取所有已达阈值但未创建的模式"""
        return [
            {"pattern_key": k, **v}
            for k, v in self._patterns.items()
            if v.get("count", 0) >= PATTERN_TRIGGER_THRESHOLD and not v.get("created")
        ]

    def get_stats(self) -> Dict[str, Any]:
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
