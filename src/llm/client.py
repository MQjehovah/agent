import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime
from typing import Any

import httpx
from openai import APIConnectionError, APIError, APITimeoutError, AsyncOpenAI, RateLimitError

from llm.cache import get_cache
from llm.usage import UsageTracker

LOG_DIR = os.environ.get("AGENT_LOG_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

api_logger = logging.getLogger("api")
api_logger.propagate = False
api_logger.setLevel(logging.DEBUG)
handler = logging.FileHandler(os.path.join(
    LOG_DIR, f"api_{datetime.now().strftime('%Y%m%d')}.log"), encoding="utf-8")
handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
api_logger.addHandler(handler)

# 重试配置
MAX_RETRIES_PER_ENDPOINT = 3     # 单端点模式下的最大重试次数
MULTI_ENDPOINT_RETRIES = 1       # 多端点模式下每端点的重试次数（快速切换）
RETRY_DELAY_BASE = 2.0
RETRY_DELAY_MAX = 60.0
RATE_LIMIT_COOLDOWN = 60.0

# 超时配置（秒）
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "300"))
LLM_CONNECT_TIMEOUT = float(os.getenv("LLM_CONNECT_TIMEOUT", "30"))

PLACEHOLDER_REASONING = "."


def uses_thinking(model: str) -> bool:
    """模型是否走 thinking 网关（deepseek/glm）"""
    return any(k in (model or "").lower() for k in ("deepseek", "glm"))


def is_reasoning_content_error(exc: Exception) -> bool:
    """上游 thinking 网关对 reasoning_content/thinking.reasoning 的 400 报错"""
    text = str(exc).lower()
    return "reasoning_content" in text or "thinking.reasoning" in text


def prepare_api_messages(messages: list, fill_reasoning: bool = False) -> list:
    """构造发给 thinking 网关的消息：去掉空 reasoning，必要时给 assistant 补占位。"""
    out = []
    for raw in messages or []:
        if isinstance(raw, dict):
            src = raw
        else:
            src = {
                "role": getattr(raw, "role", None),
                "content": getattr(raw, "content", None),
                "tool_calls": getattr(raw, "tool_calls", None),
                "tool_call_id": getattr(raw, "tool_call_id", None),
                "name": getattr(raw, "name", None),
                "reasoning_content": getattr(raw, "reasoning_content", None),
            }
        item: dict[str, Any] = {"role": src.get("role")}
        if src.get("content") is not None:
            item["content"] = src.get("content")
        elif item.get("role") in ("assistant", "tool", "user"):
            item["content"] = src.get("content") or ""
        for key in ("tool_calls", "tool_call_id", "name"):
            val = src.get(key)
            if val:
                item[key] = val
        rc = src.get("reasoning_content")
        if isinstance(rc, str) and rc.strip():
            item["reasoning_content"] = rc
        elif fill_reasoning and item.get("role") == "assistant":
            item["reasoning_content"] = PLACEHOLDER_REASONING
        out.append(item)
    return out


def thinking_variants(model: str, messages: list) -> list[tuple[list, bool]]:
    """thinking 400 回退：原样回传 → 补齐占位 → 关闭 thinking。"""
    prepared = prepare_api_messages(messages, fill_reasoning=False)
    thinking = uses_thinking(model)
    variants = [(prepared, thinking)]
    if thinking:
        variants.append((prepare_api_messages(messages, fill_reasoning=True), True))
        variants.append((prepared, False))
    return variants


