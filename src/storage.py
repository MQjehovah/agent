import sqlite3
import json
import logging
import threading
import asyncio
import time
from typing import List, Dict, Any, Optional
from datetime import datetime
from pathlib import Path
from queue import Queue
from contextlib import contextmanager

logger = logging.getLogger("agent.storage")

_storage_instance: Optional["Storage"] = None


def get_storage() -> Optional["Storage"]:
    return _storage_instance


def init_storage(workspace: str, config_dir: str = "") -> "Storage":
    global _storage_instance
    _storage_instance = Storage(workspace, config_dir=config_dir)
    return _storage_instance


class Storage:
    """SQLite 存储管理器，使用连接池和批量写入"""

    def __init__(self, workspace: str, pool_size: int = 5, config_dir: str = ""):
        self.workspace = workspace
        self.config_dir = config_dir
        self.db_path = self._resolve_db_path(workspace, config_dir)
        self.pool_size = pool_size
        self._connection_pool: List[sqlite3.Connection] = []
        self._pool_lock = threading.Lock()
        self._write_lock = threading.RLock()
        self._write_queue: Queue = Queue()
        self._write_thread: Optional[threading.Thread] = None
        self._running = True
        self._init_db()
        self._rename_legacy_memory_files()
        self._init_pool()
        self._start_write_thread()
        if config_dir:
            self._migrate_legacy_dbs(config_dir)

    @staticmethod
    def _resolve_db_path(workspace: str, config_dir: str = "") -> Path:
        if config_dir:
            db_path = Path(config_dir) / "data.db"
            try:
                db_path.parent.mkdir(parents=True, exist_ok=True)
                with sqlite3.connect(str(db_path)) as conn:
                    conn.execute("CREATE TABLE IF NOT EXISTS _probe (id INTEGER)")
                    conn.execute("DROP TABLE IF EXISTS _probe")
                    conn.commit()
                return db_path
            except sqlite3.OperationalError:
                pass
            fallback = Path("/tmp/agent_storage") / Path(config_dir).name / "data.db"
            fallback.parent.mkdir(parents=True, exist_ok=True)
            logger.warning(f"SQLite 在 {db_path} 不可用，回退到 {fallback}")
            return fallback

        """选择合适的数据库路径：优先 workspace 本地，WSL 跨分区时回退到 /tmp"""
        db_path = Path(workspace) / "data.db"
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS _probe (id INTEGER)")
                conn.execute("DROP TABLE IF EXISTS _probe")
                conn.commit()
            return db_path
        except sqlite3.OperationalError:
            pass
        # WSL /mnt 路径不可用，回退到 /tmp
        fallback = Path("/tmp/agent_storage") / Path(workspace).name / "data.db"
        fallback.parent.mkdir(parents=True, exist_ok=True)
        logger.warning(f"SQLite 在 {db_path} 不可用，回退到 {fallback}")
        return fallback

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.db_path) as conn:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                pass
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
                    reasoning_content TEXT,
                    created_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
                CREATE INDEX IF NOT EXISTS idx_messages_agent ON messages(agent_id);
                CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);

                CREATE TABLE IF NOT EXISTS eventbus_events (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    source TEXT DEFAULT '',
                    payload TEXT DEFAULT '{}',
                    priority INTEGER DEFAULT 3,
                    created_at REAL,
                    consumed INTEGER DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_eventbus_consumed ON eventbus_events(consumed, priority);

                CREATE TABLE IF NOT EXISTS autonomous_goals (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    source TEXT DEFAULT 'user',
                    status TEXT DEFAULT 'pending',
                    priority INTEGER DEFAULT 3,
                    plan_json TEXT,
                    retry_count INTEGER DEFAULT 0,
                    max_retries INTEGER DEFAULT 3,
                    created_at TEXT,
                    updated_at TEXT,
                    completed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_goals_status ON autonomous_goals(status);
                CREATE INDEX IF NOT EXISTS idx_goals_priority ON autonomous_goals(priority DESC);

                CREATE TABLE IF NOT EXISTS kanban_tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    priority INTEGER DEFAULT 3,
                    column TEXT DEFAULT 'backlog',
                    assignee TEXT,
                    source TEXT DEFAULT 'user',
                    tags TEXT DEFAULT '[]',
                    parent_id TEXT,
                    interval INTEGER,
                    last_run REAL,
                    result TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_kanban_column ON kanban_tasks(column, priority, created_at);

                CREATE TABLE IF NOT EXISTS rbac_roles (
                    name TEXT PRIMARY KEY,
                    description TEXT DEFAULT '',
                    allowed_tools TEXT DEFAULT '[]',
                    allowed_agents TEXT DEFAULT '[]',
                    created_at TEXT
                );

                CREATE TABLE IF NOT EXISTS rbac_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    department TEXT DEFAULT '',
                    role TEXT NOT NULL DEFAULT 'default',
                    status TEXT DEFAULT 'active',
                    created_at TEXT,
                    updated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS rbac_user_identities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    platform TEXT NOT NULL,
                    platform_uid TEXT NOT NULL,
                    created_at TEXT,
                    FOREIGN KEY (user_id) REFERENCES rbac_users(id),
                    UNIQUE(platform, platform_uid)
                );

                CREATE INDEX IF NOT EXISTS idx_rbac_identities ON rbac_user_identities(platform, platform_uid);

                CREATE TABLE IF NOT EXISTS memories (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope       TEXT NOT NULL,
                    owner_id    TEXT NOT NULL DEFAULT '',
                    agent_id    TEXT DEFAULT '',
                    category    TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    source      TEXT DEFAULT '',
                    importance  INTEGER DEFAULT 3,
                    created_at  TEXT,
                    updated_at  TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_memories_scope_owner ON memories(scope, owner_id);
                CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);

                CREATE TABLE IF NOT EXISTS memory_proposals (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    content       TEXT NOT NULL,
                    source_users  TEXT DEFAULT '[]',
                    reason        TEXT DEFAULT '',
                    status        TEXT DEFAULT 'pending',
                    created_at    TEXT,
                    reviewed_at   TEXT,
                    reviewer      TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_proposals_status ON memory_proposals(status);

                CREATE TABLE IF NOT EXISTS web_tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token TEXT NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL,
                    description TEXT DEFAULT '',
                    created_at TEXT,
                    last_used_at TEXT,
                    FOREIGN KEY (user_id) REFERENCES rbac_users(id)
                );
                CREATE INDEX IF NOT EXISTS idx_web_tokens_token ON web_tokens(token);
            """)
            conn.execute("""
                INSERT OR IGNORE INTO rbac_roles (name, description, allowed_tools, allowed_agents, created_at)
                VALUES ('default', '默认角色-只能对话', '[]', '[]', datetime('now'))
            """)
            conn.execute("""
                INSERT OR IGNORE INTO rbac_roles (name, description, allowed_tools, allowed_agents, created_at)
                VALUES ('admin', '管理员-全部权限', '["*"]', '["*"]', datetime('now'))
            """)
            conn.commit()
            # migration: add reasoning_content column if missing
            try:
                conn.execute("ALTER TABLE messages ADD COLUMN reasoning_content TEXT")
            except sqlite3.OperationalError:
                pass
            # migration: add password_hash column for webui login
            try:
                conn.execute("ALTER TABLE rbac_users ADD COLUMN password_hash TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass

    def _new_connection(self) -> sqlite3.Connection:
        """创建一个设置了并发保护参数的 SQLite 连接

        - timeout / busy_timeout：写锁冲突时内部等待，而非立即抛 database is locked
        - WAL：读写并发，读不阻塞写
        - synchronous=NORMAL：WAL 下安全且更快
        """
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.OperationalError:
            pass
        return conn

    def _init_pool(self):
        """初始化连接池"""
        for _ in range(self.pool_size):
            self._connection_pool.append(self._new_connection())
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
                conn = self._new_connection()

        try:
            yield conn
        finally:
            with self._pool_lock:
                if len(self._connection_pool) < self.pool_size:
                    self._connection_pool.append(conn)
                else:
                    # 连接池已满，关闭临时连接
                    conn.close()

    @contextmanager
    def get_connection(self):
        with self._get_connection() as conn:
            yield conn

    def _migrate_legacy_dbs(self, config_dir: str):
        config_path = Path(config_dir)
        migrations = [
            ("autonomous.db", ["eventbus_events", "autonomous_goals"]),
            ("kanban.db", ["kanban_tasks"]),
        ]
        for db_name, tables in migrations:
            legacy_path = config_path / db_name
            if not legacy_path.exists():
                continue
            try:
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute("ATTACH DATABASE ? AS legacy_db", (str(legacy_path),))
                    for table in tables:
                        try:
                            count = conn.execute(
                                f"SELECT count(*) FROM legacy_db.{table}"
                            ).fetchone()[0]
                            conn.execute(
                                f"INSERT OR IGNORE INTO {table} SELECT * FROM legacy_db.{table}"
                            )
                            logger.info(f"迁移 {db_name}.{table}: {count} 条记录")
                        except sqlite3.OperationalError as e:
                            logger.warning(f"迁移 {db_name}.{table} 跳过: {e}")
                    conn.execute("DETACH DATABASE legacy_db")
                    conn.commit()
                bak_path = legacy_path.rename(legacy_path.with_suffix(".db.bak"))
                logger.info(f"已将 {legacy_path} 重命名为 {bak_path}")
            except Exception as e:
                logger.error(f"迁移 {db_name} 失败: {e}")

    def _start_write_thread(self):
        """启动批量写入线程"""
        self._write_thread = threading.Thread(target=self._write_worker, daemon=True)
        self._write_thread.start()
        logger.debug("批量写入线程已启动")

    def _write_worker(self):
        """批量写入工作线程

        注意：原实现用单个 `except Exception` 同时吞掉 queue.Empty 和 flush 异常，
        且 flush 失败不记日志、不重试，导致静默丢消息。现已分离处理。
        """
        batch = []
        batch_size = 10
        flush_interval = 1.0  # 秒
        last_flush = time.time()

        while self._running:
            try:
                item = self._write_queue.get(timeout=0.1)
                batch.append(item)
            except Exception:
                # queue.Empty 超时：检查是否需要按时间刷新
                if batch and (time.time() - last_flush) >= flush_interval:
                    self._safe_flush(batch)
                    batch = []
                    last_flush = time.time()
                continue

            if len(batch) >= batch_size or (time.time() - last_flush) >= flush_interval:
                self._safe_flush(batch)
                batch = []
                last_flush = time.time()

        # 线程停止时，写入剩余消息
        if batch:
            self._safe_flush(batch)

    def _safe_flush(self, batch: List[Dict]):
        """刷新写入批次；失败时记录日志并把消息重新入队等待重试，绝不静默丢弃"""
        try:
            self._flush_batch(batch)
        except Exception as e:
            logger.error(f"批量写入失败（{len(batch)} 条），已重新入队等待重试: {e}", exc_info=True)
            time.sleep(1.0)
            for item in batch:
                self._write_queue.put(item)

    def _flush_batch(self, batch: List[Dict]):
        """执行批量写入"""
        if not batch:
            return

        with self._write_lock, self._get_connection() as conn:
            conn.executemany("""
                INSERT INTO messages (agent_id, session_id, role, content, tool_calls, tool_call_id, name, reasoning_content, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                (
                    item['agent_id'],
                    item['session_id'],
                    item['role'],
                    item['content'],
                    json.dumps(item['tool_calls']) if item.get('tool_calls') else None,
                    item.get('tool_call_id', ''),
                    item.get('name', ''),
                    item.get('reasoning_content', ''),
                    item['created_at']
                )
                for item in batch
            ])
            conn.commit()

        logger.debug(f"批量写入 {len(batch)} 条消息")

    def save_message(self, agent_id: str, session_id: str, role: str, content: str,
                     tool_calls: Optional[List] = None,
                     tool_call_id: str = "", name: str = "", reasoning_content: str = ""):
        """保存消息到写入队列（异步写入）"""
        self._write_queue.put({
            'agent_id': agent_id,
            'session_id': session_id,
            'role': role,
            'content': content or "",
            'tool_calls': tool_calls,
            'tool_call_id': tool_call_id,
            'name': name,
            'reasoning_content': reasoning_content or "",
            'created_at': datetime.now().isoformat()
        })

    def save_message_sync(self, agent_id: str, session_id: str, role: str, content: str,
                          tool_calls: Optional[List] = None,
                          tool_call_id: str = "", name: str = "", reasoning_content: str = ""):
        """同步保存消息（立即写入）"""
        with self._write_lock, self._get_connection() as conn:
            conn.execute("""
                INSERT INTO messages (agent_id, session_id, role, content, tool_calls, tool_call_id, name, reasoning_content, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                agent_id, session_id, role, content or "",
                json.dumps(tool_calls) if tool_calls else None,
                tool_call_id, name, reasoning_content or "", datetime.now().isoformat()
            ))
            conn.commit()

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT role, content, tool_calls, tool_call_id, name, reasoning_content
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
            if row["reasoning_content"]:
                msg["reasoning_content"] = row["reasoning_content"]
            messages.append(msg)
        return messages

    def get_messages_by_date(self, date_str: str, agent_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            if agent_id:
                rows = conn.execute("""
                    SELECT session_id, role, content, tool_calls, tool_call_id, name, reasoning_content
                    FROM messages
                    WHERE DATE(created_at) = ? AND agent_id = ?
                    ORDER BY session_id, id
                """, (date_str, agent_id)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT session_id, role, content, tool_calls, tool_call_id, name, reasoning_content
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
            if row["reasoning_content"]:
                msg["reasoning_content"] = row["reasoning_content"]
            messages.append(msg)
        return messages

    def get_all_agent_ids(self) -> List[str]:
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT DISTINCT agent_id FROM messages WHERE agent_id != ''
            """).fetchall()
        return [row[0] for row in rows]

    def list_recent_sessions(self, limit: int = 20, agent_id: str = "") -> List[Dict[str, Any]]:
        """从 messages 表聚合最近 N 个会话（按最后活跃时间倒序）。

        内存中的 session_manager 仅保留活跃会话，重启即丢失；此方法基于
        持久化的 messages 表，可展示全部历史会话。agent_id 非空时仅返回该 agent 的会话。
        """
        sql = ("SELECT session_id, MAX(agent_id) AS agent_id, COUNT(*) AS msg_count, "
               "MIN(created_at) AS first_at, MAX(created_at) AS last_at "
               "FROM messages "
               "WHERE session_id IS NOT NULL AND session_id != '' AND session_id != 'temp'")
        args: List[Any] = []
        if agent_id:
            sql += " AND agent_id = ?"; args.append(agent_id)
        sql += " GROUP BY session_id ORDER BY MAX(created_at) DESC LIMIT ?"
        args.append(limit)
        with self._get_connection() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def list_session_agents(self) -> List[Dict[str, Any]]:
        """所有出现过的 agent_id 及其会话数（供 Sessions 按 agent 筛选下拉使用）"""
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT agent_id, COUNT(DISTINCT session_id) AS sessions, MAX(created_at) AS last_at
                FROM messages
                WHERE session_id IS NOT NULL AND session_id != '' AND session_id != 'temp'
                  AND agent_id != ''
                GROUP BY agent_id
                ORDER BY MAX(created_at) DESC
            """).fetchall()
        return [dict(r) for r in rows]

    def save_memory(self, scope: str, owner_id: str, category: str, content: str,
                    agent_id: str = "", source: str = "", importance: int = 3,
                    created_at: str = None) -> int:
        """写入一条记忆，返回 id"""
        now = created_at or datetime.now().isoformat()
        with self._write_lock, self._get_connection() as conn:
            cur = conn.execute(
                """INSERT INTO memories (scope, owner_id, agent_id, category, content, source, importance, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (scope, owner_id, agent_id, category, content, source, importance, now, now),
            )
            conn.commit()
            return cur.lastrowid

    def query_memories(self, user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """查询某用户可见记忆：自身 user 私有 + global 公共"""
        with self._get_connection() as conn:
            rows = conn.execute(
                """SELECT id, scope, owner_id, category, content, source, importance, created_at
                   FROM memories
                   WHERE (scope='user' AND owner_id = ?) OR scope = 'global'
                   ORDER BY scope DESC, importance DESC, updated_at DESC
                   LIMIT ?""",
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 记忆管理（WebUI 后台用）----------------

    def list_memories(self, scope: str = "", owner_id: str = "",
                      category: str = "", keyword: str = "",
                      limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        """管理用：带筛选的记忆列表（跨用户，按更新时间倒序）"""
        sql = ("SELECT id, scope, owner_id, agent_id, category, content, source, "
               "importance, created_at, updated_at FROM memories WHERE 1=1")
        args: List[Any] = []
        if scope:
            sql += " AND scope = ?"; args.append(scope)
        if owner_id:
            sql += " AND owner_id = ?"; args.append(owner_id)
        if category:
            sql += " AND category = ?"; args.append(category)
        if keyword:
            sql += " AND content LIKE ?"; args.append(f"%{keyword}%")
        sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        args.extend([limit, offset])
        with self._get_connection() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def count_memories(self, scope: str = "", owner_id: str = "",
                       category: str = "", keyword: str = "") -> int:
        sql = "SELECT COUNT(*) FROM memories WHERE 1=1"
        args: List[Any] = []
        if scope:
            sql += " AND scope = ?"; args.append(scope)
        if owner_id:
            sql += " AND owner_id = ?"; args.append(owner_id)
        if category:
            sql += " AND category = ?"; args.append(category)
        if keyword:
            sql += " AND content LIKE ?"; args.append(f"%{keyword}%")
        with self._get_connection() as conn:
            return conn.execute(sql, args).fetchone()[0]

    def get_memory(self, memory_id: int) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return dict(row) if row else None

    def update_memory(self, memory_id: int, content: str = None,
                      importance: int = None, category: str = None,
                      scope: str = None, owner_id: str = None) -> bool:
        sets, args = [], []
        if content is not None:
            sets.append("content = ?"); args.append(content)
        if importance is not None:
            sets.append("importance = ?"); args.append(int(importance))
        if category is not None:
            sets.append("category = ?"); args.append(category)
        if scope is not None:
            sets.append("scope = ?"); args.append(scope)
        if owner_id is not None:
            sets.append("owner_id = ?"); args.append(owner_id)
        if not sets:
            return False
        sets.append("updated_at = ?"); args.append(datetime.now().isoformat())
        args.append(memory_id)
        with self._write_lock, self._get_connection() as conn:
            cur = conn.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id = ?", args)
            conn.commit()
            return cur.rowcount > 0

    def delete_memory(self, memory_id: int) -> bool:
        with self._write_lock, self._get_connection() as conn:
            cur = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            conn.commit()
            return cur.rowcount > 0

    def save_proposal(self, content: str, source_users: str = "[]", reason: str = "") -> int:
        now = datetime.now().isoformat()
        with self._write_lock, self._get_connection() as conn:
            cur = conn.execute(
                """INSERT INTO memory_proposals (content, source_users, reason, status, created_at)
                   VALUES (?, ?, ?, 'pending', ?)""",
                (content, source_users, reason, now),
            )
            conn.commit()
            return cur.lastrowid

    def list_proposals(self, status: str = "pending") -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM memory_proposals WHERE status = ? ORDER BY id DESC", (status,)
            ).fetchall()
        return [dict(r) for r in rows]

    def update_proposal_status(self, proposal_id: int, status: str, reviewer: str) -> None:
        now = datetime.now().isoformat()
        with self._write_lock, self._get_connection() as conn:
            conn.execute(
                "UPDATE memory_proposals SET status = ?, reviewer = ?, reviewed_at = ? WHERE id = ?",
                (status, reviewer, now, proposal_id),
            )
            conn.commit()

    def get_proposal(self, proposal_id: int) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM memory_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
        return dict(row) if row else None

    def _rename_legacy_memory_files(self):
        """将旧文件记忆重命名为 .legacy.bak（不读入系统，方案B丢弃旧数据）"""
        import os as _os
        roots = []
        if self.config_dir:
            roots.append(_os.path.join(self.config_dir, "memory"))
        roots.append(_os.path.join(self.workspace, "memory"))
        for memory_dir in roots:
            if not _os.path.isdir(memory_dir):
                continue
            for name in _os.listdir(memory_dir):
                if name.endswith(".legacy.bak"):
                    continue
                path = _os.path.join(memory_dir, name)
                try:
                    _os.rename(path, path + ".legacy.bak")
                    logger.info(f"旧记忆文件已归档: {name}")
                except Exception:
                    pass

    # ---------------- WebUI Token & 密码 鉴权 ----------------

    def create_token(self, token: str, user_id: int, description: str = "") -> int:
        with self._write_lock, self._get_connection() as conn:
            cur = conn.execute(
                "INSERT INTO web_tokens (token, user_id, description, created_at) VALUES (?,?,?,?)",
                (token, user_id, description, datetime.now().isoformat()),
            )
            conn.commit()
            return cur.lastrowid

    def get_user_by_token(self, token: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT u.id, u.name, u.department, u.role, u.status, t.id AS token_id
                FROM web_tokens t JOIN rbac_users u ON t.user_id = u.id WHERE t.token = ?
            """, (token,)).fetchone()
        if row:
            with self._write_lock, self._get_connection() as conn:
                conn.execute("UPDATE web_tokens SET last_used_at = ? WHERE token = ?",
                             (datetime.now().isoformat(), token))
                conn.commit()
            return dict(row)
        return None

    def list_tokens(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT t.id, substring(t.token,1,8)||'...' AS token_preview, t.user_id,
                       u.name AS user_name, t.description, t.created_at, t.last_used_at
                FROM web_tokens t JOIN rbac_users u ON t.user_id = u.id ORDER BY t.id DESC
            """).fetchall()
        return [dict(r) for r in rows]

    def delete_token(self, token_id: int) -> bool:
        with self._write_lock, self._get_connection() as conn:
            cur = conn.execute("DELETE FROM web_tokens WHERE id = ?", (token_id,))
            conn.commit()
            return cur.rowcount > 0

    def set_user_password(self, user_id: int, password: str):
        import hashlib, secrets, base64
        salt = secrets.token_bytes(16)
        key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200000)
        ph = base64.b64encode(salt + key).decode()
        with self._write_lock, self._get_connection() as conn:
            conn.execute("UPDATE rbac_users SET password_hash = ? WHERE id = ?", (ph, user_id))
            conn.commit()

    def verify_user_password(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        import hashlib, base64
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT id, name, department, role, status, password_hash FROM rbac_users WHERE name = ? AND status = 'active'",
                (username,),
            ).fetchone()
        if not row or not row["password_hash"]:
            return None
        try:
            raw = base64.b64decode(row["password_hash"])
            salt, stored = raw[:16], raw[16:]
            key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200000)
            if key != stored:
                return None
        except Exception:
            return None
        return {"id": row["id"], "name": row["name"], "department": row["department"],
                "role": row["role"], "status": row["status"]}

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