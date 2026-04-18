"""Capa de transacciones SIP (RFC 3261 §17).

Implementa las cuatro state machines:
    * Cliente INVITE  (§17.1.1.2)
    * Cliente non-INVITE (§17.1.2.2)
    * Servidor INVITE (§17.2.1)
    * Servidor non-INVITE (§17.2.2)

Cada transacción se identifica por (branch, sent-by-host:port, method) — el
método se extrae de CSeq pero la rama (branch) RFC 3261 ya es única dentro
del UAC y es lo que usamos como clave principal.

El TransactionManager se sienta entre la capa de transporte y la lógica de
aplicación (B2BUA, registrar, etc). Reglas:
    * Llama a `on_message(msg, ep, transport)` desde la capa de transporte.
    * Si el mensaje encaja con una transacción existente, lo entrega ahí.
    * Si es una request nueva, crea una server transaction y la entrega al
      handler de aplicación (router_request).
    * Si es una respuesta sin client tx, opcionalmente la rebota al
      stateless handler (para respuestas a transacciones consumidas o forks).
"""
from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Dict, Optional, Tuple

from ..util.logger import get_logger
from .message import SipMessage, make_response, Via
from .transport import Endpoint, Transport

log = get_logger("sip.tx")


class TxState(str, Enum):
    CALLING = "CALLING"
    TRYING = "TRYING"
    PROCEEDING = "PROCEEDING"
    COMPLETED = "COMPLETED"
    CONFIRMED = "CONFIRMED"
    TERMINATED = "TERMINATED"
    ACCEPTED = "ACCEPTED"  # RFC 6026


def new_branch() -> str:
    return "z9hG4bK-" + secrets.token_hex(8)


def tx_key(msg: SipMessage, role: str) -> Tuple[str, str, str]:
    """Devuelve (branch, sent-by, method) para identificar la transacción.

    role: 'client' | 'server'.
    Para servidor, el método de la transacción es el de la request original
    (los ACK a 200 son sus propios tx; los ACK a no-2xx pertenecen a la
    server INVITE tx).
    """
    via = msg.via_top()
    branch = via.branch if via else ""
    sent_by = ""
    if via:
        sent_by = f"{via.host}:{via.port or 5060}"
    if msg.is_request:
        method = msg.method or "INVALID"
    else:
        _, method = msg.cseq
    if method == "ACK":
        method = "INVITE"
    return branch, sent_by, method


# =================== Server Transaction ===================

