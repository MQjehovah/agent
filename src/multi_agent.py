"""
多智能体管理模块
支持创建、切换、协作的多个AI智能体
"""
import asyncio
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, field
from enum import Enum
import logging

logger = logging.getLogger("agent")


class AgentRole(Enum):
    """智能体角色类型"""
    COORDINATOR = "coordinator"      # 协调者 - 任务分配
    EXECUTOR = "executor"            # 执行者 - 实际执行
    PLANNER = "planner"              # 规划者 - 制定计划
    RESEARCHER = "researcher"        # 研究者 - 信息收集
    REVIEWER = "reviewer"           # 审查者 - 结果审核
    GENERAL = "general"              # 通用 - 混合功能


@dataclass
class AgentConfig:
    """单个智能体配置"""
    name: str
    role: AgentRole = AgentRole.GENERAL
    model: str = "MiniMax-M2.5"
    system_prompt: str = ""
    tools: List[str] = field(default_factory=list)  # 允许的工具列表
    personality: str = ""  # 性格描述
    enabled: bool = True
    
    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "role": self.role.value,
            "model": self.model,
            "system_prompt": self.system_prompt,
            "tools": self.tools,
            "personality": self.personality,
            "enabled": self.enabled
        }


@dataclass
class TaskResult:
    """任务执行结果"""
    agent_name: str
    role: AgentRole
    content: str
    success: bool
    metadata: Dict = field(default_factory=dict)


