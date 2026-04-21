import os
import json
import logging
import re
from datetime import datetime
from typing import Dict, Any, Optional, TYPE_CHECKING

from .categories import (
    AUTO_CREATE_SKILL_PROMPT, AUTO_CREATE_SUBAGENT_PROMPT,
    AUTO_CREATE_SYSTEM_PROMPT, CREATION_LOG_FILE,
)

if TYPE_CHECKING:
    from skills.skill import SkillManager
    from subagent_manager import SubagentManager

logger = logging.getLogger("agent.learning.auto_creator")


class AutoCreator:
    """自动创建器 — 根据模式信息生成技能或子代理模板"""

    def __init__(
        self,
        memory_dir: str,
        skills_dir: str,
        agents_dir: str,
        llm_client=None,
        skill_manager: "SkillManager" = None,
        subagent_manager: "SubagentManager" = None,
    ):
        self.memory_dir = memory_dir
        self.skills_dir = skills_dir
        self.agents_dir = agents_dir
        self.llm_client = llm_client
        self.skill_manager = skill_manager
        self.subagent_manager = subagent_manager

    def set_llm_client(self, client):
        self.llm_client = client

    def set_skill_manager(self, manager: "SkillManager"):
        self.skill_manager = manager

    def set_subagent_manager(self, manager: "SubagentManager"):
        self.subagent_manager = manager

    async def create_from_pattern(self, pattern_info: Dict[str, Any]) -> Optional[str]:
        """
        根据模式信息自动创建技能或子代理。

        返回:
            创建结果路径，失败返回 None
        """
        category = pattern_info.get("category", "skill")
        suggested_name = pattern_info.get("suggested_name", "")
        description = pattern_info.get("description", "")
        examples = pattern_info.get("examples", [])
        pattern_key = pattern_info.get("pattern_key", "")

        if not suggested_name:
            suggested_name = pattern_key

        if not self.llm_client:
            logger.warning("[自动创建] 无 LLM 客户端，跳过创建")
            return None

        if category == "skill":
            return await self._create_skill(
                suggested_name, description, examples, pattern_key
            )
        else:
            return await self._create_subagent(
                suggested_name, description, examples, pattern_key
            )

    async def _create_skill(
        self,
        name: str,
        description: str,
        examples: list,
        pattern_key: str,
    ) -> Optional[str]:
        """自动创建技能"""
        if not self.skills_dir:
            logger.warning("[自动创建] skills_dir 未设置")
            return None

        safe_name = self._sanitize_name(name)
        skill_dir = os.path.join(self.skills_dir, safe_name)

        if os.path.exists(skill_dir):
            logger.info(f"[自动创建] 技能目录已存在: {skill_dir}，跳过")
            return None

        examples_text = "\n".join(f"- {ex}" for ex in examples)
        prompt = AUTO_CREATE_SKILL_PROMPT.format(
            name=name,
            description=description,
            examples=examples_text,
        )

        try:
            content = await self._call_llm(AUTO_CREATE_SYSTEM_PROMPT, prompt)
            if not content:
                return None

            content = self._clean_llm_output(content)

            os.makedirs(skill_dir, exist_ok=True)
            skill_file = os.path.join(skill_dir, "SKILL.md")
            with open(skill_file, "w", encoding="utf-8") as f:
                f.write(content)

            if self.skill_manager:
                loaded = self.skill_manager._load_skill(skill_dir)
                if loaded:
                    logger.info(
                        f"[自动创建] 技能 '{loaded.name}' 已创建并热加载: {skill_dir}"
                    )
                else:
                    logger.warning(f"[自动创建] 技能文件已生成但加载失败: {skill_dir}")

            self._log_creation("skill", name, description, skill_dir, pattern_key)
            return skill_dir

        except Exception as e:
            logger.error(f"[自动创建] 创建技能失败: {e}", exc_info=True)
            return None

    async def _create_subagent(
        self,
        name: str,
        description: str,
        examples: list,
        pattern_key: str,
    ) -> Optional[str]:
        """自动创建子代理模板"""
        if not self.agents_dir:
            logger.warning("[自动创建] agents_dir 未设置")
            return None

        safe_name = self._sanitize_name(name)
        agent_dir = os.path.join(self.agents_dir, safe_name)

        if os.path.exists(agent_dir):
            logger.info(f"[自动创建] 子代理目录已存在: {agent_dir}，跳过")
            return None

        examples_text = "\n".join(f"- {ex}" for ex in examples)
        prompt = AUTO_CREATE_SUBAGENT_PROMPT.format(
            name=name,
            description=description,
            examples=examples_text,
        )

        try:
            content = await self._call_llm(AUTO_CREATE_SYSTEM_PROMPT, prompt)
            if not content:
                return None

            content = self._clean_llm_output(content)

            os.makedirs(agent_dir, exist_ok=True)

            prompt_file = os.path.join(agent_dir, "PROMPT.md")
            with open(prompt_file, "w", encoding="utf-8") as f:
                f.write(content)

            mcp_file = os.path.join(agent_dir, "mcp_servers.json")
            with open(mcp_file, "w", encoding="utf-8") as f:
                json.dump([], f)

            skills_subdir = os.path.join(agent_dir, "skills")
            os.makedirs(skills_subdir, exist_ok=True)

            if self.subagent_manager:
                self.subagent_manager._load_all()
                logger.info(
                    f"[自动创建] 子代理模板 '{name}' 已创建并热加载: {agent_dir}"
                )

            self._log_creation("subagent", name, description, agent_dir, pattern_key)
            return agent_dir

        except Exception as e:
            logger.error(f"[自动创建] 创建子代理失败: {e}", exc_info=True)
            return None

    def _sanitize_name(self, name: str) -> str:
        safe = re.sub(r'[<>:"/\\|?*\s]', '_', name)
        safe = safe.strip('_')
        if not safe:
            safe = "unnamed"
        return safe

    def _clean_llm_output(self, text: str) -> str:
        text = text.strip()
        if text.startswith("```markdown"):
            text = text[len("```markdown"):]
        elif text.startswith("```md"):
            text = text[len("```md"):]
        elif text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        return text.strip()

    def _log_creation(
        self,
        category: str,
        name: str,
        description: str,
        path: str,
        pattern_key: str,
    ):
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "category": category,
            "name": name,
            "description": description,
            "path": path,
            "pattern_key": pattern_key,
            "status": "created",
            "reviewed": False,
        }

        log_file = os.path.join(self.memory_dir, CREATION_LOG_FILE)
        os.makedirs(os.path.dirname(log_file), exist_ok=True)

        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

        logger.info(f"[自动创建] 已记录创建日志: {category}/{name}")

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
        return (response.choices[0].message.content or "").strip()
