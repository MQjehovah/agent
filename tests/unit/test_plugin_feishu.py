import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from plugins.feishu import (
    FeishuConfig,
    FeishuPlugin,
    FeishuSession,
    _verify_event_signature,
)


def test_config_load_defaults():
    config = FeishuConfig()
    assert config.event.app_id == ""
    assert config.event.app_secret == ""
    assert config.enabled is True


def test_config_load_from_dict():
    data = {
        "enabled": True,
        "event": {
            "app_id": "cli_test123",
            "app_secret": "secret456",
            "verification_token": "token789",
            "encrypt_key": "key000",
            "enabled": True,
        },
    }
    config = FeishuConfig()
    config.load_from_dict(data)
    assert config.event.app_id == "cli_test123"
    assert config.event.app_secret == "secret456"
    assert config.event.verification_token == "token789"
    assert config.event.encrypt_key == "key000"


def test_plugin_init_no_config():
    plugin = FeishuPlugin(config_path="/nonexistent/feishu.json")
    assert plugin.name == "feishu"
    assert plugin.config.event.app_id == ""
    assert len(plugin.sessions) == 0


def test_plugin_get_tool_defs():
    plugin = FeishuPlugin(config_path="/nonexistent/feishu.json")
    defs = plugin.get_tool_defs()
    assert len(defs) == 2
    names = [d["function"]["name"] for d in defs]
    assert "send_feishu_message" in names
    assert "send_feishu_image" in names


def test_plugin_get_info():
    plugin = FeishuPlugin(config_path="/nonexistent/feishu.json")
    info = plugin.get_info()
    assert info["name"] == "feishu"
    assert info["version"] == "1.0.0"
    assert info["enabled"] is True


def test_verify_signature_empty_key():
    assert _verify_event_signature(b"body", "sig", "ts", "") is True


def test_verify_signature_mismatch():
    assert (
        _verify_event_signature(b"body", "badsig", "1234", "mytoken") is False
    )


def test_session_send_to_agent_no_plugin():
    session = FeishuSession(
        session_id="test_sid",
        chat_id="chat_123",
        user_id="user_456",
        user_name="张三",
        app_id="cli_test",
    )
    import asyncio

    result = asyncio.run(session.send_to_agent("hello"))
    assert "PluginManager" in result


def test_get_or_create_session():
    plugin = FeishuPlugin(config_path="/nonexistent/feishu.json")
    s1 = plugin._get_or_create_session("chat_1", "user_1", "Alice")
    assert s1.session_id == "feishu_chat_1_user_1"
    assert s1.user_name == "Alice"

    s2 = plugin._get_or_create_session("chat_1", "user_1", "Alice")
    assert s2 is s1
    assert len(plugin.sessions) == 1


def test_plugin_start_disabled():
    plugin = FeishuPlugin(config_path="/nonexistent/feishu.json")
    plugin.config.event.enabled = False
    plugin.start()
    assert plugin._client is None


def test_plugin_start_no_credentials():
    plugin = FeishuPlugin(config_path="/nonexistent/feishu.json")
    plugin.config.event.enabled = True
    plugin.config.event.app_id = ""
    plugin.start()
    assert plugin._client is None


def test_config_from_temp_file(tmp_path):
    config_data = {
        "enabled": True,
        "event": {
            "app_id": "cli_from_file",
            "app_secret": "sec_from_file",
            "verification_token": "vt_from_file",
            "encrypt_key": "",
            "enabled": True,
        },
    }
    config_file = tmp_path / "feishu.json"
    config_file.write_text(json.dumps(config_data), encoding="utf-8")

    plugin = FeishuPlugin(config_path=str(config_file))
    assert plugin.config.event.app_id == "cli_from_file"
    assert plugin.config.event.app_secret == "sec_from_file"


def test_tool_send_no_client():
    plugin = FeishuPlugin(config_path="/nonexistent/feishu.json")
    import asyncio

    result = asyncio.run(
        plugin._tool_send_message({"text": "hello"})
    )
    assert "未连接" in result


def test_tool_send_image_no_client():
    plugin = FeishuPlugin(config_path="/nonexistent/feishu.json")
    import asyncio

    result = asyncio.run(
        plugin._tool_send_image({"image_path": "/tmp/test.png"})
    )
    assert "未连接" in result


def test_tool_send_image_no_sessions():
    plugin = FeishuPlugin(config_path="/nonexistent/feishu.json")
    plugin._client = object()
    import asyncio

    result = asyncio.run(
        plugin._tool_send_image({"image_path": "/tmp/test.png"})
    )
    assert "没有活跃" in result
