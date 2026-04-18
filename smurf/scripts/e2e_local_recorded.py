"""End-to-end LOCAL real con grabación: dos UAs reales se registran en SMURF,
1000 llama a 1001, ambos intercambian RTP G.711 µ-law durante 6 segundos,
SMURF graba la llamada en estéreo (left=enviado por A, right=enviado por B)
y el script descarga la grabación al final.

Demuestra todo el camino:
    - REGISTER + digest auth (ambos UAs)
    - INVITE + auth + B2BUA fork
    - 180/200 + ACK con SDP en ambos lados
    - RTP bidireccional con dos tonos distintos (440 Hz desde A, 880 Hz desde B)
    - BYE + propagación al peer
    - CDR persistido + WAV de grabación verificable
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import socket
import struct
import sys
import threading
import time
import wave

sys.path.insert(0, "/workspace/smurf")

from smurfd.rtp.codecs import encode_from_pcm16, samples_per_frame
from smurfd.rtp.packet import RtpPacket
from smurfd.sip.auth import parse_auth_header
from smurfd.sip.message import SipMessage, make_response
from smurfd.sip.sdp import SDP
from smurfd.sip.uri import NameAddr, SipURI
import math

SIP_HOST = "127.0.0.1"
SIP_PORT = 5060
REALM = "smurf.local"


def md5(s): return hashlib.md5(s.encode()).hexdigest()


def digest(challenge, ext, pwd, method, ruri):
    _, p = parse_auth_header(challenge)
    nonce, realm, algo = p["nonce"], p["realm"], p.get("algorithm", "MD5")
    qop = "auth" if "auth" in p.get("qop", "") else p.get("qop", "")
    cnonce, nc = secrets.token_hex(8), "00000001"
    ha1 = md5(f"{ext}:{realm}:{pwd}")
    ha2 = md5(f"{method}:{ruri}")
    if qop:
        resp = md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
    else:
        resp = md5(f"{ha1}:{nonce}:{ha2}")
    out = (f'Digest username="{ext}", realm="{realm}", nonce="{nonce}", uri="{ruri}", '
           f'algorithm={algo}, response="{resp}"')
    if qop: out += f', qop={qop}, nc={nc}, cnonce="{cnonce}"'
    if "opaque" in p: out += f', opaque="{p["opaque"]}"'
    return out


def gen_tone(freq, dur_s=1.0, sample_rate=8000, amp=0.25):
    n = int(sample_rate * dur_s); out = bytearray(n * 2)
    for i in range(n):
        v = int(amp * 32767 * math.sin(2*math.pi*freq*(i/sample_rate)))
        struct.pack_into("<h", out, i*2, v)
    return bytes(out)


class UA:
    def __init__(self, ext, pwd):
        self.ext = ext; self.pwd = pwd
        self.tag = secrets.token_hex(4)
        self.cseq = 0
        self.call_id = secrets.token_hex(10) + "@e2e.test"
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0)); self.sock.settimeout(5)
        self.host, self.port = self.sock.getsockname()
        self.rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp.bind(("127.0.0.1", 0)); self.rtp.settimeout(0.05)
        self.rtp_port = self.rtp.getsockname()[1]
        self.rtp_seq = secrets.randbelow(0xFFFF)
        self.rtp_ts = secrets.randbelow(0xFFFFFFFF)
        self.rtp_ssrc = secrets.randbelow(0xFFFFFFFF)
        self.remote_rtp = None
        self.recvd = 0
        self._inbox = []
        self._lock = threading.Lock()
        self._stop = False
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _reader(self):
        while not self._stop:
            try:
                data, addr = self.sock.recvfrom(65535)
                with self._lock:
                    self._inbox.append(SipMessage.parse(data))
            except socket.timeout:
                continue
            except Exception:
                if self._stop: return

    def close(self):
        self._stop = True
        try: self.sock.close()
        except Exception: pass
        try: self.rtp.close()
        except Exception: pass

    def send(self, msg):
        self.sock.sendto(bytes(msg), (SIP_HOST, SIP_PORT))

    def wait_for(self, predicate, timeout=8.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                for i, m in enumerate(self._inbox):
                    if predicate(m):
                        return self._inbox.pop(i)
            time.sleep(0.02)
        return None

    def wait_response(self, cseq_method, timeout=8.0):
        return self.wait_for(lambda m: not m.is_request and m.cseq[1] == cseq_method, timeout)

    def wait_request(self, method, timeout=8.0):
        return self.wait_for(lambda m: m.is_request and m.method == method, timeout)

    def _branch(self): return "z9hG4bK-" + secrets.token_hex(6)

    def _common(self, method, ruri, call_id=None, cseq=None, body=b""):
        m = SipMessage(is_request=True, method=method, request_uri=ruri)
        m.add("Via", f"SIP/2.0/UDP {self.host}:{self.port};branch={self._branch()};rport")
        m.set("From", str(NameAddr(uri=SipURI(user=self.ext, host=REALM),
                                   parameters={"tag": self.tag})))
        if method == "REGISTER":
            m.set("To", str(NameAddr(uri=SipURI(user=self.ext, host=REALM))))
        else:
            m.set("To", str(NameAddr(uri=SipURI(user=ruri.user, host=REALM))))
        self.cseq = cseq or (self.cseq + 1)
        m.set("Call-ID", call_id or self.call_id)
        m.set("CSeq", f"{self.cseq} {method}")
        m.set("Max-Forwards", "70")
        m.set("Contact", str(NameAddr(uri=SipURI(user=self.ext, host=self.host, port=self.port))))
        m.set("User-Agent", "SMURF-E2E/1.0")
        m.set("Content-Length", str(len(body)))
        m.body = body
        return m

    def register(self):
        ruri = SipURI(scheme="sip", host=REALM)
        m = self._common("REGISTER", ruri); m.set("Expires", "120")
        self.send(m)
        r = self.wait_response("REGISTER", 4)
        if not r or r.status_code != 401: raise RuntimeError(f"reg 1: {r and r.status_code}")
        m2 = self._common("REGISTER", ruri, call_id=self.call_id, cseq=self.cseq+1)
        m2.set("Expires", "120")
        m2.set("Authorization", digest(r.get("WWW-Authenticate"), self.ext, self.pwd, "REGISTER", str(ruri)))
        self.send(m2)
        r2 = self.wait_response("REGISTER", 4)
        if not r2 or r2.status_code != 200: raise RuntimeError(f"reg 2: {r2 and r2.status_code}")
        print(f"  [{self.ext}] REGISTER OK")

    def make_sdp(self):
        return (
            f"v=0\r\no=test 1 1 IN IP4 127.0.0.1\r\ns=e2e\r\n"
            f"c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
            f"m=audio {self.rtp_port} RTP/AVP 0 8 101\r\n"
            f"a=rtpmap:0 PCMU/8000\r\na=rtpmap:8 PCMA/8000\r\n"
            f"a=rtpmap:101 telephone-event/8000\r\na=fmtp:101 0-16\r\n"
            f"a=ptime:20\r\na=sendrecv\r\n"
        ).encode()

    def call(self, target):
        ruri = SipURI(scheme="sip", user=target, host=REALM)
        sdp = self.make_sdp()
        inv = self._common("INVITE", ruri, body=sdp)
        inv.set("Content-Type", "application/sdp")
        self.send(inv)
        last_inv = inv
        while True:
            r = self.wait_response("INVITE", 12)
            if r is None: raise RuntimeError("call timeout")
            print(f"  [{self.ext}] CALL ← {r.status_code} {r.reason_phrase}")
            if r.status_code == 100: continue
            if r.status_code in (401, 407):
                ch_h = "Proxy-Authenticate" if r.status_code == 407 else "WWW-Authenticate"
                # ACK al 4xx
                ack = SipMessage(is_request=True, method="ACK", request_uri=ruri)
                ack.add("Via", inv.get("Via"))
                ack.set("From", inv.get("From")); ack.set("To", r.get("To"))
                ack.set("Call-ID", inv.call_id)
                n,_ = inv.cseq; ack.set("CSeq", f"{n} ACK")
                ack.set("Max-Forwards","70"); ack.set("Content-Length","0")
                self.send(ack)
                inv = self._common("INVITE", ruri, call_id=self.call_id, cseq=self.cseq+1, body=sdp)
                inv.set("Content-Type", "application/sdp")
                hname = "Proxy-Authorization" if r.status_code == 407 else "Authorization"
                inv.set(hname, digest(r.get(ch_h), self.ext, self.pwd, "INVITE", str(ruri)))
                self.send(inv); last_inv = inv
                continue
            if 100 < r.status_code < 200: continue
            return last_inv, r

    def answer_incoming(self):
        """Espera un INVITE entrante y contesta 180+200."""
        inv = self.wait_request("INVITE", 8)
        if not inv:
            print(f"  [{self.ext}] no llegó INVITE entrante"); return None
        print(f"  [{self.ext}] INVITE entrante recibido")
        # 180 Ringing
        ring = make_response(inv, 180, "Ringing", to_tag=self.tag)
        ring.set("Contact", str(NameAddr(uri=SipURI(user=self.ext, host=self.host, port=self.port))))
        self.send_to_caller(inv, ring)
        time.sleep(0.3)
        # 200 OK
        sdp = self.make_sdp()
        ok = make_response(inv, 200, "OK", to_tag=self.tag, body=sdp, content_type="application/sdp")
        ok.set("Contact", str(NameAddr(uri=SipURI(user=self.ext, host=self.host, port=self.port))))
        self.send_to_caller(inv, ok)
        # Esperar ACK
        ack = self.wait_request("ACK", 4)
        if ack: print(f"  [{self.ext}] ACK recibido")
        # parsear SDP entrante
        try:
            sdp_in = SDP.parse(inv.body); m = sdp_in.first_audio()
            ip = m.connection or sdp_in.connection or "127.0.0.1"
            self.remote_rtp = ("127.0.0.1", m.port)  # SMURF está local
            print(f"  [{self.ext}] remote RTP advertised={ip}:{m.port}, usaré 127.0.0.1:{m.port}")
        except Exception as e:
            print(f"  [{self.ext}] error sdp entrante: {e}")
        return inv

    def send_to_caller(self, inv, msg):
        self.sock.sendto(bytes(msg), (SIP_HOST, SIP_PORT))

    def parse_remote_rtp(self, resp):
        sdp = SDP.parse(resp.body); m = sdp.first_audio()
        ip = m.connection or sdp.connection
        if ip and not ip.startswith("127."): ip = "127.0.0.1"
        self.remote_rtp = (ip, m.port)
        return self.remote_rtp

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

    def stream_rtp(self, freq, duration_s):
        if not self.remote_rtp: print(f"  [{self.ext}] no remote rtp"); return
        tone = gen_tone(freq, 1.0)
        bpf = samples_per_frame(0, 20) * 2
        frames = [tone[i:i+bpf] for i in range(0, len(tone)-bpf+1, bpf)]
        total = int(duration_s * 50)
        next_t = time.time(); sent = 0
        for k in range(total):
            chunk = frames[k % len(frames)]
            self.rtp_seq = (self.rtp_seq + 1) & 0xFFFF
            self.rtp_ts  = (self.rtp_ts + 160) & 0xFFFFFFFF
            pkt = RtpPacket(payload_type=0, sequence=self.rtp_seq,
                            timestamp=self.rtp_ts, ssrc=self.rtp_ssrc,
                            payload=encode_from_pcm16(chunk, 0))
            try:
                self.rtp.sendto(pkt.serialize(), self.remote_rtp); sent += 1
            except Exception:
                pass
            try:
                while True:
                    d, a = self.rtp.recvfrom(2048)
                    self.recvd += 1
            except (socket.timeout, BlockingIOError):
                pass
            next_t += 0.020
            sl = next_t - time.time()
            if sl > 0: time.sleep(sl)
        print(f"  [{self.ext}] RTP enviados={sent} recibidos={self.recvd}")


def main():
    import sqlite3
    db = sqlite3.connect("/workspace/smurf/.tmp/smurf.db")
    creds = dict(db.execute("SELECT number, sip_password FROM extensions").fetchall())
    db.close()
    print(f"=== Credenciales: {creds} ===")

    a = UA("1000", creds["1000"])
    b = UA("1001", creds["1001"])
    try:
        print("\n=== Fase 1: REGISTER ambos UAs ===")
        a.register()
        b.register()
        time.sleep(0.5)

        print("\n=== Fase 2: 1000 llama a 1001 ===")
        # B en hilo paralelo: contesta cuando llegue el INVITE
        b_inv_holder = {}
        def b_thread():
            inv = b.answer_incoming()
            b_inv_holder["inv"] = inv
        bt = threading.Thread(target=b_thread, daemon=True); bt.start()
        time.sleep(0.2)
        inv_a, final_a = a.call("1001")
        if final_a.status_code != 200:
            print(f"FAIL: caller no recibió 200, fue {final_a.status_code}"); return 1
        print(f"  [1000] llamada contestada, parseando SDP")
        a.parse_remote_rtp(final_a)
        a.ack(inv_a, final_a)
        bt.join(timeout=3)
        if not b_inv_holder.get("inv"):
            print("FAIL: B no procesó INVITE"); return 1
        time.sleep(0.5)

        print("\n=== Fase 3: 6s de RTP bidireccional (A=440Hz, B=880Hz) ===")
        ta = threading.Thread(target=lambda: a.stream_rtp(440, 6.0), daemon=True)
        tb = threading.Thread(target=lambda: b.stream_rtp(880, 6.0), daemon=True)
        ta.start(); tb.start()
        ta.join(); tb.join()

        print("\n=== Fase 4: BYE ===")
        a.bye(inv_a, final_a)
        time.sleep(1.5)

        print("\n=== Fase 5: Verificar grabación ===")
        rec_dir = "/workspace/smurf/.tmp/recordings"
        recs = sorted([f for f in os.listdir(rec_dir) if f.endswith(".wav")],
                      key=lambda f: os.path.getmtime(os.path.join(rec_dir, f)))
        if not recs:
            print("FAIL: no hay grabaciones"); return 2
        last = os.path.join(rec_dir, recs[-1])
        sz = os.path.getsize(last)
        with wave.open(last, "rb") as w:
            print(f"  WAV: {last}")
            print(f"       canales={w.getnchannels()} sw={w.getsampwidth()*8}b sr={w.getframerate()} frames={w.getnframes()} dur={w.getnframes()/w.getframerate():.2f}s size={sz}b")
        return 0
    finally:
        a.close(); b.close()


if __name__ == "__main__":
    sys.exit(main())
