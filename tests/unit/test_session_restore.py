"""P4c 测试：会话压缩摘要持久化与无损重建。

验证 session_meta 表的 save/get、compress_if_needed Layer-3 后持久化 summary、
以及重启恢复路径能读取持久化摘要（供 agent 注入为 [对话历史摘要]）。
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import storage as storage_mod  # noqa: E402
from agent_session import AgentSessionManager  # noqa: E402
from storage import Storage  # noqa: E402


def test_session_meta_save_and_get(tmp_path):
    s = Storage(str(tmp_path))
    assert s.get_session_meta("sess1") is None
    s.save_session_meta("sess1", "历史摘要内容")
    meta = s.get_session_meta("sess1")
    assert meta["session_id"] == "sess1"
    assert meta["last_summary"] == "历史摘要内容"
    # 覆盖更新（INSERT OR REPLACE）
    s.save_session_meta("sess1", "新摘要")
    assert s.get_session_meta("sess1")["last_summary"] == "新摘要"
    s.close()


@pytest.mark.asyncio
async def test_compress_persists_summary(tmp_path, monkeypatch):
    """Layer-3 压缩生成 summary 后，持久化到 session_meta。"""
    s = Storage(str(tmp_path))
    orig = storage_mod._storage_instance
    storage_mod._storage_instance = s
    try:
        client = MagicMock()
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = "这是压缩后的历史摘要"
        client.chat = AsyncMock(return_value=resp)

        # 放大窗口、抬高 token 估算，强制进入 Layer-3 且 history 非空
        monkeypatch.setattr(AgentSessionManager, "SLIDING_WINDOW_SIZE", 100)
        monkeypatch.setattr(AgentSessionManager, "estimate_tokens", lambda *a, **k: 100000)
        messages = [{"role": "system", "content": "sys"}] + [
            {"role": "user", "content": f"msg{i}"} for i in range(15)]

        await AgentSessionManager.compress_if_needed(
            messages, client, max_tokens=1000, session_id="sess-compress")

        meta = s.get_session_meta("sess-compress")
        assert meta is not None
        assert "压缩后的历史摘要" in meta["last_summary"]
    finally:
        storage_mod._storage_instance = orig
        s.close()


@pytest.mark.asyncio
async def test_compress_without_session_id_skips_persist(tmp_path, monkeypatch):
    """未传 session_id 时不持久化（向后兼容）。"""
    s = Storage(str(tmp_path))
    orig = storage_mod._storage_instance
    storage_mod._storage_instance = s
    try:
        client = MagicMock()
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = "摘要"
        client.chat = AsyncMock(return_value=resp)
        monkeypatch.setattr(AgentSessionManager, "SLIDING_WINDOW_SIZE", 100)
        monkeypatch.setattr(AgentSessionManager, "estimate_tokens", lambda *a, **k: 100000)
        messages = [{"role": "system", "content": "sys"}] + [
            {"role": "user", "content": f"m{i}"} for i in range(15)]
        await AgentSessionManager.compress_if_needed(messages, client, max_tokens=1000)
        # 未传 session_id → 不应写入任何 session_meta
        with s.get_connection() as conn:
            n = conn.execute("SELECT count(*) FROM session_meta").fetchone()[0]
        assert n == 0
    finally:
        storage_mod._storage_instance = orig
        s.close()


def test_restore_reads_persisted_summary(tmp_path):
    """恢复路径：get_session_meta 返回持久化摘要，供 agent 注入为历史摘要。"""
    s = Storage(str(tmp_path))
    s.save_session_meta("sess-r", "跨重启的历史摘要")
    meta = s.get_session_meta("sess-r")
    assert meta and meta.get("last_summary") == "跨重启的历史摘要"
    s.close()
