"""B2BUA (Back-to-Back User Agent) de SMURF.

El B2BUA actúa como dos UAs unidos: termina la llamada entrante (UAS) y
abre una llamada saliente al destino real (UAC). Ambos diálogos están
desacoplados pero coordinados por una sesión `Call` que mantiene un par
de RtpLegs como puente de medios.

Soporta:
    * Resolución de destino vía dial plan (extensión, queue, ivr, conferencia,
      voicemail, trunk, ringgroup, hangup).
    * Llamada a extensión registrada usando la LocationService del registrar.
    * Forking secuencial entre múltiples bindings de la misma extensión.
    * Re-INVITE para hold/unhold (sendonly/sendrecv).
    * REFER para transferencia ciega (RFC 3515) y atendida.
    * BYE / CANCEL correctos en todos los estados.
    * Grabación opcional integrada con el motor RTP.
    * Generación de CDRs.
    * Buzón de voz cuando la extensión no contesta o está ocupada.
"""
from __future__ import annotations

import asyncio
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from ..db import Database
from ..rtp.engine import RtpAllocator, RtpLeg, RtpRelay
from ..rtp.recorder import CallRecorder
from ..rtp.sounds import busy_tone, congestion_tone, ringback_tone
from ..rtp.wavfile import AudioPlayer
from ..sip.auth import build_challenge, DigestCredentials, verify_response
from ..sip.dialog import Dialog, gen_call_id, gen_tag
from ..sip.message import SipMessage, Via, make_response, reason_for
from ..sip.registrar import Binding, LocationService
from ..sip.sdp import SDP, build_audio_offer, negotiate_audio
from ..sip.transaction import (ClientTransaction, ServerTransaction,
                               TransactionManager)
from ..sip.transport import Endpoint, Transport
from ..sip.uri import NameAddr, SipURI
from ..util.config import SmurfConfig
from ..util.logger import get_logger
from .dialplan import Dialplan, DialplanRoute
from .events import EventBus

log = get_logger("pbx.b2bua")


class CallState(str, Enum):
    INIT = "init"
    RINGING = "ringing"
    EARLY = "early"
    ANSWERED = "answered"
    HOLD = "hold"
    BYE = "bye"
    DONE = "done"


@dataclass
class CallLegInfo:
    dialog: Optional[Dialog] = None
    leg: Optional[RtpLeg] = None
    transport: Optional[Transport] = None
    endpoint: Optional[Endpoint] = None
    contact: Optional[SipURI] = None
    invite_request: Optional[SipMessage] = None
    invite_response: Optional[SipMessage] = None
    server_tx: Optional[ServerTransaction] = None
    client_tx: Optional[ClientTransaction] = None


@dataclass
class Call:
    id: str
    src_number: str
    dst_number: str
    direction: str
    started_at: float = field(default_factory=time.time)
    answered_at: Optional[float] = None
    ended_at: Optional[float] = None
    state: CallState = CallState.INIT
    a: CallLegInfo = field(default_factory=CallLegInfo)
    b: CallLegInfo = field(default_factory=CallLegInfo)
    relay: Optional[RtpRelay] = None
    recorder: Optional[CallRecorder] = None
    moh_player: Optional[AudioPlayer] = None
    via_trunk: Optional[str] = None
    hangup_cause: Optional[str] = None
    disposition: str = "FAILED"


