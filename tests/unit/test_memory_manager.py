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
