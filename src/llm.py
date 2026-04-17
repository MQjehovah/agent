import os
import json
import logging
import asyncio
from datetime import datetime
from typing import Optional, List, Dict, Any
from openai import AsyncOpenAI
from openai import (
    APIError,
    APIConnectionError,
    RateLimitError,
    APITimeoutError
)

from cache import get_cache
from usage import UsageTracker


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
RETRY_DELAY_BASE = 1.0
RETRY_DELAY_MAX = 30.0
RATE_LIMIT_COOLDOWN = 60.0


class LLMClient:
    def __init__(self, model: str = None, base_url: Optional[str] = None,
                 api_key: Optional[str] = None, enable_cache: bool = True):
        self.model = model or os.getenv("MODEL_NAME", "glm-5")
        self.enable_cache = enable_cache
        self.usage_tracker = UsageTracker()

        resolved_base_url = base_url or os.getenv("OPENAI_BASE_URL")
        resolved_api_key = api_key or os.getenv("OPENAI_API_KEY")

        if not resolved_api_key:
            raise ValueError("API Key 未配置。请设置 OPENAI_API_KEY 环境变量或在初始化时传入 api_key 参数")

        if not resolved_base_url:
            resolved_base_url = "https://coding.dashscope.aliyuncs.com/v1"

        self.base_url = resolved_base_url
        self.api_key = resolved_api_key

        self.client = AsyncOpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=180.0
        )

    def _log_request(self, params: Dict[str, Any]):
        log_data = {"type": "request", "model": params.get("model"), "messages": params.get(
            "messages", []), "tools": params.get("tools"), "stream": params.get("stream")}
        api_logger.debug(json.dumps(log_data, ensure_ascii=False))

    def _log_response(self, response):
        try:
            content = None
            tool_calls = None
            if response.choices:
                content = response.choices[0].message.content
                if response.choices[0].message.tool_calls:
                    tool_calls = []
                    for tc in response.choices[0].message.tool_calls:
                        func_args = tc.function.arguments
                        if isinstance(func_args, str):
                            try:
                                json.loads(func_args)
                            except (json.JSONDecodeError, ValueError):
                                try:
                                    func_args = json.dumps(func_args, ensure_ascii=False)
                                except Exception:
                                    func_args = "{}"
                        elif isinstance(func_args, dict):
                            func_args = json.dumps(func_args, ensure_ascii=False)
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

            log_data = {
                "type": "response", "model": response.model,
                "choices": {"content": content, "tool_calls": tool_calls} if response.choices else None,
                "usage": {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens
                } if response.usage else None
            }
            api_logger.debug(json.dumps(log_data, ensure_ascii=False))
        except Exception as e:
            api_logger.error(f"日志记录失败: {e}")

    def _log_stream_response(self, total_tokens: int, chunks: int):
        log_data = {"type": "response", "model": self.model,
                    "stream": True, "chunks": chunks, "total_tokens": total_tokens}
        api_logger.debug(json.dumps(log_data, ensure_ascii=False))

    def _calculate_retry_delay(self, attempt: int, exception: Exception) -> float:
        if isinstance(exception, RateLimitError):
            return RATE_LIMIT_COOLDOWN
        return min(RETRY_DELAY_BASE * (2 ** attempt), RETRY_DELAY_MAX)

    def _should_retry(self, exception: Exception) -> bool:
        if isinstance(exception, RateLimitError):
            return True
        if isinstance(exception, APIConnectionError):
            return True
        if isinstance(exception, APITimeoutError):
            return True
        if isinstance(exception, APIError):
            if hasattr(exception, 'status_code') and exception.status_code >= 500:
                return True
        return False

    async def chat(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]] = None,
                   stream: bool = False, use_cache: bool = True):
        """发送聊天请求，支持自动重试和缓存"""
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
                response = await self.client.chat.completions.create(**params)

                if not stream:
                    self._log_response(response)
                    # 用量追踪
                    if hasattr(response, 'usage') and response.usage:
                        self.usage_tracker.track(self.model, {
                            "prompt_tokens": response.usage.prompt_tokens,
                            "completion_tokens": response.usage.completion_tokens,
                        })
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
                    await asyncio.sleep(delay)
                else:
                    api_logger.error(f"已达最大重试次数 {MAX_RETRIES}")

        raise last_exception or Exception("Unknown error")

    async def _create_stream(self, params: Dict[str, Any]):
        """创建流式连接，失败时自动重试"""
        last_exception = None
        for attempt in range(MAX_RETRIES):
            try:
                stream = await self.client.chat.completions.create(**params)
                # 尝试消费第一个 chunk 以验证连接建立成功
                first_chunk = await stream.__anext__()
                return stream, first_chunk
            except StopAsyncIteration:
                # 空流，直接返回
                return stream, None
            except Exception as e:
                last_exception = e
                api_logger.error(f"流式API调用失败 (尝试 {attempt + 1}/{MAX_RETRIES}): {e}")

                if not self._should_retry(e):
                    api_logger.error(f"不可重试的错误，放弃重试: {type(e).__name__}")
                    raise e

                if attempt < MAX_RETRIES - 1:
                    delay = self._calculate_retry_delay(attempt, e)
                    api_logger.info(f"将在 {delay:.1f} 秒后重试...")
                    await asyncio.sleep(delay)
                else:
                    api_logger.error(f"已达最大重试次数 {MAX_RETRIES}")

        raise last_exception or Exception("Unknown error")

    async def stream_chat(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]] = None):
        """流式聊天，逐 token 返回，支持自动重试"""
        params = {
            "model": self.model,
            "messages": messages,
            "stream": True
        }
        if tools:
            params["tools"] = tools

        self._log_request(params)

        stream, first_chunk = await self._create_stream(params)

        total_tokens = 0
        chunks = 0

        # 先 yield 第一个 chunk（_create_stream 中已预取）
        if first_chunk is not None:
            chunks += 1
            if first_chunk.usage:
                total_tokens = first_chunk.usage.total_tokens
            yield first_chunk

        async for chunk in stream:
            chunks += 1
            if chunk.usage:
                total_tokens = chunk.usage.total_tokens
            yield chunk

        self._log_stream_response(total_tokens, chunks)
