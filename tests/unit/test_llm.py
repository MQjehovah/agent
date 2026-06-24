"""LLM 多端点 failover 测试"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
import pytest
from unittest.mock import AsyncMock, MagicMock
from openai import APITimeoutError
from llm import LLMClient


def test_single_endpoint():
    """单端点"""
    c = LLMClient(endpoints=[
        {"model": "gpt-4", "base_url": "https://x.com/v1", "api_key": "sk-test"},
    ])
    assert not c._is_multi
    assert len(c._endpoints) == 1
    assert c.model == "gpt-4"
    assert c.base_url == "https://x.com/v1"


def test_multi_endpoint():
    """多端点"""
    c = LLMClient(endpoints=[
        {"model": "ep1", "base_url": "https://a.com", "api_key": "sk-a"},
        {"model": "ep2", "base_url": "https://b.com", "api_key": "sk-b"},
    ])
    assert c._is_multi
    assert len(c._endpoints) == 2
    assert c.model == "ep1"
    assert c._endpoints[1]["model"] == "ep2"


@pytest.mark.asyncio
async def test_failover_on_timeout():
    """第一端点超时 → 自动切换第二端点"""
    c = LLMClient.__new__(LLMClient)
    c.enable_cache = False
    c.usage_tracker = MagicMock()
    c.usage_tracker.start_timer = MagicMock()
    c.usage_tracker.track = MagicMock()

    mock1 = MagicMock()
    mock1.chat.completions.create = AsyncMock(
        side_effect=APITimeoutError("timeout"))
    mock2 = MagicMock()
    fake_resp = MagicMock()
    fake_resp.choices = [MagicMock()]
    fake_resp.choices[0].message.content = "hello from ep2"
    fake_resp.usage = MagicMock()
    fake_resp.usage.prompt_tokens = 1
    fake_resp.usage.completion_tokens = 1
    mock2.chat.completions.create = AsyncMock(return_value=fake_resp)

    c._endpoints = [
        {"client": mock1, "model": "m1", "base_url": "https://bad.com", "api_key": "sk-a"},
        {"client": mock2, "model": "m2", "base_url": "https://good.com", "api_key": "sk-b"},
    ]
    c._is_multi = True
    c.model = "m1"
    c._primary_client = mock1

    resp = await c.chat([{"role": "user", "content": "hi"}])
    assert resp.choices[0].message.content == "hello from ep2"
    mock2.chat.completions.create.assert_called_once()


def test_no_endpoints():
    """无端点应报错"""
    with pytest.raises(ValueError, match="端点未配置"):
        LLMClient(endpoints=[])


def test_missing_field():
    """缺必填字段应报错"""
    with pytest.raises(ValueError, match="缺字段"):
        LLMClient(endpoints=[{"model": "x", "base_url": "https://x.com"}])
