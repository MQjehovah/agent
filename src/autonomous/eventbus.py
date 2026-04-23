import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


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


class EventBus:
    def __init__(self):
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._subscribers: list[Callable[[Event], Any]] = []

    async def publish(self, event: Event):
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
        return event

    def subscribe(self, callback: Callable[[Event], Any]):
        self._subscribers.append(callback)

    def size(self) -> int:
        return self._queue.qsize()

    def empty(self) -> bool:
        return self._queue.empty()
