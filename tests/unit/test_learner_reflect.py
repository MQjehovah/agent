"""P1 测试：反思结构化输出。

验证 _parse_reflection 用 parse_llm_json 解析 JSON 契约、按 category 分流落库、
importance 限幅、非法 category 兜底，并对损坏输出/skip 安全返回 0。
"""
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from learning.learner import Learner  # noqa: E402
from memory.manager import MemoryManager  # noqa: E402
from storage import Storage  # noqa: E402


def _make_learner(tmp_path):
    s = Storage(str(tmp_path))
    mm = MemoryManager(storage=s, agent_id="test")
    learner = Learner(memory_manager=mm, llm_client=None, agent_id="test")
    return learner, mm, s


# ── _parse_reflection（纯解析 + 落库）──

def test_parse_valid_json_saves_with_category(tmp_path):
    learner, _, s = _make_learner(tmp_path)
    text = json.dumps({
        "items": [
            {"knowledge": "提交前先跑测试", "category": "failure_lesson", "importance": 5},
            {"knowledge": "rebase 可整理历史", "category": "knowledge", "importance": 3},
        ],
        "skip": False,
    })
    assert learner._parse_reflection(text, user_id="u1") == 2
    rows = s.query_memories("u1")
    cats = {r["category"] for r in rows}
    assert "failure_lesson" in cats
    assert "knowledge" in cats
    s.close()


def test_parse_normalizes_unknown_category(tmp_path):
    learner, _, s = _make_learner(tmp_path)
    text = json.dumps({"items": [{"knowledge": "x", "category": "bogus", "importance": 3}]})
    learner._parse_reflection(text, user_id="u1")
    assert s.query_memories("u1")[0]["category"] == "reflection"
    s.close()


def test_parse_clamps_importance(tmp_path):
    learner, _, s = _make_learner(tmp_path)
    text = json.dumps({"items": [{"knowledge": "x", "category": "reflection", "importance": 99}]})
    learner._parse_reflection(text, user_id="u1")
    assert s.query_memories("u1")[0]["importance"] == 5
    s.close()


def test_parse_skip_returns_zero(tmp_path):
    learner, _, s = _make_learner(tmp_path)
    text = json.dumps({"items": [], "skip": True})
    assert learner._parse_reflection(text, user_id="u1") == 0
    assert s.query_memories("u1") == []
    s.close()


def test_parse_malformed_returns_zero(tmp_path):
    learner, _, s = _make_learner(tmp_path)
    assert learner._parse_reflection("这不是 JSON，模型在乱说", user_id="u1") == 0
    assert s.query_memories("u1") == []
    s.close()


def test_parse_handles_code_fenced_json(tmp_path):
    learner, _, s = _make_learner(tmp_path)
    text = '```json\n{"items": [{"knowledge": "栅栏包裹", "category": "knowledge", "importance": 2}]}\n```'
    assert learner._parse_reflection(text, user_id="u1") == 1
    assert s.query_memories("u1")[0]["content"] == "栅栏包裹"
    s.close()


# ── 反思关键词重排（P1c load_memory）──

def test_load_memory_keyword_rerank_prioritizes_relevant(tmp_path):
    """有任务关键词匹配时，相关记忆排在前面被注入。"""
    _, mm, s = _make_learner(tmp_path)
    s.save_memory(scope="user", owner_id="u1", category="key_info", content="餐厅订餐电话 13800000000")
    s.save_memory(scope="user", owner_id="u1", category="key_info", content="部署用 docker compose，端口 8080")
    s.save_memory(scope="user", owner_id="u1", category="knowledge", content="Python 装饰器用法")

    out = mm.load_memory("u1", task="如何部署这个服务到 docker")
    # docker 相关记忆应在无关记忆之前
    assert out.index("docker") < out.index("订餐电话")
    s.close()


def test_load_memory_falls_back_when_no_keyword_match(tmp_path):
    """无任何关键词匹配时回退原序（importance/recency），不丢记忆。"""
    _, mm, s = _make_learner(tmp_path)
    s.save_memory(scope="user", owner_id="u1", category="key_info", content="偏好深色主题")
    out = mm.load_memory("u1", task="zzz完全不相关的任务qxr")
    assert "深色主题" in out
    s.close()


# ── reflect_on_task（端到端，含 LLM mock）──

@pytest.mark.asyncio
async def test_reflect_on_task_saves_from_llm_json(tmp_path):
    learner, _, s = _make_learner(tmp_path)
    client = MagicMock()
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = json.dumps({
        "items": [{"knowledge": "端到端经验", "category": "knowledge", "importance": 4}],
        "skip": False,
    })
    client.chat = AsyncMock(return_value=resp)
    learner.llm_client = client

    messages = [
        {"role": "user", "content": "帮我生成周报"},
        {"role": "assistant", "content": "已完成周报"},
    ]
    saved = await learner.reflect_on_task("生成周报", messages, user_id="u1")
    assert saved == 1
    assert any(r["content"] == "端到端经验" for r in s.query_memories("u1"))
    s.close()
