"""LLM 多端点 failover 测试"""
import sys, os, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from openai import APITimeoutError
from llm import LLMClient


def test_single_endpoint_from_env():
    """向后兼容：只配 env 时，单端点模式"""
    with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test", "OPENAI_BASE_URL": "https://x.com/v1",
                                  "MODEL_NAME": "gpt-4"}, clear=False):
        c = LLMClient()
    assert not c._is_multi
    assert len(c._endpoints) == 1
    assert c.model == "gpt-4"
    assert c.base_url == "https://x.com/v1"


def test_multi_endpoint_from_config():
    """从 llm_endpoints.json 加载多端点"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False,
                                       encoding="utf-8") as f:
        json.dump([
            {"model": "ep1", "base_url": "https://a.com", "api_key": "sk-a"},
            {"model": "ep2", "base_url": "https://b.com", "api_key": "sk-b"},
        ], f)
        config_path = f.name

    try:
        td = os.path.dirname(config_path)
        # 将临时文件命名为 llm_endpoints.json 放在 config_dir
        dest = os.path.join(td, "llm_endpoints.json")
        os.rename(config_path, dest)
        c = LLMClient(config_dir=td)
        assert c._is_multi
        assert len(c._endpoints) == 2
        assert c.model == "ep1"  # primary
        assert c._endpoints[0]["model"] == "ep1"
        assert c._endpoints[1]["model"] == "ep2"
    finally:
        if os.path.exists(dest):
            os.remove(dest)


@pytest.mark.asyncio
async def test_failover_on_connection_error():
    """第一端点连接失败 → 自动切换第二端点成功"""
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
        {"client": mock1, "model": "m1", "base_url": "https://bad.com"},
        {"client": mock2, "model": "m2", "base_url": "https://good.com"},
    ]
    c._is_multi = True
    c.model = "m1"
    c._primary_client = mock1

    resp = await c.chat([{"role": "user", "content": "hi"}])
    assert resp.choices[0].message.content == "hello from ep2"
    mock1.chat.completions.create.assert_called_once()
    mock2.chat.completions.create.assert_called_once()


def test_no_api_key_raises():
    """无 api_key 且无配置文件时应抛异常"""
    with patch.dict(os.environ, {"OPENAI_API_KEY": "", "OPENAI_BASE_URL": "",
                                  "MODEL_NAME": ""}, clear=False):
        with pytest.raises(ValueError, match="API Key 未配置"):
            LLMClient(config_dir="/nonexistent")
