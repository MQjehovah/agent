import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from plugins.base import BasePlugin

logger = logging.getLogger("plugin.feishu")

FEISHU_BASE = "https://open.feishu.cn/open-apis"
ENDPOINT_URL = "https://open.feishu.cn/callback/ws/endpoint"


@dataclass
class FeishuConfig:
    app_id: str = ""
    app_secret: str = ""
    enabled: bool = True

    def load_from_dict(self, data: dict):
        self.app_id = data.get("app_id", "")
        self.app_secret = data.get("app_secret", "")
        if "enabled" in data:
            self.enabled = data["enabled"]


@dataclass
class FeishuSession:
    session_id: str
    chat_id: str
    user_id: str
    user_name: str
    app_id: str
    _plugin: "FeishuPlugin | None" = field(default=None, repr=False)

    async def send_to_agent(self, content: str) -> str:
        if not self._plugin or not self._plugin.plugin_manager:
            return "PluginManager未就绪"
        try:
            result = await self._plugin.plugin_manager.execute(
                self.session_id, content
            )
            return result
        except Exception as e:
            logger.error(f"Session {self.session_id} 执行失败: {e!r}")
            return f"处理失败: {e}"


class FeishuClient:
    """飞书开放平台 HTTP 客户端，负责 token 管理和 API 调用"""

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._tenant_token: str = ""
        self._token_expires: float = 0

    async def _ensure_token(self):
        if self._tenant_token and time.time() < self._token_expires - 60:
            return
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"获取飞书 token 失败: {data.get('msg')}")
            self._tenant_token = data["tenant_access_token"]
            self._token_expires = time.time() + data.get("expire", 7200)
            logger.debug("飞书 tenant_access_token 已刷新")

    async def _api(self, method: str, path: str, body: dict | None = None) -> dict:
        await self._ensure_token()
        headers = {
            "Authorization": f"Bearer {self._tenant_token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await getattr(client, method)(
                f"{FEISHU_BASE}{path}", headers=headers, json=body
            )
            return resp.json()

    async def send_text_message(self, chat_id: str, text: str) -> dict:
        return await self._api(
            "post",
            "/im/v1/messages",
            {
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}),
            },
        )

    async def send_markdown_message(
        self, chat_id: str, content: str, title: str = ""
    ) -> dict:
        body: dict[str, Any] = {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(
                {
                    "config": {"wide_screen_mode": True},
                    "header": {
                        "title": {
                            "tag": "plain_text",
                            "content": title or content[:40],
                        }
                    },
                    "elements": [{"tag": "markdown", "content": content}],
                }
            ),
        }
        return await self._api("post", "/im/v1/messages", body)

    async def reply_message(
        self, message_id: str, text: str, msg_type: str = "interactive"
    ) -> dict:
        content: dict[str, Any]
        if msg_type == "interactive":
            title = text.split("\n")[0].lstrip("# ").strip()[:50] or "回复"
            content = {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": title}
                },
                "elements": [{"tag": "markdown", "content": text}],
            }
            msg_type = "interactive"
        else:
            content = {"text": text}
        return await self._api(
            "post",
            f"/im/v1/messages/{message_id}/reply",
            {"msg_type": msg_type, "content": json.dumps(content)},
        )

    async def upload_image(self, image_path: str) -> str | None:
        await self._ensure_token()
        headers = {"Authorization": f"Bearer {self._tenant_token}"}
        async with httpx.AsyncClient(timeout=30) as client:
            with open(image_path, "rb") as f:
                resp = await client.post(
                    f"{FEISHU_BASE}/im/v1/images",
                    headers=headers,
                    data={"image_type": "message"},
                    files={"image": f},
                )
            data = resp.json()
            if data.get("code") != 0:
                logger.error(f"上传图片失败: {data}")
                return None
            return data.get("data", {}).get("image_key")

    async def send_image_message(self, chat_id: str, image_key: str) -> dict:
        return await self._api(
            "post",
            "/im/v1/messages",
            {
                "receive_id": chat_id,
                "msg_type": "image",
                "content": json.dumps({"image_key": image_key}),
            },
        )

    async def get_user_info(self, user_id: str) -> dict:
        return await self._api("get", f"/contact/v3/users/{user_id}")


async def _get_ws_endpoint(app_id: str, app_secret: str) -> str:
    """调用飞书 endpoint API 获取 WebSocket 长连接地址"""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            ENDPOINT_URL,
            headers={"locale": "zh"},
            json={"AppID": app_id, "AppSecret": app_secret},
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(
                f"获取飞书 WS endpoint 失败: {data.get('msg', data)}"
            )
        url = data.get("data", {}).get("URL", "")
        if not url:
            raise RuntimeError("飞书 WS endpoint 返回空 URL")
        return url


def _split_markdown(text: str, max_len: int = 3800) -> list[str]:
    """将 markdown 文本按段落边界分段，避免截断代码块"""
    if len(text) <= max_len:
        return [text]

    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break

        cut = remaining.rfind("\n\n", 0, max_len)
        if cut < max_len // 2:
            cut = remaining.rfind("\n", 0, max_len)
        if cut < max_len // 2:
            cut = max_len
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")

    return chunks


