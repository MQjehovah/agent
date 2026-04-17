"""
Prompt 分层拼装器

结构：
┌─────────────────────────────────────┐
│ Static Section (可被 prompt cache)    │
│  - 角色定义                          │
│  - 行为规则                          │
│  - 工具使用指南                      │
│  - 工具描述汇总                      │
├────── DYNAMIC_BOUNDARY ──────────────┤
│ Dynamic Section (每轮可能变化)        │
│  - 环境上下文 (cwd, git, platform)   │
│  - 记忆上下文 (按任务筛选)            │
│  - 技能上下文 (按需加载)             │
│  - 子代理列表                        │
│  - 会话状态 (当前任务进度)           │
└─────────────────────────────────────┘
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger("agent.prompt")

DYNAMIC_BOUNDARY = "\n\n---\n\n"


@dataclass
class PromptSection:
    name: str
    content: str
    is_static: bool = True
    priority: int = 0


class PromptBuilder:
    def __init__(self):
        self._sections: list[PromptSection] = []

    def add(self, name: str, content: str, is_static: bool = True, priority: int = 0):
        """添加一个 prompt 区块（同名区块会被替换）"""
        if not content or not content.strip():
            return
        self._sections = [s for s in self._sections if s.name != name]
        self._sections.append(PromptSection(
            name=name, content=content.strip(),
            is_static=is_static, priority=priority
        ))

    def remove(self, name: str):
        self._sections = [s for s in self._sections if s.name != name]

    def build(self) -> tuple[str, str]:
        """构建最终 prompt，返回 (static_prompt, dynamic_prompt)"""
        static = []
        dynamic = []

        for section in sorted(self._sections, key=lambda s: s.priority):
            if section.is_static:
                static.append(f"## {section.name}\n\n{section.content}")
            else:
                dynamic.append(f"## {section.name}\n\n{section.content}")

        static_str = "\n\n".join(static)
        dynamic_str = DYNAMIC_BOUNDARY + "\n\n".join(dynamic) if dynamic else ""
        return static_str, dynamic_str

    def build_full(self) -> str:
        """构建完整 prompt"""
        s, d = self.build()
        return s + d

    def list_sections(self) -> list[dict]:
        return [
            {"name": s.name, "is_static": s.is_static,
             "priority": s.priority, "chars": len(s.content)}
            for s in sorted(self._sections, key=lambda s: s.priority)
        ]
