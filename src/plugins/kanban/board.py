import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger("plugin.kanban.board")

COLUMNS = ("backlog", "todo", "in_progress", "done")


@dataclass
class KanbanTask:
    id: str
    title: str
    description: str = ""
    priority: int = 3
    column: str = "backlog"
    assignee: str | None = None
    source: str = "user"
    tags: list = field(default_factory=list)
    parent_id: str | None = None
    interval: int | None = None
    last_run: float | None = None
    result: str | None = None
    error: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    started_at: str | None = None
    completed_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "priority": self.priority,
            "column": self.column,
            "assignee": self.assignee,
            "source": self.source,
            "tags": self.tags,
            "parent_id": self.parent_id,
            "interval": self.interval,
            "last_run": self.last_run,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_row(cls, row: dict) -> "KanbanTask":
        tags = row.get("tags", "[]")
        if isinstance(tags, str):
            tags = json.loads(tags)
        return cls(
            id=row["id"],
            title=row["title"],
            description=row.get("description", ""),
            priority=row.get("priority", 3),
            column=row.get("column", "backlog"),
            assignee=row.get("assignee"),
            source=row.get("source", "user"),
            tags=tags,
            parent_id=row.get("parent_id"),
            interval=row.get("interval"),
            last_run=row.get("last_run"),
            result=row.get("result"),
            error=row.get("error"),
            created_at=row.get("created_at", datetime.now().isoformat()),
            updated_at=row.get("updated_at", datetime.now().isoformat()),
            started_at=row.get("started_at"),
            completed_at=row.get("completed_at"),
        )

    @property
    def is_due(self) -> bool:
        if self.column not in ("backlog", "todo"):
            return False
        if self.interval is None:
            return True
        if self.last_run is None:
            return True
        return (time.time() - self.last_run) >= self.interval


