"""Bus interno de eventos (publish-subscribe asíncrono).

Todos los componentes (B2BUA, registrar, voicemail, recorder, conferencias)
publican eventos aquí. El API REST y el panel web los consumen vía
WebSocket. Los webhooks externos también.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set


@dataclass
class Event:
    type: str
    payload: Dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps({"type": self.type, "ts": self.ts,
                           "payload": self.payload}, default=str)


Subscriber = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._subs: Dict[str, List[Subscriber]] = {}
        self._all: List[Subscriber] = []
        self._lock = asyncio.Lock()
        self._history: List[Event] = []
        self._history_max = 500

    async def publish(self, type_: str, **payload: Any) -> Event:
        ev = Event(type=type_, payload=payload)
        self._history.append(ev)
        if len(self._history) > self._history_max:
            self._history = self._history[-self._history_max :]
        targets = list(self._all) + list(self._subs.get(type_, []))
        if targets:
            await asyncio.gather(*(self._safe(s, ev) for s in targets))
        return ev

    async def _safe(self, s: Subscriber, ev: Event) -> None:
        try:
            await s(ev)
        except Exception:
            pass

    def subscribe(self, type_: str, cb: Subscriber) -> Callable[[], None]:
        self._subs.setdefault(type_, []).append(cb)
        return lambda: self._subs.get(type_, []).remove(cb) if cb in self._subs.get(type_, []) else None

    def subscribe_all(self, cb: Subscriber) -> Callable[[], None]:
        self._all.append(cb)
        def off():
            if cb in self._all:
                self._all.remove(cb)
        return off

    def history(self) -> List[Event]:
        return list(self._history)


_BUS: Optional[EventBus] = None


def get_event_bus() -> EventBus:
    global _BUS
    if _BUS is None:
        _BUS = EventBus()
    return _BUS
