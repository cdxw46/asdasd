"""SMURF media core service.

Provides:
- RTP/RTCP relay sessions
- RTP packet statistics and jitter approximation
- DTMF event extraction (RFC 4733 telephone-event payloads)
- Basic call recording sink metadata management
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import signal
import socket
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.bus import JsonCommandServer
from core.config import load_config
from core.db import Database
from core.logging_utils import configure_json_logging, get_logger

LOGGER = get_logger("media-core")


@dataclass(slots=True)
class RTPStats:
    packets: int = 0
    bytes: int = 0
    last_seq: int = -1
    lost: int = 0
    jitter: float = 0.0
    transit: float = 0.0
    last_timestamp: int = 0
    dtmf_events: list[int] = field(default_factory=list)


@dataclass(slots=True)
class RTPPeer:
    ip: str
    rtp_port: int
    rtcp_port: int


@dataclass(slots=True)
class RTPSession:
    call_id: str
    codec: str
    payload_type: int
    sample_rate: int
    peer_a: RTPPeer
    peer_b: RTPPeer
    created_at: int
    record: bool = False
    recording_path: str = ""
    stats_a_to_b: RTPStats = field(default_factory=RTPStats)
    stats_b_to_a: RTPStats = field(default_factory=RTPStats)


def _parse_rtp_header(packet: bytes) -> tuple[int, int, int, int, int] | None:
    if len(packet) < 12:
        return None
    b0, b1, seq, ts, ssrc = struct.unpack("!BBHII", packet[:12])
    version = (b0 >> 6) & 0x03
    cc = b0 & 0x0F
    if version != 2:
        return None
    pt = b1 & 0x7F
    header_len = 12 + (cc * 4)
    if len(packet) < header_len:
        return None
    return pt, seq, ts, ssrc, header_len


def _estimate_jitter(stats: RTPStats, ts: int, arrival: float, clock_rate: int) -> None:
    transit = arrival - (ts / float(clock_rate))
    if stats.packets == 0:
        stats.transit = transit
        return
    d = abs(transit - stats.transit)
    stats.transit = transit
    stats.jitter += (d - stats.jitter) / 16.0


def _extract_telephone_event(payload: bytes) -> int | None:
    # RFC 4733 named event payload: event(8),E/R/volume(8),duration(16)
    if len(payload) < 4:
        return None
    event = payload[0]
    return int(event)


class MediaCoreService:
    def __init__(self, config_path: str | None):
        self.config = load_config(config_path)
        configure_json_logging("media-core", self.config.global_.log_level)
        self.db = Database(self.config.database.sqlite_path)
        self.command_server = JsonCommandServer(
            host=self.config.bus.media_command_host,
            port=self.config.bus.media_command_port,
            handler=self._handle_command,
        )
        self.shutdown_event = asyncio.Event()
        self.sessions: dict[str, RTPSession] = {}
        self.port_pool = list(range(self.config.rtp.min_port, self.config.rtp.max_port, 2))
        random.shuffle(self.port_pool)
        self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_socket.setblocking(False)
        self.bound_port = self._allocate_rtp_port()
        self.udp_socket.bind((self.config.rtp.bind_host, self.bound_port))
        try:
            self.udp_socket.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, int(self.config.rtp.dscp) << 2)
        except OSError:
            LOGGER.warning("Unable to set DSCP on RTP socket")
        self.tasks: list[asyncio.Task] = []

        self.recording_dir = Path(self.config.rtp.recording_path)
        self.recording_dir.mkdir(parents=True, exist_ok=True)

    def _allocate_rtp_port(self) -> int:
        if not self.port_pool:
            raise RuntimeError("RTP port pool exhausted")
        return self.port_pool.pop()

    async def run(self):
        await self.command_server.start()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.shutdown_event.set)
            except NotImplementedError:
                pass

        self.tasks.append(asyncio.create_task(self._rtp_loop()))
        self.tasks.append(asyncio.create_task(self._rtcp_report_loop()))

        LOGGER.info(
            "media-core started",
            extra={
                "extra": {
                    "command_host": self.config.bus.media_command_host,
                    "command_port": self.config.bus.media_command_port,
                    "rtp_port_min": self.config.rtp.min_port,
                    "rtp_port_max": self.config.rtp.max_port,
                }
            },
        )

        await self.shutdown_event.wait()
        for task in self.tasks:
            task.cancel()
        await self.command_server.stop()
        self.udp_socket.close()

    async def _rtp_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            try:
                data, addr = await loop.sock_recvfrom(self.udp_socket, 65535)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                await asyncio.sleep(0.01)
                continue
            await self._handle_rtp_packet(data, addr)

    async def _handle_rtp_packet(self, packet: bytes, addr: tuple[str, int]):
        parsed = _parse_rtp_header(packet)
        if not parsed:
            return
        pt, seq, ts, _ssrc, header_len = parsed
        source_ip, source_port = addr
        now = time.time()

        for session in self.sessions.values():
            if source_ip == session.peer_a.ip and source_port == session.peer_a.rtp_port:
                self._update_stats(session.stats_a_to_b, seq, ts, len(packet), now, session.sample_rate)
                if pt == 101:  # default telephone-event dynamic PT
                    event = _extract_telephone_event(packet[header_len:])
                    if event is not None:
                        session.stats_a_to_b.dtmf_events.append(event)
                await self._relay_packet(packet, session.peer_b.ip, session.peer_b.rtp_port)
                if session.record:
                    self._append_recording_chunk(session.recording_path, packet)
                return
            if source_ip == session.peer_b.ip and source_port == session.peer_b.rtp_port:
                self._update_stats(session.stats_b_to_a, seq, ts, len(packet), now, session.sample_rate)
                if pt == 101:
                    event = _extract_telephone_event(packet[header_len:])
                    if event is not None:
                        session.stats_b_to_a.dtmf_events.append(event)
                await self._relay_packet(packet, session.peer_a.ip, session.peer_a.rtp_port)
                if session.record:
                    self._append_recording_chunk(session.recording_path, packet)
                return

    async def _relay_packet(self, packet: bytes, ip: str, port: int):
        loop = asyncio.get_running_loop()
        await loop.sock_sendto(self.udp_socket, packet, (ip, port))

    def _update_stats(
        self,
        stats: RTPStats,
        seq: int,
        ts: int,
        packet_size: int,
        arrival: float,
        sample_rate: int,
    ):
        if stats.last_seq >= 0 and seq > stats.last_seq + 1:
            stats.lost += seq - stats.last_seq - 1
        stats.last_seq = seq
        stats.last_timestamp = ts
        stats.packets += 1
        stats.bytes += packet_size
        _estimate_jitter(stats, ts, arrival, sample_rate)

    async def _rtcp_report_loop(self):
        while True:
            await asyncio.sleep(max(1, int(self.config.rtp.rtcp_interval_seconds)))
            for call_id, session in list(self.sessions.items()):
                LOGGER.info(
                    "rtcp-summary",
                    extra={
                        "extra": {
                            "call_id": call_id,
                            "a_to_b_packets": session.stats_a_to_b.packets,
                            "a_to_b_lost": session.stats_a_to_b.lost,
                            "a_to_b_jitter": round(session.stats_a_to_b.jitter, 3),
                            "b_to_a_packets": session.stats_b_to_a.packets,
                            "b_to_a_lost": session.stats_b_to_a.lost,
                            "b_to_a_jitter": round(session.stats_b_to_a.jitter, 3),
                        }
                    },
                )

    def _append_recording_chunk(self, recording_path: str, packet: bytes):
        # Raw RTP dump storage (for post-processing conversion to WAV/MP3 by offline worker).
        with open(recording_path, "ab") as f:
            f.write(packet)

    def _session_payload(self, session: RTPSession) -> dict[str, Any]:
        return {
            "call_id": session.call_id,
            "codec": session.codec,
            "payload_type": session.payload_type,
            "sample_rate": session.sample_rate,
            "peer_a": {
                "ip": session.peer_a.ip,
                "rtp_port": session.peer_a.rtp_port,
                "rtcp_port": session.peer_a.rtcp_port,
            },
            "peer_b": {
                "ip": session.peer_b.ip,
                "rtp_port": session.peer_b.rtp_port,
                "rtcp_port": session.peer_b.rtcp_port,
            },
            "record": session.record,
            "recording_path": session.recording_path,
            "stats": {
                "a_to_b": {
                    "packets": session.stats_a_to_b.packets,
                    "bytes": session.stats_a_to_b.bytes,
                    "lost": session.stats_a_to_b.lost,
                    "jitter": session.stats_a_to_b.jitter,
                    "dtmf_events": session.stats_a_to_b.dtmf_events[-50:],
                },
                "b_to_a": {
                    "packets": session.stats_b_to_a.packets,
                    "bytes": session.stats_b_to_a.bytes,
                    "lost": session.stats_b_to_a.lost,
                    "jitter": session.stats_b_to_a.jitter,
                    "dtmf_events": session.stats_b_to_a.dtmf_events[-50:],
                },
            },
        }

    async def _handle_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action", "")).lower()

        if action == "ping":
            return {"ok": True, "service": "media-core"}

        if action == "create_session":
            call_id = str(payload.get("call_id", "")).strip()
            if not call_id:
                return {"ok": False, "error": "missing call_id"}
            if call_id in self.sessions:
                return {"ok": True, "session": self._session_payload(self.sessions[call_id])}

            peer_a = payload.get("peer_a", {})
            peer_b = payload.get("peer_b", {})
            if not isinstance(peer_a, dict) or not isinstance(peer_b, dict):
                return {"ok": False, "error": "invalid peers"}

            session = RTPSession(
                call_id=call_id,
                codec=str(payload.get("codec", "PCMU")),
                payload_type=int(payload.get("payload_type", 0)),
                sample_rate=int(payload.get("sample_rate", 8000)),
                peer_a=RTPPeer(
                    ip=str(peer_a.get("ip", "127.0.0.1")),
                    rtp_port=int(peer_a.get("rtp_port", self._allocate_rtp_port())),
                    rtcp_port=int(peer_a.get("rtcp_port", 0)),
                ),
                peer_b=RTPPeer(
                    ip=str(peer_b.get("ip", "127.0.0.1")),
                    rtp_port=int(peer_b.get("rtp_port", self._allocate_rtp_port())),
                    rtcp_port=int(peer_b.get("rtcp_port", 0)),
                ),
                created_at=int(time.time()),
                record=bool(payload.get("record", False)),
            )
            if session.peer_a.rtcp_port == 0:
                session.peer_a.rtcp_port = session.peer_a.rtp_port + 1
            if session.peer_b.rtcp_port == 0:
                session.peer_b.rtcp_port = session.peer_b.rtp_port + 1

            if session.record:
                recording_path = self.recording_dir / f"{call_id}.rtp"
                session.recording_path = str(recording_path)
                recording_path.touch(exist_ok=True)

            self.sessions[call_id] = session
            started_at = int(time.time())
            self.db.execute(
                """
                INSERT INTO rtp_sessions (
                    call_id, caller_ip, caller_port, callee_ip, callee_port, codec, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(call_id) DO UPDATE SET
                    caller_ip = excluded.caller_ip,
                    caller_port = excluded.caller_port,
                    callee_ip = excluded.callee_ip,
                    callee_port = excluded.callee_port,
                    codec = excluded.codec,
                    started_at = excluded.started_at,
                    ended_at = NULL
                """,
                (
                    call_id,
                    session.peer_a.ip,
                    session.peer_a.rtp_port,
                    session.peer_b.ip,
                    session.peer_b.rtp_port,
                    session.codec,
                    started_at,
                ),
            )
            return {"ok": True, "session": self._session_payload(session)}

        if action == "end_session":
            call_id = str(payload.get("call_id", "")).strip()
            session = self.sessions.pop(call_id, None)
            if not session:
                return {"ok": False, "error": "session_not_found"}
            ended_at = int(time.time())
            self.db.execute(
                "UPDATE rtp_sessions SET ended_at = ? WHERE call_id = ?",
                (ended_at, call_id),
            )
            if session.record and session.recording_path:
                duration = max(0, ended_at - session.created_at)
                self.db.add_recording(call_id, session.recording_path, session.codec, duration)
            return {"ok": True}

        if action == "session_stats":
            call_id = str(payload.get("call_id", "")).strip()
            if call_id:
                session = self.sessions.get(call_id)
                if not session:
                    return {"ok": False, "error": "session_not_found"}
                return {"ok": True, "session": self._session_payload(session)}
            return {
                "ok": True,
                "sessions": [self._session_payload(s) for s in self.sessions.values()],
            }

        if action == "list_recordings":
            limit = int(payload.get("limit", 200))
            return {"ok": True, "recordings": self.db.list_recordings(limit)}

        if action == "build_rtcp_report":
            call_id = str(payload.get("call_id", "")).strip()
            session = self.sessions.get(call_id)
            if not session:
                return {"ok": False, "error": "session_not_found"}
            report = {
                "call_id": call_id,
                "rtcp": {
                    "sender_a": {
                        "packets": session.stats_a_to_b.packets,
                        "octets": session.stats_a_to_b.bytes,
                        "jitter": session.stats_a_to_b.jitter,
                        "lost": session.stats_a_to_b.lost,
                    },
                    "sender_b": {
                        "packets": session.stats_b_to_a.packets,
                        "octets": session.stats_b_to_a.bytes,
                        "jitter": session.stats_b_to_a.jitter,
                        "lost": session.stats_b_to_a.lost,
                    },
                },
            }
            return {"ok": True, "report": report}

        if action == "export_state":
            path = str(payload.get("path", "/tmp/smurf_media_state.json"))
            dump = {
                "sessions": [self._session_payload(s) for s in self.sessions.values()],
                "timestamp": int(time.time()),
            }
            Path(path).write_text(json.dumps(dump, indent=2), encoding="utf-8")
            return {"ok": True, "path": path}

        return {"ok": False, "error": f"unknown action: {action}"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SMURF media-core service")
    parser.add_argument("--config", default=None, help="Path to config YAML")
    return parser.parse_args()


def main():
    args = parse_args()
    service = MediaCoreService(config_path=args.config)
    asyncio.run(service.run())


if __name__ == "__main__":
    main()

