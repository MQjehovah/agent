import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from plugins.base import BasePlugin

logger = logging.getLogger("plugin.dingtalk")
logging.getLogger("dingtalk_stream").setLevel(logging.WARNING)

@dataclass
class DingTalkStreamConfig:
    client_id: str = ""
    client_secret: str = ""


@dataclass
class DingTalkConfig:
    stream: DingTalkStreamConfig = field(default_factory=DingTalkStreamConfig)
    enabled: bool = True

    def load_from_dict(self, data: dict):
        stream_data = data.get("stream", {})
        self.stream = DingTalkStreamConfig(
            client_id=stream_data.get("client_id", ""),
            client_secret=stream_data.get("client_secret", ""),
        )
        if "enabled" in data:
            self.enabled = data["enabled"]


@dataclass
class DingTalkSession:
    session_id: str
    conversation_id: str
    sender_id: str
    sender_nick: str
    robot_code: str
    _plugin: "DingTalkPlugin | None" = field(default=None, repr=False)

    async def send_to_agent(self, content: str) -> str:
        if not self._plugin or not self._plugin.plugin_manager:
            return "PluginManager未就绪"

        try:
            result = await self._plugin.plugin_manager.execute(self.session_id, content)
            return result
        except Exception as e:
            logger.error(f"Session {self.session_id} 执行失败: {e!r}")
            return f"处理失败: {e}"

    async def send_image(self, image_path: str) -> bool:
        if not self._plugin or not self._plugin._client:
            logger.warning("DingTalk client not initialized")
            return False

        try:
            import dingtalk_stream
            await self._plugin._client.media.upload(
                media_type=dingtalk_stream.MediaType.IMAGE,
                file_path=image_path,
                conversation_id=self.conversation_id
            )
            logger.info(f"已发送图片: {image_path}")
            return True
        except Exception as e:
            logger.error(f"发送图片失败: {e!r}")
            return False


