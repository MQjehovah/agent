import sqlite3
import json
import logging
import threading
import asyncio
from typing import List, Dict, Any, Optional
from datetime import datetime
from pathlib import Path
from queue import Queue
from contextlib import contextmanager

logger = logging.getLogger("agent.storage")

_storage_instance: Optional["Storage"] = None


def get_storage() -> Optional["Storage"]:
    return _storage_instance


def init_storage(workspace: str) -> "Storage":
    global _storage_instance
    _storage_instance = Storage(workspace)
    return _storage_instance


class Storage:
    """SQLite 存储管理器，使用连接池和批量写入"""

    def __init__(self, workspace: str, pool_size: int = 5):
        self.workspace = workspace
        self.db_path = Path(workspace) / "data.db"
        self.pool_size = pool_size
        self._connection_pool: List[sqlite3.Connection] = []
        self._pool_lock = threading.Lock()
        self._write_queue: Queue = Queue()
        self._write_thread: Optional[threading.Thread] = None
        self._running = True
        self._init_db()
        self._init_pool()
        self._start_write_thread()

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    agent_id TEXT,
                    role TEXT,
                    content TEXT,
                    tool_calls TEXT,
                    tool_call_id TEXT,
                    name TEXT,
                    created_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
                CREATE INDEX IF NOT EXISTS idx_messages_agent ON messages(agent_id);
                CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
            """)

    def _init_pool(self):
        """初始化连接池"""
        for _ in range(self.pool_size):
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._connection_pool.append(conn)
        logger.debug(f"数据库连接池初始化完成，大小: {self.pool_size}")

    @contextmanager
    def _get_connection(self):
        """从连接池获取连接"""
        conn = None
        with self._pool_lock:
            if self._connection_pool:
                conn = self._connection_pool.pop()
            else:
                # 连接池耗尽，创建临时连接
                conn = sqlite3.connect(self.db_path, check_same_thread=False)
                conn.row_factory = sqlite3.Row

        try:
            yield conn
        finally:
            with self._pool_lock:
                if len(self._connection_pool) < self.pool_size:
                    self._connection_pool.append(conn)
                else:
                    # 连接池已满，关闭临时连接
                    conn.close()

    def _start_write_thread(self):
        """启动批量写入线程"""
        self._write_thread = threading.Thread(target=self._write_worker, daemon=True)
        self._write_thread.start()
        logger.debug("批量写入线程已启动")

    def _write_worker(self):
        """批量写入工作线程"""
        batch = []
        batch_size = 10
        flush_interval = 1.0  # 秒

        import time
        last_flush = time.time()

        while self._running:
            try:
                # 从队列获取消息
                item = self._write_queue.get(timeout=0.1)
                batch.append(item)

                # 批量写入条件: 达到批次大小或超过刷新间隔
                if len(batch) >= batch_size or (time.time() - last_flush) >= flush_interval:
                    self._flush_batch(batch)
                    batch = []
                    last_flush = time.time()

            except Exception:
                # 队列获取超时，检查是否需要刷新
                if batch and (time.time() - last_flush) >= flush_interval:
                    self._flush_batch(batch)
                    batch = []
                    last_flush = time.time()

        # 线程停止时，写入剩余消息
        if batch:
            self._flush_batch(batch)

    def _flush_batch(self, batch: List[Dict]):
        """执行批量写入"""
        if not batch:
            return

        with self._get_connection() as conn:
            conn.executemany("""
                INSERT INTO messages (agent_id, session_id, role, content, tool_calls, tool_call_id, name, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                (
                    item['agent_id'],
                    item['session_id'],
                    item['role'],
                    item['content'],
                    json.dumps(item['tool_calls']) if item.get('tool_calls') else None,
                    item.get('tool_call_id', ''),
                    item.get('name', ''),
                    item['created_at']
                )
                for item in batch
            ])
            conn.commit()

        logger.debug(f"批量写入 {len(batch)} 条消息")

    def save_message(self, agent_id: str, session_id: str, role: str, content: str,
                     tool_calls: Optional[List] = None,
                     tool_call_id: str = "", name: str = ""):
        """保存消息到写入队列（异步写入）"""
        self._write_queue.put({
            'agent_id': agent_id,
            'session_id': session_id,
            'role': role,
            'content': content or "",
            'tool_calls': tool_calls,
            'tool_call_id': tool_call_id,
            'name': name,
            'created_at': datetime.now().isoformat()
        })

    def save_message_sync(self, agent_id: str, session_id: str, role: str, content: str,
                          tool_calls: Optional[List] = None,
                          tool_call_id: str = "", name: str = ""):
        """同步保存消息（立即写入）"""
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO messages (agent_id, session_id, role, content, tool_calls, tool_call_id, name, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                agent_id, session_id, role, content or "",
                json.dumps(tool_calls) if tool_calls else None,
                tool_call_id, name, datetime.now().isoformat()
            ))
            conn.commit()

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT role, content, tool_calls, tool_call_id, name
                FROM messages WHERE session_id = ? ORDER BY id
            """, (session_id,)).fetchall()

        messages = []
        for row in rows:
            msg = {"role": row["role"], "content": row["content"] or ""}
            if row["tool_calls"]:
                msg["tool_calls"] = json.loads(row["tool_calls"])
            if row["tool_call_id"]:
                msg["tool_call_id"] = row["tool_call_id"]
            if row["name"]:
                msg["name"] = row["name"]
            messages.append(msg)
        return messages

    def get_messages_by_date(self, date_str: str, agent_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            if agent_id:
                rows = conn.execute("""
                    SELECT session_id, role, content, tool_calls, tool_call_id, name
                    FROM messages
                    WHERE DATE(created_at) = ? AND agent_id = ?
                    ORDER BY session_id, id
                """, (date_str, agent_id)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT session_id, role, content, tool_calls, tool_call_id, name
                    FROM messages
                    WHERE DATE(created_at) = ?
                    ORDER BY session_id, id
                """, (date_str,)).fetchall()

        messages = []
        for row in rows:
            msg = {"session_id": row["session_id"], "role": row["role"], "content": row["content"] or ""}
            if row["tool_calls"]:
                msg["tool_calls"] = json.loads(row["tool_calls"])
            if row["tool_call_id"]:
                msg["tool_call_id"] = row["tool_call_id"]
            if row["name"]:
                msg["name"] = row["name"]
            messages.append(msg)
        return messages

    def get_all_agent_ids(self) -> List[str]:
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT DISTINCT agent_id FROM messages WHERE agent_id != ''
            """).fetchall()
        return [row[0] for row in rows]

    def close(self):
        """关闭存储管理器"""
        self._running = False
        if self._write_thread:
            self._write_thread.join(timeout=2.0)

        with self._pool_lock:
            for conn in self._connection_pool:
                conn.close()
            self._connection_pool.clear()

        logger.info("数据库存储管理器已关闭")