class ServerTransaction:
    def __init__(self, mgr: "TransactionManager", request: SipMessage,
                 endpoint: Endpoint, transport: Transport):
        self.mgr = mgr
        self.request = request
        self.endpoint = endpoint
        self.transport = transport
        self.method = request.method or "INVALID"
        self.is_invite = self.method == "INVITE"
        self.state: TxState = TxState.PROCEEDING if self.is_invite else TxState.TRYING
        self.last_response: Optional[SipMessage] = None
        self._timer_g: Optional[asyncio.TimerHandle] = None  # retransmisión INVITE 2xx-/non-2xx final
        self._timer_h: Optional[asyncio.TimerHandle] = None  # timeout COMPLETED
        self._timer_i: Optional[asyncio.TimerHandle] = None  # CONFIRMED → terminate
        self._timer_j: Optional[asyncio.TimerHandle] = None  # non-INVITE COMPLETED
        self._g_interval = mgr.t1
        self._created_at = time.time()
        self.key = tx_key(request, "server")
        self.terminated_cb: Optional[Callable[["ServerTransaction"], None]] = None

    # ------------ API pública ------------

    async def respond(self, code: int, reason: Optional[str] = None,
                      to_tag: Optional[str] = None,
                      body: bytes = b"", content_type: Optional[str] = None,
                      extra_headers: Optional[Dict[str, str]] = None) -> SipMessage:
        resp = make_response(self.request, code, reason, to_tag=to_tag,
                             body=body, content_type=content_type)
        if extra_headers:
            for k, v in extra_headers.items():
                resp.set(k, v)
        await self.send_response(resp)
        return resp

    async def send_response(self, resp: SipMessage) -> None:
        self.last_response = resp
        code = resp.status_code or 0
        await self._send(resp)
        if self.is_invite:
            if 100 <= code < 200:
                self.state = TxState.PROCEEDING
            elif 200 <= code < 300:
                self.state = TxState.ACCEPTED
                self._arm_timer_l()  # 64*T1 absorber retransmisiones 2xx ACK
            else:  # 300-699
                self.state = TxState.COMPLETED
                self._g_interval = self.mgr.t1
                self._timer_g = self.mgr.loop.call_later(self._g_interval, self._on_timer_g)
                self._timer_h = self.mgr.loop.call_later(64 * self.mgr.t1, self._on_timer_h)
        else:
            if 100 <= code < 200:
                self.state = TxState.PROCEEDING
            else:  # final
                self.state = TxState.COMPLETED
                self._timer_j = self.mgr.loop.call_later(64 * self.mgr.t1, self._terminate)

    async def _send(self, resp: SipMessage) -> None:
        try:
            await self.transport.send(bytes(resp), self.endpoint)
        except Exception as exc:
            log.warning("Tx srv: error enviando respuesta: %s", exc)

    # ------------ entradas desde TransactionManager ------------

    async def on_request_retransmit(self, msg: SipMessage) -> None:
        if self.last_response is None:
            return
        await self._send(self.last_response)

    async def on_ack(self, msg: SipMessage) -> None:
        if not self.is_invite:
            return
        if self.state == TxState.COMPLETED:
            self.state = TxState.CONFIRMED
            if self._timer_g:
                self._timer_g.cancel()
                self._timer_g = None
            if self._timer_h:
                self._timer_h.cancel()
                self._timer_h = None
            self._timer_i = self.mgr.loop.call_later(self.mgr.t4, self._terminate)

    # ------------ timers ------------

    def _on_timer_g(self) -> None:
        if self.state != TxState.COMPLETED or self.last_response is None:
            return
        asyncio.create_task(self._send(self.last_response))
        self._g_interval = min(self._g_interval * 2, self.mgr.t2)
        self._timer_g = self.mgr.loop.call_later(self._g_interval, self._on_timer_g)

    def _on_timer_h(self) -> None:
        log.info("Tx srv INVITE %s: timer H expiró sin ACK", self.key)
        self._terminate()

    def _arm_timer_l(self) -> None:
        self.mgr.loop.call_later(64 * self.mgr.t1, self._terminate)

    def _terminate(self) -> None:
        if self.state == TxState.TERMINATED:
            return
        self.state = TxState.TERMINATED
        for t in (self._timer_g, self._timer_h, self._timer_i, self._timer_j):
            if t:
                t.cancel()
        self.mgr._remove_server(self)
        if self.terminated_cb:
            try:
                self.terminated_cb(self)
            except Exception:
                log.exception("terminated_cb falló")


# =================== Client Transaction ===================

