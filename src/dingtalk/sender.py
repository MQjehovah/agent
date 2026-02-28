import time
import hmac
import hashlib
import base64
import urllib.parse
import requests
import logging
from typing import Optional, List, Dict, Any

from .config import DingTalkConfig, DingTalkServer
from .models import (
    TextMessage,
    MarkdownMessage,
    LinkMessage,
    ActionCardMessage,
    ActionCardButton,
    SendResult,
)

logger = logging.getLogger("dingtalk.sender")


class DingTalkSender:
    def __init__(self, config: Optional[DingTalkConfig] = None):
        self.config = config or DingTalkConfig.load()
        self.session = requests.Session()

    def _generate_sign(self, secret: str) -> tuple:
        timestamp = str(round(time.time() * 1000))
        secret_enc = secret.encode("utf-8")
        string_to_sign = f"{timestamp}\n{secret}"
        string_to_sign_enc = string_to_sign.encode("utf-8")
        hmac_code = hmac.new(secret_enc, string_to_sign_enc, digestmod=hashlib.sha256).digest()
        sign = base64.b64encode(hmac_code).decode("utf-8")
        return timestamp, sign

    def _build_url(self, server: DingTalkServer) -> str:
        timestamp, sign = self._generate_sign(server.secret)
        return f"{server.webhook_url}&timestamp={timestamp}&sign={urllib.parse.quote(sign)}"

    def _send_to_server(self, server: Optional[DingTalkServer], message: Dict[str, Any]) -> SendResult:
        if not server:
            return SendResult(success=False, errmsg="没有可用的钉钉服务器")

        try:
            url = self._build_url(server)
            headers = {"Content-Type": "application/json"}
            response = self.session.post(url, json=message, headers=headers, timeout=10)
            result = response.json()
            logger.info(f"钉钉消息发送结果: {result}")
            return SendResult.from_dict(result)
        except Exception as e:
            logger.error(f"钉钉消息发送失败: {e}")
            return SendResult(success=False, errmsg=str(e))

    def send_text(
        self,
        content: str,
        at_mobiles: Optional[List[str]] = None,
        is_at_all: bool = False,
        server_name: Optional[str] = None
    ) -> SendResult:
        if server_name:
            server = self.config.get_server_by_name(server_name)
        else:
            servers = self.config.get_enabled_servers()
            server = servers[0] if servers else None

        message = TextMessage(
            content=content,
            at_mobiles=at_mobiles or [],
            is_at_all=is_at_all
        )
        return self._send_to_server(server, message.to_dict())

    def send_markdown(
        self,
        title: str,
        content: str,
        at_mobiles: Optional[List[str]] = None,
        is_at_all: bool = False,
        server_name: Optional[str] = None
    ) -> SendResult:
        if server_name:
            server = self.config.get_server_by_name(server_name)
        else:
            servers = self.config.get_enabled_servers()
            server = servers[0] if servers else None

        message = MarkdownMessage(
            title=title,
            text=content,
            at_mobiles=at_mobiles or [],
            is_at_all=is_at_all
        )
        return self._send_to_server(server, message.to_dict())

    def send_link(
        self,
        title: str,
        text: str,
        message_url: str,
        pic_url: str = "",
        server_name: Optional[str] = None
    ) -> SendResult:
        if server_name:
            server = self.config.get_server_by_name(server_name)
        else:
            servers = self.config.get_enabled_servers()
            server = servers[0] if servers else None

        message = LinkMessage(
            title=title,
            text=text,
            message_url=message_url,
            pic_url=pic_url
        )
        return self._send_to_server(server, message.to_dict())

    def send_actioncard(
        self,
        title: str,
        text: str,
        btn_orientation: str = "0",
        btns: Optional[List[ActionCardButton]] = None,
        single_title: str = "",
        single_url: str = "",
        server_name: Optional[str] = None
    ) -> SendResult:
        if server_name:
            server = self.config.get_server_by_name(server_name)
        else:
            servers = self.config.get_enabled_servers()
            server = servers[0] if servers else None

        message = ActionCardMessage(
            title=title,
            text=text,
            btn_orientation=btn_orientation,
            btns=btns or [],
            single_title=single_title,
            single_url=single_url
        )
        return self._send_to_server(server, message.to_dict())

    def send_to_all(
        self,
        content: str,
        server_name: Optional[str] = None
    ) -> Dict[str, SendResult]:
        results = {}
        for server in self.config.get_enabled_servers():
            result = self.send_text(content, is_at_all=True, server_name=server.name)
            results[server.name] = result
        return results
