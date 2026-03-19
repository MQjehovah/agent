import os
import json
import logging
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("agent.skills.skill")


@dataclass
class SkillResult:
    success: bool
    data: Any = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


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
    examples: List[Dict[str, str]] = field(default_factory=list)
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
        for key, value in variables.items():
            prompt = prompt.replace("{{" + key + "}}", str(value))
            prompt = prompt.replace("{" + key + "}", str(value))
        
        return prompt
    
    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        return self.tools


class SkillLoader:
    SKILL_FILE = "skill.json"
    PROMPT_FILE = "prompt.md"
    EXAMPLES_DIR = "examples"
    
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
                    loaded += 1
        
        logger.info(f"加载了 {loaded} 个技能")
        return loaded
    
    def load_skill(self, skill_dir: str) -> Optional[Skill]:
        skill_file = os.path.join(skill_dir, self.SKILL_FILE)
        
        if not os.path.exists(skill_file):
            logger.warning(f"未找到skill.json: {skill_dir}")
            return None
        
        try:
            with open(skill_file, encoding="utf-8") as f:
                data = json.load(f)
            
            prompt_template = self._load_prompt(skill_dir)
            examples = self._load_examples(skill_dir)
            
            skill = Skill(
                name=data.get("name", os.path.basename(skill_dir)),
                description=data.get("description", ""),
                version=data.get("version", "1.0.0"),
                author=data.get("author", ""),
                tags=data.get("tags", []),
                enabled=data.get("enabled", True),
                prompt_template=prompt_template,
                tools=data.get("tools", []),
                examples=examples,
                variables=data.get("variables", []),
                output_format=data.get("output_format", "markdown"),
                skill_dir=skill_dir
            )
            
            self.skills[skill.name] = skill
            logger.info(f"加载技能: {skill.name}")
            return skill
            
        except Exception as e:
            logger.error(f"加载技能失败: {skill_dir}, 错误: {e}")
            return None
    
    def _load_prompt(self, skill_dir: str) -> str:
        prompt_file = os.path.join(skill_dir, self.PROMPT_FILE)
        if os.path.exists(prompt_file):
            with open(prompt_file, encoding="utf-8") as f:
                return f.read()
        return ""
    
    def _load_examples(self, skill_dir: str) -> List[Dict[str, str]]:
        examples_dir = os.path.join(skill_dir, self.EXAMPLES_DIR)
        examples = []
        
        if os.path.exists(examples_dir):
            for file_name in os.listdir(examples_dir):
                if file_name.endswith(".json"):
                    file_path = os.path.join(examples_dir, file_name)
                    try:
                        with open(file_path, encoding="utf-8") as f:
                            examples.append(json.load(f))
                    except Exception as e:
                        logger.warning(f"加载示例失败: {file_path}, 错误: {e}")
        
        return examples
    
    def get(self, name: str) -> Optional[Skill]:
        return self.skills.get(name)
    
    def list_skills(self) -> List[Dict[str, Any]]:
        return [skill.get_info() for skill in self.skills.values() if skill.enabled]
    
    def get_skills_description(self) -> str:
        if not self.skills:
            return "没有可用的技能"
        
        lines = ["技能列表:\n"]
        for skill in self.skills.values():
            if skill.enabled:
                lines.append(f"[{skill.name}]")
                lines.append(f"  描述: {skill.description}")
                if skill.tools:
                    lines.append(f"  工具: {', '.join([t.get('name', '') for t in skill.tools])}")
                lines.append("")
        
        return "\n".join(lines)
    
    def create_skill(self, name: str, description: str = "") -> str:
        skill_dir = os.path.join(self.skills_dir, name)
        os.makedirs(skill_dir, exist_ok=True)
        
        skill_data = {
            "name": name,
            "description": description or f"{name} skill",
            "version": "1.0.0",
            "author": "",
            "tags": [],
            "enabled": True,
            "tools": [],
            "variables": [],
            "output_format": "markdown"
        }
        
        skill_file = os.path.join(skill_dir, self.SKILL_FILE)
        with open(skill_file, "w", encoding="utf-8") as f:
            json.dump(skill_data, f, ensure_ascii=False, indent=2)
        
        prompt_file = os.path.join(skill_dir, self.PROMPT_FILE)
        with open(prompt_file, "w", encoding="utf-8") as f:
            f.write(f"# {name}\n\n请在此编写提示词模板...\n\n使用 {{{{variable}}}} 作为变量占位符。\n")
        
        logger.info(f"创建技能目录: {skill_dir}")
        return skill_dir