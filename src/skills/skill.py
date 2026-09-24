import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from utils.frontmatter import extract_frontmatter

logger = logging.getLogger("agent.skills.skill")


@dataclass
class Skill:
    name: str
    description: str
    version: str = "1.0.0"
    author: str = ""
    tags: list[str] = field(default_factory=list)
    enabled: bool = True
    prompt_template: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)
    variables: list[dict[str, Any]] = field(default_factory=list)
    output_format: str = "markdown"
    skill_dir: str = ""
    # 可见性限制(frontmatter 可选): 空列表=该维度不限; 两维度都非空须同时命中(AND)
    departments: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    # 限制字段类型非法(见 _parse_access_list): fail-closed, 该技能对所有用户不可见
    access_invalid: bool = False

    def is_available_to(self, department: str = "", role: str = "") -> bool:
        """技能对给定用户是否可用(部门/角色维度, 与市场 services/access.py 同语义)。

        fail-closed: 维度非空时用户值缺失或未命中即不可用; 缺省(空)维度不限制。
        """
        if self.access_invalid:
            return False
        return (self._dimension_allows(self.departments, department)
                and self._dimension_allows(self.roles, role))

    @staticmethod
    def _dimension_allows(required: list[str], actual: str) -> bool:
        if not required:
            return True
        return bool(actual) and actual in required

    def get_info(self) -> dict[str, Any]:
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

    def render_prompt(self, variables: dict[str, Any]) -> str:
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

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return self.tools


