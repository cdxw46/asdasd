"""Gestion de una campana de llamadas (cola, concurrencia, resultados)."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Awaitable, Callable

from ami import RESULT_LABELS, Dialer

log = logging.getLogger("p1.campaign")

# Resultados que indican que la llamada ha terminado (libera la linea).
TERMINAL = {
    "NO_INPUT",
    "INVALID",
    "NO_ANSWER",
    "BUSY",
    "UNREACHABLE",
    "FAILED",
    "TRANSFER_END",
}

# Prioridad para decidir el resultado "titular" de cada numero.
PRIORITY = {
    "PRESSED1": 100,
    "TRANSFER_END": 90,
    "NO_INPUT": 50,
    "INVALID": 45,
    "BUSY": 40,
    "NO_ANSWER": 30,
    "UNREACHABLE": 25,
    "FAILED": 20,
    "ANSWERED": 10,
}

NotifyCallback = Callable[[str], Awaitable[None]]


class CallState:
    def __init__(self, number: str) -> None:
        self.number = number
        self.result: str | None = None
        self.event = asyncio.Event()

    def update(self, result: str) -> None:
        if self.result is None or PRIORITY.get(result, 0) > PRIORITY.get(self.result, 0):
            self.result = result
        if result in TERMINAL:
            self.event.set()


class Campaign:
    def __init__(
        self,
        numbers: list[str],
        max_concurrent: int,
        per_call_timeout: int,
        notify: NotifyCallback,
    ) -> None:
        self.id = uuid.uuid4().hex[:8]
        self.numbers = numbers
        self.max_concurrent = max(1, max_concurrent)
        self.per_call_timeout = per_call_timeout
        self.notify = notify
        self.calls: dict[str, CallState] = {n: CallState(n) for n in numbers}
        self.started_at = time.time()
        self.finished = False
        self._cancelled = False
        self._sem = asyncio.Semaphore(self.max_concurrent)

    def cancel(self) -> None:
        self._cancelled = True

    def handle_result(self, target: str, result: str, extra: dict) -> None:
        call = self.calls.get(target)
        if call is None:
            return
        call.update(result)

    @property
    def done_count(self) -> int:
        return sum(1 for c in self.calls.values() if c.result in TERMINAL or c.event.is_set())

    async def run(self, dialer: Dialer) -> None:
        await self.notify(
            f"🚀 Campaña <code>{self.id}</code> iniciada: "
            f"{len(self.numbers)} número(s), {self.max_concurrent} simultánea(s)."
        )
        tasks = [asyncio.create_task(self._run_one(dialer, n)) for n in self.numbers]
        await asyncio.gather(*tasks)
        self.finished = True
        await self.notify(self._summary())

    async def _run_one(self, dialer: Dialer, number: str) -> None:
        async with self._sem:
            if self._cancelled:
                self.calls[number].update("FAILED")
                return
            call = self.calls[number]
            try:
                await dialer.originate(number, self.id)
            except Exception as exc:  # noqa: BLE001
                log.exception("Error al originar %s", number)
                call.update("FAILED")
                await self.notify(f"❌ <code>{number}</code>: error al llamar ({exc})")
                return
            try:
                await asyncio.wait_for(call.event.wait(), timeout=self.per_call_timeout)
            except asyncio.TimeoutError:
                if call.result is None:
                    call.update("NO_ANSWER")
            label = RESULT_LABELS.get(call.result or "FAILED", call.result or "?")
            await self.notify(f"<code>{number}</code> → {label}")

    def _summary(self) -> str:
        counts: dict[str, int] = {}
        pressed: list[str] = []
        for c in self.calls.values():
            res = c.result or "FAILED"
            counts[res] = counts.get(res, 0) + 1
            if res in ("PRESSED1", "TRANSFER_END"):
                pressed.append(c.number)
        elapsed = int(time.time() - self.started_at)
        lines = [
            f"🏁 <b>Campaña {self.id} finalizada</b> ({elapsed}s)",
            f"Total: {len(self.numbers)}",
        ]
        for res, n in sorted(counts.items(), key=lambda x: -x[1]):
            lines.append(f"  {RESULT_LABELS.get(res, res)}: {n}")
        if pressed:
            lines.append("")
            lines.append("✅ <b>Pulsaron 1 / transferidos:</b>")
            lines.append("<code>" + "\n".join(pressed) + "</code>")
        return "\n".join(lines)
