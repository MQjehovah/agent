import os
import json
import logging
import re
from typing import Dict, Any, List, Optional, Callable, Awaitable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("agent.skills.skill")


@dataclass
class Skill:
    name: str
    description: str
    version: str = "1.0.0"
    author: str = ""
    tags: List[str] = field(default_factory=list)
    enabled: bool = True
    prompt_template: str = ""
    tools: List[Dict[str, Any]] = field(default_factory=list)
    references: List[Dict[str, str]] = field(default_factory=list)
    variables: List[Dict[str, Any]] = field(default_factory=list)
    output_format: str = "markdown"
    skill_dir: str = ""

    def get_info(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "author": self.author,
            "tags": self.tags,
            "enabled": self.enabled,
            "tools": [t.get("name") for t in self.tools],
            "has_prompt": bool(self.prompt_template)
        }

    def render_prompt(self, variables: Dict[str, Any]) -> str:
        if not self.prompt_template:
            return ""

        prompt = self.prompt_template
        # for key, value in variables.items():
        #     prompt = prompt.replace("{{" + key + "}}", str(value))
        #     prompt = prompt.replace("{" + key + "}", str(value))
        prompt = prompt + "\n".join(self.references)

        return prompt

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        return self.tools


@dataclass
class SkillResult:
    success: bool
    data: Any = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class SkillLoader:
    SKILL_FILE = "SKILL.md"
    REFERENCE_DIR = "references"

    def __init__(self, skills_dir: str):
        self.skills_dir = skills_dir
        self.skills: Dict[str, Skill] = {}

    def load_all(self) -> int:
        if not os.path.exists(self.skills_dir):
            logger.warning(f"Skills目录不存在: {self.skills_dir}")
            return 0

        loaded = 0
        for item in os.listdir(self.skills_dir):
            skill_path = os.path.join(self.skills_dir, item)
            if os.path.isdir(skill_path):
                if self.load_skill(skill_path):
                    logger.debug(f"加载技能: {item}")
                    loaded += 1

        return loaded

    def load_skill(self, skill_dir: str) -> Optional[Skill]:
        skill_file = os.path.join(skill_dir, self.SKILL_FILE)

        if not os.path.exists(skill_file):
            logger.warning(f"未找到SKILL.md: {skill_dir}")
            return None

        try:
            with open(skill_file, encoding="utf-8") as f:
                content = f.read()

            front_matter, prompt_template = self._parse_skill(content)
            if not front_matter:
                logger.warning(f"SKILL.md格式错误: {skill_file}")
                return None

            name = front_matter.get("name", os.path.basename(skill_dir))
            description = front_matter.get("description", "")
            references = self._load_references(skill_dir)

            skill = Skill(
                name=name,
                description=description,
                version=front_matter.get("version", "1.0.0"),
                author=front_matter.get("author", ""),
                tags=front_matter.get("tags", []),
                enabled=front_matter.get("enabled", True),
                prompt_template=prompt_template,
                tools=front_matter.get("tools", []),
                references=references,
                variables=front_matter.get("variables", []),
                output_format=front_matter.get("output_format", "markdown"),
                skill_dir=skill_dir
            )

            self.skills[skill.name] = skill
            # logger.info(f"加载技能: {skill.name}")
            return skill

        except Exception as e:
            logger.error(f"加载技能失败: {skill_dir}, 错误: {e}")
            return None

    def _parse_skill(self, content: str) -> tuple:
        pattern = r'^---\s*\n(.*?)\n---\s*\n(.*)$'
        match = re.match(pattern, content, re.DOTALL)

        if not match:
            return None, content

        front_matter_str = match.group(1)
        prompt_template = match.group(2).strip()

        front_matter = {}
        for line in front_matter_str.split('\n'):
            if ':' in line:
                key, value = line.split(':', 1)
                key = key.strip()
                value = value.strip()

                if value.startswith('[') and value.endswith(']'):
                    items = [item.strip().strip('"\'')
                             for item in value[1:-1].split(',')]
                    front_matter[key] = [item for item in items if item]
                elif value.lower() == 'true':
                    front_matter[key] = True
                elif value.lower() == 'false':
                    front_matter[key] = False
                else:
                    front_matter[key] = value.strip('"\'')

        return front_matter, prompt_template

    def _load_references(self, skill_dir: str) -> List[Dict[str, str]]:
        references_dir = os.path.join(skill_dir, self.REFERENCE_DIR)
        references = []

        if os.path.exists(references_dir):
            for file_name in os.listdir(references_dir):
                if file_name.endswith(".json"):
                    file_path = os.path.join(references_dir, file_name)
                    try:
                        with open(file_path, encoding="utf-8") as f:
                            references.append(json.load(f))
                    except Exception as e:
                        logger.warning(f"加载示例失败: {file_path}, 错误: {e}")

        return references

    def get(self, name: str) -> Optional[Skill]:
        return self.skills.get(name)

    def list_skills(self) -> List[str]:
        return [skill.name for skill in self.skills.values() if skill.enabled]

    def get_skills_prompt(self) -> str:
        if not self.skills:
            return "没有可用的技能"

        lines = ["\n\n## 【技能列表】\n"]
        for skill in self.skills.values():
            if skill.enabled:
                lines.append(f"  名称：{skill.name}")
                lines.append(f"  描述: {skill.description}")
                if skill.tools:
                    lines.append(
                        f"  工具: {', '.join([t.get('name', '') for t in skill.tools])}")
                lines.append("")

        return "\n".join(lines) + "\n通过execute_skill工具调用激活"

    def create_skill(self, name: str, description: str = "") -> str:
        skill_dir = os.path.join(self.skills_dir, name)
        os.makedirs(skill_dir, exist_ok=True)

        skill_md_content = f'''---
name: {name}
description: {description or f"{name} skill"}
---

# {name}

## Overview

{description or f"{name} skill description"}

## When to Use

- 

## Workflow

1. 
2. 

## Output Format

### Result

{{{{result}}}}
'''

        skill_file = os.path.join(skill_dir, self.SKILL_FILE)
        with open(skill_file, "w", encoding="utf-8") as f:
            f.write(skill_md_content)

        logger.info(f"创建技能目录: {skill_dir}")
        return skill_dir


