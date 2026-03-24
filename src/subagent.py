import asyncio
import logging
import uuid
from typing import Optional, Dict, Any, List, Callable
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger("agent.subagent")


@dataclass
class SubagentTemplate:
    name: str
    system_prompt: str
    tools: List[str] = field(default_factory=list)
    max_iterations: int = 50
    description: str = ""
    filename: str = ""


@dataclass
class SubagentConfig:
    name: str
    task: str
    system_prompt: str = ""
    tools: List[str] = field(default_factory=list)
    max_iterations: int = 50
    parent_session_id: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass 
class SubagentResult:
    subagent_id: str
    status: str
    result: str
    iterations: int
    error: Optional[str] = None
    completed_at: str = field(default_factory=lambda: datetime.now().isoformat())


class Subagent:
    def __init__(
        self,
        config: SubagentConfig,
        client,
        tool_registry,
        mcp_manager=None,
        skill_manager=None
    ):
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
            self.messages.append({"role": "system", "content": config.system_prompt})
        
        self.messages.append({"role": "user", "content": config.task})
    
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
            tools = [t for t in all_tools if t.get("function", {}).get("name") in tool_names]
        else:
            tools = all_tools
        
        return tools
    
    async def run(self) -> SubagentResult:
        self.status = "running"
        logger.info(f"Subagent [{self.config.name}] ({self.subagent_id}) 开始执行: {self.config.task[:50]}...")
        
        try:
            for i in range(self.config.max_iterations):
                self.iterations = i + 1
                logger.debug(f"Subagent [{self.config.name}] iteration {i + 1}")
                
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
                        
                        logger.info(f"Subagent [{self.config.name}] -> 调用工具: {func_name}")
                        result = await self._execute_tool(func_name, func_args)
                        logger.info(f"Subagent [{self.config.name}] <- {func_name} 完成")
                        
                        self.messages.append({
                            "role": "tool",
                            "content": result,
                            "tool_call_id": tc.get("id", "")
                        })
                    
                    continue
                
                if msg.get("content"):
                    self.status = "completed"
                    self.result = msg.get("content")
                    logger.info(f"Subagent [{self.config.name}] ({self.subagent_id}) 完成")
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
    def __init__(self, agent):
        self.agent = agent
        self.subagents: Dict[str, Subagent] = {}
        self.results: Dict[str, SubagentResult] = {}
        self.templates: Dict[str, SubagentTemplate] = {}
    
    def load_templates(self, templates: Dict[str, "SubagentTemplate"]):
        self.templates.update(templates)
        logger.info(f"已加载 {len(templates)} 个子代理模板: {list(templates.keys())}")
    
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
    
    def create_subagent(
        self,
        task: str,
        name: str = "",
        system_prompt: str = "",
        tools: List[str] = None,
        max_iterations: int = 50,
        parent_session_id: str = "",
        template: str = ""
    ) -> str:
        if template and template in self.templates:
            t = self.templates[template]
            name = name or t.name
            system_prompt = system_prompt or t.system_prompt
            tools = tools if tools is not None else t.tools
            max_iterations = max_iterations or t.max_iterations
            logger.info(f"使用模板 '{template}' 创建子代理")
        
        subagent_name = name or f"subagent_{len(self.subagents) + 1}"
        
        config = SubagentConfig(
            name=subagent_name,
            task=task,
            system_prompt=system_prompt,
            tools=tools or [],
            max_iterations=max_iterations,
            parent_session_id=parent_session_id
        )
        
        subagent = Subagent(
            config=config,
            client=self.agent.client,
            tool_registry=self.agent.tool_registry,
            mcp_manager=self.agent.mcp if hasattr(self.agent, 'mcp') else None,
            skill_manager=self.agent.skill_manager
        )
        
        self.subagents[subagent.subagent_id] = subagent
        logger.info(f"创建 Subagent: {subagent_name} ({subagent.subagent_id})")
        
        return subagent.subagent_id
    
    async def run_subagent(self, subagent_id: str) -> SubagentResult:
        subagent = self.subagents.get(subagent_id)
        if not subagent:
            return SubagentResult(
                subagent_id=subagent_id,
                status="failed",
                result="",
                iterations=0,
                error="Subagent not found"
            )
        
        result = await subagent.run()
        self.results[subagent_id] = result
        return result
    
    async def create_and_run(
        self,
        task: str,
        name: str = "",
        system_prompt: str = "",
        tools: List[str] = None,
        max_iterations: int = 50,
        template: str = ""
    ) -> SubagentResult:
        subagent_id = self.create_subagent(
            task=task,
            name=name,
            system_prompt=system_prompt,
            tools=tools,
            max_iterations=max_iterations,
            template=template
        )
        return await self.run_subagent(subagent_id)
    
    def get_result(self, subagent_id: str) -> Optional[SubagentResult]:
        return self.results.get(subagent_id)
    
    def list_subagents(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": sa.subagent_id,
                "name": sa.config.name,
                "status": sa.status,
                "task": sa.config.task[:100],
                "iterations": sa.iterations
            }
            for sa in self.subagents.values()
        ]
    
    def clear_completed(self):
        completed_ids = [
            sid for sid, sa in self.subagents.items()
            if sa.status in ("completed", "failed", "max_iterations")
        ]
        for sid in completed_ids:
            del self.subagents[sid]
        return len(completed_ids)