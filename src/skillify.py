"""
Skillify — 从 Agent Session 自动提取工作流为可复用 Skill

设计思路（参考 grok-build 的 /skillify 命令）：
- 从当前 session 的消息历史中提取成功的工作流模式
- 自动生成 SKILL.md 文件
- 注册到技能系统，下次可自动匹配和复用

用法:
    skillifier = Skillifier(agent)
    skill_path = await skillifier.skillify(session_id, "bug-fix-template")
"""
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("agent.skillify")


class Skillifier:
    """Skill 自动提取器"""

    def __init__(self, agent=None, skill_manager=None, skills_dir: str = ""):
        self.agent = agent
        self.skill_manager = skill_manager
        self._skills_dir = skills_dir

    @property
    def skills_dir(self) -> str:
        if self._skills_dir:
            return self._skills_dir
        if self.agent and self.agent.skill_manager:
            # 从 skill_manager 获取默认技能目录
            if hasattr(self.agent.skill_manager, 'skills_dir'):
                return self.agent.skill_manager.skills_dir
        # 兜底
        if self.agent:
            return os.path.join(self.agent.config_dir, "skills")
        return "skills"

    async def skillify(
        self,
        session_id: str,
        skill_name: str = "",
        description: str = "",
        require_llm: bool = True,
    ) -> str:
        """从 session 中提取 Skill

        Args:
            session_id: 要提取的 session ID
            skill_name: 技能名称（为空则由 LLM 自动生成）
            description: 技能描述
            require_llm: 是否使用 LLM 提取（False=规则提取）

        Returns:
            创建的 SKILL.md 路径
        """
        # 1. 获取 session 消息
        messages = await self._get_session_messages(session_id)
        if not messages:
            return ""

        # 2. 分析 session 提取工作流
        if require_llm and self.agent and self.agent.client:
            skill_data = await self._llm_extract(messages, skill_name, description)
        else:
            skill_data = self._rule_extract(messages, skill_name, description)

        # 3. 生成 SKILL.md
        skill_path = await self._write_skill(skill_data)
        if not skill_path:
            return ""

        # 4. 注册到技能系统
        self._register_skill(skill_data, skill_path)

        logger.info(f"[skillify] 技能已创建: {skill_data['name']} -> {skill_path}")
        return skill_path

    async def _get_session_messages(self, session_id: str) -> list[dict]:
        """获取 session 消息"""
        if self.agent and self.agent.session_manager:
            session = await self.agent.session_manager.get_session(session_id)
            if session and session.messages:
                return session.messages

        # 从 storage 恢复
        if self.agent and hasattr(self.agent, 'storage') and self.agent.storage:
            msgs = self.agent.storage.get_messages(session_id)
            if msgs:
                return msgs

        logger.warning(f"[skillify] 未找到 session: {session_id}")
        return []

    async def _llm_extract(
        self,
        messages: list[dict],
        name_hint: str = "",
        description_hint: str = "",
    ) -> dict:
        """用 LLM 从消息中提取 Skill 定义"""
        # 准备消息摘要（取关键交互，避免超长）
        summary = self._summarize_messages(messages)

        prompt = f"""你是一个工作流分析师。请分析以下 AI Agent 的对话记录，提取其中可复用的工作流模式。

## 对话记录摘要
{summary[:4000]}

## 输出要求
分析这段对话中 Agent 完成任务的步骤和模式，生成一个可复用的 Skill 定义。

```json
{{
  "name": "技能名称（英文短横线式，如 bug-fix-workflow）",
  "description": "技能描述（20字以内，说明何时使用）",
  "trigger_patterns": ["触发关键词列表，如 ['bug', '修复', 'fix']"],
  "lifecycle_stage": "DEFINE|PLAN|BUILD|VERIFY|REVIEW|SHIP|ALL",
  "steps": [
    {{
      "order": 1,
      "action": "读文件|搜索代码|修改文件|运行命令|运行测试|审查代码",
      "prompt": "这一步的完整指令"
    }}
  ],
  "checklist": ["完成后的检查项列表"],
  "red_flags": ["需要注意的危险信号"],
  "estimated_tokens": 2000
}}
```

## 提取原则
1. 聚焦于 Agent 实际执行的操作序列，不要编造
2. 如果对话没有清晰的步骤模式，则生成一个简单的工作流
3. steps 中的 prompt 要通用化（去掉具体文件名，替换为{{变量名}}）
4. name 要简短（3-5个单词），description 要精准
5. 如果对话是简单的问答（无工具调用），返回空的 steps

只返回 JSON。"""

        try:
            resp = await self.agent.client.chat([{"role": "user", "content": prompt}])
            content = resp.choices[0].message.content if hasattr(resp, 'choices') else str(resp)
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            data = json.loads(content)
        except Exception as e:
            logger.warning(f"[skillify] LLM 提取失败，使用规则提取: {e}")
            data = self._rule_extract(messages, name_hint, description_hint)

        # 补充字段
        if name_hint:
            data["name"] = name_hint
        if description_hint:
            data["description"] = description_hint
        data.setdefault("name", name_hint or f"skill-{datetime.now():%Y%m%d%H%M%S}")
        data.setdefault("description", description_hint or "从对话提取的技能")
        data.setdefault("steps", [])
        data.setdefault("checklist", [])
        data.setdefault("red_flags", [])
        data.setdefault("trigger_patterns", [])
        data.setdefault("lifecycle_stage", "ALL")
        return data

    def _rule_extract(
        self,
        messages: list[dict],
        name_hint: str = "",
        description_hint: str = "",
    ) -> dict:
        """规则提取（无 LLM 时使用）"""
        # 提取工具调用序列
        tool_sequence = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "") or ""
            tool_calls = msg.get("tool_calls") or []

            if role == "assistant" and tool_calls:
                for tc in tool_calls:
                    fn = tc.get("function", {}).get("name", "")
                    if fn:
                        tool_sequence.append(fn)
            elif role == "assistant" and content:
                if not tool_sequence or tool_sequence[-1] != "llm_response":
                    tool_sequence.append("llm_response")

        # 生成步骤
        steps = []
        seen = set()
        for i, tool in enumerate(tool_sequence):
            if tool not in seen or tool in ("llm_response",):
                steps.append({
                    "order": len(steps) + 1,
                    "action": self._tool_to_action(tool),
                    "prompt": f"执行 {self._tool_to_action(tool)} 操作",
                })
                seen.add(tool)

        if not steps:
            steps = [{"order": 1, "action": "分析", "prompt": "分析用户需求并制定方案"}]

        return {
            "name": name_hint or f"workflow-{datetime.now():%Y%m%d%H%M%S}",
            "description": description_hint or f"自动提取的工作流 ({len(steps)} 步)",
            "trigger_patterns": [],
            "lifecycle_stage": "ALL",
            "steps": steps,
            "checklist": ["确认每个步骤都已完成"],
            "red_flags": [],
            "estimated_tokens": 2000,
        }

    @staticmethod
    def _tool_to_action(tool_name: str) -> str:
        mapping = {
            "file_operation": "文件操作",
            "shell": "执行命令",
            "grep": "搜索代码",
            "glob": "查找文件",
            "edit": "修改文件",
            "file_operation(preview)": "阅读代码",
            "web_search": "搜索网络",
            "web_fetch": "抓取网页",
            "subagent": "委派子代理",
            "memory": "读写记忆",
            "llm_response": "LLM 回复",
            "skill": "加载技能",
            "execute_skill": "执行技能",
        }
        return mapping.get(tool_name, tool_name)

    async def _write_skill(self, skill_data: dict) -> str:
        """写入 SKILL.md 文件"""
        name = skill_data.get("name", "unnamed-skill")
        description = skill_data.get("description", "")
        steps = skill_data.get("steps", [])
        checklist = skill_data.get("checklist", [])
        red_flags = skill_data.get("red_flags", [])
        trigger_patterns = skill_data.get("trigger_patterns", [])
        lifecycle_stage = skill_data.get("lifecycle_stage", "ALL")

        # 安全化名称
        safe_name = re.sub(r"[^\w一-鿿\-]", "_", name).strip("_").lower()
        if not safe_name:
            safe_name = f"skill_{datetime.now():%Y%m%d%H%M%S}"

        skill_dir = os.path.join(self.skills_dir, safe_name)
        os.makedirs(skill_dir, exist_ok=True)

        # 构建 steps 内容
        steps_md = ""
        if steps:
            steps_md = "\n## 执行步骤\n\n"
            for i, step in enumerate(steps, 1):
                action = step.get("action", "操作")
                prompt = step.get("prompt", "")
                steps_md += f"### {i}. {action}\n\n{prompt}\n\n"

        # 构建检查清单
        checklist_md = ""
        if checklist:
            checklist_md = "\n## 检查清单\n\n"
            for item in checklist:
                checklist_md += f"- [ ] {item}\n"
            checklist_md += "\n"

        # 构建 red flags
        red_flags_md = ""
        if red_flags:
            red_flags_md = "\n## 危险信号\n\n"
            for rf in red_flags:
                red_flags_md += f"- ⚠️ {rf}\n"
            red_flags_md += "\n"

        triggers_yaml = ""
        if trigger_patterns:
            triggers_yaml = "\n" + "\n".join(f"  - \"{p}\"" for p in trigger_patterns)

        sk_md = f"""---
name: {safe_name}
description: {description}
lifecycle_stage: {lifecycle_stage}
trigger_patterns:{triggers_yaml}
estimated_tokens: {skill_data.get('estimated_tokens', 2000)}
---
# {safe_name}

{description}

{steps_md}{checklist_md}{red_flags_md}
## 元信息

- 创建时间: {datetime.now():%Y-%m-%d %H:%M:%S}
- 提取来源: Agent 对话记录
- 步骤数: {len(steps)}
"""

        skill_path = os.path.join(skill_dir, "SKILL.md")
        with open(skill_path, "w", encoding="utf-8") as f:
            f.write(sk_md)

        logger.info(f"[skillify] SKILL.md 已写入: {skill_path}")
        return skill_path

    def _register_skill(self, skill_data: dict, skill_path: str):
        """将新技能注册到技能系统"""
        if not self.skill_manager:
            return

        try:
            skill_name = skill_data.get("name", "")
            if skill_name and self.skill_manager:
                self.skill_manager.reload_skills()
                logger.info(f"[skillify] 技能已注册: {skill_name}")
        except Exception as e:
            logger.warning(f"[skillify] 技能注册失败: {e}")

    @staticmethod
    def _summarize_messages(messages: list[dict]) -> str:
        """压缩消息为摘要"""
        parts = []
        for i, msg in enumerate(messages[-30:]):  # 只取最近 30 条
            role = msg.get("role", "?")
            content = msg.get("content", "") or ""
            tool_calls = msg.get("tool_calls") or []

            if role == "system":
                continue

            if tool_calls:
                call_names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
                parts.append(f"[{i}] assistant → 调用工具: {', '.join(call_names)}")
            elif content:
                truncated = content[:200].replace("\n", " ")
                parts.append(f"[{i}] {role}: {truncated}")

        return "\n".join(parts)
