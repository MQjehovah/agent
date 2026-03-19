import os
import json
import logging
import re
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
    SKILL_MD_FILE = "SKILL.md"
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
        skill_md_file = os.path.join(skill_dir, self.SKILL_MD_FILE)
        
        if not os.path.exists(skill_md_file):
            logger.warning(f"未找到SKILL.md: {skill_dir}")
            return None
        
        try:
            with open(skill_md_file, encoding="utf-8") as f:
                content = f.read()
            
            front_matter, prompt_template = self._parse_skill_md(content)
            if not front_matter:
                logger.warning(f"SKILL.md格式错误: {skill_md_file}")
                return None
            
            name = front_matter.get("name", os.path.basename(skill_dir))
            description = front_matter.get("description", "")
            examples = self._load_examples(skill_dir)
            
            skill = Skill(
                name=name,
                description=description,
                version=front_matter.get("version", "1.0.0"),
                author=front_matter.get("author", ""),
                tags=front_matter.get("tags", []),
                enabled=front_matter.get("enabled", True),
                prompt_template=prompt_template,
                tools=front_matter.get("tools", []),
                examples=examples,
                variables=front_matter.get("variables", []),
                output_format=front_matter.get("output_format", "markdown"),
                skill_dir=skill_dir
            )
            
            self.skills[skill.name] = skill
            logger.info(f"加载技能: {skill.name}")
            return skill
            
        except Exception as e:
            logger.error(f"加载技能失败: {skill_dir}, 错误: {e}")
            return None
    
    def _parse_skill_md(self, content: str) -> tuple:
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
                    items = [item.strip().strip('"\'') for item in value[1:-1].split(',')]
                    front_matter[key] = [item for item in items if item]
                elif value.lower() == 'true':
                    front_matter[key] = True
                elif value.lower() == 'false':
                    front_matter[key] = False
                else:
                    front_matter[key] = value.strip('"\'')
        
        return front_matter, prompt_template
    
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
        
        skill_md_file = os.path.join(skill_dir, self.SKILL_MD_FILE)
        with open(skill_md_file, "w", encoding="utf-8") as f:
            f.write(skill_md_content)
        
        logger.info(f"创建技能目录: {skill_dir}")
        return skill_dir