class ClientTransaction:
    def __init__(self, mgr: "TransactionManager", request: SipMessage,
                 endpoint: Endpoint, transport: Transport):
        self.mgr = mgr
        self.request = request
        self.endpoint = endpoint
        self.transport = transport
        self.method = request.method or "INVALID"
        self.is_invite = self.method == "INVITE"
        self.state: TxState = TxState.CALLING if self.is_invite else TxState.TRYING
        self.responses: list[SipMessage] = []
        self._fut: asyncio.Future[SipMessage] = mgr.loop.create_future()
        self._final_fut: asyncio.Future[SipMessage] = mgr.loop.create_future()
        self._retx_handle: Optional[asyncio.TimerHandle] = None
        self._retx_interval = mgr.t1
        self._timer_b: Optional[asyncio.TimerHandle] = None
        self._timer_d: Optional[asyncio.TimerHandle] = None
        self._timer_f: Optional[asyncio.TimerHandle] = None
        self._timer_k: Optional[asyncio.TimerHandle] = None
        self.key = tx_key(request, "client")
        self.on_response: Optional[Callable[[SipMessage], Awaitable[None]]] = None

    async def start(self) -> None:
        await self.transport.send(bytes(self.request), self.endpoint)
        if self.transport.name == "udp":
            self._retx_handle = self.mgr.loop.call_later(
                self._retx_interval, self._on_retransmit
            )
        if self.is_invite:
            self._timer_b = self.mgr.loop.call_later(64 * self.mgr.t1, self._on_timeout)
        else:
            self._timer_f = self.mgr.loop.call_later(64 * self.mgr.t1, self._on_timeout)

    async def receive_response(self, msg: SipMessage) -> None:
        self.responses.append(msg)
        code = msg.status_code or 0
        if self.is_invite:
            if 100 <= code < 200:
                self.state = TxState.PROCEEDING
                self._cancel_retx()
                if self._timer_b:
                    self._timer_b.cancel(); self._timer_b = None
            elif 200 <= code < 300:
                self.state = TxState.TERMINATED
                self._cancel_all()
                if not self._fut.done():
                    self._fut.set_result(msg)
                if not self._final_fut.done():
                    self._final_fut.set_result(msg)
                self.mgr._remove_client(self)
            else:  # 300-699
                self.state = TxState.COMPLETED
                self._cancel_retx()
                await self._send_ack_for_failure(msg)
                self._timer_d = self.mgr.loop.call_later(32.0, self._terminate)
                if not self._fut.done():
                    self._fut.set_result(msg)
                if not self._final_fut.done():
                    self._final_fut.set_result(msg)
        else:
            if 100 <= code < 200:
                self.state = TxState.PROCEEDING
            else:
                self.state = TxState.COMPLETED
                self._cancel_retx()
                self._timer_k = self.mgr.loop.call_later(self.mgr.t4, self._terminate)
                if not self._fut.done():
                    self._fut.set_result(msg)
                if not self._final_fut.done():
                    self._final_fut.set_result(msg)
        if self.on_response:
            try:
                await self.on_response(msg)
            except Exception:
                log.exception("on_response handler falló")

    async def wait_provisional_or_final(self) -> SipMessage:
        return await self._fut

    async def wait_final(self) -> SipMessage:
        return await self._final_fut

    async def _send_ack_for_failure(self, resp: SipMessage) -> None:
        """ACK para respuestas finales no-2xx (RFC 3261 §17.1.1.3).
        El ACK aquí pertenece a la transacción cliente y va a la URI del Via top.
        """
        ack = SipMessage(is_request=True, method="ACK",
                         request_uri=self.request.request_uri)
        v = self.request.via_top()
        if v:
            ack.add("Via", str(v))
        ack.set("From", self.request.get("From", ""))
        ack.set("To", resp.get("To", self.request.get("To", "")))
        ack.set("Call-ID", self.request.call_id)
        n, _ = self.request.cseq
        ack.set("CSeq", f"{n} ACK")
        ack.set("Max-Forwards", "70")
        for r in self.request.get_all("Route"):
            ack.add("Route", r)
        ack.set("Content-Length", "0")
        try:
            await self.transport.send(bytes(ack), self.endpoint)
        except Exception:
            log.exception("ACK por fallo no se pudo enviar")

    def _on_retransmit(self) -> None:
        if self.state in (TxState.TERMINATED, TxState.COMPLETED, TxState.CONFIRMED):
            return
        if self.is_invite and self.state != TxState.CALLING:
            return
        if not self.is_invite and self.state not in (TxState.TRYING, TxState.PROCEEDING):
            return
        asyncio.create_task(self.transport.send(bytes(self.request), self.endpoint))
        if self.is_invite:
            self._retx_interval = min(self._retx_interval * 2, self.mgr.t2)
        else:
            self._retx_interval = min(self._retx_interval * 2, self.mgr.t2)
        self._retx_handle = self.mgr.loop.call_later(self._retx_interval, self._on_retransmit)

    def _on_timeout(self) -> None:
        log.warning("Tx cli %s: timeout (sin respuesta final)", self.key)
        self._cancel_all()
        self.state = TxState.TERMINATED
        if not self._fut.done():
            self._fut.set_exception(TimeoutError("SIP transaction timeout"))
        if not self._final_fut.done():
            self._final_fut.set_exception(TimeoutError("SIP transaction timeout"))
        self.mgr._remove_client(self)

    def _cancel_retx(self) -> None:
        if self._retx_handle:
            self._retx_handle.cancel()
            self._retx_handle = None

    def _cancel_all(self) -> None:
        self._cancel_retx()
        for t in (self._timer_b, self._timer_d, self._timer_f, self._timer_k):
            if t:
                t.cancel()

    def _terminate(self) -> None:
        if self.state == TxState.TERMINATED:
            return
        self.state = TxState.TERMINATED
        self._cancel_all()
        self.mgr._remove_client(self)


