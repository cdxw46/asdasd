"""Síntesis interna de tonos (DTMF, ringback, busy, dial, MoH).

Generamos PCM 16-bit mono a 8 kHz para no depender de ficheros WAV.
"""
from __future__ import annotations

import math
import struct
from typing import Iterable, List, Tuple

SR = 8000


def _sine(freq: float, dur_s: float, amp: float = 0.25,
          phase: float = 0.0) -> bytes:
    n = int(SR * dur_s)
    out = bytearray(n * 2)
    two_pi = 2 * math.pi
    A = amp * 32767
    for i in range(n):
        v = int(A * math.sin(two_pi * freq * (i / SR) + phase))
        struct.pack_into("<h", out, i * 2, v)
    return bytes(out)


def _silence(dur_s: float) -> bytes:
    return b"\x00" * int(SR * dur_s) * 2


def _mix(*streams: bytes) -> bytes:
    n = max(len(s) for s in streams)
    out = bytearray(n)
    for s in streams:
        for i in range(0, len(s), 2):
            cur = struct.unpack_from("<h", out, i)[0]
            add = struct.unpack_from("<h", s, i)[0]
            v = max(-32768, min(32767, cur + add))
            struct.pack_into("<h", out, i, v)
    return bytes(out)


def dial_tone(seconds: float = 1.0) -> bytes:
    return _mix(_sine(350, seconds, 0.25), _sine(440, seconds, 0.25))


def ringback_tone(seconds: float = 6.0) -> bytes:
    """Patrón EU: 1s tono / 4s silencio. NA: 2s/4s."""
    cycle = _mix(_sine(440, 1.0, 0.25), _sine(480, 1.0, 0.25)) + _silence(4.0)
    out = b""
    while len(out) / 2 / SR < seconds:
        out += cycle
    return out[: int(seconds * SR) * 2]


def busy_tone(seconds: float = 4.0) -> bytes:
    cycle = _mix(_sine(480, 0.5, 0.25), _sine(620, 0.5, 0.25)) + _silence(0.5)
    out = b""
    while len(out) / 2 / SR < seconds:
        out += cycle
    return out[: int(seconds * SR) * 2]


def congestion_tone(seconds: float = 4.0) -> bytes:
    cycle = _mix(_sine(480, 0.25, 0.25), _sine(620, 0.25, 0.25)) + _silence(0.25)
    out = b""
    while len(out) / 2 / SR < seconds:
        out += cycle
    return out[: int(seconds * SR) * 2]


_DTMF_FREQS = {
    "1": (697, 1209), "2": (697, 1336), "3": (697, 1477), "A": (697, 1633),
    "4": (770, 1209), "5": (770, 1336), "6": (770, 1477), "B": (770, 1633),
    "7": (852, 1209), "8": (852, 1336), "9": (852, 1477), "C": (852, 1633),
    "*": (941, 1209), "0": (941, 1336), "#": (941, 1477), "D": (941, 1633),
}


def dtmf_tone(digit: str, dur_s: float = 0.18, gap_s: float = 0.05) -> bytes:
    f1, f2 = _DTMF_FREQS[digit.upper()]
    return _mix(_sine(f1, dur_s, 0.30), _sine(f2, dur_s, 0.30)) + _silence(gap_s)


def synth_say_digits(digits: str) -> bytes:
    """Pseudo-TTS: emite los dígitos como DTMF lentos. Rudimentario pero útil."""
    out = b""
    for d in digits:
        if d in _DTMF_FREQS:
            out += dtmf_tone(d, 0.25, 0.15)
        elif d == " ":
            out += _silence(0.2)
    return out


def moh_loop(seconds: float = 30.0) -> bytes:
    """Música de espera muy simple (acorde sostenido con vibrato)."""
    notes = [261.63, 329.63, 392.0]  # C-E-G
    out = bytearray()
    sec_per_note = 0.6
    n_cycles = int(seconds / (len(notes) * sec_per_note)) + 1
    for _ in range(n_cycles):
        for f in notes:
            seg = _mix(_sine(f, sec_per_note, 0.18),
                       _sine(f * 2, sec_per_note, 0.06),
                       _sine(f * 1.5, sec_per_note, 0.04))
            out += seg
    out = bytes(out)
    return out[: int(seconds * SR) * 2]
