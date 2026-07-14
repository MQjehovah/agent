import os
import json
import logging
from typing import Dict, Any, List, Optional, Callable, Awaitable
from dataclasses import dataclass, field
from pathlib import Path

from utils.frontmatter import extract_frontmatter

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
            "has_prompt": bool(self.prompt_template),
            "has_references": os.path.isdir(os.path.join(self.skill_dir, "references")),
            "has_scripts": os.path.isdir(os.path.join(self.skill_dir, "scripts")),
            "has_assets": os.path.isdir(os.path.join(self.skill_dir, "assets")),
        }

    def render_prompt(self, variables: Dict[str, Any]) -> str:
        if not self.prompt_template:
            return ""

        prompt = self.prompt_template
        for key, value in variables.items():
            prompt = prompt.replace("{{" + key + "}}", str(value))
            prompt = prompt.replace("{" + key + "}", str(value))

        return prompt

    def load_references(self) -> str:
        """延迟加载 references/ 目录内容（激活阶段调用）"""
        ref_dir = os.path.join(self.skill_dir, "references")
        if not os.path.isdir(ref_dir):
            return ""

        ref_lines = []
        for file_name in sorted(os.listdir(ref_dir)):
            if not file_name.endswith(".json"):
                continue
            file_path = os.path.join(ref_dir, file_name)
            try:
                with open(file_path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    for item in data:
                        ref_lines.append(json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else str(item))
                else:
                    ref_lines.append(json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data))
            except Exception as e:
                logger.warning(f"加载参考文件失败: {file_path}, 错误: {e}")

        return "\n".join(ref_lines) if ref_lines else ""

    def scan_resources(self) -> str:
        """扫描 scripts/ 和 assets/ 目录，生成可用资源清单（激活阶段调用）"""
        sections = []

        scripts_dir = os.path.join(self.skill_dir, "scripts")
        if os.path.isdir(scripts_dir):
            scripts = [f for f in sorted(os.listdir(scripts_dir))
                       if f.endswith((".py", ".sh", ".js", ".ts"))]
            if scripts:
                lines = [
                    "## 可用脚本\n",
                    "注意：不要 cd 到脚本目录，直接用下面的命令执行即可。"
                    "脚本的产出文件会自动写入当前工作目录（workspace）。\n",
                ]
                for s in scripts:
                    script_path = os.path.abspath(os.path.join(scripts_dir, s))
                    ext = os.path.splitext(s)[1]
                    if ext == ".py":
                        hint = f"python \"{script_path}\""
                    elif ext == ".sh":
                        hint = f"bash \"{script_path}\""
                    elif ext in (".js", ".ts"):
                        hint = f"node \"{script_path}\""
                    else:
                        hint = f"\"{script_path}\""
                    lines.append(f"- `{s}` → `{hint}`")
                sections.append("\n".join(lines))

        assets_dir = os.path.join(self.skill_dir, "assets")
        if os.path.isdir(assets_dir):
            assets = sorted(os.listdir(assets_dir))
            if assets:
                lines = ["## 可用资源\n"]
                for a in assets:
                    asset_path = os.path.abspath(os.path.join(assets_dir, a))
                    if os.path.isfile(asset_path):
                        lines.append(f"- `{asset_path}`")
                    else:
                        lines.append(f"- `{asset_path}/`")
                sections.append("\n".join(lines))

        return "\n\n".join(sections)

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        return self.tools


