import os
import json
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any
from openai import OpenAI


LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

api_logger = logging.getLogger("api")
api_logger.propagate = False
api_logger.setLevel(logging.DEBUG)
handler = logging.FileHandler(os.path.join(
    LOG_DIR, f"api_{datetime.now().strftime('%Y%m%d')}.log"), encoding="utf-8")
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
        log_data = {"type": "request", "model": params.get("model"), "messages": params.get(
            "messages", []), "tools": params.get("tools"), "stream": params.get("stream")}
        api_logger.debug(json.dumps(log_data, ensure_ascii=False))

    def _log_response(self, response):
        try:
            if response.choices:
                content = response.choices[0].message.content
                tool_calls = None
                if response.choices[0].message.tool_calls:
                    tool_calls = []
                    for tc in response.choices[0].message.tool_calls:
                        func_args = tc.function.arguments
                        if isinstance(func_args, str):
                            try:
                                json.loads(func_args)
                            except (json.JSONDecodeError, ValueError):
                                try:
                                    func_args = json.dumps(
                                        func_args, ensure_ascii=False)
                                except Exception:
                                    func_args = "{}"
                        elif isinstance(func_args, dict):
                            func_args = json.dumps(
                                func_args, ensure_ascii=False)
                        else:
                            func_args = "{}"
                        tool_calls.append({
                            "id": tc.id,
                            "type": tc.type,
                            "function": {
                                "name": tc.function.name,
                                "arguments": func_args
                            }
                        })

            log_data = {"type": "response", "model": response.model, "choices": {"content": content, "tool_calls": tool_calls} if response.choices else None,
                        "usage": {"prompt_tokens": response.usage.prompt_tokens, "completion_tokens": response.usage.completion_tokens, "total_tokens": response.usage.total_tokens} if response.usage else None}
            api_logger.debug(json.dumps(log_data, ensure_ascii=False))
        except Exception as e:
            print("+++++++++++++++++++++++++++++++", e)
            print(response)

    def _log_stream_response(self, response):
        total_tokens = 0
        chunks = 0
        for chunk in response:
            chunks += 1
            if chunk.usage:
                total_tokens = chunk.usage.total_tokens
            yield chunk
        log_data = {"type": "response", "model": self.model,
                    "stream": True, "chunks": chunks, "total_tokens": total_tokens}
        api_logger.debug(json.dumps(log_data, ensure_ascii=False))

    def chat(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], stream: bool = True):
        params = {
            "model": self.model,
            "messages": messages,
            "stream": stream
        }
        if tools:
            params["tools"] = tools

        self._log_request(params)
        response = self.client.chat.completions.create(**params)
        if stream:
            self._log_stream_response(response)
        else:
            self._log_response(response)
        return response
