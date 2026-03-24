import os
import json
import logging
import threading
import asyncio
from typing import Optional, Dict, Any, Callable, List
from dataclasses import dataclass, field

from plugins.base import BasePlugin

logger = logging.getLogger("plugin.dingtalk")


@dataclass
class DingTalkServer:
    name: str
    webhook_url: str
    secret: str
    enabled: bool = True


@dataclass
class DingTalkReceiverConfig:
    host: str = "0.0.0.0"
    port: int = 5000
    webhook_path: str = "/dingtalk/callback"
    token: str = ""
    encoding_aes_key: str = ""


@dataclass
class DingTalkConfig:
    servers: List[DingTalkServer] = field(default_factory=list)
    receiver: DingTalkReceiverConfig = field(default_factory=DingTalkReceiverConfig)

    def get_enabled_servers(self) -> List[DingTalkServer]:
        return [s for s in self.servers if s.enabled]


@dataclass
class DingTalkSession:
    session_id: str
    chatid: str
    sender: str
    sender_nick: str
    robot_code: str
    create_at: int
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
    description = "钉钉机器人插件，支持消息接收和发送"
    version = "1.0.0"

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
                
                servers = [
                    DingTalkServer(
                        name=s["name"],
                        webhook_url=s["webhook_url"],
                        secret=s["secret"],
                        enabled=s.get("enabled", True)
                    )
                    for s in data.get("servers", [])
                ]
                
                receiver_data = data.get("receiver", {})
                receiver = DingTalkReceiverConfig(
                    host=receiver_data.get("host", "0.0.0.0"),
                    port=receiver_data.get("port", 5000),
                    webhook_path=receiver_data.get("webhook_path", "/dingtalk/callback"),
                )
                
                self.config = DingTalkConfig(servers=servers, receiver=receiver)
                logger.info(f"Loaded dingtalk config from {config_file}")
            except Exception as e:
                logger.error(f"Failed to load dingtalk config: {e}")
        
        self.sessions: Dict[str, DingTalkSession] = {}
        self._thread: Optional[threading.Thread] = None
        self._app = None

    def start(self):
        try:
            from flask import Flask, request, jsonify
        except ImportError:
            logger.error("flask is required. Install: pip install flask")
            return
        
        self._app = Flask(__name__)
        self._setup_routes()
        
        host = self.config.receiver.host
        port = self.config.receiver.port
        
        self._thread = threading.Thread(
            target=self._run_server,
            args=(host, port),
            daemon=True
        )
        self._thread.start()
        logger.info(f"DingTalk plugin started: http://{host}:{port}{self.config.receiver.webhook_path}")

    def _setup_routes(self):
        from flask import request, jsonify
        webhook_path = self.config.receiver.webhook_path

        @self._app.route(webhook_path, methods=["GET"])
        def verify():
            return jsonify({"errcode": 0, "errmsg": "success"})

        @self._app.route(webhook_path, methods=["POST"])
        def callback():
            return self._handle_callback(request)

        @self._app.route("/health", methods=["GET"])
        def health():
            return jsonify({"status": "ok"})

    def _handle_callback(self, request) -> Dict[str, Any]:
        try:
            data = request.get_json()
            logger.debug(f"收到钉钉回调: {data}")

            if not data:
                return {"errcode": 0, "errmsg": "success"}

            msg_type = data.get("msgtype", "")
            
            if msg_type == "text":
                content = data.get("text", {}).get("content", "").strip()
            elif msg_type == "markdown":
                content = data.get("markdown", {}).get("text", "").strip()
            else:
                return {"errcode": 0, "errmsg": "不支持的消息类型"}

            chatid = data.get("chatid", "")
            sender = data.get("sender", "")
            sender_nick = data.get("senderNick", "")
            robot_code = data.get("robotCode", "")
            create_at = data.get("createAt", 0)

            session_id = f"{chatid}_{sender}_{create_at}"
            
            if session_id not in self.sessions:
                session = DingTalkSession(
                    session_id=session_id,
                    chatid=chatid,
                    sender=sender,
                    sender_nick=sender_nick,
                    robot_code=robot_code,
                    create_at=create_at
                )
                session._plugin = self
                self.sessions[session_id] = session
                logger.info(f"创建新Session: {session_id} by {sender_nick}")
            
            session = self.sessions[session_id]
            
            if not self.agent_executor:
                return {"errcode": 1, "errmsg": "Agent未注册"}

            asyncio.create_task(self._process_message(session, content))
            return {"errcode": 0, "errmsg": "success"}
        except Exception as e:
            logger.error(f"处理回调失败: {e}")
            return {"errcode": 1, "errmsg": str(e)}

    async def _process_message(self, session: DingTalkSession, content: str):
        result = await session.send_to_agent(content)
        self.send_reply(session, result)

    def send_reply(self, session: DingTalkSession, content: str):
        if not self.config.servers:
            logger.warning("No dingtalk servers configured")
            return
        
        server = self.config.get_enabled_servers()[0]
        try:
            import time
            import hmac
            import hashlib
            import base64
            import urllib.parse
            import requests
            
            timestamp = str(round(time.time() * 1000))
            secret_enc = server.secret.encode("utf-8")
            string_to_sign = f"{timestamp}\n{server.secret}"
            string_to_sign_enc = string_to_sign.encode("utf-8")
            hmac_code = hmac.new(secret_enc, string_to_sign_enc, digestmod=hashlib.sha256).digest()
            sign = base64.b64encode(hmac_code).decode("utf-8")
            url = f"{server.webhook_url}&timestamp={timestamp}&sign={urllib.parse.quote(sign)}"
            
            message = {
                "msgtype": "text",
                "text": {"content": f"@{session.sender_nick}\n{content}"}
            }
            
            response = requests.post(url, json=message, timeout=10)
            logger.info(f"已回复 Session {session.session_id}: {response.json()}")
        except Exception as e:
            logger.error(f"发送回复失败: {e}")

    def _run_server(self, host: str, port: int):
        self._app.run(host=host, port=port, threaded=True, use_reloader=False)

    def stop(self):
        logger.info("DingTalk plugin stopped")


plugin = DingTalkPlugin