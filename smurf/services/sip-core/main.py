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
import secrets
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
    MAX_CONTENT_LENGTH,
    SIPMessage,
    build_response,
    digest_response as sip_digest_response,
    parse_aor,
    parse_auth_header,
    parse_contact_uri,
    parse_uri_header,
    parse_sip_message,
    secure_compare_digest,
    split_sip_messages,
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


@dataclass(slots=True)
class TransactionState:
    response: SIPMessage
    expires_at: float


@dataclass(slots=True)
class DialogState:
    call_id: str
    caller_extension: str
    callee_extension: str
    created_at: float


class NonceStore:
    def __init__(self, ttl_seconds: int):
        self.ttl_seconds = ttl_seconds
        self.nonces: dict[str, int] = {}

    def create_nonce(self) -> str:
        nonce = secrets.token_hex(16)
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
        self._sender_extensions: dict[int, set[str]] = defaultdict(set)
        self.server_transactions: dict[str, TransactionState] = {}
        self.dialogs: dict[str, DialogState] = {}
        self.failed_auth_attempts: dict[str, deque[int]] = defaultdict(deque)
        self.udp_transport: asyncio.DatagramTransport | None = None
        self.shutdown_event = asyncio.Event()
        self.tasks: list[asyncio.Task] = []
        self.supported_methods = {
            "REGISTER",
            "INVITE",
            "ACK",
            "BYE",
            "CANCEL",
            "MESSAGE",
            "OPTIONS",
            "INFO",
            "UPDATE",
            "REFER",
            "NOTIFY",
            "SUBSCRIBE",
        }

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
            self._cleanup_runtime_state()

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
        previous = self.endpoint_transports.get(extension)
        if previous:
            previous_sender = id(previous.send_sip)
            sender_extensions = self._sender_extensions.get(previous_sender)
            if sender_extensions and extension in sender_extensions:
                sender_extensions.discard(extension)
                if not sender_extensions:
                    self._sender_extensions.pop(previous_sender, None)
        self.endpoint_transports[extension] = transport
        self._sender_extensions[id(transport.send_sip)].add(extension)

    def _drop_sender_bindings(self, send_sip: Callable[[SIPMessage], Any]):
        sender_id = id(send_sip)
        self._sender_extensions.pop(sender_id, None)
        stale_extensions = [
            ext
            for ext, transport in self.endpoint_transports.items()
            if id(transport.send_sip) == sender_id
        ]
        for extension in stale_extensions:
            self.endpoint_transports.pop(extension, None)

    def _is_sender_allowed_for_extension(
        self,
        send_sip: Callable[[SIPMessage], Any],
        extension: str,
        peer_ip: str,
        peer_port: int,
    ) -> bool:
        sender_id = id(send_sip)
        if extension in self._sender_extensions.get(sender_id, set()):
            return True
        for reg in self.db.active_registrations():
            if (
                str(reg.get("extension", "")) == extension
                and str(reg.get("source_ip", "")) == peer_ip
                and int(reg.get("source_port", 0)) == peer_port
            ):
                return True
        return False

    def _transaction_key(self, msg: SIPMessage) -> str:
        via = msg.get("Via", "")
        params = parse_uri_header(via)
        branch = params.get("branch", "")
        cseq = msg.get("Cseq", "")
        call_id = msg.get("Call-Id", "")
        return f"{call_id}|{cseq}|{branch}"

    def _cache_transaction_response(
        self,
        msg: SIPMessage,
        response: SIPMessage,
        ttl_seconds: int = 32,
    ) -> None:
        if msg.method == "ACK":
            return
        key = self._transaction_key(msg)
        if not key:
            return
        self.server_transactions[key] = TransactionState(
            response=response,
            expires_at=time.time() + max(1, ttl_seconds),
        )

    def _cached_transaction_response(self, msg: SIPMessage) -> SIPMessage | None:
        key = self._transaction_key(msg)
        if not key:
            return None
        state = self.server_transactions.get(key)
        if not state:
            return None
        if state.expires_at < time.time():
            self.server_transactions.pop(key, None)
            return None
        return state.response

    def _cleanup_runtime_state(self) -> None:
        now = time.time()
        stale_tx = [
            key
            for key, tx in self.server_transactions.items()
            if tx.expires_at <= now
        ]
        for key in stale_tx:
            self.server_transactions.pop(key, None)

        stale_dialogs = [
            call_id
            for call_id, dialog in self.dialogs.items()
            if now - dialog.created_at > 8 * 3600
        ]
        for call_id in stale_dialogs:
            self.dialogs.pop(call_id, None)

        window = int(self.config.security.failed_auth_window_seconds)
        threshold_ms = int(time.time() * 1000) - (window * 1000)
        for ip, bucket in list(self.failed_auth_attempts.items()):
            while bucket and bucket[0] < threshold_ms:
                bucket.popleft()
            if not bucket:
                self.failed_auth_attempts.pop(ip, None)

    def _record_failed_auth(self, peer_ip: str) -> None:
        now_ms = int(time.time() * 1000)
        window = int(self.config.security.failed_auth_window_seconds)
        threshold = int(self.config.security.failed_auth_block_threshold)
        bucket = self.failed_auth_attempts[peer_ip]
        oldest = now_ms - (window * 1000)
        while bucket and bucket[0] < oldest:
            bucket.popleft()
        bucket.append(now_ms)
        if len(bucket) >= threshold:
            self.db.add_security_block(
                peer_ip,
                "digest_fail_threshold",
                int(time.time()) + self.config.security.block_duration_seconds,
            )

    def _clear_failed_auth(self, peer_ip: str) -> None:
        self.failed_auth_attempts.pop(peer_ip, None)

    def _register_expires(self, msg: SIPMessage) -> int:
        expires = self.config.sip.registration_max_expires
        expires_header = (msg.get("Expires") or "").strip()
        if expires_header.isdigit():
            expires = int(expires_header)

        contact = msg.get("Contact")
        if contact:
            contact_params = parse_uri_header(contact)
            contact_expires = str(contact_params.get("expires", "")).strip()
            if contact_expires.isdigit():
                expires = int(contact_expires)

        if expires == 0:
            return 0
        return max(
            self.config.sip.registration_min_expires,
            min(self.config.sip.registration_max_expires, expires),
        )

    def _send_response(
        self,
        request: SIPMessage,
        response: SIPMessage,
        send_sip: Callable[[SIPMessage], Any],
        cache_final: bool = True,
    ) -> None:
        send_sip(response)
        if (
            cache_final
            and response.status_code is not None
            and response.status_code >= 200
            and request.is_request
        ):
            self._cache_transaction_response(request, response)

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
        if not secure_compare_digest(expected, response):
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

        if msg.is_request:
            cached = self._cached_transaction_response(msg)
            if cached:
                send_sip(cached)
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
        if method not in self.supported_methods:
            response = build_response(msg, 405, "Method Not Allowed")
            self._send_response(msg, response, send_sip)
            return

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
        if method == "CANCEL":
            await self._handle_cancel(msg, send_sip)
            return
        if method == "MESSAGE":
            await self._handle_message(msg, peer_ip, peer_port, send_sip)
            return
        if method in {"OPTIONS", "INFO", "UPDATE", "REFER", "NOTIFY", "SUBSCRIBE"}:
            response = build_response(msg, 200, "OK")
            self._send_response(msg, response, send_sip)
            return

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
            response = build_response(
                msg,
                401,
                "Unauthorized",
                extra_headers={"WWW-Authenticate": self._build_www_authenticate("MD5")},
            )
            self._send_response(msg, response, send_sip)
            self._record_failed_auth(peer_ip)
            return

        extension = detail
        expires = self._register_expires(msg)
        if expires == 0:
            self.db.remove_registration(extension, peer_ip, peer_port)
            response = build_response(msg, 200, "OK", extra_headers={"Expires": "0"})
            self._send_response(msg, response, send_sip)
            self._clear_failed_auth(peer_ip)
            return

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

        response = build_response(msg, 200, "OK", extra_headers={"Expires": str(expires)})
        self._send_response(msg, response, send_sip)
        self._clear_failed_auth(peer_ip)

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
        call_id = msg.get("Call-Id", "") or ""
        if not from_ext or not to_ext or not call_id:
            response = build_response(msg, 400, "Bad Request")
            self._send_response(msg, response, send_sip)
            return
        if not self._is_sender_allowed_for_extension(
            send_sip, from_ext, peer_ip, peer_port
        ):
            response = build_response(msg, 403, "Forbidden")
            self._send_response(msg, response, send_sip)
            return

        trying = build_response(msg, 100, "Trying")
        self._send_response(msg, trying, send_sip, cache_final=False)

        pbx_result = await self.pbx_client.request(
            {
                "action": "route_call",
                "call_id": call_id,
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
            response = build_response(msg, 404, "Not Found")
            self._send_response(msg, response, send_sip)
            return

        target_ext = str(pbx_result.get("target_extension", to_ext))
        target_transport = self.endpoint_transports.get(target_ext)
        if not target_transport:
            response = build_response(msg, 480, "Temporarily Unavailable")
            self._send_response(msg, response, send_sip)
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
        ringing = build_response(msg, 180, "Ringing")
        self._send_response(msg, ringing, send_sip, cache_final=False)
        self.dialogs[call_id] = DialogState(
            call_id=call_id,
            caller_extension=from_ext,
            callee_extension=target_ext,
            created_at=time.time(),
        )

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
        self.dialogs.pop(call_id, None)
        response = build_response(msg, 200, "OK")
        self._send_response(msg, response, send_sip)

    async def _handle_cancel(
        self,
        msg: SIPMessage,
        send_sip: Callable[[SIPMessage], Any],
    ):
        call_id = msg.get("Call-Id", "")
        if call_id:
            await self.pbx_client.request(
                {"action": "end_call", "call_id": call_id, "reason": "cancelled"}
            )
            self.dialogs.pop(call_id, None)
        response = build_response(msg, 200, "OK")
        self._send_response(msg, response, send_sip)

    async def _handle_message(
        self,
        msg: SIPMessage,
        peer_ip: str,
        peer_port: int,
        send_sip: Callable[[SIPMessage], Any],
    ):
        from_ext = _header_to_extension(msg.get("From"))
        to_ext = _header_to_extension(msg.get("To"))
        if not from_ext or not to_ext:
            response = build_response(msg, 400, "Bad Request")
            self._send_response(msg, response, send_sip)
            return
        if not self._is_sender_allowed_for_extension(
            send_sip, from_ext, peer_ip, peer_port
        ):
            response = build_response(msg, 403, "Forbidden")
            self._send_response(msg, response, send_sip)
            return

        await self.pbx_client.request(
            {
                "action": "chat_send",
                "from_ext": from_ext,
                "to_ext": to_ext,
                "message": msg.body,
            }
        )
        response = build_response(msg, 200, "OK")
        self._send_response(msg, response, send_sip)

    async def _handle_tcp_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ):
        peer = writer.get_extra_info("peername") or ("0.0.0.0", 0)
        peer_ip, peer_port = str(peer[0]), int(peer[1])

        def send_sip(msg: SIPMessage):
            writer.write(msg.to_bytes())

        buffer = b""
        try:
            while not reader.at_eof():
                data = await reader.read(65535)
                if not data:
                    break
                buffer += data
                if len(buffer) > MAX_CONTENT_LENGTH * 2:
                    LOGGER.warning(
                        "TCP SIP buffer exceeded max size",
                        extra={"extra": {"peer_ip": peer_ip, "peer_port": peer_port}},
                    )
                    break
                messages, buffer = split_sip_messages(buffer)
                for message in messages:
                    await self._handle_sip_message(
                        message, "TCP", peer_ip, peer_port, send_sip
                    )
                    await writer.drain()
        finally:
            self._drop_sender_bindings(send_sip)
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

        buffer = b""
        try:
            while not reader.at_eof():
                data = await reader.read(65535)
                if not data:
                    break
                buffer += data
                if len(buffer) > MAX_CONTENT_LENGTH * 2:
                    LOGGER.warning(
                        "TLS SIP buffer exceeded max size",
                        extra={"extra": {"peer_ip": peer_ip, "peer_port": peer_port}},
                    )
                    break
                messages, buffer = split_sip_messages(buffer)
                for message in messages:
                    await self._handle_sip_message(
                        message, "TLS", peer_ip, peer_port, send_sip
                    )
                    await writer.drain()
        finally:
            self._drop_sender_bindings(send_sip)
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
        finally:
            self._drop_sender_bindings(send_sip)


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

