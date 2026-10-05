"""Provider and bounded event fan-out primitives."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import Counter
from pathlib import Path
from queue import Full, Queue
from threading import RLock
from typing import Any


NormalizedEvent = dict[str, Any]


class NormalizedEventBus:
    """Fan out events to bounded queues without waiting for slow consumers."""

    def __init__(self, queue_size: int = 256) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be at least 1")
        self.queue_size = queue_size
        self._subscribers: dict[str, Queue[NormalizedEvent]] = {}
        self._lock = RLock()
        self.dropped: Counter[str] = Counter()
        self.failures: Counter[str] = Counter()

    def subscribe(
        self, name: str, *, queue_size: int | None = None
    ) -> Queue[NormalizedEvent]:
        if not name:
            raise ValueError("subscriber name must not be empty")
        with self._lock:
            if name in self._subscribers:
                raise ValueError(f"subscriber already exists: {name}")
            size = self.queue_size if queue_size is None else queue_size
            if size < 1:
                raise ValueError("queue_size must be at least 1")
            queue: Queue[NormalizedEvent] = Queue(maxsize=size)
            self._subscribers[name] = queue
            return queue

    def unsubscribe(self, name: str) -> None:
        with self._lock:
            self._subscribers.pop(name, None)

    def publish(self, event: NormalizedEvent) -> None:
        snapshot = dict(event)
        with self._lock:
            subscribers = tuple(self._subscribers.items())
        for name, queue in subscribers:
            try:
                queue.put_nowait(dict(snapshot))
            except Full:
                self.dropped[name] += 1
            except Exception:
                # A broken subscriber must not interrupt the provider callback.
                self.failures[name] += 1


class BaseEventProvider(ABC):
    """Explicitly selected provider that writes normalized events to a session."""

    def __init__(self, event_bus: NormalizedEventBus | None = None) -> None:
        self.event_bus = event_bus

    def publish_event(self, event: NormalizedEvent) -> None:
        if self.event_bus is None:
            return
        try:
            self.event_bus.publish(event)
        except Exception:
            # Event fan-out is auxiliary; provider capture and durable output win.
            return

    @abstractmethod
    async def run(self) -> Path:
        """Run until stopped and return the path to the provider session."""
