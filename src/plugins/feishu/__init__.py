import asyncio
import base64
import hashlib
import hmac
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


@dataclass
class FeishuEventConfig:
    app_id: str = ""
    app_secret: str = ""
    verification_token: str = ""
    encrypt_key: str = ""
    enabled: bool = True


@dataclass
class FeishuConfig:
    event: FeishuEventConfig = field(default_factory=FeishuEventConfig)
    enabled: bool = True

    def load_from_dict(self, data: dict):
        event_data = data.get("event", {})
        self.event = FeishuEventConfig(
            app_id=event_data.get("app_id", ""),
            app_secret=event_data.get("app_secret", ""),
            verification_token=event_data.get("verification_token", ""),
            encrypt_key=event_data.get("encrypt_key", ""),
            enabled=event_data.get("enabled", True),
        )
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
                json={
                    "app_id": self.app_id,
                    "app_secret": self.app_secret,
                },
            )
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"获取飞书 token 失败: {data.get('msg')}")
            self._tenant_token = data["tenant_access_token"]
            self._token_expires = time.time() + data.get("expire", 7200)
            logger.debug("飞书 tenant_access_token 已刷新")

    async def _api(
        self, method: str, path: str, body: dict | None = None
    ) -> dict:
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
                        "title": {"tag": "plain_text", "content": title or content[:40]}
                    },
                    "elements": [
                        {"tag": "markdown", "content": content}
                    ],
                }
            ),
        }
        return await self._api("post", "/im/v1/messages", body)

    async def reply_message(
        self, message_id: str, text: str, msg_type: str = "text"
    ) -> dict:
        content: dict[str, Any]
        if msg_type == "markdown":
            content = {
                "config": {"wide_screen_mode": True},
                "elements": [{"tag": "markdown", "content": text}],
            }
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

    async def send_image_message(
        self, chat_id: str, image_key: str
    ) -> dict:
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


def _verify_event_signature(
    body: bytes, signature: str, timestamp: str, verify_key: str
) -> bool:
    """验证飞书事件回调签名 (v2)"""
    if not verify_key:
        return True
    token = verify_key
    str_to_sign = f"{timestamp}{token}{body.decode('utf-8')}"
    sig = hmac.new(
        token.encode("utf-8"),
        str_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return sig == signature


def _decrypt_event(encrypt_key: str, cipher: str) -> str:
    """解飞书事件加密体"""
    key = hashlib.sha256(encrypt_key.encode()).digest()
    from cryptography.hazmat.primitives import padding as sym_padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    raw = base64.b64decode(cipher)
    iv = raw[:16]
    ct = raw[16:]
    cipher_obj = Cipher(algorithms.AES(key), modes.CBC(iv))
    dec = cipher_obj.decryptor()
    padded = dec.update(ct) + dec.finalize()
    unpadder = sym_padding.PKCS7(128).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()
    return plaintext.decode("utf-8")


class FeishuPlugin(BasePlugin):
    name = "feishu"
    description = "飞书机器人插件，通过HTTP回调接收事件，调用飞书API回复消息"
    version = "1.0.0"

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

        self.sessions: dict[str, FeishuSession] = {}
        self._client: FeishuClient | None = None
        self._running = False
        self._flask_app = None
        self._thread = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self):
        if not self.config.event.enabled:
            logger.info("飞书插件已禁用")
            return
        if not self.config.event.app_id or not self.config.event.app_secret:
            logger.warning("飞书 app_id 或 app_secret 未配置")
            return

        self._client = FeishuClient(
            self.config.event.app_id, self.config.event.app_secret
        )
        self._running = True

        try:
            from flask import Flask
        except ImportError:
            logger.error("flask is required. Install: pip install flask")
            return

        logging.getLogger("werkzeug").setLevel(logging.WARNING)

        self._flask_app = Flask(__name__)
        self._flask_app.debug = False
        self._setup_routes()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            self._loop = loop
            self._thread = __import__("threading").Thread(
                target=self._run_server, daemon=True
            )
            self._thread.start()
        else:
            import threading

            self._thread = threading.Thread(
                target=self._run_server_with_loop, daemon=True
            )
            self._thread.start()

        logger.info("飞书插件已启动")

    def _run_server_with_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._run_server()

    def _run_server(self):
        self._flask_app.run(
            host="0.0.0.0",
            port=self._get_port(),
            threaded=True,
            use_reloader=False,
        )

    def _get_port(self) -> int:
        return getattr(self.config, "port", 8082)

    def _setup_routes(self):
        from flask import Response, request

        @self._flask_app.route("/feishu/event", methods=["POST"])
        def feishu_event():
            return self._handle_event(request)

        @self._flask_app.route("/feishu/health", methods=["GET"])
        def health():
            return Response(
                json.dumps({"status": "ok", "service": "feishu"}),
                mimetype="application/json",
            )

    def _handle_event(self, request):
        from flask import Response

        body = request.get_data()
        timestamp = request.headers.get("X-Lark-Signature-Timestamp", "")
        signature = request.headers.get("X-Lark-Signature", "")

        if not _verify_event_signature(
            body,
            signature,
            timestamp,
            self.config.event.verification_token,
        ):
            logger.warning("飞书事件签名验证失败")
            return Response(
                json.dumps({"error": "invalid signature"}),
                status=403,
                mimetype="application/json",
            )

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return Response(
                json.dumps({"error": "invalid json"}),
                status=400,
                mimetype="application/json",
            )

        # URL验证 (首次配置回调地址)
        if data.get("type") == "url_verification":
            challenge = data.get("challenge", "")
            logger.info("飞书 URL 验证请求")
            return Response(
                json.dumps({"challenge": challenge}),
                mimetype="application/json",
            )

        # 事件回调
        event = data.get("event", data.get("header", {}))
        if not event:
            return Response(
                json.dumps({"status": "ignored"}),
                mimetype="application/json",
            )

        # 飞书v2回调格式: header.event_type + event
        header = data.get("header", {})
        event_type = header.get("event_type", "")
        event_data = data.get("event", {})

        if event_type == "im.message.receive_v1":
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._process_message(event_data), self._loop
                )
            else:
                logger.warning("事件循环未就绪，无法处理飞书消息")

        return Response(
            json.dumps({"code": 0}),
            mimetype="application/json",
        )

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
                    await self._client.send_markdown_message(
                        chat_id, response[:4000] + "\n\n...(内容过长已截断)"
                    )
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
                app_id=self.config.event.app_id,
            )
            session._plugin = self
            self.sessions[session_id] = session
            logger.debug(f"创建新飞书会话: {session_id} by {user_name}")
        return self.sessions[session_id]

    def stop(self):
        logger.info("停止飞书插件...")
        self._running = False
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
