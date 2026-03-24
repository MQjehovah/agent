import os
import json
import logging
from typing import Optional, List
from dataclasses import dataclass, field

logger = logging.getLogger("webhook.config")


@dataclass
class WebhookConfig:
    host: str = "0.0.0.0"
    port: int = 8081
    path: str = "/webhook/execute"
    tokens: List[str] = field(default_factory=list)
    callback_timeout: int = 30
    max_content_length: int = 10000

    @classmethod
    def load(cls, config_path: Optional[str] = None) -> "WebhookConfig":
        if not config_path:
            config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "config", "webhook.json"
            )
        
        if not os.path.exists(config_path):
            logger.warning(f"Webhook配置文件不存在: {config_path}，使用默认配置")
            return cls()
        
        try:
            with open(config_path, encoding="utf-8") as f:
                data = json.load(f)
            
            return cls(
                host=data.get("host", "0.0.0.0"),
                port=data.get("port", 8081),
                path=data.get("path", "/webhook/execute"),
                tokens=data.get("tokens", []),
                callback_timeout=data.get("callback_timeout", 30),
                max_content_length=data.get("max_content_length", 10000)
            )
        except Exception as e:
            logger.error(f"加载Webhook配置失败: {e}")
            return cls()

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "path": self.path,
            "tokens": self.tokens,
            "callback_timeout": self.callback_timeout,
            "max_content_length": self.max_content_length
        }

    @staticmethod
    def get_example_config() -> dict:
        return {
            "host": "0.0.0.0",
            "port": 8081,
            "path": "/webhook/execute",
            "tokens": [
                "your-secret-token-here"
            ],
            "callback_timeout": 30,
            "max_content_length": 10000
        }