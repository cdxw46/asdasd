"""Motor RTP de SMURF: relay/proxy + mezclador para conferencias.

Diseño:
    * `RtpPort` representa un par de sockets UDP (RTP + RTCP) reservados.
    * `RtpAllocator` reserva pares de puertos pares/impares en un rango.
    * `RtpLeg` modela un endpoint remoto (IP:puerto) y mantiene contadores
      RTCP. Tiene un callback `on_audio` y un método `send_pcm` que aplican
      el códec adecuado.
    * `RtpRelay` enlaza dos legs y reenvía paquetes RTP entre ambos sin
      transcodear si la PT es la misma; transcodea a μ-law/A-law cuando
      difieren.
    * `ConferenceBridge` mezcla varios legs en un único stream PCM.

Para 500 llamadas simultáneas el motor maneja 1000 legs (2 por llamada).
Cada leg consume un par de sockets. Usamos asyncio DatagramProtocol y
buffers en memoria (sin GIL release pesado) para minimizar latencia.
"""
from __future__ import annotations

import asyncio
import os
import random
import secrets
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional, Set, Tuple

from ..util.logger import get_logger
from .codecs import (decode_to_pcm16, encode_from_pcm16, pcm_mix,
                     pcm_resample, samples_per_frame)
from .jitter import JitterBuffer
from .packet import (DtmfEvent, RtcpReportBlock, RtcpSR, RtpPacket,
                     build_rtcp_bye, build_rtcp_sr, parse_rtcp_compound)

log = get_logger("rtp")


def _set_dscp(sock: socket.socket, dscp: int) -> None:
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, dscp << 2)
    except Exception:
        pass


# ===================== Allocator =====================

class RtpAllocator:
    """Reserva pares (rtp, rtcp) de puertos UDP en el rango configurado.

    RFC 3550 §11: el puerto RTP debe ser par y el RTCP el siguiente impar.
    """
    def __init__(self, bind: str = "0.0.0.0",
                 port_min: int = 16384, port_max: int = 32767,
                 dscp: int = 46):
        self.bind = bind
        self.port_min = port_min if port_min % 2 == 0 else port_min + 1
        self.port_max = port_max
        self.dscp = dscp
        self._used: Set[int] = set()
        self._lock = asyncio.Lock()

    async def allocate(self) -> Tuple[socket.socket, socket.socket, int]:
        async with self._lock:
            attempts = 0
            while attempts < 200:
                p = random.randrange(self.port_min, self.port_max - 1, 2)
                if p in self._used or (p + 1) in self._used:
                    attempts += 1
                    continue
                try:
                    s_rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s_rtp.setblocking(False)
                    _set_dscp(s_rtp, self.dscp)
                    s_rtp.bind((self.bind, p))
                    s_rtcp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s_rtcp.setblocking(False)
                    _set_dscp(s_rtcp, self.dscp)
                    s_rtcp.bind((self.bind, p + 1))
                except OSError:
                    try: s_rtp.close()
                    except Exception: pass
                    try: s_rtcp.close()
                    except Exception: pass
                    attempts += 1
                    continue
                self._used.add(p)
                self._used.add(p + 1)
                return s_rtp, s_rtcp, p
            raise RuntimeError("No hay puertos RTP libres")

    def release(self, port: int) -> None:
        self._used.discard(port)
        self._used.discard(port + 1)


# ===================== Leg =====================

class _RtpDatagram(asyncio.DatagramProtocol):
    def __init__(self, owner: "RtpLeg", is_rtcp: bool):
        self.owner = owner
        self.is_rtcp = is_rtcp
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            if self.is_rtcp:
                self.owner._on_rtcp(data, addr)
            else:
                self.owner._on_rtp(data, addr)
        except Exception:
            log.exception("Error procesando paquete %s", "RTCP" if self.is_rtcp else "RTP")


