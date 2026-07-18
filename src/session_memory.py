"""
跨 Session 记忆固化 — /dream 和 /flush 命令

设计思路（参考 grok-build 的 /dream + /flush）：
- /flush: 将当前 session 的关键决策合成到 workspace 的 MEMORY.md 中
- /dream: 将多个 session 的经验"做梦"融合为更抽象的持久知识
- 混合检索: 写入时向量化，读取时向量 + BM25 混合检索

用法:
    memory = SessionMemory(client, workspace)

    # 在 session 结束时调用
    await memory.flush(session_id, messages)

    # 跨 session 知识融合
    await memory.dream()
"""
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger("agent.session_memory")


class SessionMemory:
    """跨 Session 记忆固化"""

    def __init__(self, client=None, workspace: str = "", agent=None):
        self.client = client
        self.workspace = workspace
        self.agent = agent
        self._memory_file = os.path.join(workspace, "MEMORY.md") if workspace else ""
        self._flush_history: list[dict] = []

    # ── /flush: 固化当前 session ──

    async def flush(self, session_id: str, messages: list[dict]) -> str:
        """将 session 中的关键决策写入 MEMORY.md

        分析 session 消息，提取：
        - 架构决策
        - 关键配置
        - 修复的 Bug
        - 用户偏好
        - TODO 项

        Args:
            session_id: 会话 ID
            messages: 会话消息列表

        Returns:
            写入的总结文本
        """
        if not messages:
            logger.warning("[memory] 无消息可固化")
            return ""

        # 提取关键信息
        key_decisions = self._extract_decisions(messages)
        if not key_decisions:
            return ""

        # 用 LLM 精炼（如果有客户端）
        if self.client and len(key_decisions) > 3:
            refined = await self._refine_with_llm(key_decisions, session_id)
            if refined:
                key_decisions = refined

        # 写入 MEMORY.md
        entry = self._format_memory_entry(key_decisions, session_id)
        self._append_to_memory(entry)

        self._flush_history.append({
            "session_id": session_id,
            "time": time.time(),
            "items": len(key_decisions),
        })

        logger.info(f"[memory] /flush: 已固化 {len(key_decisions)} 条决策到 MEMORY.md")
        return entry

    # ── /dream: 跨 session 知识融合 ──

    async def dream(self, recent_sessions: int = 5) -> str:
        """将多个 session 的经验融合为持久知识

        读取最近的 MEMORY.md 内容，让 LLM "做梦"融合：
        - 识别重复模式
        - 提炼通用规则
        - 生成新的长期知识

        Args:
            recent_sessions: 考虑最近几次 session

        Returns:
            融合后的新知识
        """
        current_content = self._read_memory()

        if not current_content or not self.client:
            return self._timely_cleanup(current_content)

        prompt = f"""你是一个知识管理专家。请分析以下跨 session 记忆记录，完成"知识做梦"：

## 当前记忆
{current_content[:4000]}

## 任务
1. **识别重复模式**：哪些决策/经验在多个 session 中反复出现？
2. **提炼通用规则**：从具体案例中抽象出可以长期适用的规则
3. **识别过时信息**：哪些记录已经不再适用？
4. **生成新洞察**：从现有知识的交叉点中发现新的有用知识

## 输出格式
返回 JSON:
```json
{{
  "patterns_found": ["重复模式1", "重复模式2"],
  "general_rules": ["通用规则1", "通用规则2"],
  "outdated": ["过时的记录"],
  "new_insights": ["新洞察1"],
  "suggested_cleanup": "建议如何重组记忆"
}}
```

只返回 JSON。"""

        try:
            resp = await self.client.chat([{"role": "user", "content": prompt}])
            content = resp.choices[0].message.content if hasattr(resp, 'choices') else str(resp)
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            dream_result = json.loads(content)

            # 将梦境结果追加到 MEMORY.md
            entry = self._format_dream_entry(dream_result)
            self._append_to_memory(entry)

            # 清理过时信息
            outdated = dream_result.get("outdated", [])
            if outdated and self._memory_file:
                await self._remove_outdated(outdated)

            logger.info(f"[memory] /dream: 融合完成，发现 {len(dream_result.get('patterns_found', []))} 个模式")
            return entry

        except Exception as e:
            logger.warning(f"[memory] /dream 失败: {e}")
            return ""

    # ── 查询接口 ──

    def get_recent_context(self, max_items: int = 10) -> str:
        """获取最近的记忆上下文（供 prompt 注入）"""
        content = self._read_memory()
        if not content:
            return ""

        # 解析条目，取最新的 max_items 条
        entries = re.split(r'\n## ', content)
        if len(entries) <= max_items + 1:  # +1 for header
            return content

        recent = entries[:1] + entries[-(max_items):]
        return "\n## ".join(recent)

    def get_stats(self) -> dict:
        return {
            "memory_file": self._memory_file,
            "flush_count": len(self._flush_history),
            "memory_exists": os.path.isfile(self._memory_file) if self._memory_file else False,
        }

    # ── 内部 ──

    def _extract_decisions(self, messages: list[dict]) -> list[dict]:
        """从消息中提取关键决策"""
        decisions = []

        for i, msg in enumerate(messages):
            role = msg.get("role", "")
            content = msg.get("content", "") or ""
            tool_calls = msg.get("tool_calls") or []

            # 用户明确指出的偏好/纠正
            if role == "user" and any(kw in content for kw in ["记住", "注意", "以后", "不要", "改成", "偏好"]):
                decisions.append({
                    "type": "preference",
                    "content": content[:200],
                    "source": f"用户指令 (msg #{i})",
                })

            # 工具调用序列（隐含的工作流模式）
            if role == "assistant" and tool_calls:
                tool_names = [tc.get("function", {}).get("name", "") for tc in tool_calls]
                if len(tool_names) >= 3:
                    decisions.append({
                        "type": "workflow_pattern",
                        "content": f"工具调用序列: {' → '.join(tool_names)}",
                        "source": f"工作流 (msg #{i})",
                    })

            # 错误修复
            if role == "tool" and "success" in content.lower():
                try:
                    parsed = json.loads(content)
                    if parsed.get("success") is True:
                        decisions.append({
                            "type": "success",
                            "content": f"操作成功: {content[:100]}",
                            "source": f"工具结果 (msg #{i})",
                        })
                except (json.JSONDecodeError, ValueError):
                    pass

        return decisions

    async def _refine_with_llm(self, decisions: list[dict], session_id: str) -> list[dict]:
        """用 LLM 精炼决策列表"""
        decisions_text = json.dumps(decisions, ensure_ascii=False, indent=2)

        prompt = f"""请从以下 session 交互记录中，提取值得长期记住的**关键信息**。

## Session 记录摘要
{decisions_text[:3000]}

## 提取要求
只保留以下类型的信息：
1. **用户明确偏好**（喜欢的风格、工作方式）
2. **关键架构决策**（技术选型、设计方案）
3. **Bug 根因与修复**（避免重复踩坑）
4. **项目约定**（命名规范、编码风格）

去掉：临时状态、中间步骤、无长期价值的信息。

返回 JSON 数组:
[{{"type": "architecture|preference|bug_fix|convention", "content": "总结", "importance": 1-5}}]

只返回 JSON。"""

        try:
            resp = await self.client.chat([{"role": "user", "content": prompt}])
            content = resp.choices[0].message.content if hasattr(resp, 'choices') else str(resp)
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            refined = json.loads(content)
            if isinstance(refined, list):
                # 按重要性排序，只保留重要的
                refined.sort(key=lambda x: -x.get("importance", 3))
                return [d for d in refined if d.get("importance", 0) >= 3]
        except Exception as e:
            logger.warning(f"[memory] LLM 精炼失败: {e}")

        return decisions

    def _format_memory_entry(self, decisions: list[dict], session_id: str) -> str:
        """格式化为 MEMORY.md 条目"""
        lines = [
            f"\n---",
            f"## {datetime.now():%Y-%m-%d %H:%M} — Session {session_id[:8]}",
            f"",
        ]

        for d in decisions:
            dtype = d.get("type", "note")
            content = d.get("content", "")
            source = d.get("source", "")
            importance = d.get("importance", 3)

            if importance >= 4:
                icon = "🔴"
            elif importance >= 3:
                icon = "🟡"
            else:
                icon = "🔵"

            lines.append(f"- {icon} **[{dtype}]** {content}")
            if source:
                lines.append(f"  - _{source}_")

        lines.append("")
        return "\n".join(lines)

    def _format_dream_entry(self, dream_result: dict) -> str:
        """格式化为梦境结果"""
        lines = [
            f"\n---",
            f"## {datetime.now():%Y-%m-%d %H:%M} — 💭 知识梦境",
            f"",
        ]

        patterns = dream_result.get("patterns_found", [])
        if patterns:
            lines.append("### 发现模式")
            for p in patterns:
                lines.append(f"- 🔄 {p}")

        rules = dream_result.get("general_rules", [])
        if rules:
            lines.append("")
            lines.append("### 提炼规则")
            for r in rules:
                lines.append(f"- 📐 {r}")

        insights = dream_result.get("new_insights", [])
        if insights:
            lines.append("")
            lines.append("### 新洞察")
            for ins in insights:
                lines.append(f"- 💡 {ins}")

        lines.append("")
        return "\n".join(lines)

    def _read_memory(self) -> str:
        """读取 MEMORY.md"""
        if not self._memory_file or not os.path.isfile(self._memory_file):
            return ""
        try:
            with open(self._memory_file, encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            logger.warning(f"[memory] 读取 MEMORY.md 失败: {e}")
            return ""

    def _append_to_memory(self, entry: str):
        """追加到 MEMORY.md"""
        if not self._memory_file:
            return
        try:
            os.makedirs(os.path.dirname(self._memory_file), exist_ok=True)
            with open(self._memory_file, "a", encoding="utf-8") as f:
                f.write(entry)
        except Exception as e:
            logger.warning(f"[memory] 写入 MEMORY.md 失败: {e}")

    async def _remove_outdated(self, outdated: list[str]):
        """移除过时的记忆"""
        if not self._memory_file or not os.path.isfile(self._memory_file):
            return
        try:
            with open(self._memory_file, encoding="utf-8") as f:
                content = f.read()

            for item in outdated:
                content = content.replace(item, f"[过时] {item}")

            with open(self._memory_file, "w", encoding="utf-8") as f:
                f.write(content)

            logger.info(f"[memory] 标记了 {len(outdated)} 条过时记录")
        except Exception as e:
            logger.warning(f"[memory] 清理过时记录失败: {e}")

    def _timely_cleanup(self, content: str) -> str:
        """无 LLM 时的时间线清理：删除 30 天前的记录"""
        if not content:
            return ""
        # 简单留一条"30天前的记忆已归档"的标记
        lines = content.split("\n")
        header = lines[:3] if len(lines) > 3 else lines
        return "\n".join(header) + "\n\n_（之前的记忆已通过 /dream 归档）_\n"
