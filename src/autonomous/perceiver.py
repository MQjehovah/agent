import json
import logging
import re
from typing import Any

from autonomous.eventbus import Event, EventBus

logger = logging.getLogger("agent.autonomous.perceiver")

URGENCY_KEYWORDS = ("紧急", "告警", "异常", "故障")
HIGH_SEVERITY_LEVELS = ("high", "critical", "urgent")


def _strip_json_block(text: str) -> str:
    """去除 LLM 返回的 markdown 代码块包裹（```json ... ```）"""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text


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
            memory_manager = getattr(self.agent, "memory_manager", None)
            if memory_manager is None:
                logger.debug("无memory_manager，跳过自发现检查")
                return
            memory_content = await memory_manager.load_context() if hasattr(memory_manager, "load_context") else ""
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
            result = json.loads(_strip_json_block(content))
            if result.get("need_action"):
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
        client = getattr(self.agent, "client", None)
        if client is None:
            return None
        text = payload.get("text", "")
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
        try:
            return json.loads(_strip_json_block(content))
        except json.JSONDecodeError:
            logger.warning("解析目标判断结果失败: %s", content)
            return None

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