@dataclass
class RtpStats:
    rx_pkts: int = 0
    rx_bytes: int = 0
    tx_pkts: int = 0
    tx_bytes: int = 0
    lost: int = 0
    jitter_ms: float = 0.0
    last_rx: float = 0.0
    last_tx: float = 0.0


class RtpLeg:
    """Un endpoint remoto y los sockets locales asociados.

    El payload type negociado se almacena en `pt` (formato dominante de audio).
    `dtmf_pt` es el payload-type para telephone-event si está negociado.
    """
    def __init__(self, allocator: RtpAllocator, pt: int = 0, dtmf_pt: Optional[int] = 101,
                 ptime_ms: int = 20, sample_rate: int = 8000):
        self.allocator = allocator
        self.pt = pt
        self.dtmf_pt = dtmf_pt
        self.ptime_ms = ptime_ms
        self.sample_rate = sample_rate
        self.local_sock: Optional[socket.socket] = None
        self.local_rtcp: Optional[socket.socket] = None
        self.local_port: int = 0
        self._rtp_proto: Optional[_RtpDatagram] = None
        self._rtcp_proto: Optional[_RtpDatagram] = None
        self.remote_addr: Optional[Tuple[str, int]] = None
        self.remote_rtcp: Optional[Tuple[str, int]] = None
        self.ssrc_local: int = secrets.randbelow(0xFFFFFFFF)
        self.ssrc_remote: Optional[int] = None
        self.seq: int = secrets.randbelow(0xFFFF)
        self.timestamp: int = secrets.randbelow(0xFFFFFFFF)
        self.jitter = JitterBuffer(sample_rate=sample_rate)
        self.stats = RtpStats()
        self.on_rtp: Optional[Callable[[RtpPacket], None]] = None
        self.on_dtmf: Optional[Callable[[DtmfEvent], None]] = None
        self.on_rtcp: Optional[Callable[[bytes], None]] = None
        self.closed: bool = False
        self._auto_learn: bool = True
        self._tx_octets: int = 0
        self._tx_pkts_since_sr: int = 0
        self._rtcp_task: Optional[asyncio.Task] = None
        self._symmetric_rtcp: bool = True

    async def open(self) -> None:
        s_rtp, s_rtcp, port = await self.allocator.allocate()
        self.local_sock = s_rtp
        self.local_rtcp = s_rtcp
        self.local_port = port
        loop = asyncio.get_running_loop()
        rtp_t, rtp_p = await loop.create_datagram_endpoint(
            lambda: _RtpDatagram(self, False), sock=s_rtp,
        )
        rtcp_t, rtcp_p = await loop.create_datagram_endpoint(
            lambda: _RtpDatagram(self, True), sock=s_rtcp,
        )
        self._rtp_proto = rtp_p
        self._rtcp_proto = rtcp_p
        self._rtcp_task = asyncio.create_task(self._rtcp_loop())

    def set_remote(self, host: str, port: int) -> None:
        self.remote_addr = (host, port)
        self.remote_rtcp = (host, port + 1)
        # NAT keep-alive: enviar 3 paquetes RTP "comfort noise"/silencio para
        # abrir el pinhole en cualquier NAT/firewall del operador (sin esto,
        # el RTP de retorno desde un trunk externo no llega a SMURF).
        self._send_nat_punches()

    def _send_nat_punches(self, n: int = 4) -> None:
        if self.closed or not self.local_sock or not self.remote_addr:
            return
        # Payload PCMU: 0xFF representa silencio en µ-law (160 muestras = 20 ms)
        silence = b"\xff" * 160
        for _ in range(n):
            self.seq = (self.seq + 1) & 0xFFFF
            self.timestamp = (self.timestamp + 160) & 0xFFFFFFFF
            pkt = RtpPacket(payload_type=self.pt or 0, sequence=self.seq,
                            timestamp=self.timestamp, ssrc=self.ssrc_local,
                            payload=silence if (self.pt or 0) == 0 else b"\xd5" * 160)
            try:
                self.local_sock.sendto(pkt.serialize(), self.remote_addr)
                self.stats.tx_pkts += 1
                self._tx_pkts_since_sr += 1
                self._tx_octets += len(silence)
            except Exception:
                return

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self._rtcp_task:
                self._rtcp_task.cancel()
            if self.remote_rtcp and self.local_rtcp:
                try:
                    self.local_rtcp.sendto(build_rtcp_bye(self.ssrc_local, "smurf"), self.remote_rtcp)
                except Exception:
                    pass
            if self._rtp_proto and self._rtp_proto.transport:
                self._rtp_proto.transport.close()
            if self._rtcp_proto and self._rtcp_proto.transport:
                self._rtcp_proto.transport.close()
        finally:
            if self.local_port:
                self.allocator.release(self.local_port)

    # ------------- Recepción -------------

    def _on_rtp(self, data: bytes, addr) -> None:
        try:
            pkt = RtpPacket.parse(data)
        except Exception:
            return
        self.stats.rx_pkts += 1
        self.stats.rx_bytes += len(data)
        self.stats.last_rx = time.time()
        if self._auto_learn and (self.remote_addr is None or
                                 self.remote_addr[0] != addr[0]):
            self.remote_addr = addr
            if self._symmetric_rtcp:
                self.remote_rtcp = (addr[0], addr[1] + 1)
        if self.ssrc_remote is None:
            self.ssrc_remote = pkt.ssrc
        if self.dtmf_pt is not None and pkt.payload_type == self.dtmf_pt:
            ev = DtmfEvent.parse(pkt.payload)
            if ev and self.on_dtmf:
                try: self.on_dtmf(ev)
                except Exception: log.exception("on_dtmf falló")
            return
        self.jitter.push(pkt)
        self.stats.jitter_ms = self.jitter.jitter_ms
        self.stats.lost = self.jitter.packets_lost
        if self.on_rtp:
            try: self.on_rtp(pkt)
            except Exception: log.exception("on_rtp falló")

    def _on_rtcp(self, data: bytes, addr) -> None:
        if self.on_rtcp:
            try: self.on_rtcp(data)
            except Exception: log.exception("on_rtcp falló")

    # ------------- Envío -------------

    def send_pkt(self, pt: int, payload: bytes, marker: bool = False,
                 ts_increment: Optional[int] = None) -> None:
        if not self.remote_addr or not self.local_sock or self.closed:
            return
        self.seq = (self.seq + 1) & 0xFFFF
        if ts_increment is None:
            ts_increment = samples_per_frame(pt, self.ptime_ms)
        self.timestamp = (self.timestamp + ts_increment) & 0xFFFFFFFF
        pkt = RtpPacket(
            payload_type=pt, sequence=self.seq, timestamp=self.timestamp,
            ssrc=self.ssrc_local, marker=marker, payload=payload,
        )
        try:
            self.local_sock.sendto(pkt.serialize(), self.remote_addr)
            self.stats.tx_pkts += 1
            self.stats.tx_bytes += len(payload) + 12
            self._tx_octets += len(payload)
            self._tx_pkts_since_sr += 1
            self.stats.last_tx = time.time()
        except Exception:
            log.warning("RTP send falló a %s", self.remote_addr)

    def send_pcm16(self, pcm: bytes, marker: bool = False) -> None:
        encoded = encode_from_pcm16(pcm, self.pt)
        if encoded:
            self.send_pkt(self.pt, encoded, marker=marker)

    def send_dtmf(self, digit: str, duration_ms: int = 200) -> None:
        if self.dtmf_pt is None:
            return
        chars = "0123456789*#ABCD"
        idx = chars.find(digit.upper())
        if idx < 0:
            return
        samples_per_pkt = samples_per_frame(self.pt, self.ptime_ms)
        total_samples = int(self.sample_rate * duration_ms / 1000)
        samples_so_far = 0
        first = True
        while samples_so_far < total_samples:
            samples_so_far = min(total_samples, samples_so_far + samples_per_pkt)
            end = samples_so_far >= total_samples
            payload = struct.pack("!BBH", idx & 0x0F,
                                  (0x80 if end else 0) | 10,  # vol=-10dBm0
                                  samples_so_far)
            self.send_pkt(self.dtmf_pt, payload, marker=first,
                          ts_increment=0 if not first else samples_per_pkt)
            first = False
        # tres paquetes finales redundantes con bit E
        for _ in range(2):
            payload = struct.pack("!BBH", idx & 0x0F, 0x80 | 10, total_samples)
            self.send_pkt(self.dtmf_pt, payload, ts_increment=0)

    async def _rtcp_loop(self) -> None:
        while not self.closed:
            await asyncio.sleep(5.0)
            if not self.remote_rtcp or not self.local_rtcp:
                continue
            now = time.time()
            ntp = now + 2208988800
            ntp_msw = int(ntp)
            ntp_lsw = int((ntp - ntp_msw) * 4294967296)
            sr = RtcpSR(
                ssrc=self.ssrc_local,
                ntp_msw=ntp_msw, ntp_lsw=ntp_lsw & 0xFFFFFFFF,
                rtp_ts=self.timestamp,
                pkt_count=self.stats.tx_pkts,
                octet_count=self._tx_octets & 0xFFFFFFFF,
                reports=[],
            )
            try:
                self.local_rtcp.sendto(build_rtcp_sr(sr), self.remote_rtcp)
            except Exception:
                pass