class DingTalkPlugin(BasePlugin):
    name = "dingtalk"
    description = "钉钉机器人插件，使用Stream模式接收和发送消息"
    version = "2.0.0"

    def _load_config(self):
        config_file = self.config_path
        if not config_file:
            config_file = os.path.join(
                self.config_dir or os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
                "plugins", "dingtalk.json"
            )

        self.config = DingTalkConfig()

        if os.path.exists(config_file):
            try:
                with open(config_file, encoding="utf-8") as f:
                    data = json.load(f)
                self.config.load_from_dict(data)
            except Exception as e:
                logger.error(f"Failed to load dingtalk config: {e!r}")
        else:
            logger.warning(f"DingTalk config file not found: {config_file}")

        self.sessions: dict[str, DingTalkSession] = {}
        self._client = None
        self._running = False
        self._task: asyncio.Task | None = None
        self._token_cache: dict = {}
        self.enabled = self.config.enabled

    def start(self):
        if not self.config.enabled:
            logger.info("钉钉插件已禁用")
            return

        if not self.config.stream.client_id or not self.config.stream.client_secret:
            logger.warning("DingTalk client_id or client_secret not configured")
            return

        try:
            import dingtalk_stream  # noqa: F401 — availability check
        except ImportError as e:
            logger.error(f"dingtalk-stream is required. Install: pip install dingtalk-stream. Error: {e!r}")
            return

        self._running = True

        try:
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._run_stream_client())
        except RuntimeError:
            logger.warning("No running event loop, will start in separate thread")
            import threading
            self._thread = threading.Thread(target=self._run_in_thread, daemon=True)
            self._thread.start()

    def _run_in_thread(self):
        asyncio.run(self._run_stream_client())

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
                await self._client.start()
            except asyncio.CancelledError:
                logger.warning("DingTalk Stream client cancelled")
                raise
            except Exception as e:
                logger.error(f"DingTalk Stream client error: {type(e).__name__}: {e!r}")
                if self._running:
                    logger.info("Reconnecting in 5 seconds...")
                    await asyncio.sleep(5)
                else:
                    break

        logger.info("DingTalk Stream client stopped")

    def stop(self):
        logger.info("Stopping DingTalk plugin...")
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("DingTalk plugin stopped")

    def get_tool_defs(self) -> list[dict[str, Any]]:
        return [{
            "type": "function",
            "function": {
                "name": "send_image_to_dingtalk",
                "description": "发送本地图片到钉钉对话中。适用于需要展示图片给用户的场景，例如截图、图表等。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "image_path": {
                            "type": "string",
                            "description": "图片的本地文件路径，例如: /path/to/image.png 或 screenshot.png"
                        }
                    },
                    "required": ["image_path"]
                }
            }
        }]

    async def execute_tool(self, name: str, args: dict[str, Any]) -> str:
        if name == "send_image_to_dingtalk":
            local_user_id = args.pop("_local_user_id", None)
            return await self._send_image(args.get("image_path", ""), local_user_id)
        return f"Tool {name} not implemented"

    def _get_dingtalk_staff_id(self, local_user_id) -> str | None:
        from storage.storage import get_storage
        storage = get_storage()
        if not storage or not local_user_id:
            return None
        try:
            int(local_user_id)
        except (ValueError, TypeError):
            return None
        with storage.get_connection() as conn:
            row = conn.execute(
                "SELECT platform_uid FROM rbac_user_identities WHERE user_id = ? AND platform = 'dingtalk'",
                (local_user_id,)
            ).fetchone()
        return row[0] if row else None

    async def _get_access_token(self) -> str:
        now = time.time()
        if self._token_cache.get("token") and now < self._token_cache.get("expires", 0):
            return self._token_cache["token"]
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                    json={
                        "appKey": self.config.stream.client_id,
                        "appSecret": self.config.stream.client_secret,
                    }
                )
                data = r.json()
                token = data.get("accessToken", "")
                expire_in = data.get("expireIn", 7200)
                self._token_cache = {"token": token, "expires": now + expire_in - 300}
                return token
        except Exception as e:
            logger.error(f"获取access_token失败: {e}")
            return ""

    async def _upload_media(self, access_token: str, image_path: str) -> str:
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                with open(image_path, "rb") as f:
                    r = await client.post(
                        "https://oapi.dingtalk.com/media/upload",
                        params={"access_token": access_token, "type": "image"},
                        files={"media": (os.path.basename(image_path), f)},
                    )
                data = r.json()
                return data.get("media_id", "")
        except Exception as e:
            logger.error(f"上传图片失败: {e}")
            return ""

    async def _send_image(self, image_path: str, local_user_id=None) -> str:
        if not image_path or not os.path.isfile(image_path):
            return f"图片文件不存在: {image_path}"

        staff_id = self._get_dingtalk_staff_id(local_user_id)
        if not staff_id:
            return "当前用户未绑定钉钉账号，无法发送图片"

        access_token = await self._get_access_token()
        if not access_token:
            return "获取钉钉access_token失败"

        media_id = await self._upload_media(access_token, image_path)
        if not media_id:
            return "上传图片到钉钉失败"

        try:
            import httpx
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
                    headers={"x-acs-dingtalk-access-token": access_token},
                    json={
                        "robotCode": self.config.stream.client_id,
                        "userIds": [staff_id],
                        "msgKey": "sampleImage",
                        "msgParam": json.dumps({"sampleImageMediaId": media_id}),
                    }
                )
                if r.status_code == 200:
                    logger.info(f"图片已发送给用户 staff_id={staff_id}: {image_path}")
                    return f"图片已发送: {image_path}"
                return f"发送图片失败: {r.text}"
        except Exception as e:
            return f"发送图片失败: {e}"

    def get_session(self, conversation_id: str, sender_id: str, sender_nick: str, robot_code: str) -> DingTalkSession:
        router = getattr(self.plugin_manager, "router", None) if self.plugin_manager else None
        if router:
            session_id = router.format_session_id("dingtalk", conversation_id, sender_id)
        else:
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
            logger.debug(f"创建新Session: {session_id} by {sender_nick}")

        return self.sessions[session_id]