class SkillManager:
    def __init__(self, skills_dir: str):
        self.skills_dir = skills_dir
        self.tools: List[Dict[str, Any]] = []
        self.loader: Optional[SkillLoader] = SkillLoader(self.skills_dir)
        loaded = self.loader.load_all()
        self._build_builtin_tools()

    def _build_builtin_tools(self):
        self._builtin_tool_defs = [{
            "type": "function",
            "function": {
                "name": "execute_skill",
                "description": f"""执行指定技能

    参数:
    - skill_name: 技能名称
    - user_input: 用户输入
    
    返回技能执行结果
    """,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill_name": {
                            "type": "string",
                            "description": "技能名称"
                        },
                        "user_input": {
                            "type": "string",
                            "description": "用户输入"
                        }
                    },
                    "required": ["skill_name"]
                }
            }
        }]

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        return self._builtin_tool_defs

    def get_skill_names(self) -> List[str]:
        if not self.loader:
            return []
        return [s.name for s in self.loader.skills.values() if s.enabled]

    async def execute_tool(self, tool_name: str, args: Dict[str, Any]) -> str:
        if tool_name == "execute_skill":
            return await self._execute_skill(args)
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    async def _execute_skill(self, args: Dict[str, Any]) -> str:
        skill_name = args.get("skill_name", "")
        user_input = args.get("user_input", "")

        if not self.loader:
            return json.dumps({"error": "Skill loader not initialized"})

        skill = self.loader.get(skill_name)
        if not skill:
            available = self.loader.list_skills()
            return json.dumps({"error": f"Skill not found: {skill_name}", "available_skills": available})

        result = skill.render_prompt({"user_input": user_input})

        logger.debug(f"Executing Skill: {skill_name} executed successfully")
        return result

    def list_skills(self) -> List[str]:
        if not self.loader:
            return []
        return self.loader.list_skills()

    def get_skill(self, name: str) -> Optional[Skill]:
        if not self.loader:
            return None
        return self.loader.get(name)

    def get_skills_prompt(self) -> str:
        if not self.loader:
            return "No skills loaded"
        return self.loader.get_skills_prompt()
