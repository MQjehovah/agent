import os
import json
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field


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
    _config_path: str = "dingtalk_config.json"

    @classmethod
    def load(cls, config_path: Optional[str] = None) -> "DingTalkConfig":
        if config_path:
            cls._config_path = config_path
        
        base_dir = os.path.dirname(os.path.dirname(__file__))
        config_file = os.path.join(base_dir, cls._config_path)
        
        if not os.path.exists(config_file):
            return cls._create_default(config_file)
        
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
            token=receiver_data.get("token", ""),
            encoding_aes_key=receiver_data.get("encoding_aes_key", "")
        )
        
        return cls(servers=servers, receiver=receiver)

    @classmethod
    def _create_default(cls, path: str) -> "DingTalkConfig":
        default_config = {
            "servers": [
                {
                    "name": "默认群",
                    "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN",
                    "secret": "YOUR_SECRET",
                    "enabled": False
                }
            ],
            "receiver": {
                "host": "0.0.0.0",
                "port": 5000,
                "webhook_path": "/dingtalk/callback",
                "token": "",
                "encoding_aes_key": ""
            }
        }
        
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default_config, f, ensure_ascii=False, indent=2)
        
        return cls(
            servers=[DingTalkServer(**default_config["servers"][0])],
            receiver=DingTalkReceiverConfig(**default_config["receiver"])
        )

    def get_enabled_servers(self) -> List[DingTalkServer]:
        return [s for s in self.servers if s.enabled]

    def get_server_by_name(self, name: str) -> Optional[DingTalkServer]:
        for s in self.servers:
            if s.name == name and s.enabled:
                return s
        return self.get_enabled_servers()[0] if self.get_enabled_servers() else None
