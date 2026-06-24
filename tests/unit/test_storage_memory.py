import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
from datetime import datetime
from storage import Storage


def _new_storage(tmp_path):
    return Storage(str(tmp_path))


def test_memories_table_exists(tmp_path):
    s = _new_storage(tmp_path)
    with s.get_connection() as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='memories'").fetchall()
    assert len(rows) == 1
    s.close()


def test_memory_proposals_table_exists(tmp_path):
    s = _new_storage(tmp_path)
    with s.get_connection() as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='memory_proposals'").fetchall()
    assert len(rows) == 1
    s.close()


def test_memories_indexes_exist(tmp_path):
    s = _new_storage(tmp_path)
    with s.get_connection() as conn:
        idx = conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_memories%'").fetchall()
    names = [r[0] for r in idx]
    assert "idx_memories_scope_owner" in names
    assert "idx_memories_category" in names
    s.close()


def test_save_and_query_memory_isolation(tmp_path):
    s = _new_storage(tmp_path)
    now = datetime.now().isoformat()
    s.save_memory(scope="user", owner_id="userA", category="key_info", content="A的秘密", created_at=now)
    s.save_memory(scope="user", owner_id="userB", category="key_info", content="B的秘密", created_at=now)
    s.save_memory(scope="global", owner_id="", category="knowledge", content="公共知识", created_at=now)

    a = s.query_memories(user_id="userA")
    contents = [r["content"] for r in a]
    assert "A的秘密" in contents
    assert "公共知识" in contents
    assert "B的秘密" not in contents  # 隔离核心断言
    s.close()


def test_proposal_lifecycle(tmp_path):
    s = _new_storage(tmp_path)
    pid = s.save_proposal(content="候选通用知识", source_users='["userA"]', reason="通用")
    pending = s.list_proposals(status="pending")
    assert any(p["id"] == pid for p in pending)

    s.update_proposal_status(pid, status="approved", reviewer="admin1")
    approved = s.list_proposals(status="approved")
    assert any(p["id"] == pid and p["reviewer"] == "admin1" for p in approved)
    s.close()