class KanbanBoard:
    def __init__(self, storage=None, db_path: str = ""):
        self._storage = storage
        self.db_path = db_path
        if not storage:
            self._init_db()

    @contextmanager
    def _get_conn(self):
        if self._storage:
            with self._storage.get_connection() as conn:
                yield conn
        else:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            try:
                yield conn
            finally:
                conn.close()

    def _init_db(self):
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        with self._get_conn() as conn:
            conn.execute("""
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
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kanban_column "
                "ON kanban_tasks(column, priority, created_at)"
            )
            conn.commit()

    def add_task(
        self,
        title: str,
        description: str = "",
        priority: int = 3,
        column: str = "backlog",
        source: str = "user",
        tags: list | None = None,
        parent_id: str | None = None,
        interval: int | None = None,
    ) -> KanbanTask:
        task = KanbanTask(
            id=uuid.uuid4().hex[:12],
            title=title,
            description=description,
            priority=priority,
            column=column,
            source=source,
            tags=tags or [],
            parent_id=parent_id,
            interval=interval,
        )
        now = datetime.now().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO kanban_tasks
                   (id, title, description, priority, column, source,
                    tags, parent_id, interval,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task.id, task.title, task.description, task.priority,
                    task.column, task.source,
                    json.dumps(task.tags, ensure_ascii=False),
                    task.parent_id, task.interval,
                    now, now,
                ),
            )
            conn.commit()
        logger.info("看板任务已创建: [%s] %s → %s", source, title, column)
        return task

    def remove_task(self, task_id: str) -> bool:
        with self._get_conn() as conn:
            cursor = conn.execute("DELETE FROM kanban_tasks WHERE id = ?", (task_id,))
            conn.commit()
            return cursor.rowcount > 0

    def get_task(self, task_id: str) -> KanbanTask | None:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM kanban_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if row:
            return KanbanTask.from_row(dict(row))
        return None

    def list_tasks(
        self,
        column: str | None = None,
        source: str | None = None,
        assignee: str | None = None,
    ) -> list[KanbanTask]:
        clauses = []
        params: list = []
        if column:
            clauses.append("`column` = ?")
            params.append(column)
        if source:
            clauses.append("source = ?")
            params.append(source)
        if assignee is not None:
            if assignee == "":
                clauses.append("assignee IS NULL")
            else:
                clauses.append("assignee = ?")
                params.append(assignee)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._get_conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM kanban_tasks{where} "
                f"ORDER BY priority ASC, created_at ASC",
                params,
            ).fetchall()
        return [KanbanTask.from_row(dict(r)) for r in rows]

    def move_task(
        self, task_id: str, column: str, assignee: str | None = None
    ) -> bool:
        if column not in COLUMNS:
            return False
        now = datetime.now().isoformat()
        sets = ["`column` = ?", "updated_at = ?"]
        params: list = [column, now]
        if assignee is not None:
            sets.append("assignee = ?")
            params.append(assignee)
        params.append(task_id)
        with self._get_conn() as conn:
            conn.execute(
                f"UPDATE kanban_tasks SET {', '.join(sets)} WHERE id = ?", params
            )
            conn.commit()
        return True

    def mark_started(self, task_id: str, assignee: str) -> bool:
        now = datetime.now().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                """UPDATE kanban_tasks
                   SET `column`='in_progress', assignee=?,
                       started_at=?, updated_at=?
                   WHERE id=?""",
                (assignee, now, now, task_id),
            )
            conn.commit()
        return True

    def mark_completed(self, task_id: str, result: str | None = None) -> bool:
        now = datetime.now().isoformat()
        ts = time.time()
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT interval FROM kanban_tasks WHERE id=?", (task_id,)
            ).fetchone()
            interval = row["interval"] if row else None
            if interval is not None:
                conn.execute(
                    """UPDATE kanban_tasks
                       SET `column`='backlog', last_run=?, result=?, error=NULL,
                           started_at=NULL, completed_at=?, updated_at=?
                       WHERE id=?""",
                    (ts, result, now, now, task_id),
                )
            else:
                conn.execute(
                    """UPDATE kanban_tasks
                       SET `column`='done', result=?, error=NULL,
                           completed_at=?, updated_at=?
                       WHERE id=?""",
                    (result, now, now, task_id),
                )
            conn.commit()
        return True

    def mark_failed(self, task_id: str, error: str) -> bool:
        now = datetime.now().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                """UPDATE kanban_tasks
                   SET `column`='todo', error=?, started_at=NULL, updated_at=?
                   WHERE id=?""",
                (error, now, task_id),
            )
            conn.commit()
        return True

    def get_due_tasks(self) -> list[KanbanTask]:
        tasks = self.list_tasks(column="todo")
        backlog = self.list_tasks(column="backlog")
        candidates = backlog + tasks
        return sorted(
            [t for t in candidates if t.is_due],
            key=lambda t: (t.priority, t.created_at),
        )

    def get_in_progress_count(self) -> int:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM kanban_tasks WHERE `column`='in_progress'"
            ).fetchone()
        return row["cnt"] if row else 0

    def get_stats(self) -> dict:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT `column`, COUNT(*) as cnt FROM kanban_tasks GROUP BY `column`"
            ).fetchall()
        counts = {c: 0 for c in COLUMNS}
        for r in rows:
            counts[r["column"]] = r["cnt"]
        total = sum(counts.values())
        return {"total": total, "by_column": counts}

    def is_empty(self) -> bool:
        return self.get_stats()["total"] == 0

    def migrate_from_panel(self, panel_path: str):
        if not os.path.exists(panel_path):
            return
        try:
            with open(panel_path, encoding="utf-8") as f:
                data = json.load(f)
            migrated = 0
            for item in data.get("tasks", []):
                status_map = {
                    "pending": "todo",
                    "active": "in_progress",
                    "completed": "done",
                }
                col = status_map.get(item.get("status", "pending"), "backlog")
                self.add_task(
                    title=item.get("title", ""),
                    description=item.get("description", ""),
                    priority=item.get("priority", 3),
                    column=col,
                    source=item.get("source", "llm"),
                    interval=item.get("interval"),
                )
                migrated += 1
            backup = panel_path + ".bak"
            os.rename(panel_path, backup)
            logger.info("从 task_panel.json 迁移 %d 个任务到看板", migrated)
        except Exception:
            logger.exception("迁移 task_panel.json 失败")
