"""
任务模式识别引擎

跟踪任务和工具调用模式，识别可自动化的重复模式：
- 统计任务类型频率
- 记录工具调用链模式
- 识别高频组合，建议创建技能或子代理
"""

import os
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from collections import Counter

logger = logging.getLogger("agent.patterns")


class TaskPatternTracker:
    TASK_LOG_FILE = "task_patterns.json"

    def __init__(self, memory_dir: str):
        self.memory_dir = memory_dir
        self._log_path = os.path.join(memory_dir, self.TASK_LOG_FILE)
        self._task_counter: Counter = Counter()
        self._tool_chains: Dict[str, Counter] = {}
        self._task_tools: Dict[str, List[str]] = {}
        self._recent_tasks: List[Dict[str, Any]] = []
        self._load()

    def _load(self):
        if os.path.exists(self._log_path):
            try:
                with open(self._log_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._task_counter = Counter(data.get("task_counter", {}))
                self._tool_chains = {
                    k: Counter(v) for k, v in data.get("tool_chains", {}).items()
                }
                self._task_tools = data.get("task_tools", {})
                self._recent_tasks = data.get("recent_tasks", [])
                cutoff = (datetime.now() - timedelta(days=30)).isoformat()
                self._recent_tasks = [
                    t for t in self._recent_tasks
                    if t.get("timestamp", "") > cutoff
                ]
            except Exception as e:
                logger.warning(f"加载任务模式数据失败: {e}")

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self._log_path), exist_ok=True)
            data = {
                "task_counter": dict(self._task_counter),
                "tool_chains": {k: dict(v) for k, v in self._tool_chains.items()},
                "task_tools": self._task_tools,
                "recent_tasks": self._recent_tasks[-500:],
            }
            with open(self._log_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存任务模式数据失败: {e}")

    def record_task(self, task: str, tools_used: List[str], success: bool, result_summary: str = ""):
        task_type = self._classify_task(task)
        self._task_counter[task_type] += 1

        if tools_used:
            chain_key = task_type
            if chain_key not in self._tool_chains:
                self._tool_chains[chain_key] = Counter()
            tool_chain = " → ".join(tools_used)
            self._tool_chains[chain_key][tool_chain] += 1
            self._task_tools[task_type] = tools_used

        self._recent_tasks.append({
            "task": task[:200],
            "task_type": task_type,
            "tools": tools_used,
            "success": success,
            "timestamp": datetime.now().isoformat(),
            "result_summary": result_summary[:200],
        })

        if len(self._recent_tasks) % 10 == 0:
            self._save()

    def get_hot_patterns(self, min_count: int = 3) -> List[Dict[str, Any]]:
        patterns = []
        for task_type, count in self._task_counter.most_common(20):
            if count < min_count:
                continue
            top_chain = ""
            chains = self._tool_chains.get(task_type, {})
            if chains:
                top_chain = chains.most_common(1)[0][0]
            tools = self._task_tools.get(task_type, [])
            patterns.append({
                "task_type": task_type,
                "count": count,
                "top_tool_chain": top_chain,
                "tools": tools,
            })
        return patterns

    def get_suggestions(self, min_count: int = 3) -> List[Dict[str, Any]]:
        suggestions = []
        for pattern in self.get_hot_patterns(min_count):
            task_type = pattern["task_type"]
            count = pattern["count"]
            existing_skills = self._get_existing_skill_names()
            existing_agents = self._get_existing_agent_names()

            if task_type in existing_skills or task_type in existing_agents:
                continue

            if count >= 5 and len(pattern["tools"]) >= 3:
                suggestions.append({
                    "type": "subagent",
                    "task_type": task_type,
                    "reason": f"出现{count}次，涉及{len(pattern['tools'])}个工具，建议创建专业子代理",
                    "tools": pattern["tools"],
                    "tool_chain": pattern["top_tool_chain"],
                })
            elif count >= 3 and pattern["tools"]:
                suggestions.append({
                    "type": "skill",
                    "task_type": task_type,
                    "reason": f"出现{count}次，有固定工具链，建议创建技能模板",
                    "tools": pattern["tools"],
                    "tool_chain": pattern["top_tool_chain"],
                })

        return suggestions

    async def auto_create_skill(self, task_type: str, skill_manager) -> Optional[str]:
        pattern = None
        for p in self.get_hot_patterns(2):
            if p["task_type"] == task_type:
                pattern = p
                break
        if not pattern:
            return None

        existing_tasks = [
            t for t in self._recent_tasks
            if t.get("task_type") == task_type and t.get("success")
        ]
        examples = []
        for t in existing_tasks[-5:]:
            examples.append(f"- 任务: {t['task']}\n  工具: {' → '.join(t.get('tools', []))}\n  结果: {t.get('result_summary', '完成')}")

        description = f"处理{task_type}类任务的专业技能，基于{pattern['count']}次实际经验总结"
        prompt_template = (
            f"# {task_type}\n\n"
            f"## 概述\n{description}\n\n"
            f"## 适用场景\n- {task_type}相关请求\n\n"
            f"## 标准工作流\n"
        )
        for i, tool in enumerate(pattern.get("tools", []), 1):
            prompt_template += f"{i}. 使用 {tool} 获取/处理数据\n"
        prompt_template += f"\n## 历史案例\n" + "\n".join(examples)

        skill_dir = skill_manager.create_skill(task_type, description)

        skill_file = os.path.join(skill_dir, "SKILL.md")
        with open(skill_file, "w", encoding="utf-8") as f:
            f.write(f"---\nname: {task_type}\ndescription: {description}\n---\n\n{prompt_template}\n")

        skill_manager._load_skill(skill_dir)
        logger.info(f"[自学习] 自动创建技能: {task_type}")
        self._save()
        return skill_dir

    async def auto_create_subagent(self, task_type: str, subagent_manager, workspace_dir: str) -> Optional[str]:
        pattern = None
        for p in self.get_hot_patterns(2):
            if p["task_type"] == task_type:
                pattern = p
                break
        if not pattern:
            return None

        existing_tasks = [
            t for t in self._recent_tasks
            if t.get("task_type") == task_type and t.get("success")
        ]

        agent_dir = os.path.join(workspace_dir, "agents", task_type)
        os.makedirs(agent_dir, exist_ok=True)

        tools_list = ", ".join(pattern.get("tools", []))
        prompt_content = f"""---
name: {task_type}
description: |
  你是专门处理{task_type}类任务的专业代理。基于历史经验，你擅长使用{tools_list}等工具高效完成任务。
---
### 角色定义

你是公司的 **{task_type}代理** ，专门负责{task_type}相关的所有事务。你以**专业、高效**为核心，确保每次任务都被闭环处理。

### 核心职责

1. **任务接收与分析**：理解{task_type}相关需求，判断复杂度。
2. **标准流程执行**：按照既定工作流，使用{tools_list}等工具完成任务。
3. **结果验证**：确认任务完成质量，必要时修正。

### 标准工作流

"""
        for i, tool in enumerate(pattern.get("tools", []), 1):
            prompt_content += f"{i}. 使用 {tool} 处理关键步骤\n"
        prompt_content += "\n### 历史经验\n\n"
        for t in existing_tasks[-5:]:
            prompt_content += f"- 任务: {t['task']}\n  工具链: {' → '.join(t.get('tools', []))}\n  结果: {t.get('result_summary', '完成')}\n"
        prompt_content += """
### 交互风格

* **专业简洁**：直接给出处理结果，不啰嗦。
* **结构化反馈**：包含处理状态、执行动作、关键数据、下一步建议。

### 限制与边界

* 不处理非本领域的问题——应转回零号员工重新分派。
* 不执行超出权限的操作。
"""

        prompt_file = os.path.join(agent_dir, "PROMPT.md")
        with open(prompt_file, "w", encoding="utf-8") as f:
            f.write(prompt_content)

        subagent_manager._load_all()
        logger.info(f"[自学习] 自动创建子代理: {task_type}")
        self._save()
        return agent_dir

    def _classify_task(self, task: str) -> str:
        task_lower = task.lower()
        keywords_map = {
            "设备运维": ["设备", "机器人", "sn", "运维", "离线", "固件", "重启"],
            "售后客服": ["退货", "换货", "投诉", "维修", "物流", "保修", "售后"],
            "IT运维": ["gitlab", "jenkins", "jira", "gerrit", "构建", "合入代码", "ci"],
            "数字中台": ["报表", "数据", "日报", "周报", "月报", "统计"],
            "代码审查": ["代码", "review", "pr", "合并", "代码审查"],
            "报告撰写": ["报告", "撰写", "生成报告", "分析报告"],
            "文档查询": ["查询", "搜索", "查找", "帮我找", "看一下"],
            "问题诊断": ["诊断", "排查", "为什么", "原因", "故障", "异常"],
            "配置变更": ["配置", "修改", "更新", "设置", "变更"],
            "信息通知": ["通知", "提醒", "发送", "告警", "邮件"],
        }

        for task_type, keywords in keywords_map.items():
            if any(kw in task_lower for kw in keywords):
                return task_type

        task_words = [w for w in task_lower.split() if len(w) >= 2]
        if task_words:
            return task_words[0][:10]
        return "其他"

    def _get_existing_skill_names(self) -> List[str]:
        skills_dir = os.path.join(self.memory_dir, "..", "skills")
        names = []
        if os.path.exists(skills_dir):
            for item in os.listdir(skills_dir):
                if os.path.isdir(os.path.join(skills_dir, item)):
                    names.append(item)
        return names

    def _get_existing_agent_names(self) -> List[str]:
        agents_dir = os.path.join(self.memory_dir, "..", "agents")
        names = []
        if os.path.exists(agents_dir):
            for item in os.listdir(agents_dir):
                if os.path.isdir(os.path.join(agents_dir, item)):
                    names.append(item)
        return names
