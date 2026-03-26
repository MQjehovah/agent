import os
import json
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any
from openai import OpenAI


LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

api_logger = logging.getLogger("api_call")
api_logger.setLevel(logging.DEBUG)
handler = logging.FileHandler(os.path.join(LOG_DIR, f"api_calls_{datetime.now().strftime('%Y%m%d')}.log"), encoding="utf-8")
handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
api_logger.addHandler(handler)


class LLMClient:
    def __init__(self, model: str = "MiniMax-M2.5", base_url: Optional[str] = None, api_key: Optional[str] = None):
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.client = OpenAI(
            base_url=base_url or os.getenv(
                "OPENAI_BASE_URL", "https://coding.dashscope.aliyuncs.com/v1"),
            api_key=api_key or os.getenv(
                "OPENAI_API_KEY", "sk-sp-39ab191a77af4bbda827e309afa60b12"),
            timeout=60.0
        )

    def _log_request(self, params: Dict[str, Any]):
        log_data = {"type": "request", "model": params.get("model"), "messages_count": len(params.get("messages", [])), "tools": bool(params.get("tools")), "stream": params.get("stream")}
        api_logger.debug(json.dumps(log_data, ensure_ascii=False))

    def _log_response(self, response):
        try:
            log_data = {"type": "response", "model": response.model, "usage": {"prompt_tokens": response.usage.prompt_tokens, "completion_tokens": response.usage.completion_tokens, "total_tokens": response.usage.total_tokens} if response.usage else None, "choices_count": len(response.choices)}
            api_logger.debug(json.dumps(log_data, ensure_ascii=False))
        except Exception:
            pass

    def chat(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], stream: bool = True):
        params = {
            "model": self.model,
            "messages": messages,
            "stream": stream
        }
        if tools:
            params["tools"] = tools
        self._log_request(params)
        return self.client.chat.completions.create(**params)

    def chat_sync(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]):
        params = {
            "model": self.model,
            "messages": messages,
            "stream": False
        }
        if tools:
            params["tools"] = tools
        self._log_request(params)
        response = self.client.chat.completions.create(**params)
        self._log_response(response)
        return response