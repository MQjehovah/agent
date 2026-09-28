"""sso_tokens 表 client_id 列: 老库迁移(补列, 幂等) + 存取 roundtrip。"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from storage.storage import Storage  # noqa: E402


def _cols(db_path) -> set:
    conn = sqlite3.connect(db_path)
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(sso_tokens)")}
    finally:
        conn.close()


def _old_db(db_path) -> None:
    """构造无 client_id 列的老库 sso_tokens 表 + 一条老行。"""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE sso_tokens (user_id INTEGER PRIMARY KEY, id_token TEXT NOT NULL DEFAULT '', "
        "refresh_token TEXT NOT NULL DEFAULT '', id_expires_at REAL NOT NULL DEFAULT 0, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO sso_tokens (user_id, id_token, refresh_token, id_expires_at) "
        "VALUES (7, 'id-old', 'rt-old', 1.0)"
    )
    conn.commit()
    conn.close()


def test_migration_adds_client_id_defaulting_to_agent(tmp_path):
    """老库启动迁移补列, 老行读缺省 'agent'; 重复启动幂等。"""
    db = tmp_path / "data.db"
    _old_db(db)

    s = Storage(str(tmp_path))
    try:
        assert "client_id" in _cols(db)
        row = s.get_sso_tokens(7)
        assert row is not None
        assert (row["id_token"], row["refresh_token"]) == ("id-old", "rt-old")
        assert row["client_id"] == "agent"
    finally:
        s.close()

    s2 = Storage(str(tmp_path))  # 幂等: 再次启动不报错, 数据仍在
    try:
        assert s2.get_sso_tokens(7)["client_id"] == "agent"
    finally:
        s2.close()


def test_save_get_roundtrip_keeps_client_id(tmp_path):
    s = Storage(str(tmp_path))
    try:
        s.save_sso_tokens(7, "id", "rt", 123.0, client_id="dashboard-gateway")
        row = s.get_sso_tokens(7)
        assert row is not None
        assert (row["id_token"], row["refresh_token"], row["id_expires_at"]) == ("id", "rt", 123.0)
        assert row["client_id"] == "dashboard-gateway"

        s.save_sso_tokens(7, "id2", "rt2", 124.0)  # 缺省 agent(web 回调/刷新回存)
        assert s.get_sso_tokens(7)["client_id"] == "agent"

        # 缺失列场景由迁移保证; get 对空值仍回退 agent
        with s.get_connection() as conn:
            conn.execute("UPDATE sso_tokens SET client_id='' WHERE user_id=7")
            conn.commit()
        assert s.get_sso_tokens(7)["client_id"] == "agent"

        assert s.get_sso_tokens(9) is None
    finally:
        s.close()
