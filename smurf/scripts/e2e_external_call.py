"""End-to-end real: una extensión registrada llama a `test.echo@sip5060.net`
a través del trunk configurado en SMURF, intercambia RTP G.711 µ-law en
ambos sentidos durante unos segundos y cuelga.

El audio que enviamos es un tono dial sintético (350+440 Hz) generado por
SMURF. El servidor remoto (Asterisk de sip5060.net) lo eco-replica, así
que oiremos el mismo tono de vuelta. SMURF graba la llamada en estéreo
(left=enviado, right=recibido) en su carpeta de grabaciones.

Imprime al final la ruta del WAV grabado para descargarlo y validarlo.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import socket
import struct
import sys
import time

sys.path.insert(0, "/workspace/smurf")

from smurfd.rtp.codecs import encode_from_pcm16, samples_per_frame
from smurfd.rtp.packet import RtpPacket
from smurfd.rtp.sounds import dial_tone, dtmf_tone, _silence
from smurfd.sip.auth import parse_auth_header
from smurfd.sip.message import SipMessage, make_response
from smurfd.sip.sdp import SDP
from smurfd.sip.uri import NameAddr, SipURI


SIP_HOST = "127.0.0.1"
SIP_PORT = 5060
REALM = "smurf.local"
EXT = "1000"
PWD = "xD1fb3s3mMGVKg"
TARGET = "music"  # → trunk antisip → music@sip.antisip.com (música de espera infinita)


def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def digest_response(challenge: str, method: str, ruri: str) -> str:
    _, p = parse_auth_header(challenge)
    nonce, realm, algo = p["nonce"], p["realm"], p.get("algorithm", "MD5")
    qop = "auth" if "auth" in p.get("qop", "") else p.get("qop", "")
    cnonce, nc = secrets.token_hex(8), "00000001"
    ha1 = md5(f"{EXT}:{realm}:{PWD}")
    ha2 = md5(f"{method}:{ruri}")
    if qop:
        resp = md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
    else:
        resp = md5(f"{ha1}:{nonce}:{ha2}")
    out = (f'Digest username="{EXT}", realm="{realm}", nonce="{nonce}", uri="{ruri}", '
           f'algorithm={algo}, response="{resp}"')
    if qop:
        out += f', qop={qop}, nc={nc}, cnonce="{cnonce}"'
    if "opaque" in p:
        out += f', opaque="{p["opaque"]}"'
    return out


class SimpleUA:
    def __init__(self, ext: str):
        self.ext = ext
        self.tag = secrets.token_hex(4)
        self.cseq = 0
        self.call_id = f"{secrets.token_hex(10)}@e2e.test"
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.settimeout(8.0)
        self.host, self.port = self._lan_addr()
        # RTP socket
        self.rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp_sock.bind(("0.0.0.0", 0))
        self.rtp_sock.settimeout(0.05)
        self.rtp_local_port = self.rtp_sock.getsockname()[1]
        # RTP state
        self.rtp_seq = secrets.randbelow(0xFFFF)
        self.rtp_ts = secrets.randbelow(0xFFFFFFFF)
        self.rtp_ssrc = secrets.randbelow(0xFFFFFFFF)
        self.remote_rtp = None
        self.recvd_pkts = 0

    def _lan_addr(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((SIP_HOST, 53))
            ip = s.getsockname()[0]
        finally:
            s.close()
        return ip, self.sock.getsockname()[1]

    def close(self):
        try: self.sock.close()
        except Exception: pass
        try: self.rtp_sock.close()
        except Exception: pass

    def _branch(self): return "z9hG4bK-" + secrets.token_hex(6)

    def send(self, msg, dest=None):
        self.sock.sendto(bytes(msg), dest or (SIP_HOST, SIP_PORT))

    def recv(self, t=4.0):
        self.sock.settimeout(t)
        data, _ = self.sock.recvfrom(65535)
        return SipMessage.parse(data)

    def _common(self, method, ruri, call_id=None, cseq=None, body=b""):
        m = SipMessage(is_request=True, method=method, request_uri=ruri)
        m.add("Via", f"SIP/2.0/UDP {self.host}:{self.port};branch={self._branch()};rport")
        m.set("From", str(NameAddr(uri=SipURI(user=self.ext, host=REALM),
                                   parameters={"tag": self.tag})))
        m.set("To", str(NameAddr(uri=SipURI(user=ruri.user, host=REALM)))
              if method != "REGISTER" else
              str(NameAddr(uri=SipURI(user=self.ext, host=REALM))))
        self.cseq = cseq or (self.cseq + 1)
        m.set("Call-ID", call_id or self.call_id)
        m.set("CSeq", f"{self.cseq} {method}")
        m.set("Max-Forwards", "70")
        m.set("Contact", str(NameAddr(uri=SipURI(user=self.ext, host=self.host, port=self.port))))
        m.set("User-Agent", "SMURF-E2E-Test/1.0")
        m.set("Content-Length", str(len(body)))
        m.body = body
        return m

    def register(self):
        ruri = SipURI(scheme="sip", host=REALM)
        m = self._common("REGISTER", ruri); m.set("Expires", "120")
        self.send(m)
        r = self.recv()
        assert r.status_code == 401, f"register 1st step: {r.status_code}"
        ch = r.get("WWW-Authenticate")
        m2 = self._common("REGISTER", ruri, call_id=self.call_id, cseq=self.cseq + 1)
        m2.set("Expires", "120")
        m2.set("Authorization", digest_response(ch, "REGISTER", str(ruri)))
        self.send(m2)
        r2 = self.recv()
        assert r2.status_code == 200, f"register 2nd: {r2.status_code} {r2.reason_phrase}"
        print(f"[REGISTER] {self.ext} OK")

    def make_sdp(self):
        # Anunciamos 127.0.0.1 para que SMURF (mismo host) envíe el RTP de
        # vuelta vía loopback, alcanzable desde nuestro UA.
        sdp_ip = "127.0.0.1"
        return (
            f"v=0\r\n"
            f"o=test 1 1 IN IP4 {sdp_ip}\r\n"
            f"s=e2e\r\n"
            f"c=IN IP4 {sdp_ip}\r\n"
            f"t=0 0\r\n"
            f"m=audio {self.rtp_local_port} RTP/AVP 0 8 101\r\n"
            f"a=rtpmap:0 PCMU/8000\r\n"
            f"a=rtpmap:8 PCMA/8000\r\n"
            f"a=rtpmap:101 telephone-event/8000\r\n"
            f"a=fmtp:101 0-16\r\n"
            f"a=ptime:20\r\n"
            f"a=sendrecv\r\n"
        ).encode()

    def call(self, target):
        ruri = SipURI(scheme="sip", user=target, host=REALM)
        sdp = self.make_sdp()
        inv = self._common("INVITE", ruri, body=sdp)
        inv.set("Content-Type", "application/sdp")
        self.send(inv)
        responses = []
        last_inv = inv
        while True:
            r = self.recv(15.0)
            print(f"[CALL] respuesta {r.status_code} {r.reason_phrase}")
            if r.status_code == 100:
                continue
            if r.status_code in (401, 407):
                ch_h = "Proxy-Authenticate" if r.status_code == 407 else "WWW-Authenticate"
                ch = r.get(ch_h)
                # ACK al 4xx
                ack = SipMessage(is_request=True, method="ACK", request_uri=ruri)
                ack.add("Via", inv.get("Via"))
                ack.set("From", inv.get("From")); ack.set("To", r.get("To"))
                ack.set("Call-ID", inv.call_id)
                n,_ = inv.cseq; ack.set("CSeq", f"{n} ACK")
                ack.set("Max-Forwards","70"); ack.set("Content-Length","0")
                self.send(ack)
                # Reintentar con auth
                inv = self._common("INVITE", ruri, call_id=self.call_id,
                                   cseq=self.cseq+1, body=sdp)
                inv.set("Content-Type", "application/sdp")
                hname = "Proxy-Authorization" if r.status_code == 407 else "Authorization"
                inv.set(hname, digest_response(ch, "INVITE", str(ruri)))
                self.send(inv); last_inv = inv
                continue
            if 100 < r.status_code < 200:
                continue
            return last_inv, r

    def ack(self, inv, resp):
        contacts = resp.contacts()
        ruri = contacts[0].uri if contacts else inv.request_uri
        ack = SipMessage(is_request=True, method="ACK",
                         request_uri=SipURI.parse(str(ruri)))
        ack.add("Via", f"SIP/2.0/UDP {self.host}:{self.port};branch={self._branch()}")
        ack.set("From", inv.get("From")); ack.set("To", resp.get("To"))
        ack.set("Call-ID", inv.call_id)
        n,_ = inv.cseq; ack.set("CSeq", f"{n} ACK")
        ack.set("Max-Forwards","70"); ack.set("Content-Length","0")
        self.send(ack)

    def bye(self, inv, resp):
        contacts = resp.contacts()
        ruri = contacts[0].uri if contacts else inv.request_uri
        bye = SipMessage(is_request=True, method="BYE",
                         request_uri=SipURI.parse(str(ruri)))
        bye.add("Via", f"SIP/2.0/UDP {self.host}:{self.port};branch={self._branch()}")
        bye.set("From", inv.get("From")); bye.set("To", resp.get("To"))
        bye.set("Call-ID", inv.call_id)
        n,_ = inv.cseq; bye.set("CSeq", f"{n+1} BYE")
        bye.set("Max-Forwards","70"); bye.set("Content-Length","0")
        self.send(bye)

    def parse_remote_rtp(self, resp):
        sdp = SDP.parse(resp.body)
        m = sdp.first_audio()
        ip = m.connection or sdp.connection
        # En este host SMURF anuncia su public_ip (NAT) que no es alcanzable
        # localmente. Como SMURF escucha en 0.0.0.0, usamos 127.0.0.1 en su lugar.
        if ip and not ip.startswith("127."):
            ip = "127.0.0.1"
        return (ip, m.port)

    def send_rtp_burst(self, duration_s=6.0):
        """Envía un tono dial PCMU al peer durante N segundos y captura lo
        que llegue de vuelta (eco) para verificar audio bidireccional."""
        if not self.remote_rtp:
            print("[RTP] no hay destino RTP remoto"); return
        print(f"[RTP] enviando hacia {self.remote_rtp}, recibiendo en :{self.rtp_local_port}")
        # Generamos 1s de tono dial repetido
        tone = dial_tone(1.0)  # PCM 16 bit 8 kHz
        bpf = samples_per_frame(0, 20) * 2
        frames = []
        i = 0
        while i + bpf <= len(tone):
            frames.append(tone[i:i+bpf]); i += bpf
        total_frames = int(duration_s * 50)  # 50 frames/s
        sent = 0
        recv_bytes = 0
        next_t = time.time()
        ok_remote = None
        for k in range(total_frames):
            chunk = frames[k % len(frames)]
            payload = encode_from_pcm16(chunk, 0)
            self.rtp_seq = (self.rtp_seq + 1) & 0xFFFF
            self.rtp_ts  = (self.rtp_ts + 160) & 0xFFFFFFFF
            pkt = RtpPacket(payload_type=0, sequence=self.rtp_seq,
                            timestamp=self.rtp_ts, ssrc=self.rtp_ssrc, payload=payload)
            try:
                self.rtp_sock.sendto(pkt.serialize(), self.remote_rtp); sent += 1
            except Exception as e:
                print(f"[RTP] send err: {e}"); break
            # Drenar todo lo que haya llegado
            try:
                while True:
                    data, addr = self.rtp_sock.recvfrom(2048)
                    recv_bytes += len(data); self.recvd_pkts += 1
                    if ok_remote is None:
                        ok_remote = addr
                        print(f"[RTP] primer paquete recibido desde {addr}, seq/ssrc detectado")
            except socket.timeout:
                pass
            except BlockingIOError:
                pass
            next_t += 0.020
            sl = next_t - time.time()
            if sl > 0: time.sleep(sl)
        print(f"[RTP] enviados={sent} paquetes, recibidos={self.recvd_pkts} paquetes ({recv_bytes} bytes)")


def main():
    ua = SimpleUA(EXT)
    try:
        print(f"=== UA local en {ua.host}:{ua.port}, RTP local :{ua.rtp_local_port} ===")
        ua.register()
        time.sleep(0.5)

        print(f"=== Llamando a {TARGET} (debe enrutar al trunk sip5060) ===")
        inv, final = ua.call(TARGET)
        if final.status_code != 200:
            print(f"[FAIL] llamada no contestada: {final.status_code}")
            return 1
        print(f"=== 200 OK recibido. Contestada por servidor remoto. ===")
        ua.remote_rtp = ua.parse_remote_rtp(final)
        print(f"=== Remote RTP advertised by SMURF: {ua.remote_rtp} ===")
        ua.ack(inv, final)
        time.sleep(0.5)

        # 10 segundos de audio: enviamos un tono y recibimos la música del servidor remoto
        ua.send_rtp_burst(duration_s=10.0)

        print("=== Colgando (BYE) ===")
        ua.bye(inv, final)
        try:
            r = ua.recv(3.0)
            print(f"[BYE] respuesta {r.status_code}")
        except Exception:
            pass

        # Esperar a que se cierre la grabación
        time.sleep(2)
        print()
        print("=== Buscando grabaciones generadas ===")
        rec_dir = "/workspace/smurf/.tmp/recordings"
        if os.path.isdir(rec_dir):
            for f in sorted(os.listdir(rec_dir))[-3:]:
                p = os.path.join(rec_dir, f); sz = os.path.getsize(p)
                print(f"  {p}  ({sz} bytes)")
        return 0 if ua.recvd_pkts > 0 else 2
    finally:
        ua.close()


if __name__ == "__main__":
    sys.exit(main())
