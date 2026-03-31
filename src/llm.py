import os
import json
import logging
import asyncio
import time
from datetime import datetime
from typing import Optional, List, Dict, Any
from openai import OpenAI
from openai import (
    APIError,
    APIConnectionError,
    RateLimitError,
    APITimeoutError
)

from cache import get_cache


LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

api_logger = logging.getLogger("api")
api_logger.propagate = False
api_logger.setLevel(logging.DEBUG)
handler = logging.FileHandler(os.path.join(
    LOG_DIR, f"api_{datetime.now().strftime('%Y%m%d')}.log"), encoding="utf-8")
handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
api_logger.addHandler(handler)

# 重试配置
MAX_RETRIES = 3
RETRY_DELAY_BASE = 1.0  # 基础重试延迟（秒）
RETRY_DELAY_MAX = 30.0  # 最大重试延迟（秒）
RATE_LIMIT_COOLDOWN = 60.0  # 速率限制冷却时间（秒）


class LLMClient:
    def __init__(self, model: str = "glm-5", base_url: Optional[str] = None, api_key: Optional[str] = None, enable_cache: bool = True):
        self.model = model
        self.enable_cache = enable_cache

        # 优先使用传入参数，其次环境变量，无默认值
        resolved_base_url = base_url or os.getenv("OPENAI_BASE_URL")
        resolved_api_key = api_key or os.getenv("OPENAI_API_KEY")

        if not resolved_api_key:
            raise ValueError("API Key 未配置。请设置 OPENAI_API_KEY 环境变量或在初始化时传入 api_key 参数")

        if not resolved_base_url:
            logger.warning("OPENAI_BASE_URL 未配置，使用默认值")
            resolved_base_url = "https://coding.dashscope.aliyuncs.com/v1"

        self.base_url = resolved_base_url
        self.api_key = resolved_api_key

        self.client = OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
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

    def _calculate_retry_delay(self, attempt: int, exception: Exception) -> float:
        """计算重试延迟时间"""
        if isinstance(exception, RateLimitError):
            return RATE_LIMIT_COOLDOWN

        # 指数退避
        delay = min(RETRY_DELAY_BASE * (2 ** attempt), RETRY_DELAY_MAX)
        return delay

    def _should_retry(self, exception: Exception) -> bool:
        """判断是否应该重试"""
        if isinstance(exception, RateLimitError):
            return True
        if isinstance(exception, APIConnectionError):
            return True
        if isinstance(exception, APITimeoutError):
            return True
        if isinstance(exception, APIError):
            # 5xx 服务器错误可以重试
            if hasattr(exception, 'status_code') and exception.status_code >= 500:
                return True
        return False

    def chat(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], stream: bool = False, use_cache: bool = True):
        """发送聊天请求，支持自动重试和缓存

        Args:
            messages: 消息列表
            tools: 工具定义列表
            stream: 是否使用流式响应
            use_cache: 是否使用缓存（仅非流式有效）
        """
        # 流式请求不使用缓存
        if self.enable_cache and use_cache and not stream:
            cache = get_cache()
            cached_response = cache.get(messages, tools, self.model)
            if cached_response is not None:
                api_logger.debug("使用缓存响应")
                return cached_response

        params = {
            "model": self.model,
            "messages": messages,
            "stream": stream
        }
        if tools:
            params["tools"] = tools

        self._log_request(params)

        last_exception = None
        for attempt in range(MAX_RETRIES):
            try:
                response = self.client.chat.completions.create(**params)
                if stream:
                    self._log_stream_response(response)
                else:
                    self._log_response(response)
                    # 缓存非流式响应
                    if self.enable_cache and use_cache:
                        cache = get_cache()
                        cache.set(messages, tools, self.model, response)
                return response

            except Exception as e:
                last_exception = e
                api_logger.error(f"API调用失败 (尝试 {attempt + 1}/{MAX_RETRIES}): {e}")

                if not self._should_retry(e):
                    api_logger.error(f"不可重试的错误，放弃重试: {type(e).__name__}")
                    raise e

                if attempt < MAX_RETRIES - 1:
                    delay = self._calculate_retry_delay(attempt, e)
                    api_logger.info(f"将在 {delay:.1f} 秒后重试...")
                    time.sleep(delay)
                else:
                    api_logger.error(f"已达最大重试次数 {MAX_RETRIES}")

        raise last_exception or Exception("Unknown error")
