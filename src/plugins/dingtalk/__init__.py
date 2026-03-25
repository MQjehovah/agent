import os
import json
import logging
import asyncio
from typing import Optional, Dict, Any, List, Callable, Awaitable
from dataclasses import dataclass, field

from plugins.base import BasePlugin

logger = logging.getLogger("plugin.dingtalk")


@dataclass
class DingTalkStreamConfig:
    client_id: str = ""
    client_secret: str = ""
    enabled: bool = True


@dataclass
class DingTalkConfig:
    stream: DingTalkStreamConfig = field(default_factory=DingTalkStreamConfig)

    def load_from_dict(self, data: dict):
        stream_data = data.get("stream", {})
        self.stream = DingTalkStreamConfig(
            client_id=stream_data.get("client_id", ""),
            client_secret=stream_data.get("client_secret", ""),
            enabled=stream_data.get("enabled", True)
        )


@dataclass
class DingTalkSession:
    session_id: str
    conversation_id: str
    sender_id: str
    sender_nick: str
    robot_code: str
    messages: list = field(default_factory=list)
    _plugin: Optional["DingTalkPlugin"] = field(default=None, repr=False)

    def add_message(self, role: str, content: str):
        self.messages.append({"role": role, "content": content})

    async def send_to_agent(self, content: str) -> str:
        if not self._plugin or not self._plugin.agent_executor:
            return "Agent未就绪"
        
        self.add_message("user", content)
        try:
            result = await self._plugin.agent_executor(self.session_id, content)
            self.add_message("assistant", result)
            return result
        except Exception as e:
            logger.error(f"Session {self.session_id} 执行失败: {e}")
            return f"处理失败: {e}"


class DingTalkPlugin(BasePlugin):
    name = "dingtalk"
    description = "钉钉机器人插件，使用Stream模式接收和发送消息"
    version = "2.0.0"

    def _load_config(self):
        config_file = self.config_path
        if not config_file:
            config_file = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "config", "dingtalk.json"
            )
        
        self.config = DingTalkConfig()
        
        if os.path.exists(config_file):
            try:
                with open(config_file, encoding="utf-8") as f:
                    data = json.load(f)
                self.config.load_from_dict(data)
                logger.info(f"Loaded dingtalk config from {config_file}")
            except Exception as e:
                logger.error(f"Failed to load dingtalk config: {e}")
        
        self.sessions: Dict[str, DingTalkSession] = {}
        self._client = None
        self._running = False
        self._task: Optional[asyncio.Task] = None

    def start(self):
        if not self.config.stream.enabled:
            logger.info("DingTalk plugin is disabled")
            return
        
        if not self.config.stream.client_id or not self.config.stream.client_secret:
            logger.warning("DingTalk client_id or client_secret not configured")
            return
        
        try:
            import dingtalk_stream
        except ImportError:
            logger.error("dingtalk-stream is required. Install: pip install dingtalk-stream")
            return
        
        self._running = True
        self._task = asyncio.create_task(self._run_stream_client())
        logger.info(f"DingTalk Stream plugin started (client_id: {self.config.stream.client_id[:8]}...)")

    async def _run_stream_client(self):
        import dingtalk_stream
        
        credential = dingtalk_stream.Credential(
            self.config.stream.client_id,
            self.config.stream.client_secret
        )
        
        self._client = dingtalk_stream.DingTalkStreamClient(credential)
        
        handler = AgentChatbotHandler(self)
        self._client.register_callback_handler(
            dingtalk_stream.ChatbotMessage.TOPIC,
            handler
        )
        
        while self._running:
            try:
                logger.info("DingTalk Stream client connecting...")
                await self._client.start()
            except asyncio.CancelledError:
                logger.info("DingTalk Stream client cancelled")
                break
            except Exception as e:
                logger.error(f"DingTalk Stream client error: {e}")
                if self._running:
                    logger.info("Reconnecting in 5 seconds...")
                    await asyncio.sleep(5)
                else:
                    break

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
        logger.info("DingTalk plugin stopped")

    def get_session(self, conversation_id: str, sender_id: str, sender_nick: str, robot_code: str) -> DingTalkSession:
        session_id = f"{conversation_id}_{sender_id}"
        
        if session_id not in self.sessions:
            session = DingTalkSession(
                session_id=session_id,
                conversation_id=conversation_id,
                sender_id=sender_id,
                sender_nick=sender_nick,
                robot_code=robot_code
            )
            session._plugin = self
            self.sessions[session_id] = session
            logger.info(f"创建新Session: {session_id} by {sender_nick}")
        
        return self.sessions[session_id]


class AgentChatbotHandler:
    def __init__(self, plugin: DingTalkPlugin):
        self.plugin = plugin
        self.logger = logging.getLogger("plugin.dingtalk.handler")

    def reply_text(self, content: str, incoming_message):
        import dingtalk_stream
        
        text_message = dingtalk_stream.TextMessage(content)
        response = dingtalk_stream.ReplyMessage(
            incoming_message.session_webhook,
            text_message
        )
        dingtalk_stream.sync_send(response)
        self.logger.info(f"已回复消息: {content[:50]}...")

    async def process(self, callback):
        import dingtalk_stream
        
        try:
            incoming_message = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
            
            content = ""
            if hasattr(incoming_message, 'text') and incoming_message.text:
                content = incoming_message.text.content.strip()
            
            if not content:
                self.logger.debug("Empty message, skipping")
                return dingtalk_stream.AckMessage.STATUS_OK, 'OK'
            
            conversation_id = incoming_message.conversation_id or ""
            sender_id = incoming_message.sender_id or ""
            sender_nick = incoming_message.sender_nick or ""
            robot_code = incoming_message.robot_code or ""
            
            self.logger.info(f"收到消息: [{sender_nick}] {content[:50]}...")
            
            session = self.plugin.get_session(
                conversation_id=conversation_id,
                sender_id=sender_id,
                sender_nick=sender_nick,
                robot_code=robot_code
            )
            
            if not self.plugin.agent_executor:
                response = "Agent未注册，请稍后再试"
            else:
                response = await session.send_to_agent(content)
            
            self.reply_text(response, incoming_message)
            
            return dingtalk_stream.AckMessage.STATUS_OK, 'OK'
            
        except Exception as e:
            self.logger.error(f"处理消息失败: {e}")
            return dingtalk_stream.AckMessage.STATUS_OK, 'OK'


plugin = DingTalkPlugin