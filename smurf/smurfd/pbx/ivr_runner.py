"""Ejecutor de IVRs (Interactive Voice Response) multinivel.

Cada IVR se carga de la tabla `ivrs` con un mapa de opciones DTMF.
Cuando llega una llamada al IVR:
    1) Contesta y envía el saludo (síntesis interna o WAV opcional).
    2) Espera DTMF durante `timeout` segundos.
    3) Si la opción coincide con un mapeo, ejecuta el destino con
       `_dispatch_simple`. Si no, repite o salta a `invalid_dest`.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import TYPE_CHECKING, Optional

from ..rtp.engine import RtpLeg
from ..rtp.packet import DtmfEvent
from ..rtp.sounds import dtmf_tone, busy_tone, congestion_tone
from ..rtp.wavfile import AudioPlayer, load_wav_pcm16
from ..sip.dialog import Dialog
from ..sip.message import make_response
from ..sip.sdp import SDP, negotiate_audio
from ..sip.uri import NameAddr
from ..util.logger import get_logger

if TYPE_CHECKING:
    from .b2bua import B2BUA, Call

log = get_logger("pbx.ivr")


async def run_ivr(b2bua: "B2BUA", call: "Call", number: str) -> None:
    row = await b2bua.db.fetchone("SELECT * FROM ivrs WHERE number=?", (number,))
    if not row:
        await call.a.server_tx.respond(404); await b2bua._end_call(call, "FAILED", "no-ivr"); return
    options = json.loads(row.get("options_json") or "{}")
    timeout = row.get("timeout", 5) or 5
    invalid_dest = row.get("invalid_dest")
    timeout_dest = row.get("timeout_dest")
    greeting_path = row.get("greeting") or ""

    # Contestar
    a_leg = RtpLeg(b2bua.rtp, pt=0); await a_leg.open()
    call.a.leg = a_leg
    try:
        sdp_a = SDP.parse(call.a.invite_request.body)
    except Exception:
        await call.a.server_tx.respond(488); await b2bua._end_call(call, "FAILED", "no-sdp"); return
    am = sdp_a.first_audio()
    a_leg.set_remote(am.connection or sdp_a.connection or call.a.endpoint.host, am.port)
    for pt in am.formats:
        if pt != 101: a_leg.pt = pt; break
    ans = negotiate_audio(sdp_a, b2bua.public_ip, a_leg.local_port)
    ok = make_response(call.a.invite_request, 200, "OK",
                       to_tag=b2bua._uas_tag_for(call),
                       body=ans.serialize(), content_type="application/sdp")
    ok.set("Contact", str(NameAddr(uri=b2bua._build_local_contact(call.a.transport))))
    await call.a.server_tx.send_response(ok)
    call.a.dialog = Dialog.from_uas_2xx(call.a.invite_request, ok,
                                        b2bua._build_local_contact(call.a.transport))
    b2bua._by_dialog[call.a.dialog.id] = call.id

    # DTMF capture
    digit_q: asyncio.Queue[str] = asyncio.Queue()
    def on_dtmf(ev: DtmfEvent):
        if ev.end:
            try: digit_q.put_nowait(ev.char)
            except Exception: pass
    a_leg.on_dtmf = on_dtmf

    # Saludo
    pcm = b""
    if greeting_path and os.path.exists(greeting_path):
        try: pcm = load_wav_pcm16(greeting_path, 8000)
        except Exception: pcm = b""
    if not pcm:
        # Síntesis: emitimos un beep + una secuencia DTMF de bienvenida (1)
        pcm = dtmf_tone("1", 0.3, 0.2)
    AudioPlayer(a_leg, pcm).start()
    await asyncio.sleep(min(3.0, len(pcm) / 16000.0))

    while True:
        # Limpia eventos previos
        while not digit_q.empty():
            try: digit_q.get_nowait()
            except Exception: break
        try:
            digit = await asyncio.wait_for(digit_q.get(), timeout=timeout)
        except asyncio.TimeoutError:
            digit = None
        if digit is None:
            if timeout_dest:
                await b2bua._dispatch_simple(call, timeout_dest); return
            await b2bua._end_call(call, "NO_ANSWER", "ivr-timeout"); return
        target = options.get(digit)
        if not target:
            AudioPlayer(a_leg, congestion_tone(1.0)).start()
            await asyncio.sleep(1.2)
            continue
        await b2bua._dispatch_simple(call, target)
        return
