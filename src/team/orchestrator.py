import json
import logging
import os
import re
import uuid
from typing import Any

from team.context import TeamContext
from team.dag import DAGNode, ExecutionDAG

logger = logging.getLogger("agent.team.orchestrator")


def _extract_json_from_llm(content: str) -> str:
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1].rsplit("```", 1)[0]
    return content.strip()


class TeamOrchestrator:
    def __init__(
        self,
        team_name: str,
        team_config: dict[str, Any],
        members: dict[str, dict[str, Any]],
        subagent_manager,
        llm_client,
        memory_manager=None,
    ):
        self.team_name = team_name
        self.config = team_config
        self.members = members
        self.leader = team_config.get("leader", "")
        self.subagent_manager = subagent_manager
        self.llm = llm_client
        self.memory = memory_manager
        self.context: TeamContext | None = None
        self.dag: ExecutionDAG | None = None
        self.project_dir: str = ""

    async def run(self, task: str) -> str:
        self.context = TeamContext(self.team_name, task)
        
        project_path = self._extract_project_path(task)
        if project_path:
            self.project_dir = project_path
        else:
            workspace = self.config.get("workspace", "")
            projects_dir = os.path.join(workspace, "projects")
            os.makedirs(projects_dir, exist_ok=True)
            self.project_dir = os.path.join(projects_dir, uuid.uuid4().hex[:8])
        
        self._init_project_dir()
        
        self.context.set_blackboard("项目路径", self.project_dir)
        self.context.set_blackboard("团队名称", self.team_name)

        self.dag = await self._plan_dag(task)
        if not self.dag.nodes:
            return "ERROR: 无法规划任务 DAG，请检查团队成员配置"

        for iteration in range(self.context.max_iterations):
            self.context.iteration = iteration + 1
            logger.info(
                f"团队 [{self.team_name}] 第 {iteration + 1} 轮执行, "
                f"DAG 节点数: {len(self.dag.nodes)}"
            )

            success = await self._execute_dag()
            if not success:
                logger.warning(f"团队 [{self.team_name}] DAG 执行未全部成功")

            confirmed, feedback = await self._leader_review()
            if confirmed:
                logger.info(f"团队 [{self.team_name}] Leader 确认任务完成")
                return self._build_report()

            logger.info(
                f"团队 [{self.team_name}] Leader 要求返工: {feedback[:100]}"
            )
            self.context.set_leader_feedback(feedback)

            self.dag = await self._replan(feedback)
            if not self.dag.nodes:
                logger.info(f"团队 [{self.team_name}] 重规划无新任务，结束")
                return self._build_report()

        logger.warning(
            f"团队 [{self.team_name}] 达到最大迭代次数 {self.context.max_iterations}"
        )
        return self._build_report()

    def _extract_project_path(self, task: str) -> str:
        patterns = [
            r'在\s*[`"]?([^`"\s]+)[`"]?\s*项目中',
            r'项目[路径目录][:：]\s*[`"]?([^`"\s]+)[`"]?',
            r'workspace/project/[\w]+',
            r'/home/[\w/]+/project/[\w]+',
            r'/[\w/]+/workspace/project/[\w]+',
        ]
        for pattern in patterns:
            match = re.search(pattern, task)
            if match:
                path = match.group(1) if match.lastindex else match.group(0)
                if os.path.isabs(path):
                    return path
        return ""

    async def _plan_dag(self, task: str) -> ExecutionDAG:
        member_list = "\n".join(
            f"- {m['name']}: {m.get('description', '')}"
            for m in self.members.values()
        )
        team_roles = self.config.get("team_roles", "")
        role_constraint = ""
        if team_roles:
            role_constraint = f"""
## 成员角色与边界（严格遵守）
{team_roles}

**关键约束**: 每个成员只能做自己角色范围内的工作。
- 代码工程师 只写代码，不写测试
- 测试工程师 只做测试（单元/功能/性能），不修改业务代码
- 软件架构师 只做架构设计，不写实现代码
- 算法研究员 只做调研分析，不写代码
- DevOps工程师 只做环境部署，不写业务代码
- 文档专员 只写文档，不写代码
如果某个角色对当前任务不需要，就不要为它创建节点。"""

        prompt = f"""你是团队 "{self.team_name}" 的任务规划师。
根据以下任务和团队成员，规划执行 DAG。

## 任务
{task}

## 团队成员
{member_list}
{role_constraint}

## 要求
返回 JSON 数组，每个元素包含:
- "id": 节点唯一标识 (如 "step1", "step2")
- "task": 分配给该成员的具体子任务描述（仅限该成员角色范围内的工作）
- "assignee": 成员 name（必须在上面的成员列表中）
- "dependencies": 依赖的前置节点 id 列表 (可为空数组 [])

规则:
- 严格遵守角色边界，不要把不属于某成员的工作分配给他
- 无依赖的节点可以并行执行
- 任务粒度适中，每个节点对应一个成员的一项工作
- 只返回 JSON 数组，不要其他文本

示例:
[{{"id":"step1","task":"研究算法方案","assignee":"算法研究员","dependencies":[]}}]"""

        return await self._build_dag_from_llm(prompt)

    def _init_project_dir(self):
        if self.project_dir and os.path.isabs(self.project_dir):
            if not os.path.exists(self.project_dir):
                os.makedirs(self.project_dir, exist_ok=True)
            logger.info(f"团队项目路径(任务指定): {self.project_dir}")
            return
        
        workspace = self.config.get("workspace", "")
        projects_dir = os.path.join(workspace, "projects")
        os.makedirs(projects_dir, exist_ok=True)
        self.project_dir = os.path.join(projects_dir, uuid.uuid4().hex[:8])
        os.makedirs(self.project_dir, exist_ok=True)
        logger.info(f"团队项目路径(自动创建): {self.project_dir}")

    async def _execute_dag(self) -> bool:
        async def executor(node: DAGNode) -> str:
            member_context = self.context.get_context_for_member(node.assignee)
            full_task = f"{member_context}\n## 你的具体任务\n{node.task}"

            logger.info(
                f"执行节点 [{node.id}] -> {node.assignee}: {node.task[:60]}"
            )

            result = await self.subagent_manager.run_team_agent(
                team_name=self.team_name,
                member_name=node.assignee,
                task=full_task,
            )
            if result.startswith("ERROR:"):
                raise RuntimeError(result)

            self.context.add_node_result(node.id, node.assignee, result)
            self._save_to_memory(node, result)
            return result

        return await self.dag.execute(executor)

    async def _leader_review(self) -> tuple[bool, str]:
        if not self.leader or self.leader not in self.members:
            logger.debug("无 Leader 配置，自动确认")
            return True, ""

        summary = self.context.get_summary()
        dag_info = "\n".join(
            f"- {n.status}: [{n.id}] {n.assignee} - {n.task[:60]}"
            for n in self.dag.nodes.values()
        )

        leader_prompt = self.config.get("leader_prompt", "")
        leader_context = ""
        if leader_prompt:
            leader_context = f"\n## Leader 角色说明\n{leader_prompt}\n"

        prompt = f"""你是团队 "{self.team_name}" 的 Leader ({self.leader})。
{leader_context}
请审核团队执行结果。

## 执行概要
{dag_info}

## 详细产出
{summary}

## 原始任务
{self.context.original_task}

请判断任务是否完成。返回 JSON:
{{"confirmed": true/false, "feedback": "如果不通过，说明需要修改什么以及由谁修改"}}

如果所有子任务都已完成且质量合格，confirmed 设为 true。
如果有问题需要返工，confirmed 设为 false 并在 feedback 中说明。
只返回 JSON。"""

        try:
            messages = [{"role": "user", "content": prompt}]
            resp = await self.llm.chat(messages)
            content = resp.choices[0].message.content if hasattr(resp, "choices") else str(resp)
            result = json.loads(_extract_json_from_llm(content))
            return result.get("confirmed", True), result.get("feedback", "")
        except Exception as e:
            logger.warning(f"Leader 审核解析失败: {e}")
            return True, ""

    async def _replan(self, feedback: str) -> ExecutionDAG:
        summary = self.context.get_summary()
        member_list = "\n".join(
            f"- {m['name']}: {m.get('description', '')}"
            for m in self.members.values()
        )
        team_roles = self.config.get("team_roles", "")
        role_constraint = ""
        if team_roles:
            role_constraint = f"""
## 成员角色与边界（严格遵守）
{team_roles}
**约束**: 每个成员只做自己角色范围内的工作，不要跨角色分配任务。"""

        prompt = f"""你是团队 "{self.team_name}" 的任务规划师。
上一轮执行未通过 Leader 审核，需要补充或修改。

## 原始任务
{self.context.original_task}

## 上一轮产出摘要
{summary}

## Leader 反馈
{feedback}

## 团队成员
{member_list}
{role_constraint}

## 要求
返回 JSON 数组，每个元素包含:
- "id": 节点唯一标识 (如 "step1", "step2")
- "task": 分配给该成员的具体子任务描述
- "assignee": 成员名称，必须是以下之一: {list(self.members.keys())}
- "dependencies": 依赖的前置节点 id 列表 (可为空数组 [])

规则:
- 只返回 JSON 数组，不要其他文本
- 如果不需要新任务，返回空数组 []
- assignee 必须完全匹配上面的成员名称

示例:
[{{"id":"fix1","task":"修复XX问题","assignee":"代码工程师","dependencies":[]}}]"""

        return await self._build_dag_from_llm(prompt)

    async def _build_dag_from_llm(self, prompt: str) -> ExecutionDAG:
        try:
            messages = [{"role": "user", "content": prompt}]
            resp = await self.llm.chat(messages)
            content = resp.choices[0].message.content if hasattr(resp, "choices") else str(resp)
            nodes_data = json.loads(_extract_json_from_llm(content))
            logger.debug(f"DAG 规划返回: {nodes_data}")
        except Exception as e:
            logger.error(f"DAG 规划解析失败: {e}")
            return ExecutionDAG()

        if not isinstance(nodes_data, list):
            logger.error(f"DAG 规划返回非数组: {type(nodes_data)}")
            return ExecutionDAG()

        dag = ExecutionDAG()
        valid_names = set(self.members.keys())
        logger.debug(f"有效成员名: {valid_names}")
        for item in nodes_data:
            if not isinstance(item, dict):
                logger.warning(f"跳过非字典项: {item}")
                continue
            assignee = item.get("assignee", "")
            if assignee not in valid_names:
                logger.warning(f"跳过无效成员: '{assignee}' (类型: {type(assignee).__name__}, 有效成员: {valid_names})")
                continue
            node_id = item.get("id", f"node_{len(dag.nodes)}")
            dag.add_node(DAGNode(
                id=node_id,
                task=item.get("task", ""),
                assignee=assignee,
                dependencies=item.get("dependencies", []),
            ))
        return dag

    def _save_to_memory(self, node: DAGNode, result: str):
        if not self.memory:
            return
        try:
            self.memory.share_knowledge(
                from_agent=f"{self.team_name}/{node.assignee}",
                knowledge=f"[{node.id}] {result[:500]}",
            )
        except Exception as e:
            logger.warning(f"保存共享记忆失败: {e}")

    def _build_report(self) -> str:
        parts = [
            f"# 团队执行报告: {self.team_name}",
            f"## 原始任务\n{self.context.original_task}",
            f"## 项目目录\n{self.project_dir}",
            f"## 执行轮次: {self.context.iteration}",
        ]
        if self.dag and self.dag.nodes:
            parts.append("## 执行 DAG")
            for node in self.dag.nodes.values():
                status_mark = "✓" if node.status == "completed" else "✗"
                parts.append(
                    f"- {status_mark} [{node.id}] {node.assignee}: {node.task[:80]}"
                )
        if self.context.node_results:
            parts.append("\n## 产出")
            for node_id, result in self.context.node_results.items():
                parts.append(f"### {node_id}\n{result[:1000]}")
        return "\n\n".join(parts)