@dataclass
class SkillResult:
    success: bool
    data: Any = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class SkillManager:
    SKILL_FILE = "SKILL.md"
    REFERENCE_DIR = "references"
    SCRIPTS_DIR = "scripts"
    ASSETS_DIR = "assets"

    def __init__(self, skills_dir: str):
        self.skills_dir = skills_dir
        self.skills: Dict[str, Skill] = {}
        self.tools: List[Dict[str, Any]] = []
        self._active_skills: Dict[str, str] = {}
        self._load_all()
        self._build_builtin_tools()

    def _load_all(self) -> int:
        if not os.path.exists(self.skills_dir):
            logger.warning(f"Skills目录不存在: {self.skills_dir}")
            return 0

        loaded = 0
        for item in os.listdir(self.skills_dir):
            skill_path = os.path.join(self.skills_dir, item)
            if os.path.isdir(skill_path):
                if self._load_skill(skill_path):
                    logger.debug(f"加载技能: {item}")
                    loaded += 1
        return loaded

    def _load_skill(self, skill_dir: str) -> Optional[Skill]:
        skill_file = os.path.join(skill_dir, self.SKILL_FILE)
        if not os.path.exists(skill_file):
            logger.warning(f"未找到SKILL.md: {skill_dir}")
            return None

        try:
            with open(skill_file, encoding="utf-8") as f:
                content = f.read()

            front_matter, prompt_template = extract_frontmatter(content)
            if not front_matter:
                logger.warning(f"SKILL.md格式错误: {skill_file}")
                return None

            name = front_matter.get("name", os.path.basename(skill_dir))
            description = front_matter.get("description", "")

            skill = Skill(
                name=name,
                description=description,
                version=front_matter.get("version", "1.0.0"),
                author=front_matter.get("author", ""),
                tags=front_matter.get("tags", []),
                enabled=front_matter.get("enabled", True),
                prompt_template=prompt_template,
                tools=front_matter.get("tools", []),
                variables=front_matter.get("variables", []),
                output_format=front_matter.get("output_format", "markdown"),
                skill_dir=skill_dir
            )

            self.skills[skill.name] = skill
            return skill

        except Exception as e:
            logger.error(f"加载技能失败: {skill_dir}, 错误: {e}")
            return None

    def _build_builtin_tools(self):
        skills_xml = ""
        for s in self.skills.values():
            if s.enabled:
                skills_xml += f"  <skill>\n    <name>{s.name}</name>\n    <description>{s.description}</description>\n  </skill>\n"
        skill_block = f"\n<available_skills>\n{skills_xml}</available_skills>" if skills_xml else ""
        self._builtin_tool_defs = [{
            "type": "function",
            "function": {
                "name": "skill",
                "description": f"加载并使用指定的技能指导完成任务。执行特定流程前先调用此工具加载对应技能，然后按技能指导执行。{skill_block}",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "技能名称"
                        },
                        "user_input": {
                            "type": "string",
                            "description": "用户输入或上下文，用于技能渲染"
                        }
                    },
                    "required": ["name"]
                }
            }
        }]

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        return self._builtin_tool_defs

    def get_skill_names(self) -> List[str]:
        return [s.name for s in self.skills.values() if s.enabled]

    async def execute_tool(self, tool_name: str, args: Dict[str, Any]) -> str:
        if tool_name in ("skill", "execute_skill"):
            return await self._execute_skill(args)
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    async def _execute_skill(self, args: Dict[str, Any]) -> str:
        skill_name = args.get("name") or args.get("skill_name", "")
        user_input = args.get("user_input", "")

        skill = self.skills.get(skill_name)
        if not skill:
            available = self.list_skills()
            return json.dumps({"error": f"Skill not found: {skill_name}", "available_skills": available}, ensure_ascii=False)

        prompt = skill.render_prompt({"user_input": user_input})

        if not prompt:
            return json.dumps({"error": f"技能 {skill_name} 没有可用的提示词模板"}, ensure_ascii=False)

        # 延迟加载参考资料
        references = skill.load_references()
        if references:
            prompt = prompt + "\n\n## 参考资料\n" + references

        # 扫描可用脚本和资源
        resources = skill.scan_resources()
        if resources:
            prompt = prompt + "\n\n" + resources

        self._active_skills[skill_name] = prompt

        result = (
            f"已激活技能: {skill_name}\n\n"
            f"请按照以下指导处理用户的请求:\n\n"
            f"{prompt}"
        )

        logger.info(f"Skill executed: {skill_name}")
        return result

    def list_skills(self) -> List[str]:
        return [skill.name for skill in self.skills.values() if skill.enabled]

    def get_skill(self, name: str) -> Optional[Skill]:
        return self.skills.get(name)

    def clear_active_skills(self):
        self._active_skills.clear()

    def get_active_skills_prompt(self) -> str:
        if not self._active_skills:
            return ""
        lines = []
        for name, prompt in self._active_skills.items():
            lines.append(f"### 技能: {name}\n\n{prompt}")
        return "\n\n".join(lines)

    def get_skills_prompt(self) -> str:
        if not self.skills:
            return ""

        lines = ["\n可用技能（通过 execute_skill 工具激活）:\n"]
        for skill in self.skills.values():
            if skill.enabled:
                lines.append(f"  - {skill.name}: {skill.description}")

        return "\n".join(lines)

    def create_skill(self, name: str, description: str = "") -> str:
        skill_dir = os.path.join(self.skills_dir, name)
        os.makedirs(skill_dir, exist_ok=True)
        os.makedirs(os.path.join(skill_dir, self.SCRIPTS_DIR), exist_ok=True)
        os.makedirs(os.path.join(skill_dir, self.REFERENCE_DIR), exist_ok=True)
        os.makedirs(os.path.join(skill_dir, self.ASSETS_DIR), exist_ok=True)

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

{{result}}
'''

        skill_file = os.path.join(skill_dir, self.SKILL_FILE)
        with open(skill_file, "w", encoding="utf-8") as f:
            f.write(skill_md_content)

        logger.info(f"创建技能目录: {skill_dir}")
        return skill_dir

    def reload_skill(self, skill_dir: str) -> Optional[Skill]:
        """热加载指定目录的技能（如果已存在则覆盖）"""
        skill = self._load_skill(skill_dir)
        if skill:
            logger.info(f"热加载技能: {skill.name}")
        return skill