# ===================== Relay =====================

class RtpRelay:
    """Une dos legs reenviando RTP entre ambos. Si las PTs difieren, transcodea.

    También expone callbacks para detectar DTMF y eventos de fin de llamada
    (silencio prolongado).
    """
    def __init__(self, leg_a: RtpLeg, leg_b: RtpLeg,
                 max_silence_ms: int = 30000):
        self.a = leg_a
        self.b = leg_b
        self.max_silence_ms = max_silence_ms
        self.dtmf_listeners: List[Callable[[str, DtmfEvent], None]] = []
        self.on_close: Optional[Callable[[], None]] = None
        self._closed = False

    def start(self) -> None:
        self.a.on_rtp = self._a_to_b
        self.b.on_rtp = self._b_to_a
        self.a.on_dtmf = lambda ev: self._dtmf("a", ev)
        self.b.on_dtmf = lambda ev: self._dtmf("b", ev)

    def _a_to_b(self, pkt: RtpPacket) -> None:
        self._forward(self.a, self.b, pkt)

    def _b_to_a(self, pkt: RtpPacket) -> None:
        self._forward(self.b, self.a, pkt)

    def _forward(self, src: RtpLeg, dst: RtpLeg, pkt: RtpPacket) -> None:
        if dst.closed or not dst.remote_addr:
            return
        if src.pt == dst.pt:
            payload = pkt.payload
            dst.send_pkt(dst.pt, payload, marker=pkt.marker,
                         ts_increment=samples_per_frame(dst.pt, dst.ptime_ms))
        else:
            pcm, _rate = decode_to_pcm16(pkt.payload, src.pt)
            if pcm:
                dst.send_pcm16(pcm, marker=pkt.marker)

    def _dtmf(self, side: str, ev: DtmfEvent) -> None:
        for cb in self.dtmf_listeners:
            try: cb(side, ev)
            except Exception: log.exception("dtmf listener falló")
        if not ev.end:
            return
        # propagamos DTMF al lado opuesto
        target = self.b if side == "a" else self.a
        target.send_dtmf(ev.char)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.a.close()
        await self.b.close()
        if self.on_close:
            try: self.on_close()
            except Exception: log.exception("relay on_close falló")
