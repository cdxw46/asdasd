"""Cliente STUN minimo (RFC 5389) sobre socket UDP existente.

Uso: descubrir el binding NAT (IP:puerto público) que el operador verá
para un socket RTP local, antes de incluirlo en el SDP saliente.
"""
from __future__ import annotations

import secrets
import socket
import struct
from typing import Optional, Tuple


_MAGIC = 0x2112A442
_STUN_SERVERS = [
    ("stun.l.google.com", 19302),
    ("stun1.l.google.com", 19302),
    ("stun.cloudflare.com", 3478),
]


def stun_query(sock: socket.socket,
               server: Optional[Tuple[str, int]] = None,
               timeout: float = 2.0) -> Optional[Tuple[str, int]]:
    """Envía un Binding Request por `sock` y devuelve (mapped_ip, mapped_port)."""
    targets = [server] if server else _STUN_SERVERS
    old_to = sock.gettimeout()
    sock.settimeout(timeout)
    try:
        for srv in targets:
            try:
                tx = secrets.token_bytes(12)
                req = struct.pack("!HHI12s", 0x0001, 0, _MAGIC, tx)
                sock.sendto(req, srv)
                while True:
                    try:
                        data, _addr = sock.recvfrom(2048)
                    except socket.timeout:
                        break
                    if len(data) < 20:
                        continue
                    typ, mlen, magic, tx2 = struct.unpack("!HHI12s", data[:20])
                    if magic != _MAGIC or tx2 != tx or typ != 0x0101:
                        # No es respuesta STUN nuestra: la dejamos pasar
                        continue
                    body = data[20:20 + mlen]
                    i = 0
                    while i + 4 <= len(body):
                        atype, alen = struct.unpack("!HH", body[i:i + 4])
                        i += 4
                        attr = body[i:i + alen]
                        i += alen + (4 - alen % 4) % 4
                        if atype == 0x0020 and len(attr) >= 8:  # XOR-MAPPED-ADDRESS
                            xport = struct.unpack("!H", attr[2:4])[0] ^ (_MAGIC >> 16)
                            xip32 = struct.unpack("!I", attr[4:8])[0] ^ _MAGIC
                            ip = socket.inet_ntoa(struct.pack("!I", xip32))
                            return (ip, xport)
                        if atype == 0x0001 and len(attr) >= 8:  # MAPPED-ADDRESS
                            port = struct.unpack("!H", attr[2:4])[0]
                            ip = socket.inet_ntoa(attr[4:8])
                            return (ip, port)
            except (socket.gaierror, OSError):
                continue
        return None
    finally:
        try: sock.settimeout(old_to)
        except Exception: pass
