"""Jitter buffer adaptativo simple para RTP.

No intenta hacer Plc/PLC ni elastic buffers complejos: sólo reordena por
sequence number, descarta duplicados, marca pérdidas y entrega frames a
tiempo. La latencia objetivo se ajusta entre `min_ms` y `max_ms` según el
jitter observado (RFC 3550 §6.4.1, fórmula recursiva).
"""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

from .packet import RtpPacket


class JitterBuffer:
    def __init__(self, sample_rate: int = 8000,
                 min_ms: int = 40, max_ms: int = 200, target_ms: int = 60):
        self.sample_rate = sample_rate
        self.min_ms = min_ms
        self.max_ms = max_ms
        self.target_ms = target_ms
        self._buf: "OrderedDict[int, RtpPacket]" = OrderedDict()
        self._next_seq: Optional[int] = None
        self._first_arrival: Optional[float] = None
        self._first_ts: Optional[int] = None
        self._jitter: float = 0.0
        self._last_transit: Optional[float] = None
        self._lost: int = 0

    def push(self, pkt: RtpPacket) -> None:
        # Estima jitter (RFC 3550)
        now = time.time()
        arrival_ts = int(now * self.sample_rate)
        transit = arrival_ts - pkt.timestamp
        if self._last_transit is not None:
            d = abs(transit - self._last_transit)
            self._jitter += (d - self._jitter) / 16.0
        self._last_transit = transit

        if self._next_seq is None:
            self._next_seq = pkt.sequence
            self._first_arrival = now
            self._first_ts = pkt.timestamp
        # descarta paquetes ya entregados (seq < next pero con wrap)
        if _seq_lt(pkt.sequence, self._next_seq):
            return
        self._buf[pkt.sequence] = pkt
        # límite duro
        if len(self._buf) > self.max_ms // 5:
            # mantén sólo los más recientes
            while len(self._buf) > self.max_ms // 5:
                self._buf.popitem(last=False)

    def pop_ready(self, ptime_ms: int = 20) -> List[Optional[RtpPacket]]:
        """Devuelve los frames listos en orden. None significa pérdida (PLC)."""
        if self._next_seq is None or self._first_arrival is None:
            return []
        elapsed_ms = (time.time() - self._first_arrival) * 1000.0
        if elapsed_ms < self.target_ms:
            return []
        out: List[Optional[RtpPacket]] = []
        for _ in range(int((elapsed_ms - self.target_ms) // ptime_ms) + 1):
            seq = self._next_seq
            pkt = self._buf.pop(seq, None)
            if pkt is None:
                self._lost += 1
                out.append(None)
            else:
                out.append(pkt)
            self._next_seq = (seq + 1) & 0xFFFF
            if not self._buf:
                break
        # ajusta target dinámicamente
        jitter_ms = self._jitter * 1000.0 / self.sample_rate
        target = max(self.min_ms, min(self.max_ms, int(2.5 * jitter_ms) + 20))
        self.target_ms = (self.target_ms * 7 + target) // 8
        return out

    @property
    def jitter_ms(self) -> float:
        return self._jitter * 1000.0 / self.sample_rate

    @property
    def packets_lost(self) -> int:
        return self._lost


def _seq_lt(a: int, b: int) -> bool:
    """Compara seq con wrap-around 16 bit: True si a < b."""
    return ((a - b) & 0xFFFF) > 0x8000
