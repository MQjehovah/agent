import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
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
