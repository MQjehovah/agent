import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from storage import Storage
from memory.manager import MemoryManager


def test_memory_isolation_across_users(tmp_path):
    """验证记忆按 user_id 隔离：写入接口与 agent 注入逻辑一致（load_memory(uid)）"""
    s = Storage(str(tmp_path))
    m = MemoryManager(storage=s)

    m.add_key_info("userA", "A的敏感数据")
    m.add_reflection("userA", "A的经验")

    # userB 看不到 userA
    assert "A的敏感数据" not in m.load_memory("userB")
    assert "A的经验" not in m.load_memory("userB")

    # userA 看得到
    assert "A的敏感数据" in m.load_memory("userA")
    assert "A的经验" in m.load_memory("userA")

    s.close()


def test_memory_empty_user_returns_empty(tmp_path):
    """无 user_id 时 load_memory 返回空串（agent._build_prompt 仅在有 uid 时加载）"""
    s = Storage(str(tmp_path))
    m = MemoryManager(storage=s)
    m.add_key_info("userA", "数据")
    assert m.load_memory("") == ""
    s.close()
