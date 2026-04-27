import json
import logging
import re
from typing import Any

from autonomous.eventbus import Event, EventBus
from autonomous import parse_llm_json

logger = logging.getLogger("agent.autonomous.perceiver")

URGENCY_KEYWORDS = ("紧急", "告警", "异常", "故障")
HIGH_SEVERITY_LEVELS = ("high", "critical", "urgent")
NON_GOAL_PATTERNS = (
    r"^(你好|hi|hello|嗨|哈喽|早上好|下午好|晚上好)[!！。.,，]?\s*$",
    r"^(谢谢|感谢|多谢|thx|thanks|thank you)[!！。.,，]?\s*$",
    r"^(好的|ok|OK|收到|明白|知道了)[!！。.,，]?\s*$",
    r"^(在吗|在不|在不在|有人吗)[!！？?。.,，]?\s*$",
    r"^\+1$",
    r"^\s*$",
)
NON_GOAL_COMPILED = [re.compile(p, re.IGNORECASE) for p in NON_GOAL_PATTERNS]

GENERATE_PROMPT = """阅读以下 Agent 的职责描述，提取其中**明确写明**的定期主动任务。

规则：
- 只提取 prompt 中明确描述的定期行为，例如：
  「每天检查Jira工单」→ 生成 ✓
  「定期巡检服务器磁盘」→ 生成 ✓
  「每小时拉取未合入的MR」→ 生成 ✓
  「维护内网gitlab、jenkins」→ 生成（维护=定期检查） ✓
- 以下情况返回空数组 []：
  「帮助开发员工合入代码」→ 被动响应，不生成
  「接收并审核申请」→ 被动响应，不生成
  「你是代码审查专家」→ 被动响应，不生成
  「分析数据、生成报告」→ 看语境，有"定期/每日"才生成
- 宁愿少生成，不要多生成。token 很贵。
- 任务要具体：查什么、怎么查、发现问题后做什么

{role}

可用工具：
{tools}

子代理：
{subagents}

只返回 JSON 数组（不要其他内容）：
[{{"title": "任务名", "description": "具体怎么做", "priority": 1-3, "interval": 秒数或null}}]
无需主动任务时返回 []"""


def _is_non_goal_message(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) <= 2:
        return True
    for pat in NON_GOAL_COMPILED:
        if pat.match(stripped):
            return True
    return False


class Perceiver:
    def __init__(self, event_bus: EventBus, agent: Any):
        self.event_bus = event_bus
        self.agent = agent

    def _detect_urgency(self, text: str) -> bool:
        return any(kw in text for kw in URGENCY_KEYWORDS)

    async def handle_dingtalk_message(self, msg: dict):
        text = msg.get("text", "")
        priority = 1 if self._detect_urgency(text) else 3
        event = Event(
            type="user_message",
            source="dingtalk",
            payload=dict(msg),
            priority=priority,
        )
        await self.event_bus.publish(event)

    async def handle_webhook(self, data: dict):
        severity = data.get("severity", "").lower()
        has_alert = "alert" in data
        priority = 1 if (has_alert or severity in HIGH_SEVERITY_LEVELS) else 3
        event = Event(
            type="webhook",
            source=data.get("source", "webhook"),
            payload=dict(data),
            priority=priority,
        )
        await self.event_bus.publish(event)

    async def handle_schedule(self, schedule: dict):
        event = Event(
            type="schedule_fired",
            source="scheduler",
            payload=dict(schedule),
            priority=3,
        )
        await self.event_bus.publish(event)

    async def resolve_goal_from_event(self, type: str, payload: dict) -> dict | None:
        if type == "user_message":
            return await self._resolve_goal_from_user_message(payload)
        elif type == "webhook":
            return self._resolve_goal_from_webhook(payload)
        elif type == "schedule_fired":
            return self._resolve_goal_from_schedule(payload)
        return None

    async def _resolve_goal_from_user_message(self, payload: dict) -> dict | None:
        text = payload.get("text", "")
        if _is_non_goal_message(text):
            return None
        client = getattr(self.agent, "client", None)
        if client is None:
            return None
        response = await client.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "判断用户消息是否构成一个可执行的目标。"
                        '返回JSON: {"is_goal": true/false, "title": "...", "description": "..."}。'
                        "如果是闲聊或问候，is_goal为false。"
                    ),
                },
                {"role": "user", "content": text},
            ],
            tools=[],
            stream=False,
        )
        content = response.choices[0].message.content
        result = parse_llm_json(content)
        return result if result else None

    def _resolve_goal_from_webhook(self, payload: dict) -> dict | None:
        alert = payload.get("alert", "")
        return {
            "is_goal": True,
            "title": f"处理告警: {alert}" if alert else "处理Webhook事件",
            "description": json.dumps(payload, ensure_ascii=False),
        }

    def _resolve_goal_from_schedule(self, payload: dict) -> dict | None:
        return {
            "is_goal": True,
            "title": payload.get("name", "定时任务"),
            "description": payload.get("task", payload.get("name", "")),
        }

    # ================================================================
    #  任务面板生成（启动时调用一次）
    # ================================================================

    @staticmethod
    async def generate_panel_tasks(agent, tool_summary: str, subagent_summary: str, panel) -> int:
        """根据 Agent 角色描述 + 可用工具 + 子代理列表，生成日常任务"""
        client = getattr(agent, "client", None)
        system_prompt = getattr(agent, "system_prompt_raw", "") or getattr(agent, "system_prompt", "")
        if not client or not system_prompt:
            return 0

        prompt = GENERATE_PROMPT.format(
            role=system_prompt[:6000],
            tools=tool_summary or "未指定（使用 shell 工具执行命令）",
            subagents=subagent_summary or "无",
        )

        response = await client.chat(
            [{"role": "user", "content": prompt}],
            tools=[],
            stream=False,
        )
        content = response.choices[0].message.content
        tasks_data = parse_llm_json(content)

        if not isinstance(tasks_data, list):
            logger.warning("生成面板任务格式错误: %s", content[:200])
            return 0

        count = 0
        for item in tasks_data:
            if not isinstance(item, dict) or "title" not in item:
                continue
            panel.add_task(
                title=item["title"],
                description=item.get("description", ""),
                priority=item.get("priority", 3),
                interval=item.get("interval"),
                source="llm",
            )
            count += 1

        logger.info("[%s] LLM 生成 %d 个日常任务", getattr(agent, "name", "?"), count)
        return count
