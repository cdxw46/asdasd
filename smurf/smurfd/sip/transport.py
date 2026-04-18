"""Capa de transporte SIP (RFC 3261 §18, RFC 7118 para WebSocket).

Implementa:
    * Transport UDP asíncrono (datagram protocol).
    * Transport TCP asíncrono con framing por Content-Length.
    * Transport TLS (reutiliza TCP con SSLContext).
    * Transport WS / WSS para SIP-over-WebSocket (subprotocolo "sip", RFC 7118).
    * Resolución por símbolo: cada transport entrega los mensajes a un único
      "router" (callback async) y permite enviar mensajes a un destino concreto.

Cada `Endpoint` representa una dirección de red identificada por
(transport_name, host, port). El router recibe siempre un objeto Endpoint
asociado al peer remoto (para poder responder por el mismo flujo).
"""
from __future__ import annotations

import asyncio
import os
import ssl
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, Optional, Tuple

from ..util.logger import get_logger
from .message import SipMessage

log = get_logger("sip.transport")


@dataclass(frozen=True)
class Endpoint:
    transport: str  # 'udp', 'tcp', 'tls', 'ws', 'wss'
    host: str
    port: int

    def __str__(self) -> str:
        return f"{self.transport}:{self.host}:{self.port}"


Router = Callable[[SipMessage, Endpoint, "Transport"], Awaitable[None]]


class Transport:
    """Interfaz base para todos los transports."""
    name: str = "base"

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send(self, data: bytes, dest: Endpoint) -> None: ...

    @property
    def local_host(self) -> str: return "0.0.0.0"

    @property
    def local_port(self) -> int: return 0


# ============================== UDP ==============================

class _UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, owner: "UdpTransport"):
        self.owner = owner
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore

    def datagram_received(self, data: bytes, addr) -> None:
        if not data.strip():
            return
        host, port = addr[0], addr[1]
        ep = Endpoint("udp", host, port)
        try:
            msg = SipMessage.parse(data)
        except Exception as exc:
            log.warning("UDP: mensaje malformado de %s:%d: %s", host, port, exc)
            return
        asyncio.create_task(self.owner._dispatch(msg, ep))

    def error_received(self, exc: Exception) -> None:
        log.warning("UDP error: %s", exc)


class UdpTransport(Transport):
    name = "udp"

    def __init__(self, host: str, port: int, router: Router):
        self.host = host
        self.port = port
        self.router = router
        self._proto: Optional[_UdpProtocol] = None
        self._sock_addr: Tuple[str, int] = (host, port)

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        transport, proto = await loop.create_datagram_endpoint(
            lambda: _UdpProtocol(self),
            local_addr=(self.host, self.port),
            allow_broadcast=False,
        )
        self._proto = proto
        sock = transport.get_extra_info("sockname")
        self._sock_addr = (sock[0], sock[1])
        log.info("SIP UDP escuchando en %s:%d", *self._sock_addr)

    async def stop(self) -> None:
        if self._proto and self._proto.transport:
            self._proto.transport.close()

    async def send(self, data: bytes, dest: Endpoint) -> None:
        if not self._proto or not self._proto.transport:
            raise RuntimeError("UDP transport no iniciado")
        self._proto.transport.sendto(data, (dest.host, dest.port))

    async def _dispatch(self, msg: SipMessage, ep: Endpoint) -> None:
        try:
            await self.router(msg, ep, self)
        except Exception:
            log.exception("Error en router SIP UDP")

    @property
    def local_host(self) -> str: return self._sock_addr[0]

    @property
    def local_port(self) -> int: return self._sock_addr[1]


# ============================== TCP / TLS ==============================