class MultiAgentManager:
    """多智能体管理器"""
    
    # 默认智能体模板
    DEFAULT_TEMPLATES = {
        AgentRole.COORDINATOR: AgentConfig(
            name="Coordinator",
            role=AgentRole.COORDINATOR,
            personality="冷静、理性、善于分析",
            system_prompt="你是一个任务协调专家，负责分析用户需求并将其分解为子任务，分配给合适的执行者。"
        ),
        AgentRole.EXECUTOR: AgentConfig(
            name="Executor", 
            role=AgentRole.EXECUTOR,
            personality="高效、务实、执行力强",
            system_prompt="你是一个任务执行专家，负责具体执行分配给你的任务，完成后返回结果。"
        ),
        AgentRole.PLANNER: AgentConfig(
            name="Planner",
            role=AgentRole.PLANNER,
            personality="前瞻性、逻辑性强",
            system_prompt="你是一个规划专家，负责制定详细的任务计划，考虑各种边界情况和优化方案。"
        ),
        AgentRole.RESEARCHER: AgentConfig(
            name="Researcher",
            role=AgentRole.RESEARCHER,
            personality="好奇、严谨、信息全面",
            system_prompt="你是一个研究专家，负责收集信息、分析数据，为决策提供支持。"
        ),
        AgentRole.REVIEWER: AgentConfig(
            name="Reviewer",
            role=AgentRole.REVIEWER,
            personality="严格、细致、追求完美",
            system_prompt="你是一个审查专家，负责审核任务结果，指出问题并提出改进建议。"
        )
    }
    
    def __init__(self, base_agent: Any = None):
        self.agents: Dict[str, AgentConfig] = {}
        self.active_agent: Optional[str] = None
        self.task_history: List[TaskResult] = []
        self.base_agent = base_agent  # 底层Agent实例
        self._executor: Optional[Callable] = None
        
    def set_executor(self, executor: Callable):
        """设置任务执行器"""
        self._executor = executor
        
    def create_agent(self, config: AgentConfig) -> bool:
        """创建新智能体"""
        if config.name in self.agents:
            logger.warning(f"智能体 {config.name} 已存在")
            return False
        
        self.agents[config.name] = config
        logger.info(f"✓ 创建智能体: {config.name} ({config.role.value})")
        return True
    
    def create_from_template(self, name: str, role: AgentRole, 
                             overrides: Dict = None) -> bool:
        """从模板创建智能体"""
        if role not in self.DEFAULT_TEMPLATES:
            logger.error(f"未知角色: {role}")
            return False
            
        template = self.DEFAULT_TEMPLATES[role]
        config = AgentConfig(
            name=name,
            role=role,
            model=template.model,
            system_prompt=template.system_prompt,
            personality=template.personality
        )
        
        if overrides:
            for key, value in overrides.items():
                if hasattr(config, key):
                    setattr(config, key, value)
        
        return self.create_agent(config)
    
    def delete_agent(self, name: str) -> bool:
        """删除智能体"""
        if name not in self.agents:
            logger.warning(f"智能体 {name} 不存在")
            return False
        
        if self.active_agent == name:
            self.active_agent = None
            
        del self.agents[name]
        logger.info(f"✓ 删除智能体: {name}")
        return True
    
    def get_agent(self, name: str) -> Optional[AgentConfig]:
        """获取智能体配置"""
        return self.agents.get(name)
    
    def list_agents(self) -> List[Dict]:
        """列出所有智能体"""
        return [
            {
                **agent.to_dict(),
                "active": name == self.active_agent
            }
            for name, agent in self.agents.items()
        ]
    
    def switch_agent(self, name: str) -> bool:
        """切换活动智能体"""
        if name not in self.agents:
            logger.error(f"智能体 {name} 不存在")
            return False
        
        self.active_agent = name
        logger.info(f"切换到智能体: {name}")
        return True
    
    async def execute_with_agent(self, agent_name: str, task: str) -> TaskResult:
        """使用指定智能体执行任务"""
        agent = self.get_agent(agent_name)
        if not agent:
            return TaskResult(
                agent_name=agent_name,
                role=AgentRole.GENERAL,
                content=f"智能体 {agent_name} 不存在",
                success=False
            )
        
        if not agent.enabled:
            return TaskResult(
                agent_name=agent_name,
                role=agent.role,
                content=f"智能体 {agent_name} 已禁用",
                success=False
            )
        
        logger.info(f"智能体 {agent_name} 开始执行任务")
        
        try:
            # 构建带有个性化提示的消息
            full_prompt = f"{agent.system_prompt}\n\n人格特点: {agent.personality}\n\n任务: {task}"
            
            if self._executor:
                result = await self._executor(full_prompt)
                task_result = TaskResult(
                    agent_name=agent_name,
                    role=agent.role,
                    content=result,
                    success=True,
                    metadata={"model": agent.model}
                )
            else:
                task_result = TaskResult(
                    agent_name=agent_name,
                    role=agent.role,
                    content="执行器未配置",
                    success=False
                )
            
            self.task_history.append(task_result)
            return task_result
            
        except Exception as e:
            error_result = TaskResult(
                agent_name=agent_name,
                role=agent.role,
                content=f"执行失败: {str(e)}",
                success=False,
                metadata={"error": str(e)}
            )
            self.task_history.append(error_result)
            return error_result
    
    async def parallel_execute(self, tasks: Dict[str, str]) -> Dict[str, TaskResult]:
        """并行执行多个任务（不同智能体）"""
        logger.info(f"启动 {len(tasks)} 个并行任务")
        
        coroutines = [
            self.execute_with_agent(agent_name, task)
            for agent_name, task in tasks.items()
        ]
        
        results = await asyncio.gather(*coroutines, return_exceptions=True)
        
        return {
            agent_name: result if isinstance(result, TaskResult) 
            else TaskResult(agent_name=agent_name, role=AgentRole.GENERAL, 
                          content=str(result), success=False)
            for agent_name, result in zip(tasks.keys(), results)
        }
    
    async def chain_execute(self, task: str, 
                           agent_sequence: List[str]) -> List[TaskResult]:
        """链式执行：结果传递给下一个智能体"""
        results = []
        current_context = task
        
        for agent_name in agent_sequence:
            result = await self.execute_with_agent(agent_name, current_context)
            results.append(result)
            
            if not result.success:
                logger.error(f"链式执行中断于 {agent_name}")
                break
                
            # 将结果传递给下一个智能体
            current_context = f"上游结果:\n{result.content}\n\n你的任务:"
        
        return results
    
    async def collaborative_execute(self, task: str) -> str:
        """协作模式：自动协调多个智能体完成复杂任务"""
        logger.info(f"启动协作模式: {task}")
        
        # 1. 先让规划者制定计划
        planner_result = await self.execute_with_agent("Planner", 
            f"分析以下任务，制定执行计划:\n{task}")
        
        if not planner_result.success:
            return f"规划失败: {planner_result.content}"
        
        # 2. 协调者分配任务
        coordinator_result = await self.execute_with_agent("Coordinator",
            f"根据以下计划，分配具体任务:\n{planner_result.content}")
        
        if not coordinator_result.success:
            return f"协调失败: {coordinator_result.content}"
        
        # 3. 执行者执行
        executor_result = await self.execute_with_agent("Executor",
            f"执行以下任务:\n{coordinator_result.content}")
        
        if not executor_result.success:
            return f"执行失败: {executor_result.content}"
        
        # 4. 审查者审核
        reviewer_result = await self.execute_with_agent("Reviewer",
            f"审核以下执行结果:\n{executor_result.content}")
        
        # 汇总结果
        summary = f"""=== 任务完成 ===

📋 计划:
{planner_result.content}

📌 执行:
{executor_result.content}

🔍 审核:
{reviewer_result.content}
"""
        return summary
    
    def get_statistics(self) -> Dict:
        """获取智能体统计信息"""
        return {
            "total_agents": len(self.agents),
            "active_agent": self.active_agent,
            "enabled_agents": sum(1 for a in self.agents.values() if a.enabled),
            "total_tasks": len(self.task_history),
            "successful_tasks": sum(1 for r in self.task_history if r.success),
            "agents": self.list_agents()
        }
    
    def load_config(self, config: List[Dict]) -> None:
        """从配置加载智能体"""
        for agent_config in config:
            role = AgentRole(agent_config.get("role", "general"))
            config_obj = AgentConfig(
                name=agent_config["name"],
                role=role,
                model=agent_config.get("model", "MiniMax-M2.5"),
                system_prompt=agent_config.get("system_prompt", ""),
                personality=agent_config.get("personality", ""),
                enabled=agent_config.get("enabled", True)
            )
            self.create_agent(config_obj)
            
    def export_config(self) -> List[Dict]:
        """导出智能体配置"""
        return [agent.to_dict() for agent in self.agents.values()]


# 便捷函数
def create_default_team(base_agent: Any = None) -> MultiAgentManager:
    """创建默认智能体团队"""
    manager = MultiAgentManager(base_agent)
    
    # 创建默认团队
    manager.create_from_template("Coordinator", AgentRole.COORDINATOR)
    manager.create_from_template("Planner", AgentRole.PLANNER)
    manager.create_from_template("Executor", AgentRole.EXECUTOR)
    manager.create_from_template("Researcher", AgentRole.RESEARCHER)
    manager.create_from_template("Reviewer", AgentRole.REVIEWER)
    
    # 默认激活协调者
    manager.switch_agent("Coordinator")
    
    return manager