class B2BUA:
    def __init__(self, cfg: SmurfConfig, db: Database, location: LocationService,
                 tx_mgr: TransactionManager, rtp_alloc: RtpAllocator,
                 events: EventBus, dialplan: Dialplan,
                 transports: Dict[str, Transport]):
        self.cfg = cfg
        self.db = db
        self.location = location
        self.tx = tx_mgr
        self.rtp = rtp_alloc
        self.events = events
        self.dialplan = dialplan
        self.transports = transports
        self.calls: Dict[str, Call] = {}
        # call-id de la pierna A → Call
        self._by_a_callid: Dict[str, str] = {}
        # call-id de la pierna B → Call (para BYE/ACK desde el callee)
        self._by_b_callid: Dict[str, str] = {}
        # Dialog id → Call (para in-dialog requests)
        self._by_dialog: Dict[Tuple[str, str, str], str] = {}
        self.public_ip = cfg.sip.public_ip or self._guess_public_ip()
        self.realm = cfg.sip.realm

    def _guess_public_ip(self) -> str:
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"

    # --------------------- Punto de entrada ---------------------

    async def handle_request(self, stx: ServerTransaction) -> bool:
        """Devuelve True si el método fue manejado."""
        req = stx.request
        method = req.method or ""
        if method == "INVITE":
            await self._handle_invite(stx)
            return True
        if method == "ACK":
            await self._handle_ack(stx)
            return True
        if method == "BYE":
            await self._handle_bye(stx)
            return True
        if method == "CANCEL":
            await self._handle_cancel(stx)
            return True
        if method == "REFER":
            await self._handle_refer(stx)
            return True
        if method == "UPDATE":
            await stx.respond(200, "OK")
            return True
        if method == "INFO":
            await self._handle_info(stx)
            return True
        if method == "OPTIONS":
            await stx.respond(200, "OK", extra_headers={
                "Allow": "INVITE, ACK, BYE, CANCEL, REGISTER, OPTIONS, REFER, UPDATE, INFO, MESSAGE, SUBSCRIBE, NOTIFY",
                "Accept": "application/sdp",
            })
            return True
        if method == "SUBSCRIBE":
            await stx.respond(489, "Bad Event")
            return True
        if method == "MESSAGE":
            await self._handle_message(stx)
            return True
        return False

    # --------------------- INVITE entrante ---------------------

    async def _handle_invite(self, stx: ServerTransaction) -> None:
        req = stx.request
        # 1) ¿Re-INVITE en diálogo existente?
        from_tag = req.from_header.parameters.get("tag", "")
        to_tag = req.to_header.parameters.get("tag", "")
        if to_tag:
            did = (req.call_id, to_tag, from_tag)
            cid = self._by_dialog.get(did)
            if cid:
                await self._reinvite(self.calls[cid], "a", stx)
                return
            did2 = (req.call_id, from_tag, to_tag)
            cid = self._by_dialog.get(did2)
            if cid:
                await self._reinvite(self.calls[cid], "b", stx)
                return

        # 2) Autenticación de la llamada (sólo extensiones registradas saliendo).
        from_user = req.from_header.uri.user or ""
        require_auth = self._is_extension_caller(from_user, req)
        if require_auth:
            cred = await self._creds_for(from_user)
            if cred is None:
                log.warning("INVITE rechazada: no hay credenciales para %s", from_user)
                await stx.respond(403, "Forbidden")
                return
            ah = req.get("Proxy-Authorization") or req.get("Authorization")
            if not ah:
                log.debug("INVITE de %s sin auth → 407", from_user)
                await stx.respond(407, "Proxy Authentication Required",
                                  extra_headers={"Proxy-Authenticate":
                                                 build_challenge(self.realm)})
                return
            ok, stale = verify_response(req.method or "", req.body, ah, cred)
            log.debug("INVITE auth verify %s: ok=%s stale=%s", from_user, ok, stale)
            if not ok:
                await stx.respond(407, "Proxy Authentication Required",
                                  extra_headers={"Proxy-Authenticate":
                                                 build_challenge(self.realm, stale=stale)})
                return

        # 3) Resolución de destino
        dst_uri = req.request_uri
        dst_number = (dst_uri.user or "").strip()
        direction = "internal"
        if not self._is_local_user(from_user):
            direction = "inbound"
        elif not await self._extension_exists(dst_number) and "@" in str(dst_uri) and (dst_uri.host != self.realm):
            direction = "outbound"

        route = await self.dialplan.match(dst_number, direction)
        # Match implícito: si el destino es exactamente una extensión, va a esa.
        if route is None and direction == "internal":
            if await self._extension_exists(dst_number):
                route = DialplanRoute(0, "implicit", "internal", "^.+$",
                                      "extension", dst_number, 9999)

        if route is None:
            await stx.respond(404, "Not Found")
            return

        call = Call(
            id=secrets.token_hex(8),
            src_number=from_user,
            dst_number=dst_number,
            direction=direction,
        )
        call.a.invite_request = req
        call.a.server_tx = stx
        call.a.transport = stx.transport
        call.a.endpoint = stx.endpoint
        self.calls[call.id] = call
        self._by_a_callid[req.call_id] = call.id
        await self.events.publish("call.start",
                                  call_id=call.id,
                                  src=from_user, dst=dst_number,
                                  direction=direction)

        try:
            await self._route(call, route)
        except Exception:
            log.exception("Error enrutando llamada")
            await self._end_call(call, "FAILED", "internal-error")
            try: await stx.respond(500, "Server Internal Error")
            except Exception: pass

    async def _route(self, call: Call, route: DialplanRoute) -> None:
        target_value = route.transform(call.dst_number)
        t = route.target_type
        log.info("Routing %s → %s:%s", call.dst_number, t, target_value)
        if t == "extension":
            await self._dial_extension(call, target_value)
        elif t == "voicemail":
            target = call.dst_number if target_value == "self" else target_value
            await self._send_to_voicemail(call, target)
        elif t == "hangup":
            await call.a.server_tx.respond(int(target_value or 603) or 603,
                                           reason_for(int(target_value or 603) or 603))
            await self._end_call(call, "FAILED", "hangup")
        elif t == "conference":
            await self._join_conference(call, target_value)
        elif t == "trunk":
            await self._dial_trunk(call, target_value)
        elif t == "queue":
            await self._enqueue(call, target_value)
        elif t == "ivr":
            await self._run_ivr(call, target_value)
        elif t == "ringgroup":
            await self._ring_group(call, target_value)
        else:
            await call.a.server_tx.respond(404, "Not Found")
            await self._end_call(call, "FAILED", "no-route")

    # --------------------- DIAL EXTENSION ---------------------

    async def _dial_extension(self, call: Call, ext: str) -> None:
        bindings = self.location.get(f"sip:{ext}@{self.realm}")
        if not bindings:
            ext_row = await self.db.fetchone(
                "SELECT * FROM extensions WHERE number=? AND enabled=1", (ext,)
            )
            if ext_row and ext_row.get("voicemail_enabled"):
                await self._send_to_voicemail(call, ext)
                return
            await call.a.server_tx.respond(480, "Temporarily Unavailable")
            await self._end_call(call, "NO_ANSWER", "user-not-registered")
            return

        ext_row = await self.db.fetchone(
            "SELECT * FROM extensions WHERE number=? AND enabled=1", (ext,)
        )
        timeout = (ext_row or {}).get("no_answer_seconds", 25) or 25

        for b in bindings:
            ok = await self._fork_one(call, b, timeout)
            if ok:
                return

        if (ext_row or {}).get("voicemail_enabled", 1):
            await self._send_to_voicemail(call, ext)
        else:
            try: await call.a.server_tx.respond(486, "Busy Here")
            except Exception: pass
            await self._end_call(call, "NO_ANSWER", "all-busy")

    async def _fork_one(self, call: Call, binding: Binding, timeout: int) -> bool:
        a_leg = RtpLeg(self.rtp, pt=0)
        b_leg = RtpLeg(self.rtp, pt=0)
        await a_leg.open()
        await b_leg.open()
        call.a.leg = a_leg
        call.b.leg = b_leg
        call.b.endpoint = binding.endpoint
        call.b.contact = binding.contact_uri
        call.b.transport = self._pick_transport(binding.endpoint.transport)

        offer_sdp = build_audio_offer(self.public_ip, b_leg.local_port,
                                      ["PCMU", "PCMA", "telephone-event"])
        invite = self._make_b_invite(call, binding, offer_sdp.serialize())
        call.b.invite_request = invite

        ctx = await self.tx.send_request(invite, binding.endpoint, call.b.transport)
        call.b.client_tx = ctx
        call.state = CallState.RINGING
        await self.events.publish("call.ringing", call_id=call.id, dst=call.dst_number)

        # Reenviar provisionales al caller (early media)
        async def fwd_prov(resp: SipMessage) -> None:
            code = resp.status_code or 0
            if 100 < code < 200:
                # 180 → 180 con SDP si lo trae
                fwd = make_response(call.a.invite_request, code,
                                    to_tag=self._uas_tag_for(call), 
                                    body=resp.body if "application/sdp" in (resp.get("Content-Type") or "").lower() else b"",
                                    content_type="application/sdp" if resp.body else None)
                contact_uri = self._build_local_contact(call.a.transport)
                fwd.set("Contact", str(NameAddr(uri=contact_uri)))
                try: await call.a.server_tx.send_response(fwd)
                except Exception: log.exception("forward prov falló")
        ctx.on_response = fwd_prov

        try:
            final = await asyncio.wait_for(ctx.wait_final(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            await self._cancel_b(call)
            return False
        except Exception:
            return False

        code = final.status_code or 0
        if 200 <= code < 300:
            return await self._answer_call(call, final)
        if code in (486, 600, 603, 480):
            # busy/decline → no probar más bindings (simulamos serial)
            return False
        return False

    def _make_b_invite(self, call: Call, b: Binding, body: bytes) -> SipMessage:
        msg = SipMessage(is_request=True, method="INVITE",
                         request_uri=SipURI.parse(str(b.contact_uri)))
        from_uri = SipURI(scheme="sip", user=call.src_number or "anonymous",
                          host=self.realm)
        from_na = NameAddr(uri=from_uri); from_na.parameters["tag"] = gen_tag()
        msg.set("From", str(from_na))
        to_uri = SipURI.parse(str(b.contact_uri))
        msg.set("To", str(NameAddr(uri=to_uri)))
        msg.set("Call-ID", gen_call_id(self.realm))
        msg.set("CSeq", "1 INVITE")
        msg.set("Max-Forwards", "70")
        msg.set("User-Agent", self.cfg.sip.user_agent)
        msg.set("Allow", "INVITE, ACK, BYE, CANCEL, OPTIONS, REFER, UPDATE, INFO, MESSAGE")
        contact_uri = self._build_local_contact(call.b.transport)
        msg.set("Contact", str(NameAddr(uri=contact_uri)))
        v = Via(transport=call.b.transport.name.upper(), host=self.public_ip,
                port=call.b.transport.local_port,
                parameters={"branch": f"z9hG4bK-{secrets.token_hex(6)}", "rport": ""})
        msg.add("Via", str(v))
        msg.set("Content-Type", "application/sdp")
        msg.set("Content-Length", str(len(body)))
        msg.body = body
        self._by_b_callid[msg.call_id] = call.id
        return msg

    def _build_local_contact(self, t: Transport) -> SipURI:
        scheme = "sips" if t.name in ("tls", "wss") else "sip"
        u = SipURI(scheme=scheme, user="smurf", host=self.public_ip,
                   port=t.local_port,
                   parameters={"transport": t.name})
        return u

    def _pick_transport(self, name: str) -> Transport:
        return self.transports.get(name) or self.transports["udp"]

    def _uas_tag_for(self, call: Call) -> str:
        # tag estable durante la llamada
        if not hasattr(call, "_uas_tag"):
            call._uas_tag = gen_tag()  # type: ignore[attr-defined]
        return call._uas_tag  # type: ignore[attr-defined]

    # --------------------- ANSWER ---------------------

    async def _answer_call(self, call: Call, final_b: SipMessage) -> bool:
        """Procesa el 200 OK del lado B y termina de levantar la pierna A."""
        try:
            sdp_b = SDP.parse(final_b.body) if final_b.body else None
        except Exception:
            sdp_b = None
        if sdp_b is None or sdp_b.first_audio() is None:
            await self._cancel_b(call)
            return False
        am_b = sdp_b.first_audio()
        # Configurar leg b según remote
        remote_ip = am_b.connection or sdp_b.connection or call.b.endpoint.host
        call.b.leg.set_remote(remote_ip, am_b.port)
        # PT acordado entre los formats que ofrecimos: el primero que NO sea telephone-event
        for pt in am_b.formats:
            if pt != 101:
                call.b.leg.pt = pt
                break

        # Diálogo B (UAC)
        call.b.dialog = Dialog.from_uac_2xx(call.b.invite_request, final_b,
                                            self._build_local_contact(call.b.transport))
        self._by_dialog[call.b.dialog.id] = call.id

        # ACK al 200 OK
        ack = call.b.dialog.build_ack(final_b)
        try:
            await call.b.transport.send(bytes(ack), call.b.endpoint)
        except Exception:
            log.exception("ACK al 200 (B) falló")

        # ===== Construir respuesta a A =====
        try:
            sdp_a_offer = SDP.parse(call.a.invite_request.body) if call.a.invite_request.body else None
        except Exception:
            sdp_a_offer = None
        if sdp_a_offer is None or sdp_a_offer.first_audio() is None:
            await self._cancel_b(call)
            return False
        am_a = sdp_a_offer.first_audio()
        # Conectar leg a
        remote_a = am_a.connection or sdp_a_offer.connection or call.a.endpoint.host
        call.a.leg.set_remote(remote_a, am_a.port)
        # PT lado A
        ans_a = negotiate_audio(sdp_a_offer, self.public_ip, call.a.leg.local_port)
        if not ans_a:
            await self._cancel_b(call)
            return False
        sel_a = ans_a.first_audio().formats
        for pt in sel_a:
            if pt != 101:
                call.a.leg.pt = pt
                break

        body = ans_a.serialize()
        ok = make_response(call.a.invite_request, 200, "OK",
                           to_tag=self._uas_tag_for(call),
                           body=body, content_type="application/sdp")
        ok.set("Contact", str(NameAddr(uri=self._build_local_contact(call.a.transport))))
        ok.set("Allow", "INVITE, ACK, BYE, CANCEL, OPTIONS, REFER, UPDATE, INFO, MESSAGE")
        ok.set("Server", self.cfg.sip.user_agent)
        await call.a.server_tx.send_response(ok)
        call.a.invite_response = ok
        call.a.dialog = Dialog.from_uas_2xx(call.a.invite_request, ok,
                                            self._build_local_contact(call.a.transport))
        self._by_dialog[call.a.dialog.id] = call.id

        # ===== Puente RTP =====
        relay = RtpRelay(call.a.leg, call.b.leg,
                         max_silence_ms=self.cfg.rtp.max_silence_ms)
        relay.start()
        call.relay = relay
        call.state = CallState.ANSWERED
        call.answered_at = time.time()
        call.disposition = "ANSWERED"
        await self.events.publish("call.answered", call_id=call.id,
                                  src=call.src_number, dst=call.dst_number)

        # ¿Grabar?
        if await self._must_record(call):
            ts = time.strftime("%Y%m%d-%H%M%S")
            path = os.path.join(self.cfg.storage.recordings_dir,
                                f"{ts}-{call.src_number}-{call.dst_number}-{call.id}.wav")
            rec = CallRecorder(path, call.a.leg, call.b.leg, stereo=True)
            rec.start()
            call.recorder = rec
        return True

    async def _must_record(self, call: Call) -> bool:
        for num in (call.src_number, call.dst_number):
            r = await self.db.fetchone(
                "SELECT record_calls FROM extensions WHERE number=?", (num,)
            )
            if r and r["record_calls"]:
                return True
        return False

    # --------------------- ACK / BYE / CANCEL ---------------------

    async def _handle_ack(self, stx: ServerTransaction) -> None:
        req = stx.request
        cid = self._by_a_callid.get(req.call_id) or self._by_b_callid.get(req.call_id)
        if cid is None:
            return
        # nada más que hacer: la sesión ya está activa o el ACK pertenece a un 2xx confirmado.

    async def _handle_bye(self, stx: ServerTransaction) -> None:
        req = stx.request
        from_tag = req.from_header.parameters.get("tag", "")
        to_tag = req.to_header.parameters.get("tag", "")
        for did in ((req.call_id, to_tag, from_tag), (req.call_id, from_tag, to_tag)):
            cid = self._by_dialog.get(did)
            if cid:
                call = self.calls[cid]
                await stx.respond(200, "OK")
                # Propagar BYE al otro lado
                other = call.b if did == call.a.dialog.id else call.a
                if other.dialog and other.transport and other.endpoint:
                    bye = other.dialog.build_request("BYE")
                    v = Via(transport=other.transport.name.upper(),
                            host=self.public_ip, port=other.transport.local_port,
                            parameters={"branch": f"z9hG4bK-{secrets.token_hex(6)}"})
                    bye.add("Via", str(v))
                    try:
                        await self.tx.send_request(bye, other.endpoint, other.transport)
                    except Exception:
                        log.exception("BYE al peer falló")
                await self._end_call(call, call.disposition, "normal")
                return
        await stx.respond(481, "Call/Transaction Does Not Exist")

    async def _handle_cancel(self, stx: ServerTransaction) -> None:
        req = stx.request
        await stx.respond(200, "OK")
        cid = self._by_a_callid.get(req.call_id)
        if cid is None:
            return
        call = self.calls[cid]
        # Responder al INVITE original con 487 si aún está pendiente
        try:
            if call.a.server_tx and call.a.server_tx.state.value not in ("COMPLETED", "ACCEPTED", "TERMINATED"):
                await call.a.server_tx.respond(487, "Request Terminated",
                                               to_tag=self._uas_tag_for(call))
        except Exception:
            pass
        await self._cancel_b(call)
        await self._end_call(call, "CANCELLED", "caller-cancel")

    async def _cancel_b(self, call: Call) -> None:
        if call.b.client_tx and call.b.client_tx.state.value in ("CALLING", "PROCEEDING"):
            req = call.b.invite_request
            cancel = SipMessage(is_request=True, method="CANCEL",
                                request_uri=req.request_uri)
            for h in ("From", "To", "Call-ID"):
                cancel.set(h, req.get(h, ""))
            n, _ = req.cseq
            cancel.set("CSeq", f"{n} CANCEL")
            cancel.set("Max-Forwards", "70")
            v = req.via_top()
            if v:
                cancel.add("Via", str(v))
            try:
                await self.tx.send_request(cancel, call.b.endpoint, call.b.transport)
            except Exception:
                log.exception("CANCEL al callee falló")
        if call.b.leg:
            await call.b.leg.close()
        if call.a.leg:
            await call.a.leg.close()

    async def _end_call(self, call: Call, disposition: str, cause: str) -> None:
        if call.state == CallState.DONE:
            return
        call.state = CallState.DONE
        call.ended_at = time.time()
        call.disposition = disposition
        call.hangup_cause = cause
        if call.recorder:
            try:
                dur = call.recorder.stop()
                log.info("Grabación cerrada (%.1fs)", dur)
            except Exception:
                log.exception("cerrando grabación")
        if call.relay:
            try:
                await call.relay.close()
            except Exception:
                log.exception("cerrando relay")
        if call.moh_player:
            try: await call.moh_player.stop()
            except Exception: pass
        # Limpiar índices
        for cid_map in (self._by_a_callid, self._by_b_callid):
            for k, v in list(cid_map.items()):
                if v == call.id:
                    cid_map.pop(k, None)
        for k, v in list(self._by_dialog.items()):
            if v == call.id:
                self._by_dialog.pop(k, None)
        self.calls.pop(call.id, None)
        # CDR
        bill = int((call.ended_at - call.answered_at)) if call.answered_at else 0
        await self.db.execute(
            "INSERT INTO cdr(call_id,started_at,answered_at,ended_at,src_number,"
            "dst_number,direction,disposition,duration,bill_seconds,via_trunk,"
            "recording_path,hangup_cause) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (call.id, call.started_at, call.answered_at, call.ended_at,
             call.src_number, call.dst_number, call.direction, call.disposition,
             int(call.ended_at - call.started_at), bill, call.via_trunk,
             call.recorder.path if call.recorder else None, cause),
        )
        await self.events.publish("call.end", call_id=call.id,
                                  disposition=disposition, cause=cause,
                                  duration=int(call.ended_at - call.started_at),
                                  bill_seconds=bill)

    # --------------------- REFER (transferencia) ---------------------

    async def _handle_refer(self, stx: ServerTransaction) -> None:
        req = stx.request
        from_tag = req.from_header.parameters.get("tag", "")
        to_tag = req.to_header.parameters.get("tag", "")
        cid = self._by_dialog.get((req.call_id, to_tag, from_tag)) or \
              self._by_dialog.get((req.call_id, from_tag, to_tag))
        if cid is None:
            await stx.respond(481, "Call/Transaction Does Not Exist")
            return
        refer_to = req.get("Refer-To")
        if not refer_to:
            await stx.respond(400, "Bad Request")
            return
        await stx.respond(202, "Accepted")
        call = self.calls[cid]
        target = NameAddr.parse(refer_to)
        new_dst = (target.uri.user or "").strip()
        log.info("REFER en call %s → %s", call.id, new_dst)
        # Cerramos pierna B y abrimos nueva contra new_dst
        if call.b.leg:
            await call.b.leg.close()
        # nueva pierna B: re-routing rápido
        new_b_leg = RtpLeg(self.rtp, pt=0)
        await new_b_leg.open()
        call.b = CallLegInfo(leg=new_b_leg)
        # buscar binding
        bs = self.location.get(f"sip:{new_dst}@{self.realm}")
        if not bs:
            await self._end_call(call, "FAILED", "transfer-no-target")
            return
        await self._fork_one(call, bs[0], 30)

    # --------------------- INFO (DTMF SIP) ---------------------

    async def _handle_info(self, stx: ServerTransaction) -> None:
        req = stx.request
        await stx.respond(200, "OK")
        ct = (req.get("Content-Type") or "").lower()
        if "dtmf-relay" in ct or "dtmf" in ct:
            body = req.body.decode("utf-8", errors="replace")
            digit = ""
            for line in body.splitlines():
                if line.lower().startswith("signal="):
                    digit = line.split("=", 1)[1].strip()
                    break
            if digit:
                cid = self._by_a_callid.get(req.call_id) or self._by_b_callid.get(req.call_id)
                if cid and self.calls[cid].b.leg:
                    self.calls[cid].b.leg.send_dtmf(digit)

    # --------------------- MESSAGE (chat IM) ---------------------

    async def _handle_message(self, stx: ServerTransaction) -> None:
        req = stx.request
        src = req.from_header.uri.user or ""
        dst = req.to_header.uri.user or ""
        body = req.body.decode("utf-8", errors="replace")
        await self.db.execute(
            "INSERT INTO chat_messages(src,dst,body) VALUES(?,?,?)",
            (src, dst, body),
        )
        await self.events.publish("chat.message", src=src, dst=dst, body=body)
        # Reenviar a destinatario si está registrado (RFC 3428 store-and-forward)
        bs = self.location.get(f"sip:{dst}@{self.realm}")
        if bs:
            for b in bs:
                fwd = SipMessage(is_request=True, method="MESSAGE",
                                 request_uri=SipURI.parse(str(b.contact_uri)))
                fwd.set("From", str(NameAddr(uri=SipURI(user=src, host=self.realm),
                                             parameters={"tag": gen_tag()})))
                fwd.set("To", str(NameAddr(uri=SipURI(user=dst, host=self.realm))))
                fwd.set("Call-ID", gen_call_id(self.realm))
                fwd.set("CSeq", "1 MESSAGE")
                fwd.set("Max-Forwards", "70")
                fwd.set("Content-Type", req.get("Content-Type") or "text/plain")
                fwd.set("Content-Length", str(len(req.body)))
                fwd.body = req.body
                v = Via(transport=b.endpoint.transport.upper(),
                        host=self.public_ip, port=self.transports[b.endpoint.transport].local_port,
                        parameters={"branch": f"z9hG4bK-{secrets.token_hex(6)}"})
                fwd.add("Via", str(v))
                try:
                    await self.tx.send_request(fwd, b.endpoint, self.transports[b.endpoint.transport])
                except Exception:
                    log.warning("Forward MESSAGE a %s falló", dst)
        await stx.respond(202, "Accepted")

    # --------------------- Re-INVITE (hold/unhold) ---------------------

    async def _reinvite(self, call: Call, side: str, stx: ServerTransaction) -> None:
        req = stx.request
        leg_info = call.a if side == "a" else call.b
        if leg_info.leg is None:
            await stx.respond(488, "Not Acceptable Here")
            return
        try:
            sdp = SDP.parse(req.body) if req.body else None
        except Exception:
            sdp = None
        if sdp is None or sdp.first_audio() is None:
            await stx.respond(488, "Not Acceptable Here")
            return
        m = sdp.first_audio()
        # Re-conectar
        remote_ip = m.connection or sdp.connection or stx.endpoint.host
        leg_info.leg.set_remote(remote_ip, m.port)
        ans = negotiate_audio(sdp, self.public_ip, leg_info.leg.local_port)
        if not ans:
            await stx.respond(488, "Not Acceptable Here")
            return
        body = ans.serialize()
        resp = make_response(req, 200, "OK",
                             to_tag=req.to_header.parameters.get("tag") or self._uas_tag_for(call),
                             body=body, content_type="application/sdp")
        resp.set("Contact", str(NameAddr(uri=self._build_local_contact(stx.transport))))
        await stx.send_response(resp)
        if m.direction in ("sendonly", "inactive"):
            call.state = CallState.HOLD
        elif call.state == CallState.HOLD:
            call.state = CallState.ANSWERED
        await self.events.publish("call.reinvite", call_id=call.id,
                                  side=side, direction=m.direction)

    # --------------------- Voicemail ---------------------

    async def _send_to_voicemail(self, call: Call, ext: str) -> None:
        """Reproduce un saludo y graba un mensaje hasta # o hangup."""
        await call.a.server_tx.respond(180, "Ringing", to_tag=self._uas_tag_for(call))
        await asyncio.sleep(0.4)
        a_leg = RtpLeg(self.rtp, pt=0)
        await a_leg.open()
        call.a.leg = a_leg
        # Negociar A
        try:
            sdp_a_offer = SDP.parse(call.a.invite_request.body) if call.a.invite_request.body else None
        except Exception:
            sdp_a_offer = None
        if not sdp_a_offer or not sdp_a_offer.first_audio():
            await call.a.server_tx.respond(488, "Not Acceptable Here")
            await self._end_call(call, "FAILED", "no-sdp")
            return
        am_a = sdp_a_offer.first_audio()
        a_leg.set_remote(am_a.connection or sdp_a_offer.connection or call.a.endpoint.host, am_a.port)
        for pt in am_a.formats:
            if pt != 101:
                a_leg.pt = pt
                break
        ans = negotiate_audio(sdp_a_offer, self.public_ip, a_leg.local_port)
        ok = make_response(call.a.invite_request, 200, "OK",
                           to_tag=self._uas_tag_for(call),
                           body=ans.serialize(), content_type="application/sdp")
        ok.set("Contact", str(NameAddr(uri=self._build_local_contact(call.a.transport))))
        await call.a.server_tx.send_response(ok)
        call.a.dialog = Dialog.from_uas_2xx(call.a.invite_request, ok,
                                            self._build_local_contact(call.a.transport))
        self._by_dialog[call.a.dialog.id] = call.id
        call.state = CallState.ANSWERED
        call.answered_at = time.time()
        call.disposition = "ANSWERED"
        # Reproducir saludo (tono indicativo + beep)
        from ..rtp.sounds import dtmf_tone, _silence  # type: ignore
        beep = dtmf_tone("1", 0.8, 0.3)
        AudioPlayer(a_leg, beep).start()
        await asyncio.sleep(1.2)
        # Grabar 30s o hasta DTMF/BYE
        ts = time.strftime("%Y%m%d-%H%M%S")
        vm_dir = os.path.join(self.cfg.storage.voicemail_dir, ext)
        os.makedirs(vm_dir, exist_ok=True)
        path = os.path.join(vm_dir, f"{ts}-{call.src_number}-{call.id}.wav")
        rec = CallRecorder(path, a_leg, None, stereo=False)
        rec.start()
        # Esperar BYE o timeout
        try:
            await asyncio.wait_for(self._wait_call_end(call), timeout=60)
        except asyncio.TimeoutError:
            pass
        dur = rec.stop()
        await self.db.execute(
            "INSERT INTO voicemail(extension,caller,duration,file_path) VALUES(?,?,?,?)",
            (ext, call.src_number, int(dur), path),
        )
        await self.events.publish("voicemail.new", extension=ext,
                                  caller=call.src_number, duration=int(dur),
                                  file=path)
        await self._end_call(call, "ANSWERED", "voicemail")

    async def _wait_call_end(self, call: Call) -> None:
        while call.id in self.calls and call.state != CallState.DONE:
            await asyncio.sleep(0.5)

    # --------------------- Conferencia ---------------------

    async def _join_conference(self, call: Call, conf_number: str) -> None:
        from ..rtp.conference import ConferenceBridge
        bridge = self._conferences.get(conf_number)
        if bridge is None:
            bridge = ConferenceBridge(conf_number)
            self._conferences[conf_number] = bridge
            bridge.start()
        a_leg = RtpLeg(self.rtp, pt=0)
        await a_leg.open()
        call.a.leg = a_leg
        try:
            sdp_a = SDP.parse(call.a.invite_request.body)
        except Exception:
            await call.a.server_tx.respond(488); await self._end_call(call, "FAILED", "no-sdp"); return
        am = sdp_a.first_audio()
        a_leg.set_remote(am.connection or sdp_a.connection or call.a.endpoint.host, am.port)
        for pt in am.formats:
            if pt != 101: a_leg.pt = pt; break
        ans = negotiate_audio(sdp_a, self.public_ip, a_leg.local_port)
        ok = make_response(call.a.invite_request, 200, "OK",
                           to_tag=self._uas_tag_for(call),
                           body=ans.serialize(), content_type="application/sdp")
        ok.set("Contact", str(NameAddr(uri=self._build_local_contact(call.a.transport))))
        await call.a.server_tx.send_response(ok)
        call.a.dialog = Dialog.from_uas_2xx(call.a.invite_request, ok,
                                            self._build_local_contact(call.a.transport))
        self._by_dialog[call.a.dialog.id] = call.id
        bridge.add(a_leg, name=call.src_number or call.id)
        call.state = CallState.ANSWERED
        call.answered_at = time.time(); call.disposition = "ANSWERED"
        await self.events.publish("conf.join", conf=conf_number, who=call.src_number)
        # Esperar BYE
        await self._wait_call_end(call)
        await self.events.publish("conf.leave", conf=conf_number, who=call.src_number)

    _conferences: Dict[str, "ConferenceBridge"] = {}  # type: ignore[name-defined]

    # --------------------- Trunk (saliente) ---------------------

    async def _dial_trunk(self, call: Call, trunk_name: str) -> None:
        trunk = await self.db.fetchone(
            "SELECT * FROM trunks WHERE (name=? OR id=?) AND enabled=1",
            (trunk_name, trunk_name if trunk_name.isdigit() else -1),
        )
        if not trunk:
            await call.a.server_tx.respond(503, "Service Unavailable")
            await self._end_call(call, "FAILED", "no-trunk")
            return
        # Construir INVITE saliente
        b_leg = RtpLeg(self.rtp, pt=0); await b_leg.open()
        a_leg = RtpLeg(self.rtp, pt=0); await a_leg.open()
        call.a.leg = a_leg; call.b.leg = b_leg
        body = build_audio_offer(self.public_ip, b_leg.local_port).serialize()
        target_uri = SipURI(scheme="sip", user=call.dst_number,
                            host=trunk["host"], port=trunk["port"])
        msg = SipMessage(is_request=True, method="INVITE", request_uri=target_uri)
        from_user = trunk["from_user"] or call.src_number or "anonymous"
        from_dom = trunk["from_domain"] or self.realm
        msg.set("From", str(NameAddr(uri=SipURI(user=from_user, host=from_dom),
                                     parameters={"tag": gen_tag()})))
        msg.set("To", str(NameAddr(uri=target_uri)))
        msg.set("Call-ID", gen_call_id(self.realm))
        msg.set("CSeq", "1 INVITE")
        msg.set("Max-Forwards", "70")
        t = self.transports.get(trunk["transport"]) or self.transports["udp"]
        msg.set("Contact", str(NameAddr(uri=self._build_local_contact(t))))
        v = Via(transport=t.name.upper(), host=self.public_ip, port=t.local_port,
                parameters={"branch": f"z9hG4bK-{secrets.token_hex(6)}", "rport": ""})
        msg.add("Via", str(v))
        msg.set("Content-Type", "application/sdp")
        msg.set("Content-Length", str(len(body))); msg.body = body
        ep = Endpoint(t.name, trunk["host"], trunk["port"])
        call.b.endpoint = ep; call.b.transport = t; call.b.invite_request = msg
        call.via_trunk = trunk["name"]
        self._by_b_callid[msg.call_id] = call.id

        ctx = await self.tx.send_request(msg, ep, t)
        call.b.client_tx = ctx
        try:
            final = await asyncio.wait_for(ctx.wait_final(), timeout=60)
        except asyncio.TimeoutError:
            await call.a.server_tx.respond(408, "Request Timeout")
            await self._end_call(call, "NO_ANSWER", "trunk-timeout"); return
        code = final.status_code or 0
        if code == 401 or code == 407:
            # Reintento con auth
            ah_name = "WWW-Authenticate" if code == 401 else "Proxy-Authenticate"
            challenge = final.get(ah_name)
            if not challenge or not trunk.get("username"):
                await call.a.server_tx.respond(503, "Service Unavailable")
                await self._end_call(call, "FAILED", "trunk-auth-missing"); return
            from .trunk_auth import build_authorization
            auth_value = build_authorization(challenge,
                                             trunk["username"], trunk["password"] or "",
                                             "INVITE", str(target_uri),
                                             call.b.invite_request.body)
            new_msg = msg
            new_msg.set("Authorization" if code == 401 else "Proxy-Authorization", auth_value)
            n, _ = msg.cseq
            new_msg.set("CSeq", f"{n+1} INVITE")
            v2 = Via(transport=t.name.upper(), host=self.public_ip, port=t.local_port,
                     parameters={"branch": f"z9hG4bK-{secrets.token_hex(6)}", "rport": ""})
            new_msg.headers["Via"] = [str(v2)]
            ctx = await self.tx.send_request(new_msg, ep, t)
            call.b.client_tx = ctx
            try:
                final = await asyncio.wait_for(ctx.wait_final(), timeout=60)
            except asyncio.TimeoutError:
                await call.a.server_tx.respond(408); await self._end_call(call, "NO_ANSWER", "trunk-timeout"); return
            code = final.status_code or 0

        if 200 <= code < 300:
            ok = await self._answer_call(call, final)
            if not ok:
                await self._end_call(call, "FAILED", "answer-failed")
            return
        # Fallo
        try: await call.a.server_tx.respond(code, final.reason_phrase or reason_for(code))
        except Exception: pass
        await self._end_call(call, "FAILED", f"trunk-{code}")

    # --------------------- Ring group / Queue / IVR (mínimo funcional) ---------------------

    async def _ring_group(self, call: Call, number: str) -> None:
        rg = await self.db.fetchone("SELECT * FROM ring_groups WHERE number=? AND enabled=1", (number,))
        if not rg:
            await call.a.server_tx.respond(404); await self._end_call(call, "FAILED", "no-rg"); return
        members = [m.strip() for m in (rg["members_csv"] or "").split(",") if m.strip()]
        if rg["strategy"] == "ringall":
            # Probamos paralelo: el primero que conteste, gana.
            tasks = []
            for ext in members:
                bs = self.location.get(f"sip:{ext}@{self.realm}")
                for b in bs:
                    tasks.append(asyncio.create_task(self._fork_one(call, b, rg["ring_seconds"])))
                    break
            if not tasks:
                if rg["no_answer_dest"]:
                    await self._dispatch_simple(call, rg["no_answer_dest"]); return
                await call.a.server_tx.respond(480); await self._end_call(call, "NO_ANSWER", "rg-empty"); return
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for p in pending: p.cancel()
            for d in done:
                if d.result(): return
            if rg["no_answer_dest"]:
                await self._dispatch_simple(call, rg["no_answer_dest"]); return
            await self._end_call(call, "NO_ANSWER", "rg-no-answer")
        else:  # hunt / random
            order = list(members)
            if rg["strategy"] == "random":
                import random as _r; _r.shuffle(order)
            for ext in order:
                bs = self.location.get(f"sip:{ext}@{self.realm}")
                if not bs: continue
                if await self._fork_one(call, bs[0], rg["ring_seconds"]):
                    return
            if rg["no_answer_dest"]:
                await self._dispatch_simple(call, rg["no_answer_dest"]); return
            await self._end_call(call, "NO_ANSWER", "rg-no-answer")

    async def _enqueue(self, call: Call, number: str) -> None:
        from .queue_manager import get_queue_manager
        qm = get_queue_manager(self)
        await qm.enqueue(call, number)

    async def _run_ivr(self, call: Call, number: str) -> None:
        from .ivr_runner import run_ivr
        await run_ivr(self, call, number)

    async def _dispatch_simple(self, call: Call, target: str) -> None:
        """Resuelve un destino tipo 'ext:1001', 'queue:2000', 'voicemail:1001'."""
        if ":" in target:
            t, v = target.split(":", 1)
        else:
            t, v = "extension", target
        if t in ("ext", "extension"): await self._dial_extension(call, v)
        elif t == "voicemail": await self._send_to_voicemail(call, v)
        elif t == "queue": await self._enqueue(call, v)
        elif t == "ivr": await self._run_ivr(call, v)
        elif t == "ringgroup": await self._ring_group(call, v)
        elif t == "trunk": await self._dial_trunk(call, v)
        elif t == "conference": await self._join_conference(call, v)
        elif t == "hangup":
            try: await call.a.server_tx.respond(int(v) if v.isdigit() else 603)
            except Exception: pass
            await self._end_call(call, "FAILED", "hangup")
        else:
            try: await call.a.server_tx.respond(404)
            except Exception: pass
            await self._end_call(call, "FAILED", "unknown-target")

    # --------------------- Helpers ---------------------

    async def _extension_exists(self, num: str) -> bool:
        if not num: return False
        r = await self.db.fetchone("SELECT 1 FROM extensions WHERE number=? AND enabled=1", (num,))
        return r is not None

    def _is_local_user(self, user: str) -> bool:
        return bool(user and user.isdigit() and len(user) <= 6)

    def _is_extension_caller(self, user: str, req: SipMessage) -> bool:
        return self._is_local_user(user)

    async def _creds_for(self, username: str) -> Optional[DigestCredentials]:
        r = await self.db.fetchone(
            "SELECT ha1_md5, sip_password FROM extensions WHERE number=? AND enabled=1",
            (username,),
        )
        if not r:
            return None
        return DigestCredentials(username=username, realm=self.realm,
                                 password=f"ha1:{r['ha1_md5']}")
