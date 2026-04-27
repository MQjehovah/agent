import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("agent.autonomous.eventbus")


@dataclass(order=False)
class Event:
    type: str
    source: str
    payload: dict
    priority: int = 3
    created_at: float = field(default_factory=time.time)
    goal_id: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __lt__(self, other):
        if not isinstance(other, Event):
            return NotImplemented
        return (self.priority, self.created_at) < (other.priority, other.created_at)

    def __le__(self, other):
        if not isinstance(other, Event):
            return NotImplemented
        return (self.priority, self.created_at) <= (other.priority, other.created_at)

    def __gt__(self, other):
        if not isinstance(other, Event):
            return NotImplemented
        return (self.priority, self.created_at) > (other.priority, other.created_at)

    def __ge__(self, other):
        if not isinstance(other, Event):
            return NotImplemented
        return (self.priority, self.created_at) >= (other.priority, other.created_at)

    def __eq__(self, other):
        if not isinstance(other, Event):
            return NotImplemented
        return self.id == other.id

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "source": self.source,
            "payload": json.dumps(self.payload, ensure_ascii=False),
            "priority": self.priority,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Event":
        payload = data.get("payload", "{}")
        if isinstance(payload, str):
            payload = json.loads(payload)
        return cls(
            id=data["id"],
            type=data["type"],
            source=data["source"],
            payload=payload,
            priority=data.get("priority", 3),
            created_at=data.get("created_at", time.time()),
        )


class EventBus:
    def __init__(self, db_path: str = ""):
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._subscribers: list[Callable[[Event], Any]] = []
        self._db_path = db_path
        if db_path:
            self._init_db()
            self._replay_pending()

    def _init_db(self):
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS eventbus_events (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    source TEXT DEFAULT '',
                    payload TEXT DEFAULT '{}',
                    priority INTEGER DEFAULT 3,
                    created_at REAL,
                    consumed INTEGER DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_eventbus_consumed ON eventbus_events(consumed, priority)"
            )
            conn.commit()

    def _replay_pending(self):
        if not self._db_path:
            return
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM eventbus_events WHERE consumed = 0 ORDER BY priority ASC, created_at ASC"
                ).fetchall()
            for row in rows:
                event = Event.from_dict(dict(row))
                self._queue.put_nowait((-event.priority, event.created_at, event.id, event))
            if rows:
                logger.info("从持久化恢复 %d 个未消费事件", len(rows))
        except Exception:
            logger.exception("恢复持久化事件失败")

    def _persist_event(self, event: Event):
        if not self._db_path:
            return
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO eventbus_events
                        (id, type, source, payload, priority, created_at, consumed)
                    VALUES (?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        event.id,
                        event.type,
                        event.source,
                        json.dumps(event.payload, ensure_ascii=False),
                        event.priority,
                        event.created_at,
                    ),
                )
                conn.commit()
        except Exception:
            logger.exception("持久化事件失败")

    def _mark_consumed(self, event: Event):
        if not self._db_path:
            return
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    "UPDATE eventbus_events SET consumed = 1 WHERE id = ?",
                    (event.id,),
                )
                conn.commit()
        except Exception:
            logger.exception("标记事件消费失败")

    async def publish(self, event: Event):
        self._persist_event(event)
        self._queue.put_nowait((-event.priority, event.created_at, event.id, event))
        for callback in self._subscribers:
            result = callback(event)
            if asyncio.iscoroutine(result):
                await result

    async def get(self, timeout: float | None = None):
        if timeout is not None:
            _, _, _, event = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        else:
            _, _, _, event = await self._queue.get()
        self._mark_consumed(event)
        return event

    def subscribe(self, callback: Callable[[Event], Any]):
        self._subscribers.append(callback)

    def size(self) -> int:
        return self._queue.qsize()

    def empty(self) -> bool:
        return self._queue.empty()
