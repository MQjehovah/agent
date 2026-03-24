import os
import re
import logging
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field

from subagent import SubagentTemplate

logger = logging.getLogger("agent.loader")


class AgentLoader:
    def __init__(self, config_dir: str = None):
        if not config_dir:
            config_dir = os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                "config", "agents"
            )
        self.config_dir = config_dir
        self.templates: Dict[str, SubagentTemplate] = {}
    
    def load_all(self) -> Dict[str, SubagentTemplate]:
        if not os.path.exists(self.config_dir):
            logger.warning(f"Agent config directory not found: {self.config_dir}")
            return {}
        
        for filename in os.listdir(self.config_dir):
            if filename.endswith(".md"):
                filepath = os.path.join(self.config_dir, filename)
                try:
                    template = self._parse_file(filepath)
                    if template:
                        self.templates[template.name] = template
                        logger.info(f"Loaded subagent template: {template.name}")
                except Exception as e:
                    logger.error(f"Failed to parse {filename}: {e}")
        
        return self.templates
    
    def _parse_file(self, filepath: str) -> Optional[SubagentTemplate]:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        
        frontmatter, body = self._extract_frontmatter(content)
        if not frontmatter:
            logger.warning(f"No frontmatter found in {filepath}")
            return None
        
        name = frontmatter.get("name")
        if not name:
            logger.warning(f"No 'name' in frontmatter: {filepath}")
            return None
        
        system_prompt = frontmatter.get("system_prompt", "")
        if isinstance(system_prompt, str):
            system_prompt = system_prompt.strip()
        
        tools = frontmatter.get("tools", [])
        if isinstance(tools, str):
            tools = [t.strip() for t in tools.split(",") if t.strip()]
        
        description = body.strip() if body else ""
        
        return SubagentTemplate(
            name=name,
            system_prompt=system_prompt,
            tools=tools,
            max_iterations=frontmatter.get("max_iterations", 50),
            description=description,
            filename=os.path.basename(filepath)
        )
    
    def _extract_frontmatter(self, content: str) -> tuple:
        pattern = r"^---\s*\n(.*?)\n---\s*\n?(.*)$"
        match = re.match(pattern, content, re.DOTALL)
        
        if not match:
            return {}, content
        
        frontmatter_str = match.group(1)
        body = match.group(2)
        
        import yaml
        try:
            frontmatter = yaml.safe_load(frontmatter_str) or {}
        except yaml.YAMLError as e:
            logger.error(f"YAML parse error: {e}")
            return {}, content
        
        return frontmatter, body
    
    def get_template(self, name: str) -> Optional[SubagentTemplate]:
        return self.templates.get(name)
    
    def list_templates(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": t.name,
                "tools": t.tools,
                "max_iterations": t.max_iterations,
                "description": t.description[:100] if t.description else ""
            }
            for t in self.templates.values()
        ]
    
    def reload(self) -> Dict[str, SubagentTemplate]:
        self.templates.clear()
        return self.load_all()