import logging
import uuid
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from datetime import datetime
import os
import re

logger = logging.getLogger("agent.subagent")


@dataclass
class SubagentConfig:
    name: str
    description: str = ""
    system_prompt: str = ""
    tools: List[str] = field(default_factory=list)
    max_iterations: int = 50
    filename: str = ""


@dataclass
class SubagentResult:
    subagent_id: str
    status: str
    result: str
    iterations: int
    error: Optional[str] = None
    completed_at: str = field(
        default_factory=lambda: datetime.now().isoformat())


class Subagent:
    def __init__(
        self,
        task: str,
        config: SubagentConfig,
        client,
        tool_registry,
        mcp_manager=None,
        skill_manager=None
    ):
        self.task = task
        self.config = config
        self.client = client
        self.tool_registry = tool_registry
        self.mcp = mcp_manager
        self.skill_manager = skill_manager
        self.subagent_id = str(uuid.uuid4())[:8]
        self.status = "pending"
        self.result: Optional[str] = None
        self.error: Optional[str] = None
        self.iterations = 0
        self.messages: List[Dict[str, Any]] = []

        if config.system_prompt:
            self.messages.append(
                {"role": "system", "content": config.system_prompt})

        self.messages.append({"role": "user", "content": task})

    @property
    def tool_defs(self) -> List[Dict[str, Any]]:
        tools = []

        all_tools = self.tool_registry.get_tool_definitions()

        if self.mcp:
            all_tools.extend(self.mcp.tool_defs)

        if self.skill_manager:
            all_tools.extend(self.skill_manager.get_tool_definitions())

        if self.config.tools:
            tool_names = set(self.config.tools)
            tools = [t for t in all_tools if t.get(
                "function", {}).get("name") in tool_names]
        else:
            tools = all_tools

        return tools

    async def run(self) -> SubagentResult:
        self.status = "running"
        logger.info(
            f"Subagent [{self.config.name}] ({self.subagent_id}) 开始执行: {self.task[:50]}...")

        try:
            for i in range(self.config.max_iterations):
                self.iterations = i + 1
                logger.debug(
                    f"Subagent [{self.config.name}] iteration {i + 1}")

                response = await self._think()
                msg = response.get("message", {})

                self.messages.append({
                    "role": "assistant",
                    "content": msg.get("content"),
                    "tool_calls": msg.get("tool_calls")
                })

                if msg.get("tool_calls"):
                    for tc in msg.get("tool_calls", []):
                        func_name = tc.get("function", {}).get("name", "")
                        func_args = tc.get("function", {}).get("arguments", {})

                        if isinstance(func_args, str):
                            import json
                            try:
                                func_args = json.loads(func_args)
                            except:
                                func_args = {}

                        logger.info(
                            f"Subagent [{self.config.name}] -> 调用工具: {func_name}")
                        result = await self._execute_tool(func_name, func_args)
                        logger.info(
                            f"Subagent [{self.config.name}] <- {func_name} 完成")

                        self.messages.append({
                            "role": "tool",
                            "content": result,
                            "tool_call_id": tc.get("id", "")
                        })

                    continue

                if msg.get("content"):
                    self.status = "completed"
                    self.result = msg.get("content")
                    logger.info(
                        f"Subagent [{self.config.name}] ({self.subagent_id}) 完成")
                    break
            else:
                self.status = "max_iterations"
                self.result = "达到最大迭代次数"
                logger.warning(f"Subagent [{self.config.name}] 达到最大迭代次数")

        except Exception as e:
            self.status = "failed"
            self.error = str(e)
            logger.error(f"Subagent [{self.config.name}] 失败: {e}")

        return SubagentResult(
            subagent_id=self.subagent_id,
            status=self.status,
            result=self.result or "",
            iterations=self.iterations,
            error=self.error
        )

    async def _think(self) -> Dict[str, Any]:
        try:
            response = self.client.chat(
                self.messages,
                self.tool_defs,
                stream=False
            )

            choice = response.choices[0]
            msg = choice.message

            tool_calls = None
            if msg.tool_calls:
                tool_calls = []
                for tc in msg.tool_calls:
                    tool_calls.append({
                        "id": tc.id,
                        "type": tc.type,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments
                        }
                    })

            return {
                "message": {
                    "content": msg.content,
                    "tool_calls": tool_calls
                }
            }
        except Exception as e:
            logger.error(f"Subagent think error: {e}")
            return {"message": {"content": f"思考出错: {e}"}}

    async def _execute_tool(self, name: str, args: Dict) -> str:
        try:
            if self.tool_registry.has_tool(name):
                return await self.tool_registry.execute(name, args)
            if self.skill_manager and name == "execute_skill":
                return await self.skill_manager.execute_tool(name, args)
            if self.mcp:
                return await self.mcp.call_tool(name, args)
            return f"工具 {name} 不存在"
        except Exception as e:
            return f"工具执行错误: {e}"


