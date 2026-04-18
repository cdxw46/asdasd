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
        self.endpoint_index: dict[tuple[str, int], tuple[str, str]] = {}
        self.session_endpoints: dict[str, set[tuple[str, int]]] = {}
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
        self.recording_handles: dict[str, Any] = {}

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
        for handle in list(self.recording_handles.values()):
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass
        self.recording_handles.clear()
        self.session_endpoints.clear()
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
        mapping = self._resolve_packet_mapping(source_ip, source_port)
        if not mapping:
            return
        call_id, direction = mapping
        session = self.sessions.get(call_id)
        if not session:
            self.endpoint_index.pop((source_ip, source_port), None)
            tracked = self.session_endpoints.get(call_id)
            if tracked:
                tracked.discard((source_ip, source_port))
                if not tracked:
                    self.session_endpoints.pop(call_id, None)
            return
        if direction == "a_to_b":
            self._update_stats(session.stats_a_to_b, seq, ts, len(packet), now, session.sample_rate)
            if pt == 101:  # default telephone-event dynamic PT
                event = _extract_telephone_event(packet[header_len:])
                if event is not None:
                    session.stats_a_to_b.dtmf_events.append(event)
            await self._relay_packet(packet, session.peer_b.ip, session.peer_b.rtp_port)
            if session.record:
                self._append_recording_chunk(session.recording_path, packet)
            return
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
        if not recording_path:
            return
        handle = self.recording_handles.get(recording_path)
        if not handle:
            handle = open(recording_path, "ab")
            self.recording_handles[recording_path] = handle
        handle.write(packet)

    def _close_recording_handle(self, recording_path: str) -> None:
        handle = self.recording_handles.pop(recording_path, None)
        if not handle:
            return
        try:
            handle.flush()
            handle.close()
        except Exception:  # noqa: BLE001
            pass

    def _remember_endpoint(self, call_id: str, endpoint: tuple[str, int], direction: str) -> None:
        self.endpoint_index[endpoint] = (call_id, direction)
        self.session_endpoints.setdefault(call_id, set()).add(endpoint)

    def _drop_session_endpoints(self, call_id: str) -> None:
        for endpoint in self.session_endpoints.pop(call_id, set()):
            self.endpoint_index.pop(endpoint, None)

    def _resolve_packet_mapping(
        self,
        source_ip: str,
        source_port: int,
    ) -> tuple[str, str] | None:
        endpoint = (source_ip, source_port)
        mapping = self.endpoint_index.get(endpoint)
        if mapping:
            return mapping

        candidates: list[tuple[str, str, int, int]] = []
        for call_id, session in self.sessions.items():
            if session.peer_a.ip == source_ip:
                candidates.append(
                    (
                        call_id,
                        "a_to_b",
                        session.peer_a.rtp_port,
                        session.stats_a_to_b.packets,
                    )
                )
            if session.peer_b.ip == source_ip:
                candidates.append(
                    (
                        call_id,
                        "b_to_a",
                        session.peer_b.rtp_port,
                        session.stats_b_to_a.packets,
                    )
                )
        if not candidates:
            return None

        exact_port_matches = [candidate for candidate in candidates if candidate[2] == source_port]
        if len(exact_port_matches) == 1:
            call_id, direction, _port, _packets = exact_port_matches[0]
            self._remember_endpoint(call_id, endpoint, direction)
            return (call_id, direction)
        if len(exact_port_matches) > 1:
            # Multiple calls advertising exactly same endpoint is ambiguous.
            return None

        call_ids = {candidate[0] for candidate in candidates}
        if len(call_ids) > 1:
            return None

        min_packets = min(candidate[3] for candidate in candidates)
        least_used = [candidate for candidate in candidates if candidate[3] == min_packets]
        chosen = sorted(least_used, key=lambda item: (item[1], item[2]))[0]
        call_id, direction, _port, _packets = chosen
        self._remember_endpoint(call_id, endpoint, direction)
        LOGGER.info(
            "learned-symmetric-rtp-endpoint",
            extra={
                "extra": {
                    "call_id": call_id,
                    "direction": direction,
                    "source_ip": source_ip,
                    "source_port": source_port,
                }
            },
        )
        return (call_id, direction)

    def _parse_peer(self, payload: dict[str, Any], key: str) -> RTPPeer:
        peer = payload.get(key, {})
        if not isinstance(peer, dict):
            raise ValueError(f"{key} must be an object")
        ip = str(peer.get("ip", "")).strip()
        if not ip:
            raise ValueError(f"{key}.ip is required")
        rtp_port_raw = str(peer.get("rtp_port", "")).strip()
        if not rtp_port_raw.isdigit():
            raise ValueError(f"{key}.rtp_port must be numeric")
        rtp_port = int(rtp_port_raw)
        if rtp_port <= 0 or rtp_port > 65535:
            raise ValueError(f"{key}.rtp_port out of range")
        rtcp_port_raw = str(peer.get("rtcp_port", "")).strip()
        if rtcp_port_raw and not rtcp_port_raw.isdigit():
            raise ValueError(f"{key}.rtcp_port must be numeric")
        rtcp_port = int(rtcp_port_raw) if rtcp_port_raw else rtp_port + 1
        if rtcp_port <= 0 or rtcp_port > 65535:
            raise ValueError(f"{key}.rtcp_port out of range")
        return RTPPeer(ip=ip, rtp_port=rtp_port, rtcp_port=rtcp_port)

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
            try:
                peer_a = self._parse_peer(payload, "peer_a")
                peer_b = self._parse_peer(payload, "peer_b")
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}

            session = RTPSession(
                call_id=call_id,
                codec=str(payload.get("codec", "PCMU")),
                payload_type=int(payload.get("payload_type", 0)),
                sample_rate=int(payload.get("sample_rate", 8000)),
                peer_a=peer_a,
                peer_b=peer_b,
                created_at=int(time.time()),
                record=bool(payload.get("record", False)),
            )

            if session.record:
                recording_path = self.recording_dir / f"{call_id}.rtp"
                session.recording_path = str(recording_path)
                recording_path.touch(exist_ok=True)

            self.sessions[call_id] = session
            self._remember_endpoint(
                call_id, (session.peer_a.ip, session.peer_a.rtp_port), "a_to_b"
            )
            self._remember_endpoint(
                call_id, (session.peer_b.ip, session.peer_b.rtp_port), "b_to_a"
            )
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
            self._drop_session_endpoints(call_id)
            ended_at = int(time.time())
            self.db.execute(
                "UPDATE rtp_sessions SET ended_at = ? WHERE call_id = ?",
                (ended_at, call_id),
            )
            if session.record and session.recording_path:
                self._close_recording_handle(session.recording_path)
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
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            dump = {
                "sessions": [self._session_payload(s) for s in self.sessions.values()],
                "timestamp": int(time.time()),
            }
            target.write_text(json.dumps(dump, indent=2), encoding="utf-8")
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

