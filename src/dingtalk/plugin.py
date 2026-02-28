import asyncio
import logging
import threading
import uuid
from typing import Optional, Dict, Any, Callable
from dataclasses import dataclass, field

try:
    from flask import Flask, request, jsonify
except ImportError:
    Flask = None

logger = logging.getLogger("dingtalk.plugin")


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

    def send_reply(self, content: str):
        if self._plugin:
            self._plugin.send_reply(self, content)


class DingTalkPlugin:
    def __init__(self, config_path: Optional[str] = None):
        if Flask is None:
            raise ImportError("flask is required. Install: pip install flask")
        
        self._load_config(config_path)
        self.sessions: Dict[str, DingTalkSession] = {}
        self.agent_executor: Optional[Callable] = None
        self._app = Flask(__name__)
        self._thread: Optional[threading.Thread] = None
        self._setup_routes()

    def _load_config(self, config_path: Optional[str]):
        from .config import DingTalkConfig
        self.config = DingTalkConfig.load(config_path)
        
        from .sender import DingTalkSender
        self.sender = DingTalkSender(self.config)

    def _setup_routes(self):
        webhook_path = self.config.receiver.webhook_path

        @self._app.route(webhook_path, methods=["GET"])
        def verify():
            return jsonify({"errcode": 0, "errmsg": "success"})

        @self._app.route(webhook_path, methods=["POST"])
        def callback():
            return self._handle_callback()

        @self._app.route("/health", methods=["GET"])
        def health():
            return jsonify({"status": "ok"})

    def register_agent(self, executor: Callable):
        self.agent_executor = executor

    def _handle_callback(self) -> Dict[str, Any]:
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

            async def process():
                result = await session.send_to_agent(content)
                session.send_reply(result)

            asyncio.create_task(process())

            return {"errcode": 0, "errmsg": "success"}
        except Exception as e:
            logger.error(f"处理回调失败: {e}")
            return {"errcode": 1, "errmsg": str(e)}

    def send_reply(self, session: DingTalkSession, content: str):
        try:
            self.sender.send_text(content=f"@{session.sender_nick}\n{content}")
            logger.info(f"已回复 Session {session.session_id}")
        except Exception as e:
            logger.error(f"发送回复失败: {e}")

    def start(self):
        host = self.config.receiver.host
        port = self.config.receiver.port
        
        self._thread = threading.Thread(
            target=self._run_server,
            args=(host, port),
            daemon=True
        )
        self._thread.start()
        logger.info(f"钉钉插件服务已启动: http://{host}:{port}{self.config.receiver.webhook_path}")

    def _run_server(self, host: str, port: int):
        self._app.run(host=host, port=port, threaded=True, use_reloader=False)

    def stop(self):
        if self._thread:
            logger.info("钉钉插件服务已停止")