# =================== Manager ===================

RequestHandler = Callable[[ServerTransaction], Awaitable[None]]
StrayResponseHandler = Callable[[SipMessage, Endpoint, Transport], Awaitable[None]]


@dataclass
class TransactionManager:
    loop: asyncio.AbstractEventLoop
    t1: float = 0.5
    t2: float = 4.0
    t4: float = 5.0
    request_handler: Optional[RequestHandler] = None
    stray_response_handler: Optional[StrayResponseHandler] = None
    _server_tx: Dict[Tuple[str, str, str], ServerTransaction] = field(default_factory=dict)
    _client_tx: Dict[Tuple[str, str, str], ClientTransaction] = field(default_factory=dict)

    async def on_message(self, msg: SipMessage, ep: Endpoint, transport: Transport) -> None:
        if msg.is_request:
            await self._handle_request(msg, ep, transport)
        else:
            await self._handle_response(msg, ep, transport)

    async def _handle_request(self, msg: SipMessage, ep: Endpoint, transport: Transport) -> None:
        key = tx_key(msg, "server")
        if msg.method == "ACK":
            stx = self._server_tx.get(key)
            if stx is not None:
                await stx.on_ack(msg)
                return
            if self.request_handler:
                # ACK de 2xx → no tiene server tx asociada; sube a la app
                tmp_stx = ServerTransaction(self, msg, ep, transport)
                tmp_stx.state = TxState.TERMINATED
                await self.request_handler(tmp_stx)
            return

        existing = self._server_tx.get(key)
        if existing is not None:
            await existing.on_request_retransmit(msg)
            return

        stx = ServerTransaction(self, msg, ep, transport)
        self._server_tx[key] = stx
        if msg.method == "INVITE":
            await stx.respond(100, "Trying")
        if self.request_handler is None:
            await stx.respond(500, "No request handler")
            return
        try:
            await self.request_handler(stx)
        except Exception:
            log.exception("request_handler falló")
            try:
                if stx.state not in (TxState.COMPLETED, TxState.ACCEPTED, TxState.TERMINATED):
                    await stx.respond(500, "Internal Server Error")
            except Exception:
                pass

    async def _handle_response(self, msg: SipMessage, ep: Endpoint, transport: Transport) -> None:
        key = tx_key(msg, "client")
        ctx = self._client_tx.get(key)
        if ctx is not None:
            await ctx.receive_response(msg)
            return
        if self.stray_response_handler:
            await self.stray_response_handler(msg, ep, transport)

    async def send_request(self, request: SipMessage, ep: Endpoint,
                           transport: Transport) -> ClientTransaction:
        key = tx_key(request, "client")
        ctx = ClientTransaction(self, request, ep, transport)
        self._client_tx[key] = ctx
        await ctx.start()
        return ctx

    def _remove_server(self, tx: ServerTransaction) -> None:
        self._server_tx.pop(tx.key, None)

    def _remove_client(self, tx: ClientTransaction) -> None:
        self._client_tx.pop(tx.key, None)
