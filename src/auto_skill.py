"""
技能自动路由 — 根据任务描述自动匹配和激活技能

设计思路（参考 grok-build 的 auto-invoke skills）：
- 每个 SKILL.md 在前置元数据中定义 trigger_patterns
- Agent 收到任务时，自动扫描所有技能的 trigger_patterns
- 匹配到后自动激活该技能（无需用户手动调用 skill 工具）

用法:
    activator = AutoSkillActivator(skill_manager)
    await activator.activate_for_task(task)
"""
import logging
import re

logger = logging.getLogger("agent.auto_skill")


class AutoSkillActivator:
    """技能自动激活器"""

    def __init__(self, skill_manager=None):
        self.skill_manager = skill_manager
        self._activated_in_this_task: set[str] = set()

    async def activate_for_task(self, task: str) -> list[str]:
        """根据任务描述自动激活匹配的技能

        Args:
            task: 用户的任务描述

        Returns:
            已激活的技能名称列表
        """
        if not self.skill_manager:
            return []

        activated = []
        task_lower = task.lower()

        for skill_name in self.skill_manager.list_skills():
            skill = self.skill_manager.get_skill(skill_name)
            if not skill:
                continue

            # 获取 trigger_patterns（从 SKILL.md 的前置元数据）
            patterns = self._get_trigger_patterns(skill)
            if not patterns:
                continue

            for pattern in patterns:
                try:
                    if re.search(pattern, task_lower, re.IGNORECASE):
                        if skill_name not in self._activated_in_this_task:
                            await self._activate_skill(skill_name)
                            self._activated_in_this_task.add(skill_name)
                            activated.append(skill_name)
                            logger.info(f"[auto_skill] ✅ 自动激活: {skill_name} (匹配: {pattern})")
                        break  # 一个技能匹配一个模式即可
                except re.error as e:
                    logger.warning(f"[auto_skill] 无效的正则模式 '{pattern}' 在技能 {skill_name}: {e}")
                    continue

        if activated:
            logger.info(f"[auto_skill] 本次任务自动激活了 {len(activated)} 个技能: {activated}")
        else:
            logger.debug("[auto_skill] 无匹配技能")

        return activated

    def reset(self):
        """重置已激活列表（每次新任务前调用）"""
        self._activated_in_this_task.clear()

    def _get_trigger_patterns(self, skill) -> list[str]:
        """从技能对象中提取 trigger_patterns

        支持两种来源:
        1. skill.metadata (SKILL.md 前置元数据中的 trigger_patterns)
        2. skill.patterns (Skill 对象本身的属性)
        """
        patterns = []

        # 尝试 metadata
        if hasattr(skill, 'metadata') and isinstance(skill.metadata, dict):
            patterns = skill.metadata.get("trigger_patterns", [])
            if isinstance(patterns, str):
                patterns = [patterns]

        # 尝试 data 属性
        if not patterns and hasattr(skill, 'data') and isinstance(skill.data, dict):
            patterns = skill.data.get("trigger_patterns", [])

        # 尝试直接属性
        if not patterns and hasattr(skill, 'trigger_patterns'):
            patterns = skill.trigger_patterns

        # 从 description 中提取关键词
        if not patterns:
            desc = ""
            if hasattr(skill, 'metadata') and isinstance(skill.metadata, dict):
                desc = skill.metadata.get("description", "")
            if not desc and hasattr(skill, 'description'):
                desc = skill.description
            if desc:
                # 从描述中提取关键词（如 "bug fix" 提取 "bug|fix|修复"）
                keywords = re.findall(r'\w+', desc)
                if keywords:
                    patterns = [f"(?=.*{'|'.join(k.lower() for k in keywords[:3])})"]

        return patterns if isinstance(patterns, list) else []

    async def _activate_skill(self, skill_name: str):
        """激活技能"""
        if not self.skill_manager:
            return

        try:
            if hasattr(self.skill_manager, 'execute_skill'):
                await self.skill_manager.execute_skill(skill_name)
            elif hasattr(self.skill_manager, 'activate_skill'):
                await self.skill_manager.activate_skill(skill_name)
        except Exception as e:
            logger.warning(f"[auto_skill] 激活技能失败 {skill_name}: {e}")

    def get_stats(self) -> dict:
        return {
            "auto_activated": list(self._activated_in_this_task),
            "total_activated": len(self._activated_in_this_task),
        }
