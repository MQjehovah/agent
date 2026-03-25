import os
from typing import Optional, List, Dict, Any
from openai import OpenAI


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

    def chat(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], stream: bool = True):
        params = {
            "model": self.model,
            "messages": messages,
            "stream": stream
        }
        if tools:
            params["tools"] = tools
        return self.client.chat.completions.create(**params)

    def chat_sync(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]):
        params = {
            "model": self.model,
            "messages": messages,
            "stream": False
        }
        if tools:
            params["tools"] = tools
        return self.client.chat.completions.create(**params)