"""Gestor de colas de llamada para SMURF.

Estrategias soportadas:
    * roundrobin    : siguiente agente disponible empezando por el último servido
    * leastrecent   : agente con menos llamadas servidas
    * random        : aleatorio
    * priority      : agente con menor prioridad numérica primero

Cada cola tiene su propio loop de despacho que mira las llamadas en espera
y los agentes disponibles (registrados y no en llamada). Mientras espera,
el caller escucha música de espera. Al timeout o cola llena, salta al
destino "no_answer_dest" o cuelga.
"""
from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, TYPE_CHECKING

from ..rtp.engine import RtpLeg
from ..rtp.sounds import moh_loop
from ..rtp.wavfile import AudioPlayer
from ..sip.dialog import Dialog
from ..sip.message import make_response
from ..sip.sdp import SDP, negotiate_audio
from ..sip.uri import NameAddr
from ..util.logger import get_logger

if TYPE_CHECKING:
    from .b2bua import B2BUA, Call

log = get_logger("pbx.queue")


@dataclass
class QueuedCall:
    call: "Call"
    enqueued_at: float = field(default_factory=time.time)
    moh: Optional[AudioPlayer] = None


class _Queue:
    def __init__(self, b2bua: "B2BUA", row: dict):
        self.b2bua = b2bua
        self.row = row
        self.number: str = row["number"]
        self.strategy: str = row["strategy"]
        self.max_wait: int = row["max_wait"] or 300
        self.timeout: int = row["timeout"] or 25
        self.members: List[str] = [m.strip() for m in (row["members_csv"] or "").split(",") if m.strip()]
        self.no_answer_dest: Optional[str] = row.get("no_answer_dest")
        self.waiting: Deque[QueuedCall] = deque()
        self._rr_idx = 0
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._served: Dict[str, int] = {m: 0 for m in self.members}

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(0.5)
            if not self.waiting:
                continue
            qc = self.waiting[0]
            # caller colgó
            if qc.call.id not in self.b2bua.calls:
                self._dequeue(qc); continue
            if time.time() - qc.enqueued_at > self.max_wait:
                self._dequeue(qc)
                if self.no_answer_dest:
                    asyncio.create_task(self.b2bua._dispatch_simple(qc.call, self.no_answer_dest))
                else:
                    asyncio.create_task(self.b2bua._end_call(qc.call, "NO_ANSWER", "queue-timeout"))
                continue
            agent = self._pick_agent()
            if agent is None:
                continue
            self._dequeue(qc)
            asyncio.create_task(self._serve(qc, agent))

    def _pick_agent(self) -> Optional[str]:
        loc = self.b2bua.location
        avail: List[str] = []
        for m in self.members:
            bs = loc.get(f"sip:{m}@{self.b2bua.realm}")
            if not bs: continue
            # ¿agente ya en llamada?
            in_call = any(c.b.dialog and c.b.dialog.remote_uri.user == m
                          for c in self.b2bua.calls.values())
            if not in_call:
                avail.append(m)
        if not avail:
            return None
        if self.strategy == "random":
            return random.choice(avail)
        if self.strategy == "leastrecent":
            return min(avail, key=lambda m: self._served.get(m, 0))
        if self.strategy == "priority":
            for m in self.members:
                if m in avail: return m
            return avail[0]
        # roundrobin
        for _ in range(len(self.members)):
            cand = self.members[self._rr_idx % len(self.members)]
            self._rr_idx += 1
            if cand in avail:
                return cand
        return None

    def _dequeue(self, qc: QueuedCall) -> None:
        try:
            self.waiting.remove(qc)
        except ValueError:
            return
        if qc.moh:
            asyncio.create_task(qc.moh.stop())

    async def _serve(self, qc: QueuedCall, agent: str) -> None:
        loc = self.b2bua.location
        bs = loc.get(f"sip:{agent}@{self.b2bua.realm}")
        if not bs:
            self.waiting.appendleft(qc); return
        b = bs[0]
        ok = await self.b2bua._fork_one(qc.call, b, self.timeout)
        if ok:
            self._served[agent] = self._served.get(agent, 0) + 1
        else:
            # Re-encolar si la llamada sigue viva
            if qc.call.id in self.b2bua.calls:
                qc.enqueued_at = time.time()
                self.waiting.append(qc)


class QueueManager:
    def __init__(self, b2bua: "B2BUA"):
        self.b2bua = b2bua
        self._queues: Dict[str, _Queue] = {}

    async def _ensure_queue(self, number: str) -> Optional[_Queue]:
        q = self._queues.get(number)
        if q: return q
        row = await self.b2bua.db.fetchone(
            "SELECT * FROM queues WHERE number=? AND enabled=1", (number,)
        )
        if not row: return None
        q = _Queue(self.b2bua, row); q.start()
        self._queues[number] = q
        return q

    async def enqueue(self, call: "Call", number: str) -> None:
        q = await self._ensure_queue(number)
        if q is None:
            await call.a.server_tx.respond(404, "Not Found")
            await self.b2bua._end_call(call, "FAILED", "no-queue"); return
        # Contestar al caller y poner MOH
        a_leg = RtpLeg(self.b2bua.rtp, pt=0); await a_leg.open()
        call.a.leg = a_leg
        try:
            sdp_a = SDP.parse(call.a.invite_request.body)
        except Exception:
            await call.a.server_tx.respond(488); await self.b2bua._end_call(call, "FAILED", "no-sdp"); return
        am = sdp_a.first_audio()
        a_leg.set_remote(am.connection or sdp_a.connection or call.a.endpoint.host, am.port)
        for pt in am.formats:
            if pt != 101: a_leg.pt = pt; break
        ans = negotiate_audio(sdp_a, self.b2bua.public_ip, a_leg.local_port)
        ok = make_response(call.a.invite_request, 200, "OK",
                           to_tag=self.b2bua._uas_tag_for(call),
                           body=ans.serialize(), content_type="application/sdp")
        ok.set("Contact", str(NameAddr(uri=self.b2bua._build_local_contact(call.a.transport))))
        await call.a.server_tx.send_response(ok)
        call.a.dialog = Dialog.from_uas_2xx(call.a.invite_request, ok,
                                            self.b2bua._build_local_contact(call.a.transport))
        self.b2bua._by_dialog[call.a.dialog.id] = call.id
        moh = AudioPlayer(a_leg, moh_loop(60), loop=True).start()
        q.waiting.append(QueuedCall(call=call, moh=moh))
        log.info("Encolada llamada %s en cola %s (pos=%d)", call.id, number, len(q.waiting))


_QM: Optional[QueueManager] = None


def get_queue_manager(b2bua: "B2BUA") -> QueueManager:
    global _QM
    if _QM is None:
        _QM = QueueManager(b2bua)
    return _QM
