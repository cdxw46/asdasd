"""Test funcional: REGISTER + INVITE entre dos extensiones por UDP.

Lanza el servidor en localhost con puertos altos y simula dos UAs (1000 y
1001). 1000 marca a 1001, recibe 200 OK, intercambian RTP unos segundos y
cuelgan con BYE.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import secrets
import struct
import sys
import tempfile
import time
import unittest

# Ajusta path para ejecutar desde repo
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from smurfd.sip.message import SipMessage
from smurfd.sip.uri import SipURI, NameAddr
from smurfd.sip.auth import parse_auth_header
from smurfd.util.config import (RtpConfig, SipConfig, SmurfConfig,
                                 StorageConfig, WebConfig, SecurityConfig)
from smurfd.server import SmurfServer
from smurfd.db.database import _ha1


def _ha1_md5(u, r, p): return _ha1(u, r, p, "MD5")


class FakeUA:
    """Mini-UA UDP para los tests."""
    def __init__(self, ext: str, password: str, server_host: str, server_port: int,
                 realm: str):
        self.ext = ext
        self.password = password
        self.server = (server_host, server_port)
        self.realm = realm
        self.tag = secrets.token_hex(4)
        self.cseq = 0
        self.call_id = f"{secrets.token_hex(8)}@{ext}.test"
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(8.0)
        self.host, self.port = self.sock.getsockname()

    def close(self):
        try: self.sock.close()
        except Exception: pass

    def _branch(self): return "z9hG4bK-" + secrets.token_hex(6)

    def send(self, msg: SipMessage, dest=None):
        self.sock.sendto(bytes(msg), dest or self.server)

    def recv(self, timeout=4.0) -> SipMessage:
        self.sock.settimeout(timeout)
        data, _ = self.sock.recvfrom(65535)
        return SipMessage.parse(data)

    def _common(self, method: str, ruri: SipURI, call_id=None, cseq=None,
                tag_to: str = "", body: bytes = b""):
        m = SipMessage(is_request=True, method=method, request_uri=ruri)
        m.add("Via", f"SIP/2.0/UDP {self.host}:{self.port};branch={self._branch()};rport")
        m.set("From", str(NameAddr(uri=SipURI(user=self.ext, host=self.realm),
                                   parameters={"tag": self.tag})))
        to_uri = ruri
        to = NameAddr(uri=SipURI(user=ruri.user, host=self.realm))
        if tag_to:
            to.parameters["tag"] = tag_to
        m.set("To", str(to))
        self.cseq = cseq or (self.cseq + 1)
        m.set("Call-ID", call_id or self.call_id)
        m.set("CSeq", f"{self.cseq} {method}")
        m.set("Max-Forwards", "70")
        m.set("Contact", str(NameAddr(uri=SipURI(user=self.ext, host=self.host, port=self.port))))
        m.set("User-Agent", "test-ua")
        m.set("Content-Length", str(len(body)))
        m.body = body
        return m

    def register(self) -> bool:
        ruri = SipURI(scheme="sip", host=self.realm)
        m = self._common("REGISTER", ruri)
        # En REGISTER, el AOR está en el To (no en R-URI)
        m.set("To", str(NameAddr(uri=SipURI(user=self.ext, host=self.realm))))
        m.set("Expires", "60")
        self.send(m)
        r = self.recv()
        assert r.status_code == 401, f"esperaba 401, vino {r.status_code}"
        ch = r.get("WWW-Authenticate")
        assert ch
        scheme, p = parse_auth_header(ch)
        nonce = p["nonce"]; realm = p["realm"]; algo = p.get("algorithm","MD5"); qop = p.get("qop","auth")
        cnonce = secrets.token_hex(8); nc = "00000001"
        ha1 = _ha1_md5(self.ext, realm, self.password)
        ha2 = hashlib.md5(f"REGISTER:{ruri}".encode()).hexdigest()
        if qop:
            response = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}".encode()).hexdigest()
        else:
            response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        m2 = self._common("REGISTER", ruri, call_id=self.call_id, cseq=self.cseq + 1)
        m2.set("To", str(NameAddr(uri=SipURI(user=self.ext, host=self.realm))))
        m2.set("Expires", "60")
        auth = (f'Digest username="{self.ext}", realm="{realm}", nonce="{nonce}", uri="{ruri}", '
                f'algorithm={algo}, response="{response}", qop=auth, nc={nc}, cnonce="{cnonce}"')
        m2.set("Authorization", auth)
        self.send(m2)
        r2 = self.recv()
        return r2.status_code == 200

    def invite(self, dst: str, sdp: bytes) -> SipMessage:
        ruri = SipURI(scheme="sip", user=dst, host=self.realm)
        m = self._common("INVITE", ruri, body=sdp)
        m.set("Content-Type", "application/sdp")
        self.send(m)
        return m

    def _digest_response(self, challenge: str, method: str, ruri: str, body: bytes = b""):
        scheme, p = parse_auth_header(challenge)
        nonce = p["nonce"]; realm = p["realm"]; algo = p.get("algorithm","MD5")
        qop_field = p.get("qop","")
        qop = "auth" if "auth" in qop_field else qop_field
        cnonce = secrets.token_hex(8); nc = "00000001"
        ha1 = _ha1_md5(self.ext, realm, self.password)
        ha2 = hashlib.md5(f"{method}:{ruri}".encode()).hexdigest()
        if qop:
            response = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()).hexdigest()
        else:
            response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        out = (f'Digest username="{self.ext}", realm="{realm}", nonce="{nonce}", uri="{ruri}", '
               f'algorithm={algo}, response="{response}"')
        if qop:
            out += f', qop={qop}, nc={nc}, cnonce="{cnonce}"'
        if "opaque" in p:
            out += f', opaque="{p["opaque"]}"'
        return out

    def invite_with_auth(self, dst: str, sdp: bytes) -> tuple:
        """Envía INVITE, responde a 407 si llega, y devuelve (request_final, lista_responses_de_la_última_tx)."""
        inv = self.invite(dst, sdp)
        responses = []
        while True:
            r = self.recv(8.0)
            if r.status_code == 100:
                responses.append(r)
                continue
            if r.status_code in (401, 407):
                responses = []  # nueva transacción tras auth
                pass
            else:
                responses.append(r)
            if r.status_code in (401, 407):
                ack_header = "Proxy-Authenticate" if r.status_code == 407 else "WWW-Authenticate"
                challenge = r.get(ack_header)
                # ACK al 407/401
                ack = SipMessage(is_request=True, method="ACK", request_uri=inv.request_uri)
                ack.add("Via", inv.get("Via"))
                ack.set("From", inv.get("From"))
                ack.set("To", r.get("To"))
                ack.set("Call-ID", inv.call_id)
                n, _ = inv.cseq
                ack.set("CSeq", f"{n} ACK")
                ack.set("Max-Forwards", "70")
                ack.set("Content-Length", "0")
                self.send(ack)
                # Reintentar con auth, nuevo CSeq, mismo Call-ID
                ruri_str = str(inv.request_uri)
                auth_value = self._digest_response(challenge, "INVITE", ruri_str, sdp)
                inv = self._common("INVITE", inv.request_uri, call_id=self.call_id,
                                   cseq=self.cseq + 1, body=sdp)
                inv.set("Content-Type", "application/sdp")
                inv.set("Proxy-Authorization" if r.status_code == 407 else "Authorization",
                        auth_value)
                self.send(inv)
                continue
            if r.status_code and r.status_code >= 200:
                return inv, responses
            # provisional 18x → seguimos esperando final

    def ack(self, original: SipMessage, response: SipMessage):
        ruri = original.request_uri
        m = SipMessage(is_request=True, method="ACK", request_uri=ruri)
        m.add("Via", f"SIP/2.0/UDP {self.host}:{self.port};branch={self._branch()}")
        m.set("From", original.get("From"))
        m.set("To", response.get("To"))
        m.set("Call-ID", original.call_id)
        n, _ = original.cseq
        m.set("CSeq", f"{n} ACK")
        m.set("Max-Forwards", "70")
        m.set("Content-Length", "0")
        self.send(m)

    def bye(self, original_req: SipMessage, response: SipMessage):
        # Construye BYE básico hacia el contact del remoto
        contacts_in_resp = response.contacts()
        ruri = contacts_in_resp[0].uri if contacts_in_resp else original_req.request_uri
        m = SipMessage(is_request=True, method="BYE", request_uri=ruri)
        m.add("Via", f"SIP/2.0/UDP {self.host}:{self.port};branch={self._branch()}")
        m.set("From", original_req.get("From"))
        m.set("To", response.get("To"))
        m.set("Call-ID", original_req.call_id)
        n, _ = original_req.cseq
        m.set("CSeq", f"{n+1} BYE")
        m.set("Max-Forwards", "70")
        m.set("Content-Length", "0")
        self.send(m)


def make_offer_sdp(ip: str, port: int) -> bytes:
    return (
        f"v=0\r\n"
        f"o=test 1 1 IN IP4 {ip}\r\n"
        f"s=test\r\n"
        f"c=IN IP4 {ip}\r\n"
        f"t=0 0\r\n"
        f"m=audio {port} RTP/AVP 0 8 101\r\n"
        f"a=rtpmap:0 PCMU/8000\r\n"
        f"a=rtpmap:8 PCMA/8000\r\n"
        f"a=rtpmap:101 telephone-event/8000\r\n"
        f"a=fmtp:101 0-16\r\n"
        f"a=ptime:20\r\n"
        f"a=sendrecv\r\n"
    ).encode()


class FullCallTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp(prefix="smurf-test-")
        from smurfd.util.logger import setup_logging
        setup_logging("DEBUG")
        cfg = SmurfConfig(
            log_level="DEBUG",
            sip=SipConfig(udp_bind="127.0.0.1", udp_port=15060,
                          tcp_port=0, tls_port=0, ws_port=0, wss_port=0,
                          public_ip="127.0.0.1", realm="smurf.test"),
            rtp=RtpConfig(bind="127.0.0.1", port_min=40000, port_max=40500),
            web=WebConfig(http_port=0, https_port=0),
            storage=StorageConfig(
                db_path=os.path.join(self.tmp, "smurf.db"),
                recordings_dir=os.path.join(self.tmp, "rec"),
                voicemail_dir=os.path.join(self.tmp, "vm"),
                sounds_dir=os.path.join(self.tmp, "snd"),
                provisioning_dir=os.path.join(self.tmp, "prov"),
                log_dir=os.path.join(self.tmp, "log"),
            ),
            security=SecurityConfig(),
        )
        # Forzar singleton DB nuevo
        import smurfd.db.database as dbmod
        dbmod._INSTANCE = None
        self.server = SmurfServer(cfg)
        await self.server.start()
        # Recoger passwords generadas
        rows = await self.server.db.fetchall("SELECT number, sip_password FROM extensions")
        self.creds = {r["number"]: r["sip_password"] for r in rows}

    async def asyncTearDown(self):
        await self.server.stop()
        import smurfd.db.database as dbmod
        dbmod._INSTANCE = None

    async def test_register_invite_bye(self):
        ua_a = FakeUA("1000", self.creds["1000"], "127.0.0.1", 15060, "smurf.test")
        ua_b = FakeUA("1001", self.creds["1001"], "127.0.0.1", 15060, "smurf.test")
        try:
            assert await asyncio.get_event_loop().run_in_executor(None, ua_a.register), "A reg failed"
            assert await asyncio.get_event_loop().run_in_executor(None, ua_b.register), "B reg failed"
            await asyncio.sleep(0.1)
            # Debe haber 2 bindings
            self.assertEqual(len(self.server.location.all()), 2)

            # A llama a B (con auth si hace falta)
            sdp_offer_a = make_offer_sdp(ua_a.host, 41001)

            # Lanzamos la INVITE+auth en hilo y al mismo tiempo escuchamos a B
            loop = asyncio.get_event_loop()
            invite_task = loop.run_in_executor(None, lambda: ua_a.invite_with_auth("1001", sdp_offer_a))

            # B debería recibir el INVITE forkeado (puede tardar varios round-trips por auth)
            req_to_b = None
            for _ in range(3):
                try:
                    r = await loop.run_in_executor(None, lambda: ua_b.recv(8.0))
                except socket.timeout:
                    continue
                if r.is_request and r.method == "INVITE":
                    req_to_b = r
                    break

            self.assertIsNotNone(req_to_b, "INVITE no reenviada a B en tiempo")
            from smurfd.sip.message import make_response
            tag = secrets.token_hex(4)
            ringing = make_response(req_to_b, 180, "Ringing", to_tag=tag); ua_b.send(ringing)
            await asyncio.sleep(0.2)
            ans_sdp = make_offer_sdp(ua_b.host, 41101)
            ok = make_response(req_to_b, 200, "OK", to_tag=tag,
                               body=ans_sdp, content_type="application/sdp")
            ok.set("Contact", str(NameAddr(uri=SipURI(user="1001", host=ua_b.host, port=ua_b.port))))
            ua_b.send(ok)
            # B debe recibir el ACK del B2BUA al 200
            try:
                ack_b = await loop.run_in_executor(None, lambda: ua_b.recv(4.0))
                self.assertEqual(ack_b.method, "ACK")
            except socket.timeout:
                pass

            inv, responses = await invite_task
            final = next((r for r in responses if r.status_code and r.status_code >= 200), None)
            self.assertIsNotNone(final, "A no recibió respuesta final")
            self.assertEqual(final.status_code, 200, f"esperado 200 OK, fue {final.status_code}")

            # A envía ACK y BYE
            ua_a.ack(inv, final)
            await asyncio.sleep(0.3)
            ua_a.bye(inv, final)
            try:
                bye_resp = await loop.run_in_executor(None, lambda: ua_a.recv(4.0))
                self.assertEqual(bye_resp.status_code, 200)
            except socket.timeout:
                pass
            try:
                bye_b = await loop.run_in_executor(None, lambda: ua_b.recv(4.0))
                self.assertEqual(bye_b.method, "BYE")
            except socket.timeout:
                pass
        finally:
            ua_a.close(); ua_b.close()


if __name__ == "__main__":
    unittest.main()
