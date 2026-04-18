"""Grabador de audio: capta los streams entrantes de uno o dos legs y
los persiste como WAV PCM 16-bit mono 8 kHz (o 16 kHz para G.722).

Para una llamada se mezclan los dos sentidos para obtener un único WAV
estéreo donde left=A→B y right=B→A si el flag stereo=True. En modo
voicemail se graba sólo un sentido (mono).
"""
from __future__ import annotations

import os
import struct
import time
import wave
from collections import deque
from typing import Deque, Optional, Tuple

from ..util.logger import get_logger
from .codecs import decode_to_pcm16, samples_per_frame
from .engine import RtpLeg
from .packet import RtpPacket

log = get_logger("rtp.recorder")


class CallRecorder:
    def __init__(self, path: str, leg_a: RtpLeg, leg_b: Optional[RtpLeg] = None,
                 stereo: bool = True):
        self.path = path
        self.leg_a = leg_a
        self.leg_b = leg_b
        self.stereo = stereo and leg_b is not None
        self._a_q: Deque[Tuple[float, bytes]] = deque()
        self._b_q: Deque[Tuple[float, bytes]] = deque()
        self._wav: Optional[wave.Wave_write] = None
        self._started_at: float = 0.0
        self._sample_rate = 8000
        self._frame_period_s = 0.020
        self._stop = False
        self._rx_a_total = 0
        self._rx_b_total = 0
        self._a_pcm_total = 0
        self._b_pcm_total = 0

        self._orig_a = leg_a.on_rtp
        leg_a.on_rtp = self._on_a
        if leg_b is not None:
            self._orig_b = leg_b.on_rtp
            leg_b.on_rtp = self._on_b

    def start(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._wav = wave.open(self.path, "wb")
        self._wav.setnchannels(2 if self.stereo else 1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(self._sample_rate)
        self._started_at = time.time()
        log.info("Grabando llamada en %s", self.path)

    def _on_a(self, pkt: RtpPacket) -> None:
        if self._orig_a:
            try: self._orig_a(pkt)
            except Exception: log.exception("orig leg_a callback")
        pcm, _ = decode_to_pcm16(pkt.payload, self.leg_a.pt)
        self._rx_a_total += 1
        if pcm:
            self._a_q.append((time.time(), pcm))
            self._a_pcm_total += 1
            self._flush()

    def _on_b(self, pkt: RtpPacket) -> None:
        if self._orig_b:
            try: self._orig_b(pkt)
            except Exception: log.exception("orig leg_b callback")
        self._rx_b_total += 1
        if self.leg_b is None:
            return
        pcm, _ = decode_to_pcm16(pkt.payload, self.leg_b.pt)
        if pcm:
            self._b_q.append((time.time(), pcm))
            self._b_pcm_total += 1
            self._flush()

    def _flush(self) -> None:
        if self._wav is None or self._stop:
            return
        silence_frame = b"\x00" * 320  # 20 ms a 8 kHz / 16 bit
        if self.stereo:
            # Estrategia: en cada flush avanzamos hasta agotar el lado más
            # corto. Si un lado se queda muy atrás (no manda RTP por NAT/silencio)
            # rellenamos su columna con silencio para no bloquear la grabación.
            while True:
                if self._a_q and self._b_q:
                    _, a = self._a_q.popleft()
                    _, b = self._b_q.popleft()
                elif self._a_q and len(self._a_q) > 5:
                    _, a = self._a_q.popleft()
                    b = silence_frame
                elif self._b_q and len(self._b_q) > 5:
                    a = silence_frame
                    _, b = self._b_q.popleft()
                else:
                    return
                m = min(len(a), len(b))
                if m == 0:
                    continue
                a, b = a[:m], b[:m]
                inter = bytearray(m * 2)
                for i in range(0, m, 2):
                    inter[i*2:i*2+2] = a[i:i+2]
                    inter[i*2+2:i*2+4] = b[i:i+2]
                self._wav.writeframes(bytes(inter))
        else:
            q = self._a_q if not self.leg_b else self._a_q
            while q:
                _, frame = q.popleft()
                self._wav.writeframes(frame)

    def stop(self) -> float:
        self._stop = True
        try:
            if self._wav:
                self._wav.close()
                self._wav = None
        except Exception:
            log.exception("cerrando wav")
        dur = time.time() - self._started_at
        log.info("Recorder stop: rx_a=%d (pcm=%d) rx_b=%d (pcm=%d) dur=%.1fs",
                 self._rx_a_total, self._a_pcm_total,
                 self._rx_b_total, self._b_pcm_total, dur)
        if hasattr(self, "_orig_a"):
            self.leg_a.on_rtp = self._orig_a
        if self.leg_b is not None and hasattr(self, "_orig_b"):
            self.leg_b.on_rtp = self._orig_b
        return dur