class FeishuPlugin(BasePlugin):
    name = "feishu"
    description = "飞书机器人插件，通过WebSocket长连接接收事件，无需公网地址"
    version = "2.0.0"

    def _load_config(self):
        config_file = self.config_path
        if not config_file:
            config_file = os.path.join(
                os.path.dirname(
                    os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
                ),
                "workspace",
                "feishu.json",
            )
        self.config = FeishuConfig()
        if os.path.exists(config_file):
            try:
                with open(config_file, encoding="utf-8") as f:
                    data = json.load(f)
                self.config.load_from_dict(data)
                logger.info(f"飞书配置已加载: {config_file}")
            except Exception as e:
                logger.error(f"加载飞书配置失败: {e!r}")
        else:
            logger.warning(f"飞书配置文件不存在: {config_file}")

        self.enabled = self.config.enabled

        self.sessions: dict[str, FeishuSession] = {}
        self._client: FeishuClient | None = None
        self._running = False
        self._task: asyncio.Task | None = None
        self._seen_messages: set[str] = set()

    def start(self):
        if not self.config.enabled:
            logger.info("飞书插件已禁用")
            return
        if not self.config.app_id or not self.config.app_secret:
            logger.warning("飞书 app_id 或 app_secret 未配置")
            return

        self._client = FeishuClient(self.config.app_id, self.config.app_secret)
        self._running = True

        try:
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._run_ws_client())
        except RuntimeError:
            import threading

            self._thread = threading.Thread(
                target=self._run_in_thread, daemon=True
            )
            self._thread.start()

        logger.info("飞书插件已启动（WebSocket长连接模式）")

    def _run_in_thread(self):
        asyncio.run(self._run_ws_client())

    async def _run_ws_client(self):
        """WebSocket 长连接主循环：获取 endpoint → 连接 → 收事件 → ACK → 断线重连"""
        try:
            import websockets
        except ImportError:
            logger.error(
                "websockets is required. Install: pip install websockets"
            )
            return

        try:
            from lark_oapi.ws.pb.pbbp2_pb2 import Frame  # noqa: F401
        except ImportError:
            logger.error("lark-oapi is required. Install: pip install lark-oapi")
            return

        while self._running:
            ws_url = ""
            try:
                ws_url = await _get_ws_endpoint(
                    self.config.app_id, self.config.app_secret
                )
                logger.info(f"飞书 WS endpoint: {ws_url[:60]}...")

                async with websockets.connect(ws_url) as ws:
                    logger.info("飞书 WebSocket 已连接")
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            await self._on_ws_frame(ws, raw)
                        except Exception as e:
                            logger.error(f"处理 WebSocket 帧异常: {e!r}")

            except asyncio.CancelledError:
                logger.info("飞书 WebSocket 连接被取消")
                raise
            except Exception as e:
                logger.error(
                    f"飞书 WebSocket 连接异常: {type(e).__name__}: {e}"
                )
                if self._running:
                    logger.info("飞书 WebSocket 5秒后重连...")
                    await asyncio.sleep(5)
                else:
                    break

        logger.info("飞书 WebSocket 客户端已停止")

    async def _on_ws_frame(self, ws, raw: bytes):
        """解析 protobuf 帧，处理事件，发送 ACK"""
        from lark_oapi.ws.pb.pbbp2_pb2 import Frame

        frame = Frame()
        frame.ParseFromString(raw)

        headers = {_h.key: _h.value for _h in frame.headers}
        msg_type = headers.get("type", "")

        if msg_type == "ping":
            return

        payload = frame.payload
        if not payload:
            # 空 payload 也要 ACK
            frame.payload = json.dumps({"code": 200}).encode("utf-8")
            await ws.send(frame.SerializeToString())
            return

        try:
            event_data = json.loads(payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning(f"飞书帧 payload 非 JSON: {payload[:200]}")
            frame.payload = json.dumps({"code": 200}).encode("utf-8")
            await ws.send(frame.SerializeToString())
            return

        header = event_data.get("header", {})
        event_type = header.get("event_type", "")
        event_id = header.get("event_id", "")

        # 按 event_id 去重
        if event_id and event_id in self._seen_messages:
            logger.debug(f"飞书重复事件，跳过: {event_id}")
            frame.payload = json.dumps({"code": 200}).encode("utf-8")
            await ws.send(frame.SerializeToString())
            return
        if event_id:
            self._seen_messages.add(event_id)
            if len(self._seen_messages) > 5000:
                self._seen_messages = set(list(self._seen_messages)[-2500:])

        if event_type == "im.message.receive_v1":
            asyncio.create_task(self._process_message(event_data.get("event", {})))

        # ACK：payload 替换为 Response(code=200)，原帧发回
        frame.payload = json.dumps({"code": 200}).encode("utf-8")
        await ws.send(frame.SerializeToString())

    async def _process_message(self, event_data: dict):
        sender = event_data.get("sender", {})
        user_id = sender.get("sender_id", {}).get("user_id", "")
        message = event_data.get("message", {})
        chat_id = message.get("chat_id", "")
        message_id = message.get("message_id", "")
        msg_type = message.get("message_type", "")

        content_str = message.get("content", "{}")
        try:
            content_obj = json.loads(content_str)
        except json.JSONDecodeError:
            content_obj = {}

        text = ""
        if msg_type == "text":
            text = content_obj.get("text", "").strip()
        elif msg_type == "post":
            content_parts = content_obj.get("content", [])
            for part_list in content_parts:
                for part in part_list:
                    text += part.get("text", "")
            text = text.strip()
        elif msg_type == "interactive":
            text = content_obj.get("user_input", "").strip()

        if not text:
            logger.debug("飞书消息为空，跳过")
            return

        user_name = user_id
        try:
            if self._client:
                info = await self._client.get_user_info(user_id)
                user_name = (
                    info.get("data", {}).get("user", {}).get("name", user_id)
                )
        except Exception:
            pass

        logger.info(f"飞书收到消息: [{user_name}] {text[:80]}")

        session = self._get_or_create_session(chat_id, user_id, user_name)

        if not self.plugin_manager:
            response = "执行器未注册，请稍后再试"
        else:
            response = await session.send_to_agent(text)

        if self._client and message_id:
            try:
                if len(response) > 4000:
                    chunks = _split_markdown(response, 3800)
                    for i, chunk in enumerate(chunks):
                        suffix = f"\n\n({i + 1}/{len(chunks)})" if len(chunks) > 1 else ""
                        await self._client.reply_message(message_id, chunk + suffix)
                else:
                    await self._client.reply_message(message_id, response)
            except Exception as e:
                logger.error(f"飞书回复失败: {e!r}")
                try:
                    await self._client.send_text_message(chat_id, response[:4000])
                except Exception as e2:
                    logger.error(f"飞书文本回复也失败: {e2!r}")

    def _get_or_create_session(
        self, chat_id: str, user_id: str, user_name: str
    ) -> FeishuSession:
        session_id = f"feishu_{chat_id}_{user_id}"
        if session_id not in self.sessions:
            session = FeishuSession(
                session_id=session_id,
                chat_id=chat_id,
                user_id=user_id,
                user_name=user_name,
                app_id=self.config.app_id,
            )
            session._plugin = self
            self.sessions[session_id] = session
            logger.debug(f"创建新飞书会话: {session_id} by {user_name}")
        return self.sessions[session_id]

    def stop(self):
        logger.info("停止飞书插件...")
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("飞书插件已停止")

    def get_tool_defs(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "send_feishu_message",
                    "description": "通过飞书发送消息到指定会话。适用于需要主动推送信息给用户的场景。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "chat_id": {
                                "type": "string",
                                "description": "飞书会话ID。留空则发送到当前活跃会话。",
                            },
                            "text": {
                                "type": "string",
                                "description": "要发送的消息内容",
                            },
                            "msg_type": {
                                "type": "string",
                                "enum": ["text", "markdown"],
                                "description": "消息类型，默认text",
                            },
                        },
                        "required": ["text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "send_feishu_image",
                    "description": "通过飞书发送本地图片到当前活跃会话。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "image_path": {
                                "type": "string",
                                "description": "图片的本地文件路径",
                            }
                        },
                        "required": ["image_path"],
                    },
                },
            },
        ]

    async def execute_tool(self, name: str, args: dict[str, Any]) -> str:
        if name == "send_feishu_message":
            return await self._tool_send_message(args)
        if name == "send_feishu_image":
            return await self._tool_send_image(args)
        return f"Tool {name} not implemented"

    async def _tool_send_message(self, args: dict[str, Any]) -> str:
        if not self._client:
            return "错误: 飞书客户端未连接"
        chat_id = args.get("chat_id", "")
        text = args.get("text", "")
        msg_type = args.get("msg_type", "text")

        if not chat_id:
            if not self.sessions:
                return "错误: 没有活跃的飞书会话，请指定 chat_id"
            chat_id = list(self.sessions.values())[0].chat_id

        try:
            if msg_type == "markdown":
                await self._client.send_markdown_message(chat_id, text)
            else:
                await self._client.send_text_message(chat_id, text)
            return f"消息已发送到 {chat_id}"
        except Exception as e:
            return f"发送消息失败: {e}"

    async def _tool_send_image(self, args: dict[str, Any]) -> str:
        if not self._client:
            return "错误: 飞书客户端未连接"
        if not self.sessions:
            return "错误: 没有活跃的飞书会话"

        image_path = args.get("image_path", "")
        if not os.path.exists(image_path):
            return f"错误: 图片文件不存在: {image_path}"

        try:
            image_key = await self._client.upload_image(image_path)
            if not image_key:
                return "错误: 图片上传失败"
            chat_id = list(self.sessions.values())[0].chat_id
            await self._client.send_image_message(chat_id, image_key)
            return f"图片已发送: {image_path}"
        except Exception as e:
            return f"发送图片失败: {e}"


plugin = FeishuPlugin