class AgentChatbotHandler:
    def __init__(self, plugin: DingTalkPlugin):
        self.plugin = plugin
        self.logger = logging.getLogger("plugin.dingtalk.handler")
        self.dingtalk_client = None
        self._handler = None

    def pre_start(self):
        import dingtalk_stream
        logging.getLogger("dingtalk_stream.client").setLevel(logging.CRITICAL)
        logging.getLogger('dingtalkchatbot').setLevel(logging.WARNING)
        self._handler = dingtalk_stream.ChatbotHandler()
        self._handler.pre_start()

    def reply_text(self, content: str, incoming_message, msgtype: str = "markdown"):
        if self._handler:
            if msgtype == "markdown":
                title = content.split('\n')[0][:50] if content else "回复"
                self._handler.reply_markdown(title, content, incoming_message)
                self.logger.info(f"已回复Markdown消息: {title}")
            else:
                self._handler.reply_text(content, incoming_message)
                self.logger.info(f"已回复文本消息: {content[:50]}...")

    def reply_image(self, image_path: str, incoming_message):
        if self._handler and self.plugin._client:
            import dingtalk_stream
            try:
                media = self.plugin._client.media.upload(
                    media_type=dingtalk_stream.MediaType.IMAGE,
                    file_path=image_path,
                    conversation_id=incoming_message.conversation_id
                )
                self._handler.reply_image(media.media_id, incoming_message)
                self.logger.info(f"已回复图片: {image_path}")
            except Exception as e:
                self.logger.error(f"回复图片失败: {e!r}")

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

            sender_id = incoming_message.sender_id or ""
            sender_staff_id = getattr(incoming_message, 'sender_staff_id', "") or getattr(incoming_message, 'staff_id', "") or sender_id
            sender_nick = incoming_message.sender_nick or ""
            conversation_id = incoming_message.conversation_id or ""
            robot_code = incoming_message.robot_code or ""

            self.logger.info(f"钉钉插件收到消息: [{sender_nick}](staff_id={sender_staff_id}) {content}...")

            session = self.plugin.get_session(
                conversation_id=conversation_id,
                sender_id=sender_id,
                sender_nick=sender_nick,
                robot_code=robot_code
            )

            user_id = f"dingtalk:{sender_staff_id}"

            if not self.plugin.plugin_manager:
                response = "执行器未注册，请稍后再试"
            else:
                router = getattr(self.plugin.plugin_manager, "router", None)
                if router:
                    result = await router.route(
                        content, channel="dingtalk",
                        session_id=session.session_id,
                        user_id=user_id, user_name=sender_nick,
                    )
                    response = result.result if hasattr(result, "result") else str(result)
                else:
                    response = await self.plugin.plugin_manager.execute(
                        session_id=session.session_id,
                        content=content,
                        user_id=user_id,
                        user_name=sender_nick
                    )

            self.reply_text(response, incoming_message)

            return dingtalk_stream.AckMessage.STATUS_OK, 'OK'

        except Exception as e:
            self.logger.error(f"处理消息失败: {e!r}")
            return dingtalk_stream.AckMessage.STATUS_OK, 'OK'

    async def raw_process(self, callback_message):
        import dingtalk_stream

        ack_message = dingtalk_stream.AckMessage()
        ack_message.code = dingtalk_stream.AckMessage.STATUS_OK
        ack_message.headers.message_id = callback_message.headers.message_id
        ack_message.headers.content_type = "application/json"
        ack_message.data = {"response": "OK"}

        asyncio.create_task(self._async_process(callback_message))

        return ack_message

    async def _async_process(self, callback_message):
        try:
            await self.process(callback_message)
        except Exception as e:
            self.logger.error(f"异步处理消息失败: {e!r}")


plugin = DingTalkPlugin
