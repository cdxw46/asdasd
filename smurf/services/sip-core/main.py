"""SMURF SIP core service.

SIP transport service with:
- SIP over UDP/TCP/TLS
- SIP over WebSocket ("sip" subprotocol)
- REGISTER (digest auth MD5/SHA-256)
- INVITE/ACK/BYE forwarding for internal endpoints
- OPTIONS/INFO/UPDATE/REFER/SUBSCRIBE/NOTIFY baseline handling
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import ssl
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Callable
import sys
from pathlib import Path

import websockets
from websockets.server import WebSocketServerProtocol

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.bus import JsonCommandClient, JsonCommandServer
from core.config import load_config
from core.db import Database
from core.logging_utils import configure_json_logging, get_logger
from core.sip import (
    SIPMessage,
    build_response,
    digest_response as sip_digest_response,
    parse_aor,
    parse_auth_header,
    parse_contact_uri,
    parse_sip_message,
)

LOGGER = get_logger("sip-core")


def _first_header_values(headers: dict[str, list[str]]) -> dict[str, str]:
    compact: dict[str, str] = {}
    for key, values in headers.items():
        if values:
            compact[key] = values[0]
    return compact


def _header_to_extension(value: str | None) -> str:
    if not value:
        return ""
    source = value
    if "<" in source and ">" in source:
        source = source[source.index("<") + 1 : source.index(">")]
    aor = parse_aor(source)
    return aor.split("@", 1)[0]


@dataclass(slots=True)
class EndpointTransport:
    send_sip: Callable[[SIPMessage], Any]
    protocol: str
    peer_ip: str
    peer_port: int


class NonceStore:
    def __init__(self, ttl_seconds: int):
        self.ttl_seconds = ttl_seconds
        self.nonces: dict[str, int] = {}

    def create_nonce(self) -> str:
        nonce = str(int(time.time() * 1000))
        self.nonces[nonce] = int(time.time()) + self.ttl_seconds
        return nonce

    def is_valid(self, nonce: str) -> bool:
        now = int(time.time())
        return self.nonces.get(nonce, 0) >= now

    def cleanup(self) -> None:
        now = int(time.time())
        stale = [nonce for nonce, exp in self.nonces.items() if exp < now]
        for nonce in stale:
            self.nonces.pop(nonce, None)


class RateLimiter:
    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window_seconds = window_seconds
        self.history: dict[str, deque[int]] = defaultdict(deque)

    def allow(self, ip: str) -> bool:
        now_ms = int(time.time() * 1000)
        oldest = now_ms - (self.window_seconds * 1000)
        bucket = self.history[ip]
        while bucket and bucket[0] < oldest:
            bucket.popleft()
        if len(bucket) >= self.limit:
            return False
        bucket.append(now_ms)
        return True


class SIPCoreService:
    def __init__(self, config_path: str | None):
        self.config = load_config(config_path)
        configure_json_logging("sip-core", self.config.global_.log_level)
        self.db = Database(self.config.database.sqlite_path)
        self.nonces = NonceStore(ttl_seconds=300)
        self.rate_limiter = RateLimiter(
            limit=self.config.security.sip_rate_limit_per_ip,
            window_seconds=self.config.security.sip_rate_window_seconds,
        )
        self.pbx_client = JsonCommandClient(
            self.config.bus.pbx_command_host,
            self.config.bus.pbx_command_port,
            timeout=3.0,
        )
        self.command_server = JsonCommandServer(
            host=self.config.bus.sip_command_host,
            port=self.config.bus.sip_command_port,
            handler=self._handle_command,
        )
        self.endpoint_transports: dict[str, EndpointTransport] = {}
        self.udp_transport: asyncio.DatagramTransport | None = None
        self.shutdown_event = asyncio.Event()
        self.tasks: list[asyncio.Task] = []

    async def run(self):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.shutdown_event.set)
            except NotImplementedError:
                pass

        await self.command_server.start()

        udp_transport, _ = await loop.create_datagram_endpoint(
            lambda: SIPUDPProtocol(self),
            local_addr=(self.config.sip.udp_host, self.config.sip.udp_port),
        )
        self.udp_transport = udp_transport

        tcp_server = await asyncio.start_server(
            self._handle_tcp_client,
            self.config.sip.tcp_host,
            self.config.sip.tcp_port,
        )

        tls_server = None
        tls_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        try:
            tls_context.load_cert_chain(
                self.config.sip.tls_cert_path, self.config.sip.tls_key_path
            )
            tls_server = await asyncio.start_server(
                self._handle_tls_client,
                self.config.sip.tls_host,
                self.config.sip.tls_port,
                ssl=tls_context,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "TLS SIP transport disabled",
                extra={"extra": {"error": str(exc)}},
            )

        ws_server = await websockets.serve(
            self._handle_ws_client,
            self.config.sip.ws_host,
            self.config.sip.ws_port,
            subprotocols=["sip"],
            ping_interval=20,
            ping_timeout=20,
        )

        self.tasks.append(asyncio.create_task(self._maintenance_loop()))

        LOGGER.info(
            "SIP core started",
            extra={
                "extra": {
                    "udp": self.config.sip.udp_port,
                    "tcp": self.config.sip.tcp_port,
                    "tls": self.config.sip.tls_port if tls_server else None,
                    "ws": self.config.sip.ws_port,
                    "cmd": self.config.bus.sip_command_port,
                }
            },
        )

        await self.shutdown_event.wait()

        for task in self.tasks:
            task.cancel()
        ws_server.close()
        await ws_server.wait_closed()
        if tls_server:
            tls_server.close()
            await tls_server.wait_closed()
        tcp_server.close()
        await tcp_server.wait_closed()
        udp_transport.close()
        await self.command_server.stop()

    async def _maintenance_loop(self):
        while True:
            await asyncio.sleep(5)
            self.nonces.cleanup()
            self.db.purge_expired_registrations()

    async def _handle_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action", "")).lower()
        if action == "ping":
            return {"ok": True, "service": "sip-core"}
        if action == "list_registrations":
            return {"ok": True, "items": self.db.active_registrations()}
        if action == "send_to_extension":
            extension = str(payload.get("extension", ""))
            sip_message = payload.get("sip_message", "")
            if not extension or not sip_message:
                return {"ok": False, "error": "missing extension or sip_message"}
            target = self.endpoint_transports.get(extension)
            if not target:
                return {"ok": False, "error": "extension_not_connected"}
            try:
                if isinstance(sip_message, bytes):
                    parsed = parse_sip_message(sip_message)
                else:
                    parsed = parse_sip_message(str(sip_message).encode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"invalid_sip_message: {exc}"}
            target.send_sip(parsed)
            return {"ok": True}
        return {"ok": False, "error": f"unknown action: {action}"}

    def _remember_transport(self, extension: str, transport: EndpointTransport):
        self.endpoint_transports[extension] = transport

    def _build_www_authenticate(self, algorithm: str) -> str:
        nonce = self.nonces.create_nonce()
        realm = self.config.security.sip_realm
        return (
            f'Digest realm="{realm}", nonce="{nonce}", algorithm={algorithm}, '
            'qop="auth"'
        )

    def _validate_register_auth(self, msg: SIPMessage) -> tuple[bool, str]:
        auth = msg.get("Authorization")
        if not auth:
            return False, "missing_authorization"

        attrs = parse_auth_header(auth)
        username = attrs.get("username", "")
        nonce = attrs.get("nonce", "")
        uri = attrs.get("uri", "")
        response = attrs.get("response", "")
        algorithm = attrs.get("algorithm", "MD5").upper()
        cnonce = attrs.get("cnonce", "")
        nc = attrs.get("nc", "")
        qop = attrs.get("qop", "auth")

        if not username or not nonce or not uri or not response:
            return False, "invalid_authorization_header"
        if not self.nonces.is_valid(nonce):
            return False, "stale_nonce"

        ext = self.db.get_extension_by_auth(username)
        if not ext:
            return False, "unknown_user"

        expected = sip_digest_response(
            username=username,
            realm=self.config.security.sip_realm,
            password=ext["auth_password"],
            method="REGISTER",
            uri=uri,
            nonce=nonce,
            nc=nc,
            cnonce=cnonce,
            qop=qop,
            algorithm=algorithm,
        )
        if expected != response:
            return False, "digest_mismatch"
        return True, ext["extension"]

    async def _handle_sip_message(
        self,
        raw_data: bytes,
        protocol: str,
        peer_ip: str,
        peer_port: int,
        send_sip: Callable[[SIPMessage], Any],
    ):
        if self.db.is_blocked_ip(peer_ip):
            return
        if not self.rate_limiter.allow(peer_ip):
            self.db.add_security_block(
                peer_ip,
                "sip_rate_limit",
                int(time.time()) + self.config.security.block_duration_seconds,
            )
            return

        try:
            msg = parse_sip_message(raw_data)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "Failed to parse SIP message",
                extra={"extra": {"peer_ip": peer_ip, "error": str(exc)}},
            )
            return

        if not msg.is_request:
            LOGGER.info(
                "Incoming SIP response",
                extra={
                    "extra": {
                        "status_code": msg.status_code,
                        "peer_ip": peer_ip,
                        "protocol": protocol,
                    }
                },
            )
            return

        method = msg.method or ""
        if method == "REGISTER":
            await self._handle_register(msg, protocol, peer_ip, peer_port, send_sip)
            return
        if method == "INVITE":
            await self._handle_invite(msg, protocol, peer_ip, peer_port, send_sip)
            return
        if method == "ACK":
            await self._handle_ack(msg)
            return
        if method == "BYE":
            await self._handle_bye(msg, send_sip)
            return
        if method == "MESSAGE":
            await self._handle_message(msg, send_sip)
            return
        if method in {"OPTIONS", "INFO", "UPDATE", "REFER", "NOTIFY", "SUBSCRIBE"}:
            send_sip(build_response(msg, 200, "OK"))
            return

        send_sip(build_response(msg, 405, "Method Not Allowed"))

    async def _handle_register(
        self,
        msg: SIPMessage,
        protocol: str,
        peer_ip: str,
        peer_port: int,
        send_sip: Callable[[SIPMessage], Any],
    ):
        valid, detail = self._validate_register_auth(msg)
        if not valid:
            send_sip(
                build_response(
                    msg,
                    401,
                    "Unauthorized",
                    extra_headers={
                        "WWW-Authenticate": self._build_www_authenticate("MD5")
                    },
                )
            )
            if detail == "digest_mismatch":
                self.db.add_security_block(
                    peer_ip,
                    "digest_fail",
                    int(time.time()) + self.config.security.block_duration_seconds,
                )
            return

        extension = detail
        expires = self.config.sip.registration_max_expires
        expires_header = msg.get("Expires")
        if expires_header and expires_header.isdigit():
            expires = int(expires_header)
        expires = max(
            self.config.sip.registration_min_expires,
            min(self.config.sip.registration_max_expires, expires),
        )

        contact = parse_contact_uri(msg.get("Contact")) or ""
        user_agent = msg.get("User-Agent", "")
        expires_at = int(time.time()) + expires
        self.db.upsert_registration(
            extension=extension,
            contact_uri=contact,
            source_ip=peer_ip,
            source_port=peer_port,
            transport=protocol,
            user_agent=user_agent,
            expires_at=expires_at,
        )
        self._remember_transport(
            extension,
            EndpointTransport(
                send_sip=send_sip,
                protocol=protocol,
                peer_ip=peer_ip,
                peer_port=peer_port,
            ),
        )

        send_sip(build_response(msg, 200, "OK", extra_headers={"Expires": str(expires)}))

    async def _handle_invite(
        self,
        msg: SIPMessage,
        protocol: str,
        peer_ip: str,
        peer_port: int,
        send_sip: Callable[[SIPMessage], Any],
    ):
        from_ext = _header_to_extension(msg.get("From"))
        to_ext = _header_to_extension(msg.get("To"))
        if not from_ext or not to_ext:
            send_sip(build_response(msg, 400, "Bad Request"))
            return

        pbx_result = await self.pbx_client.request(
            {
                "action": "route_call",
                "call_id": msg.get("Call-Id", ""),
                "from_ext": from_ext,
                "to_ext": to_ext,
                "headers": _first_header_values(msg.headers),
                "sdp": msg.body,
                "source": {
                    "ip": peer_ip,
                    "port": peer_port,
                    "protocol": protocol,
                },
            }
        )
        if pbx_result.get("status") != "ok":
            send_sip(build_response(msg, 404, "Not Found"))
            return

        target_ext = str(pbx_result.get("target_extension", to_ext))
        target_transport = self.endpoint_transports.get(target_ext)
        if not target_transport:
            send_sip(build_response(msg, 480, "Temporarily Unavailable"))
            return

        forwarded = SIPMessage(
            method="INVITE",
            request_uri=msg.request_uri,
            version=msg.version,
            body=msg.body,
        )
        for header, values in msg.headers.items():
            for value in values:
                forwarded.add_header(header, value)

        target_transport.send_sip(forwarded)
        send_sip(build_response(msg, 100, "Trying"))
        send_sip(build_response(msg, 180, "Ringing"))

    async def _handle_ack(self, msg: SIPMessage):
        call_id = msg.get("Call-Id", "")
        if call_id:
            await self.pbx_client.request({"action": "ack_call", "call_id": call_id})

    async def _handle_bye(
        self,
        msg: SIPMessage,
        send_sip: Callable[[SIPMessage], Any],
    ):
        call_id = msg.get("Call-Id", "")
        await self.pbx_client.request(
            {"action": "end_call", "call_id": call_id, "reason": "normal_clear"}
        )
        send_sip(build_response(msg, 200, "OK"))

    async def _handle_message(
        self,
        msg: SIPMessage,
        send_sip: Callable[[SIPMessage], Any],
    ):
        from_ext = _header_to_extension(msg.get("From"))
        to_ext = _header_to_extension(msg.get("To"))
        if not from_ext or not to_ext:
            send_sip(build_response(msg, 400, "Bad Request"))
            return

        await self.pbx_client.request(
            {
                "action": "chat_send",
                "from_ext": from_ext,
                "to_ext": to_ext,
                "message": msg.body,
            }
        )
        send_sip(build_response(msg, 200, "OK"))

    async def _handle_tcp_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ):
        peer = writer.get_extra_info("peername") or ("0.0.0.0", 0)
        peer_ip, peer_port = str(peer[0]), int(peer[1])

        def send_sip(msg: SIPMessage):
            writer.write(msg.to_bytes())

        try:
            while not reader.at_eof():
                data = await reader.read(65535)
                if not data:
                    break
                await self._handle_sip_message(
                    data, "TCP", peer_ip, peer_port, send_sip
                )
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def _handle_tls_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ):
        peer = writer.get_extra_info("peername") or ("0.0.0.0", 0)
        peer_ip, peer_port = str(peer[0]), int(peer[1])

        def send_sip(msg: SIPMessage):
            writer.write(msg.to_bytes())

        try:
            while not reader.at_eof():
                data = await reader.read(65535)
                if not data:
                    break
                await self._handle_sip_message(
                    data, "TLS", peer_ip, peer_port, send_sip
                )
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def _handle_ws_client(self, websocket: WebSocketServerProtocol):
        peer = websocket.remote_address or ("0.0.0.0", 0)
        peer_ip, peer_port = str(peer[0]), int(peer[1])

        async def send_ws(msg: SIPMessage):
            await websocket.send(msg.to_bytes().decode("utf-8", errors="replace"))

        def send_sip(msg: SIPMessage):
            asyncio.create_task(send_ws(msg))

        try:
            async for message in websocket:
                data = message.encode("utf-8") if isinstance(message, str) else message
                await self._handle_sip_message(
                    data,
                    "WS",
                    peer_ip,
                    peer_port,
                    send_sip,
                )
        except websockets.ConnectionClosed:
            pass


class SIPUDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, service: SIPCoreService):
        self.service = service
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        ip, port = str(addr[0]), int(addr[1])

        def send_sip(msg: SIPMessage):
            if self.transport:
                self.transport.sendto(msg.to_bytes(), addr)

        asyncio.create_task(
            self.service._handle_sip_message(data, "UDP", ip, port, send_sip)
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SMURF SIP core service")
    parser.add_argument("--config", default=None, help="Path to config YAML")
    return parser.parse_args()


def main():
    args = parse_args()
    service = SIPCoreService(config_path=args.config)
    asyncio.run(service.run())


if __name__ == "__main__":
    main()

