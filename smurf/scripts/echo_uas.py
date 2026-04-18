"""Servidor SIP UAS de echo simple usado como destino de trunk para los tests
end-to-end. Acepta INVITE, contesta 200 OK, refleja todo el RTP que recibe."""
from __future__ import annotations

import asyncio, functools, secrets, socket, struct, sys, time
sys.path.insert(0, '/workspace/smurf')
print = functools.partial(print, flush=True)

from smurfd.sip.message import SipMessage, make_response
from smurfd.sip.uri import NameAddr, SipURI
from smurfd.sip.sdp import SDP


HOST = '127.0.0.1'
PORT = 25060
RTP_PORT = 35060


class EchoServer(asyncio.DatagramProtocol):
    def __init__(self):
        self.transport = None
        self.calls = {}  # call_id -> dict

    def connection_made(self, t):
        self.transport = t
        print(f"[UAS] echo SIP escuchando en {HOST}:{PORT}")

    def datagram_received(self, data, addr):
        try:
            msg = SipMessage.parse(data)
        except Exception as e:
            return
        if not msg.is_request:
            return
        method = msg.method
        if method == 'INVITE':
            asyncio.create_task(self.handle_invite(msg, addr))
        elif method == 'ACK':
            print(f"[UAS] ACK call={msg.call_id}")
        elif method == 'BYE':
            self.transport.sendto(bytes(make_response(msg, 200, 'OK')), addr)
            c = self.calls.pop(msg.call_id, None)
            if c and c.get('rtp_task'):
                c['rtp_task'].cancel()
            print(f"[UAS] BYE call={msg.call_id} cerrado")
        elif method == 'OPTIONS':
            self.transport.sendto(bytes(make_response(msg, 200, 'OK')), addr)
        elif method == 'CANCEL':
            self.transport.sendto(bytes(make_response(msg, 200, 'OK')), addr)

    async def handle_invite(self, req, addr):
        try:
            sdp = SDP.parse(req.body) if req.body else None
        except Exception:
            sdp = None
        if not sdp or not sdp.first_audio():
            self.transport.sendto(bytes(make_response(req, 488, 'Not Acceptable Here')), addr)
            return
        m = sdp.first_audio()
        peer_ip = m.connection or sdp.connection or addr[0]
        peer_port = m.port
        # Abrir socket RTP para esta llamada
        s_rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s_rtp.bind((HOST, 0))
        local_rtp_port = s_rtp.getsockname()[1]
        # Construir SDP de respuesta
        ans = (
            f"v=0\r\no=echo {int(time.time())} {int(time.time())} IN IP4 {HOST}\r\n"
            f"s=echo\r\nc=IN IP4 {HOST}\r\nt=0 0\r\n"
            f"m=audio {local_rtp_port} RTP/AVP 0 8 101\r\n"
            f"a=rtpmap:0 PCMU/8000\r\na=rtpmap:8 PCMA/8000\r\n"
            f"a=rtpmap:101 telephone-event/8000\r\na=fmtp:101 0-16\r\n"
            f"a=ptime:20\r\na=sendrecv\r\n"
        ).encode()
        # 100, 180, 200
        self.transport.sendto(bytes(make_response(req, 100, 'Trying')), addr)
        await asyncio.sleep(0.05)
        tag = secrets.token_hex(4)
        ringing = make_response(req, 180, 'Ringing', to_tag=tag)
        ringing.set('Contact', f'<sip:echo@{HOST}:{PORT}>')
        self.transport.sendto(bytes(ringing), addr)
        await asyncio.sleep(0.2)
        ok = make_response(req, 200, 'OK', to_tag=tag, body=ans, content_type='application/sdp')
        ok.set('Contact', f'<sip:echo@{HOST}:{PORT}>')
        self.transport.sendto(bytes(ok), addr)
        # Tarea de eco RTP
        info = {'sock': s_rtp, 'peer': (peer_ip, peer_port), 'pkts': 0}
        info['rtp_task'] = asyncio.create_task(self._echo_loop(s_rtp, info))
        self.calls[req.call_id] = info
        print(f"[UAS] INVITE call={req.call_id} contestada · echo RTP {peer_ip}:{peer_port} ↔ {HOST}:{local_rtp_port}")

    async def _echo_loop(self, sock, info):
        sock.setblocking(False)
        loop = asyncio.get_event_loop()
        peer = info['peer']
        learned = False
        try:
            while True:
                try:
                    data, addr = await loop.run_in_executor(None, _recvfrom, sock)
                except Exception:
                    await asyncio.sleep(0.1); continue
                if not data:
                    continue
                if not learned:
                    print(f"[UAS] primer RTP recibido desde {addr} (sdp anunciaba {peer})")
                    peer = addr; info['peer'] = addr; learned = True
                try:
                    sock.sendto(data, peer)
                    info['pkts'] += 1
                    if info['pkts'] in (1, 50, 200):
                        print(f"[UAS] eco {info['pkts']} pkts → {peer}")
                except Exception as e:
                    print(f"[UAS] send err: {e}"); return
        except asyncio.CancelledError:
            pass


def _recvfrom(s):
    return s.recvfrom(2048)


async def main():
    loop = asyncio.get_event_loop()
    await loop.create_datagram_endpoint(EchoServer, local_addr=(HOST, PORT))
    while True:
        await asyncio.sleep(60)


if __name__ == '__main__':
    asyncio.run(main())