class TcpTransport(Transport):
    name = "tcp"

    def __init__(self, host: str, port: int, router: Router,
                 ssl_ctx: Optional[ssl.SSLContext] = None):
        self.host = host
        self.port = port
        self.router = router
        self.ssl_ctx = ssl_ctx
        if ssl_ctx is not None:
            self.name = "tls"
        self._server: Optional[asyncio.AbstractServer] = None
        self._conns: Dict[Endpoint, asyncio.StreamWriter] = {}
        self._sock_addr: Tuple[str, int] = (host, port)

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_conn, host=self.host, port=self.port, ssl=self.ssl_ctx,
        )
        sock = self._server.sockets[0].getsockname()  # type: ignore
        self._sock_addr = (sock[0], sock[1])
        log.info("SIP %s escuchando en %s:%d", self.name.upper(), *self._sock_addr)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def send(self, data: bytes, dest: Endpoint) -> None:
        w = self._conns.get(dest)
        if w is None or w.is_closing():
            try:
                r, w = await asyncio.open_connection(dest.host, dest.port,
                                                     ssl=self.ssl_ctx)
            except Exception as exc:
                raise RuntimeError(f"No se pudo conectar a {dest}: {exc}") from exc
            self._conns[dest] = w
            asyncio.create_task(self._read_loop(r, w, dest))
        w.write(data)
        try:
            await w.drain()
        except Exception:
            self._conns.pop(dest, None)

    async def _handle_conn(self, reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        ep = Endpoint(self.name, peer[0], peer[1])
        self._conns[ep] = writer
        try:
            await self._read_loop(reader, writer, ep)
        finally:
            self._conns.pop(ep, None)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _read_loop(self, reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter, ep: Endpoint) -> None:
        buf = b""
        while True:
            try:
                chunk = await reader.read(4096)
            except Exception:
                return
            if not chunk:
                return
            buf += chunk
            while True:
                sep = buf.find(b"\r\n\r\n")
                if sep == -1:
                    sep2 = buf.find(b"\n\n")
                    if sep2 == -1:
                        break
                    head_end = sep2 + 2
                else:
                    head_end = sep + 4
                head = buf[:head_end]
                cl = 0
                for line in head.replace(b"\r\n", b"\n").split(b"\n"):
                    if line[:15].lower().startswith(b"content-length:") or \
                       line[:2].lower().startswith(b"l:"):
                        try:
                            cl = int(line.split(b":", 1)[1].strip())
                        except Exception:
                            cl = 0
                        break
                total = head_end + cl
                if len(buf) < total:
                    break
                msg_bytes = buf[:total]
                buf = buf[total:]
                try:
                    msg = SipMessage.parse(msg_bytes)
                except Exception as exc:
                    log.warning("%s: mensaje malformado: %s", self.name.upper(), exc)
                    continue
                try:
                    await self.router(msg, ep, self)
                except Exception:
                    log.exception("Error en router SIP %s", self.name.upper())

    @property
    def local_host(self) -> str: return self._sock_addr[0]

    @property
    def local_port(self) -> int: return self._sock_addr[1]


# ============================== WebSocket (RFC 7118) ==============================

import base64
import hashlib
import struct


_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_accept(key: str) -> str:
    return base64.b64encode(
        hashlib.sha1((key + _WS_GUID).encode()).digest()
    ).decode()


def _ws_encode(payload: bytes, opcode: int = 0x1) -> bytes:
    """Construye un frame WS (servidor → cliente, sin máscara)."""
    header = bytearray()
    header.append(0x80 | opcode)
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack("!H", n)
    else:
        header.append(127)
        header += struct.pack("!Q", n)
    return bytes(header) + payload


async def _ws_decode(reader: asyncio.StreamReader) -> Optional[Tuple[int, bytes]]:
    """Lee un frame WS del cliente. Devuelve (opcode, payload) o None si EOF."""
    hdr = await reader.readexactly(2) if False else None
    try:
        first = await reader.readexactly(2)
    except asyncio.IncompleteReadError:
        return None
    b1, b2 = first[0], first[1]
    fin = b1 & 0x80
    opcode = b1 & 0x0F
    masked = b2 & 0x80
    plen = b2 & 0x7F
    if plen == 126:
        ext = await reader.readexactly(2)
        plen = struct.unpack("!H", ext)[0]
    elif plen == 127:
        ext = await reader.readexactly(8)
        plen = struct.unpack("!Q", ext)[0]
    mask = b""
    if masked:
        mask = await reader.readexactly(4)
    payload = await reader.readexactly(plen) if plen else b""
    if masked and mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    if not fin:
        nxt = await _ws_decode(reader)
        if nxt is not None:
            return opcode, payload + nxt[1]
    return opcode, payload


class WsTransport(Transport):
    """Transporte SIP sobre WebSocket (RFC 7118).

    Subprotocolo HTTP: 'sip'. Cada conexión es bidireccional, los mensajes
    SIP se envían como text frames UTF-8 completos (un frame por mensaje).
    """
    name = "ws"

    def __init__(self, host: str, port: int, router: Router,
                 ssl_ctx: Optional[ssl.SSLContext] = None):
        self.host = host
        self.port = port
        self.router = router
        self.ssl_ctx = ssl_ctx
        if ssl_ctx is not None:
            self.name = "wss"
        self._server: Optional[asyncio.AbstractServer] = None
        self._conns: Dict[Endpoint, asyncio.StreamWriter] = {}
        self._sock_addr: Tuple[str, int] = (host, port)

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_conn, host=self.host, port=self.port, ssl=self.ssl_ctx,
        )
        sock = self._server.sockets[0].getsockname()  # type: ignore
        self._sock_addr = (sock[0], sock[1])
        log.info("SIP %s escuchando en %s:%d", self.name.upper(), *self._sock_addr)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def send(self, data: bytes, dest: Endpoint) -> None:
        w = self._conns.get(dest)
        if w is None or w.is_closing():
            raise RuntimeError(f"No hay conexión WS abierta hacia {dest}")
        try:
            opcode = 0x1  # text
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                opcode = 0x2  # binary
            w.write(_ws_encode(data, opcode))
            await w.drain()
        except Exception:
            self._conns.pop(dest, None)

    async def _handle_conn(self, reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        ep = Endpoint(self.name, peer[0], peer[1])
        try:
            await self._handshake(reader, writer)
        except Exception as exc:
            log.warning("WS handshake falló desde %s: %s", peer, exc)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            return

        self._conns[ep] = writer
        log.info("SIP %s nueva conexión desde %s", self.name.upper(), peer)
        try:
            while True:
                frame = await _ws_decode(reader)
                if frame is None:
                    return
                opcode, payload = frame
                if opcode == 0x8:  # close
                    return
                if opcode == 0x9:  # ping
                    writer.write(_ws_encode(payload, 0xA))
                    await writer.drain()
                    continue
                if opcode == 0xA:  # pong
                    continue
                if opcode in (0x1, 0x2):
                    if not payload.strip():
                        continue
                    try:
                        msg = SipMessage.parse(payload)
                    except Exception as exc:
                        log.warning("WS mensaje SIP malformado: %s", exc)
                        continue
                    try:
                        await self.router(msg, ep, self)
                    except Exception:
                        log.exception("Error en router SIP WS")
        except asyncio.IncompleteReadError:
            pass
        finally:
            self._conns.pop(ep, None)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handshake(self, reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter) -> None:
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = await reader.read(4096)
            if not chunk:
                raise RuntimeError("conexión cerrada antes del handshake")
            head += chunk
            if len(head) > 16384:
                raise RuntimeError("handshake demasiado grande")
        text = head.decode("latin-1", errors="replace")
        lines = text.split("\r\n")
        request = lines[0]
        if not request.upper().startswith("GET "):
            raise RuntimeError("primera línea no es GET")
        headers: Dict[str, str] = {}
        for ln in lines[1:]:
            if ":" not in ln:
                continue
            k, v = ln.split(":", 1)
            headers[k.strip().lower()] = v.strip()
        upgrade = headers.get("upgrade", "").lower()
        connection = headers.get("connection", "").lower()
        key = headers.get("sec-websocket-key", "")
        version = headers.get("sec-websocket-version", "")
        protocols = [p.strip().lower() for p in
                     headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
        if "websocket" not in upgrade or "upgrade" not in connection or not key:
            raise RuntimeError("cabeceras WS inválidas")
        if version != "13":
            raise RuntimeError(f"versión WS no soportada: {version!r}")
        if "sip" not in protocols:
            raise RuntimeError("subprotocolo 'sip' no solicitado")
        accept = _ws_accept(key)
        resp = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "Sec-WebSocket-Protocol: sip\r\n"
            "\r\n"
        )
        writer.write(resp.encode("latin-1"))
        await writer.drain()

    @property
    def local_host(self) -> str: return self._sock_addr[0]

    @property
    def local_port(self) -> int: return self._sock_addr[1]


def make_self_signed_ssl_context(cert_path: str, key_path: str) -> Optional[ssl.SSLContext]:
    if not (cert_path and key_path):
        return None
    if not (os.path.exists(cert_path) and os.path.exists(key_path)):
        return None
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert_path, key_path)
    return ctx