class SubagentManager:
    def __init__(self, base_dir: str = ""):
        self.base_dir = base_dir
        self.configs: Dict[str, SubagentConfig] = {}
        self._load_all()

    def _load_all(self):
        if not self.base_dir or not os.path.exists(self.base_dir):
            logger.warning(
                f"Agent config directory not found: {self.base_dir}")
            return

        for filename in os.listdir(self.base_dir):
            if filename.endswith(".md"):
                filepath = os.path.join(self.base_dir, filename)
                try:
                    config = self._parse_file(filepath)
                    if config:
                        self.configs[config.name] = config
                        logger.info(f"Loaded subagent config: {config.name}")
                except Exception as e:
                    logger.error(f"Failed to parse {filename}: {e}")

    def _parse_file(self, filepath: str) -> Optional[SubagentConfig]:
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

        description = frontmatter.get("description", "")
        if isinstance(description, str):
            description = description.strip()

        tools = frontmatter.get("tools", [])
        if isinstance(tools, str):
            tools = [t.strip() for t in tools.split(",") if t.strip()]

        system_prompt = body.strip() if body else ""

        return SubagentConfig(
            name=name,
            description=description,
            system_prompt=system_prompt,
            tools=tools,
            max_iterations=frontmatter.get("max_iterations", 50),
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

    def get(self, name: str) -> Optional[SubagentConfig]:
        return self.configs.get(name)

    def get_subagent_prompt(self) -> str:
        if not self.configs:
            return "没有可用的子代理"

        lines = ["\n## 【SubAgent列表】\n"]
        for config in self.configs.values():
            lines.append(f"[名称：{config.name}]")
            if config.description:
                lines.append(f"描述: {config.description}")
            if config.tools:
                lines.append(f"工具: {', '.join(config.tools)}")
            lines.append("")

        return "\n".join(lines) + "\n通过subagent工具调用激活\n"

    def list_configs(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": c.name,
                "tools": c.tools,
                "max_iterations": c.max_iterations,
                "description": c.description[:100] if c.description else ""
            }
            for c in self.configs.values()
        ]

    def list_templates(self) -> List[Dict[str, Any]]:
        return self.list_configs()

    def create_config(
        self,
        name: str = "",
        system_prompt: str = "",
        tools: List[str] = None,
        max_iterations: int = 50,
        template: str = ""
    ) -> SubagentConfig:
        base_config = self.configs.get(template or name)

        if base_config:
            name = name or base_config.name
            system_prompt = system_prompt or base_config.system_prompt
            tools = tools if tools is not None else base_config.tools
            max_iterations = max_iterations if max_iterations != 50 else base_config.max_iterations
            if template:
                logger.info(f"使用配置 '{template}' 创建子代理")

        return SubagentConfig(
            name=name or f"subagent",
            system_prompt=system_prompt,
            tools=tools or [],
            max_iterations=max_iterations
        )

    def reload(self):
        self.configs.clear()
        self._load_all()
        logger.info(f"重新加载子代理配置: {list(self.configs.keys())}")
