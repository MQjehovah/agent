import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory.curator import MemoryCurator
from storage import Storage


def _setup(tmp_path, llm_text='{"items": [{"generic": "设备保养需定期进行", "reason": "通用运维常识"}]}'):
    s = Storage(str(tmp_path))
    client = MagicMock()
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = llm_text
    client.chat = AsyncMock(return_value=resp)
    c = MemoryCurator(storage=s, llm_client=client)
    c._last_run = "2000-01-01T00:00:00"  # 确定性增量窗口，使测试不依赖"今天"
    return c, s


@pytest.mark.asyncio
async def test_curator_generates_proposal(tmp_path):
    c, s = _setup(tmp_path)
    s.save_memory(scope="user", owner_id="u1", category="key_info",
                  content="设备保养周期影响故障率", created_at="2026-06-24T00:00:00")
    n = await c.curate_once()
    pending = s.list_proposals("pending")
    assert n >= 1
    assert len(pending) >= 1
    assert pending[0]["content"] == "设备保养需定期进行"
    s.close()


@pytest.mark.asyncio
async def test_curator_no_llm_skips(tmp_path):
    s = Storage(str(tmp_path))
    c = MemoryCurator(storage=s, llm_client=None)
    n = await c.curate_once()
    assert n == 0
    s.close()


@pytest.mark.asyncio
async def test_curator_none_output(tmp_path):
    c, s = _setup(tmp_path, llm_text="NONE")
    s.save_memory(scope="user", owner_id="u1", category="key_info",
                  content="某用户偏好", created_at="2026-06-24T00:00:00")
    n = await c.curate_once()
    assert n == 0
    assert s.list_proposals("pending") == []
    s.close()


@pytest.mark.asyncio
async def test_curator_dedup_against_global(tmp_path):
    c, s = _setup(tmp_path)
    # 已存在相同 global 记忆
    s.save_memory(scope="global", owner_id="", category="knowledge",
                  content="设备保养需定期进行", created_at="2026-06-23T00:00:00")
    s.save_memory(scope="user", owner_id="u1", category="key_info",
                  content="设备保养周期影响故障率", created_at="2026-06-24T00:00:00")
    n = await c.curate_once()
    assert n == 0  # 与 global 重复，不生成申请
    s.close()
