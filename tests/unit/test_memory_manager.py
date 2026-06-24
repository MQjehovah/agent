import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
from storage import Storage
from memory.manager import MemoryManager


def _setup(tmp_path):
    s = Storage(str(tmp_path))
    return MemoryManager(storage=s), s


def test_add_and_load_user_memory(tmp_path):
    m, s = _setup(tmp_path)
    m.add_key_info("userA", "A的关键信息")
    text = m.load_memory("userA")
    assert "A的关键信息" in text
    s.close()


def test_user_isolation(tmp_path):
    m, s = _setup(tmp_path)
    m.add_key_info("userA", "A的秘密")
    m.add_key_info("userB", "B的秘密")
    a = m.load_memory("userA")
    b = m.load_memory("userB")
    assert "A的秘密" in a and "B的秘密" not in a
    assert "B的秘密" in b and "A的秘密" not in b
    s.close()


def test_global_visible_to_all(tmp_path):
    m, s = _setup(tmp_path)
    m._add_global("knowledge", "公共知识", source="admin")
    assert "公共知识" in m.load_memory("userA")
    assert "公共知识" in m.load_memory("userB")
    s.close()


def test_categorized_format(tmp_path):
    m, s = _setup(tmp_path)
    m.add_preference("userA", "偏好深色模式")
    m.add_failure_lesson("userA", "shell", "rm -rf", "删除失败")
    text = m.load_memory("userA")
    assert "用户偏好" in text
    assert "避坑经验" in text
    s.close()


def test_load_memory_empty_user_returns_empty(tmp_path):
    """无 user_id 时返回空串，不加载任何记忆"""
    m, s = _setup(tmp_path)
    m.add_key_info("userA", "A的数据")
    assert m.load_memory("") == ""
    assert m.load_memory(None) == ""   # None 也应被视为无 user_id
    s.close()


def test_add_user_without_user_id_skips_and_warns(tmp_path, caplog):
    """无 user_id 时跳过写入且不抛异常，记忆不被存储"""
    import logging
    m, s = _setup(tmp_path)
    with caplog.at_level(logging.WARNING, logger="agent.memory"):
        m.add_key_info("", "不应被存的孤立数据")   # 不抛异常
    # 该数据不应出现在任何用户视角
    assert "不应被存的孤立数据" not in m.load_memory("userA")
    # 应有 warning 日志
    assert any("无 user_id" in r.message for r in caplog.records)
    s.close()