@dataclass
class SkillResult:
    success: bool
    data: Any = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class SkillManager:
    SKILL_FILE = "SKILL.md"
    REFERENCE_DIR = "references"
    SCRIPTS_DIR = "scripts"
    ASSETS_DIR = "assets"

    def __init__(self, skills_dir: str):
        self.skills_dir = skills_dir
        self.skills: dict[str, Skill] = {}
        self.tools: list[dict[str, Any]] = []
        self._active_skills: dict[str, str] = {}
        self._load_all()
        self._build_builtin_tools()

    def _load_all(self) -> int:
        if not os.path.exists(self.skills_dir):
            logger.warning(f"Skills目录不存在: {self.skills_dir}")
            return 0

        loaded = 0
        for item in os.listdir(self.skills_dir):
            skill_path = os.path.join(self.skills_dir, item)
            if os.path.isdir(skill_path) and self._load_skill(skill_path):
                logger.debug(f"加载技能: {item}")
                loaded += 1
        return loaded

    def _load_skill(self, skill_dir: str) -> Skill | None:
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

            departments, dept_invalid = self._parse_access_list(
                front_matter.get("departments"), "departments", skill_file)
            roles, role_invalid = self._parse_access_list(
                front_matter.get("roles"), "roles", skill_file)

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
                skill_dir=skill_dir,
                departments=departments,
                roles=roles,
                access_invalid=dept_invalid or role_invalid,
            )

            self.skills[skill.name] = skill
            return skill

        except Exception as e:
            logger.error(f"加载技能失败: {skill_dir}, 错误: {e}")
            return None

    @staticmethod
    def _parse_access_list(raw: Any, field: str, skill_file: str) -> tuple[list[str], bool]:
        """解析 departments/roles 限制字段 → (值列表, 是否类型非法)。

        容错规则(fail-closed):
        - 缺省(键不存在/None)或空列表 → ([], False), 该维度不限(保持既有技能行为);
        - 非空列表 → 只取非空字符串项; 清洗后为空(全部非法项) → ([], True), 恒不可见;
        - 非列表类型(如手写 ``roles: admin``) → ([], True), 恒不可见并记 WARNING。
        """
        if raw is None:
            return [], False
        if not isinstance(raw, list):
            logger.warning(
                f"技能 {skill_file} 的 {field} 必须是列表(实际 {type(raw).__name__}), "
                "按 fail-closed 处理: 该技能对所有用户不可见")
            return [], True
        values = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                values.append(item.strip())
            else:
                logger.warning(f"技能 {skill_file} 的 {field} 含非法项(忽略): {item!r}")
        if raw and not values:
            logger.warning(
                f"技能 {skill_file} 的 {field} 无有效项, "
                "按 fail-closed 处理: 该技能对所有用户不可见")
            return [], True
        return values, False

    @staticmethod
    def _build_skill_tool_defs(skills: list["Skill"]) -> list[dict[str, Any]]:
        """构建 skill 内置工具定义, <available_skills> 只列传入的技能。"""
        skills_xml = ""
        for s in skills:
            skills_xml += f"  <skill>\n    <name>{s.name}</name>\n    <description>{s.description}</description>\n  </skill>\n"
        skill_block = f"\n<available_skills>\n{skills_xml}</available_skills>" if skills_xml else ""
        return [{
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

    def _build_builtin_tools(self):
        """构建内置工具定义(全量已启用技能的目录快照, 供初始化/团队技能合并后刷新)。"""
        self._builtin_tool_defs = self._build_skill_tool_defs(
            [s for s in self.skills.values() if s.enabled])

    @staticmethod
    def _current_user_access() -> tuple[str, str]:
        """当前 run 用户的 (部门, 显式角色); 全空=渠道未解析身份, 受限技能不可见。"""
        from agent.core import current_run  # 延迟导入: 避免 skills <-> agent.core 循环依赖
        rc = current_run()
        return rc.user_department, rc.user_role

    def visible_skills(self) -> list[Skill]:
        """当前 run 用户可见的已启用技能(按部门/显式角色过滤)。

        机制与 tool_search 一致: 读进程内 contextvar(_current_run)。技能清单在 run 内
        由 Agent 组装工具表时生成, 故能拿到该 run 的用户部门/角色; 渠道未解析
        部门/角色时其值为空 → 受限技能不可见(fail-closed)。
        """
        department, role = self._current_user_access()
        return [s for s in self.skills.values()
                if s.enabled and s.is_available_to(department, role)]

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """skill 工具定义; <available_skills> 按当前 run 用户动态过滤。"""
        return self._build_skill_tool_defs(self.visible_skills())

    def get_skill_names(self) -> list[str]:
        return [s.name for s in self.skills.values() if s.enabled]

    async def execute_tool(self, tool_name: str, args: dict[str, Any]) -> str:
        if tool_name in ("skill", "execute_skill"):
            return await self._execute_skill(args)
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    async def _execute_skill(self, args: dict[str, Any]) -> str:
        skill_name = args.get("name") or args.get("skill_name", "")
        user_input = args.get("user_input", "")

        skill = self.skills.get(skill_name)
        if not skill:
            available = self.list_visible_skills()
            return json.dumps({"error": f"Skill not found: {skill_name}", "available_skills": available}, ensure_ascii=False)

        # 执行前二次校验(防绕过 <available_skills> 清单直接点名调用): 无权技能一律拒绝
        if not skill.is_available_to(*self._current_user_access()):
            logger.warning(f"拒绝执行无权访问的技能: {skill_name}")
            return json.dumps({
                "error": f"技能 {skill_name} 当前不可用（无权访问）",
                "available_skills": self.list_visible_skills(),
            }, ensure_ascii=False)

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

    def list_skills(self) -> list[str]:
        """全量已启用技能名(目录快照: 团队技能合并/日志/自动路由等非 LLM 场景)。"""
        return [skill.name for skill in self.skills.values() if skill.enabled]

    def list_visible_skills(self) -> list[str]:
        """当前 run 用户可见的技能名(校验失败提示用, 不泄漏受限技能名)。"""
        return [skill.name for skill in self.visible_skills()]

    def get_skill(self, name: str) -> Skill | None:
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

    def reload_skill(self, skill_dir: str) -> Skill | None:
        """热加载指定目录的技能（如果已存在则覆盖）"""
        skill = self._load_skill(skill_dir)
        if skill:
            logger.info(f"热加载技能: {skill.name}")
        return skill