class LLMClient:
    def __init__(self, endpoints: list = None, timeout: float = 300,
                 connect_timeout: float = 30, enable_cache: bool = True,
                 max_concurrency: int = 0):
        self.enable_cache = enable_cache
        self.usage_tracker = UsageTracker()
        self._timeout = timeout
        self._connect_timeout = connect_timeout
        # 全局并发信号量：限制同时在途的 LLM 请求数，防止 100+ 用户并发时
        # 全量打爆上游配额。0/None 表示不限制。
        self._semaphore = None
        try:
            from settings import get_settings
            mc = max_concurrency or get_settings().llm_max_concurrency
        except Exception:
            mc = max_concurrency
        if mc and mc > 0:
            self._semaphore = asyncio.Semaphore(int(mc))
        # 429 退避参数
        try:
            from settings import get_settings
            self._rl_cooldown = get_settings().llm_rate_limit_cooldown
            self._rl_max_wait = get_settings().llm_rate_limit_max_wait
            self._reasoning_effort = get_settings().llm_reasoning_effort
        except Exception:
            self._rl_cooldown = RATE_LIMIT_COOLDOWN
            self._rl_max_wait = 300.0
            self._reasoning_effort = "high"

        # 加载端点
        eps = endpoints or []
        if not eps:
            raise ValueError(
                "LLM 端点未配置。请在 config/config.json 的 llm.endpoints 中配置。\n"
                "参考 config/config.example.json"
            )
        self._endpoints = [self._build_endpoint(ep) for ep in eps]
        self._is_multi = len(self._endpoints) > 1

        # 向后兼容：暴露第一端点的信息
        primary = self._endpoints[0]
        self.model = primary["model"]
        self.base_url = primary["base_url"]
        self.api_key = primary["api_key"]
        self.client = primary["client"]
        self._primary_client = primary["client"]

        if self._is_multi:
            models = [ep["model"] for ep in self._endpoints]
            api_logger.info(f"LLM 多端点模式: {len(self._endpoints)} 个端点, 模型={models}")
        else:
            api_logger.info(f"LLM 单端点模式: {self.base_url} 模型={self.model}")


    def _build_endpoint(self, ep: dict) -> dict:
        """创建一个端点：AsyncOpenAI 客户端 + 元信息"""
        model = ep.get("model", "")
        base_url = ep.get("base_url", "")
        api_key = ep.get("api_key", "")
        if not model or not base_url or not api_key:
            raise ValueError(f"LLM 端点缺字段: model/base_url/api_key 均为必填，当前: {ep}")
        client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=httpx.Timeout(
                connect=self._connect_timeout,
                read=self._timeout,
                write=self._connect_timeout,
                pool=self._connect_timeout,
            ),
            max_retries=0,
        )
        return {
            "client": client,
            "model": model,
            "base_url": base_url,
            "api_key": api_key,
        }

    def _log_request(self, params: dict[str, Any]):
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
                    "total_tokens": response.usage.total_tokens,
                    "prompt_cache_hit_tokens": getattr(response.usage, "prompt_cache_hit_tokens", None),
                    "prompt_cache_miss_tokens": getattr(response.usage, "prompt_cache_miss_tokens", None),
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
            # 429：等待上游冷却 + 随机抖动（0.5x~1.5x），打散并发请求的同步重试，
            # 避免所有请求同时醒来再次打爆上游（重试风暴）。
            base = self._rl_cooldown
            jitter = random.uniform(0.5, 1.5)
            return min(base * jitter, self._rl_max_wait)
        if isinstance(exception, APITimeoutError):
            return min(RETRY_DELAY_BASE * (2 ** attempt) + 5, RETRY_DELAY_MAX)
        return min(RETRY_DELAY_BASE * (2 ** attempt), RETRY_DELAY_MAX)

    def _should_retry(self, exception: Exception) -> bool:
        if isinstance(exception, RateLimitError):
            return True
        if isinstance(exception, APIConnectionError):
            return True
        if isinstance(exception, APITimeoutError):
            return True
        return isinstance(exception, APIError) and hasattr(exception, 'status_code') and exception.status_code >= 500

    @staticmethod
    def _add_prompt_cache(messages: list, model: str) -> list:
        """为支持 prompt caching 的模型添加缓存标记（标注第一条 system 消息）。"""
        cache_models = {"deepseek", "gpt-4", "claude", "glm"}
        if not any(m in model.lower() for m in cache_models):
            return messages
        result = list(messages)
        for i, msg in enumerate(result):
            if msg.get("role") == "system":
                if "cache_control" not in msg:
                    result[i] = {**msg, "cache_control": {"type": "ephemeral"}}
                break
        return result

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] = None,
                   stream: bool = False, use_cache: bool = True):
        """发送聊天请求，支持多端点自动 failover 与重试"""
        # 流式请求不使用缓存
        if self.enable_cache and use_cache and not stream:
            cache = get_cache()
            cached_response = cache.get(messages, tools, self.model)
            if cached_response is not None:
                api_logger.debug("使用缓存响应")
                return cached_response

        # 本次调用的起始时间（局部变量，并发请求各自持有，避免共享计时字段竞态）
        call_start = time.monotonic()

        # 并发信号量：限制同时在途请求数，配合上游配额防打爆
        sem = self._semaphore
        if sem:
            await sem.acquire()
        try:
            return await self._chat_inner(messages, tools, stream, use_cache, call_start)
        finally:
            if sem:
                sem.release()

    async def _chat_inner(self, messages, tools, stream, use_cache, call_start):
        # 每端点重试次数
        retries_per_ep = MULTI_ENDPOINT_RETRIES if self._is_multi else MAX_RETRIES_PER_ENDPOINT
        last_exception = None

        for ep_idx, ep in enumerate(self._endpoints):
            client = ep["client"]
            model = ep["model"]

            if self._is_multi and ep_idx > 0:
                api_logger.warning(
                    f"LLM failover: 切换到端点 #{ep_idx + 1} "
                    f"({ep['base_url']} 模型={model})"
                )

            for attempt in range(retries_per_ep):
                variants = thinking_variants(model, messages)
                for v_idx, (variant_msgs, think_on) in enumerate(variants):
                    cached_messages = self._add_prompt_cache(variant_msgs, model)
                    params = {
                        "model": model,
                        "messages": cached_messages,
                        "stream": stream,
                    }
                    if tools:
                        params["tools"] = tools
                    if think_on:
                        effort = getattr(self, "_reasoning_effort", None)
                        if effort:
                            params["reasoning_effort"] = effort
                        params["extra_body"] = {"thinking": {"type": "enabled"}}

                    self._log_request(params)

                    try:
                        response = await client.chat.completions.create(**params)

                        if not stream:
                            self._log_response(response)
                            if hasattr(response, 'usage') and response.usage:
                                self.usage_tracker.track(model, {
                                    "prompt_tokens": response.usage.prompt_tokens,
                                    "completion_tokens": response.usage.completion_tokens,
                                    "prompt_cache_hit_tokens": getattr(response.usage, "prompt_cache_hit_tokens", None),
                                    "prompt_cache_miss_tokens": getattr(response.usage, "prompt_cache_miss_tokens", None),
                                }, start=call_start)
                            if self.enable_cache and use_cache:
                                cache = get_cache()
                                cache.set(messages, tools, model, response)

                        return response

                    except Exception as e:
                        last_exception = e
                        if is_reasoning_content_error(e) and v_idx < len(variants) - 1:
                            api_logger.warning(
                                f"thinking 参数错误，回退策略 {v_idx + 1}/{len(variants) - 1}: {e}"
                            )
                            continue

                        retry_type = type(e).__name__
                        ctx = f"端点#{ep_idx + 1}({model}) 尝试 {attempt + 1}/{retries_per_ep}"

                        if isinstance(e, APITimeoutError):
                            api_logger.warning(f"API超时 {ctx}: {retry_type}")
                        elif isinstance(e, (APIConnectionError, RateLimitError)):
                            api_logger.warning(f"API调用失败 {ctx}: {retry_type}: {e}")
                        else:
                            api_logger.error(f"API调用失败 {ctx}: {retry_type}: {e}")

                        if not self._should_retry(e):
                            if self._is_multi and ep_idx < len(self._endpoints) - 1:
                                api_logger.warning(f"端点 #{ep_idx + 1} 不可重试，切换下一个")
                                break
                            api_logger.error(f"不可重试的错误，放弃: {type(e).__name__}")
                            raise e

                        if attempt < retries_per_ep - 1:
                            delay = self._calculate_retry_delay(attempt, e)
                            api_logger.info(f"将在 {delay:.1f} 秒后重试 (同端点)")
                            await asyncio.sleep(delay)
                        else:
                            if self._is_multi and ep_idx < len(self._endpoints) - 1:
                                api_logger.warning(
                                    f"端点 #{ep_idx + 1} 重试耗尽 ({retries_per_ep}次)，切换下一个")
                            else:
                                api_logger.error("所有端点均已尝试，放弃")
                        break

        raise last_exception or Exception("All LLM endpoints failed")

    async def _create_stream(self, params: dict[str, Any],
                              ep: dict[str, Any], ep_idx: int, retries: int):
        """创建流式连接（单端点内重试），返回 (stream, first_chunk)"""
        client = ep["client"]
        model = ep["model"]
        params["model"] = model

        last_exception = None
        for attempt in range(retries):
            try:
                stream = await client.chat.completions.create(**params)
                first_chunk = await stream.__anext__()
                return stream, first_chunk
            except StopAsyncIteration:
                return stream, None
            except Exception as e:
                last_exception = e
                ctx = f"流式 端点#{ep_idx + 1}({model}) 尝试 {attempt + 1}/{retries}"
                api_logger.error(f"流式API调用失败 {ctx}: {e}")

                if not self._should_retry(e):
                    if self._is_multi and ep_idx < len(self._endpoints) - 1:
                        break
                    raise e

                if attempt < retries - 1:
                    delay = self._calculate_retry_delay(attempt, e)
                    api_logger.info(f"将在 {delay:.1f} 秒后重试 (同端点)")
                    await asyncio.sleep(delay)

        raise last_exception or Exception("Stream creation failed on all endpoints")

    async def stream_chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] = None):
        """流式聊天，逐 token 返回，支持多端点 failover"""
        sem = self._semaphore
        if sem:
            await sem.acquire()
        try:
            async for item in self._stream_chat_inner(messages, tools):
                yield item
        finally:
            if sem:
                sem.release()

    async def _stream_chat_inner(self, messages, tools):
        retries_per_ep = MULTI_ENDPOINT_RETRIES if self._is_multi else MAX_RETRIES_PER_ENDPOINT
        last_exception = None

        for ep_idx, ep in enumerate(self._endpoints):
            if self._is_multi and ep_idx > 0:
                api_logger.warning(
                    f"LLM failover (流式): 切换到端点 #{ep_idx + 1} "
                    f"({ep['base_url']} 模型={ep['model']})"
                )

            variants = thinking_variants(ep["model"], messages)
            stream = None
            first_chunk = None
            for v_idx, (variant_msgs, think_on) in enumerate(variants):
                params = {
                    "messages": self._add_prompt_cache(variant_msgs, ep["model"]),
                    "stream": True,
                    "stream_options": {"include_usage": True},
                }
                if tools:
                    params["tools"] = tools
                if think_on:
                    effort = getattr(self, "_reasoning_effort", None)
                    if effort:
                        params["reasoning_effort"] = effort
                    params["extra_body"] = {"thinking": {"type": "enabled"}}

                self._log_request({**params, "model": ep["model"]})
                # 本次流式调用的起始时间（局部变量，并发隔离）
                stream_start = time.monotonic()

                try:
                    stream, first_chunk = await self._create_stream(
                        params, ep, ep_idx, retries_per_ep
                    )
                    break
                except Exception as e:
                    last_exception = e
                    if is_reasoning_content_error(e) and v_idx < len(variants) - 1:
                        api_logger.warning("流式 thinking 400，回退下一策略: %s", e)
                        continue
                    if self._is_multi and ep_idx < len(self._endpoints) - 1:
                        stream = None
                        break
                    raise

            if stream is None:
                continue

            total_tokens = 0
            prompt_tokens = 0
            completion_tokens = 0
            chunks = 0

            if first_chunk is not None:
                chunks += 1
                if first_chunk.usage:
                    total_tokens = first_chunk.usage.total_tokens
                    prompt_tokens = getattr(first_chunk.usage, "prompt_tokens", 0) or 0
                    completion_tokens = getattr(first_chunk.usage, "completion_tokens", 0) or 0
                yield first_chunk

            async for chunk in stream:
                chunks += 1
                if chunk.usage:
                    total_tokens = chunk.usage.total_tokens
                    prompt_tokens = getattr(chunk.usage, "prompt_tokens", 0) or 0
                    completion_tokens = getattr(chunk.usage, "completion_tokens", 0) or 0
                yield chunk

            if prompt_tokens > 0 or completion_tokens > 0:
                self.usage_tracker.track(ep["model"], {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                }, is_stream=True, start=stream_start)

            self._log_stream_response(total_tokens, chunks)
            return

        raise last_exception or Exception("All LLM stream endpoints failed")
