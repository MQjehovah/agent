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


def _is_non_goal_message(text: str) -> bool:
    """快速判断消息是否为非目标（问候/闲聊），避免不必要的LLM调用"""
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
        logger.info("钉钉消息事件已发布: sender=%s, priority=%d", msg.get("sender_nick"), priority)

    async def handle_webhook(self, data: dict):
        severity = data.get("severity", "").lower()
        has_alert = "alert" in data
        priority = 1 if (has_alert or severity in HIGH_SEVERITY_LEVELS) else 3
        source = data.get("source", "webhook")
        event = Event(
            type="webhook",
            source=source,
            payload=dict(data),
            priority=priority,
        )
        await self.event_bus.publish(event)
        logger.info("Webhook事件已发布: source=%s, priority=%d", source, priority)

    async def handle_schedule(self, schedule: dict):
        event = Event(
            type="schedule_fired",
            source="scheduler",
            payload=dict(schedule),
            priority=3,
        )
        await self.event_bus.publish(event)
        logger.info("调度事件已发布: name=%s", schedule.get("name"))

    async def self_discovery_check(self):
        try:
            memory = getattr(self.agent, "memory", None)
            if memory is None:
                logger.debug("无memory，跳过自发现检查")
                return
            memory_content = memory.load_memory("") if hasattr(memory, "load_memory") else ""
            if not memory_content:
                return
            client = getattr(self.agent, "client", None)
            if client is None:
                return
            response = await client.chat(
                [
                    {"role": "system", "content": '你是运维助手。分析以下记忆内容，判断是否需要主动执行某项任务。如果需要，返回JSON: {"need_action": true, "title": "...", "description": "..."}。如果不需要，返回 {"need_action": false}'},
                    {"role": "user", "content": memory_content[:4000]},
                ],
                tools=[],
                stream=False,
            )
            content = response.choices[0].message.content
            result = parse_llm_json(content)
            if result and result.get("need_action"):
                event = Event(
                    type="self_discovery",
                    source="perceiver",
                    payload={
                        "title": result["title"],
                        "description": result.get("description", ""),
                    },
                    priority=2,
                )
                await self.event_bus.publish(event)
                logger.info("自发现事件已发布: title=%s", result["title"])
        except Exception:
            logger.exception("自发现检查失败")

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
        task = payload.get("task", payload.get("name", ""))
        return {
            "is_goal": True,
            "title": payload.get("name", "定时任务"),
            "description": task,
        }
