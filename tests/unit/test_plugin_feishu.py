import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from plugins.feishu import FeishuConfig, FeishuPlugin, FeishuSession


def test_config_load_defaults():
    config = FeishuConfig()
    assert config.app_id == ""
    assert config.app_secret == ""
    assert config.enabled is True


def test_config_load_from_dict():
    data = {
        "enabled": True,
        "app_id": "cli_test123",
        "app_secret": "secret456",
    }
    config = FeishuConfig()
    config.load_from_dict(data)
    assert config.app_id == "cli_test123"
    assert config.app_secret == "secret456"


def test_plugin_init_no_config():
    plugin = FeishuPlugin(config_path="/nonexistent/feishu.json")
    assert plugin.name == "feishu"
    assert plugin.config.app_id == ""
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
    assert info["version"] == "2.0.0"
    assert info["enabled"] is True


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
    plugin.config.enabled = False
    plugin.start()
    assert plugin._client is None


def test_plugin_start_no_credentials():
    plugin = FeishuPlugin(config_path="/nonexistent/feishu.json")
    plugin.config.enabled = True
    plugin.config.app_id = ""
    plugin.start()
    assert plugin._client is None


def test_config_from_temp_file(tmp_path):
    config_data = {
        "enabled": True,
        "app_id": "cli_from_file",
        "app_secret": "sec_from_file",
    }
    config_file = tmp_path / "feishu.json"
    config_file.write_text(json.dumps(config_data), encoding="utf-8")

    plugin = FeishuPlugin(config_path=str(config_file))
    assert plugin.config.app_id == "cli_from_file"
    assert plugin.config.app_secret == "sec_from_file"


def test_tool_send_no_client():
    plugin = FeishuPlugin(config_path="/nonexistent/feishu.json")
    import asyncio

    result = asyncio.run(plugin._tool_send_message({"text": "hello"}))
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


def test_on_ws_frame_processes_event_and_acks():
    """用真实 protobuf Frame 测试：解析事件 + 发送 ACK"""
    import asyncio

    from lark_oapi.ws.pb.pbbp2_pb2 import Frame

    plugin = FeishuPlugin(config_path="/nonexistent/feishu.json")

    event_json = json.dumps({
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"user_id": "u1"}},
            "message": {
                "chat_id": "c1",
                "message_id": "m1",
                "message_type": "text",
                "content": '{"text": "hello"}',
            },
        },
    })

    frame = Frame()
    frame.SeqID = 1
    frame.LogID = 1
    frame.service = 1
    frame.method = 1
    h = frame.headers.add()
    h.key = "type"
    h.value = "event"
    h2 = frame.headers.add()
    h2.key = "message_id"
    h2.value = "msg_001"
    frame.payload = event_json.encode("utf-8")

    ack_payloads = []

    class MockWS:
        async def send(self, data):
            ack_payloads.append(data)

    mock_ws = MockWS()

    asyncio.run(plugin._on_ws_frame(mock_ws, frame.SerializeToString()))

    assert "feishu_c1_u1" in plugin.sessions
    assert len(ack_payloads) == 1
    ack_frame = Frame()
    ack_frame.ParseFromString(ack_payloads[0])
    ack_resp = json.loads(ack_frame.payload.decode("utf-8"))
    assert ack_resp["code"] == 200


def test_on_ws_frame_ignores_ping():
    import asyncio

    from lark_oapi.ws.pb.pbbp2_pb2 import Frame

    plugin = FeishuPlugin(config_path="/nonexistent/feishu.json")

    frame = Frame()
    frame.SeqID = 1
    frame.LogID = 1
    frame.service = 1
    frame.method = 2
    h = frame.headers.add()
    h.key = "type"
    h.value = "ping"
    frame.payload = b""

    ack_payloads = []

    class MockWS:
        async def send(self, data):
            ack_payloads.append(data)

    mock_ws = MockWS()

    asyncio.run(plugin._on_ws_frame(mock_ws, frame.SerializeToString()))
    assert len(ack_payloads) == 0


def test_split_markdown():
    from plugins.feishu import _split_markdown

    assert _split_markdown("short", 100) == ["short"]

    long_text = "## Title\n\n" + "para\n\n" * 100
    chunks = _split_markdown(long_text, 50)
    assert len(chunks) > 1
    assert all(len(c) <= 60 for c in chunks)


def test_split_markdown_no_double_newline():
    from plugins.feishu import _split_markdown

    text = "a" * 200
    chunks = _split_markdown(text, 50)
    assert len(chunks) > 1
