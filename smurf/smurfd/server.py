"""Orquestador principal de SMURF.

Arranca los transportes SIP (UDP/TCP/TLS/WS/WSS), el TransactionManager,
el Registrar, el B2BUA, el motor RTP, los hilos de provisioning y la
interfaz HTTP/HTTPS con la API REST y el panel web.
"""
from __future__ import annotations

import asyncio
import os
import signal
import ssl
import sys
from typing import Dict, List, Optional

from .db import Database, get_database
from .pbx.b2bua import B2BUA
from .pbx.dialplan import Dialplan
from .pbx.events import EventBus, get_event_bus
from .rtp.engine import RtpAllocator
from .security.firewall import Firewall
from .sip.auth import DigestCredentials
from .sip.registrar import LocationService, Registrar
from .sip.transaction import ServerTransaction, TransactionManager
from .sip.transport import (Endpoint, TcpTransport, Transport, UdpTransport,
                            WsTransport, make_self_signed_ssl_context)
from .util.config import SmurfConfig, load_config
from .util.logger import get_logger, setup_logging

log = get_logger("server")


class SmurfServer:
    def __init__(self, cfg: Optional[SmurfConfig] = None) -> None:
        self.cfg = cfg or load_config()
        self.events: EventBus = get_event_bus()
        self.db: Optional[Database] = None
        self.location: Optional[LocationService] = None
        self.tx: Optional[TransactionManager] = None
        self.transports: Dict[str, Transport] = {}
        self.registrar: Optional[Registrar] = None
        self.b2bua: Optional[B2BUA] = None
        self.rtp_alloc: Optional[RtpAllocator] = None
        self.dialplan: Optional[Dialplan] = None
        self.firewall: Optional[Firewall] = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        for d in (self.cfg.storage.recordings_dir,
                  self.cfg.storage.voicemail_dir,
                  self.cfg.storage.sounds_dir,
                  self.cfg.storage.provisioning_dir,
                  self.cfg.storage.log_dir,
                  os.path.dirname(self.cfg.storage.db_path) or "."):
            os.makedirs(d, exist_ok=True)

        self.db = await get_database(self.cfg)
        self.location = LocationService()
        self.firewall = Firewall(
            self.db,
            window_sec=self.cfg.security.fail2ban_window_sec,
            max_failures=self.cfg.security.fail2ban_max_failures,
            ban_sec=self.cfg.security.fail2ban_ban_sec,
            pps_limit=self.cfg.security.rate_limit_per_ip_pps,
        )
        await self.firewall.init_from_db()

        loop = asyncio.get_running_loop()
        self.tx = TransactionManager(loop=loop,
                                     t1=self.cfg.sip.transactions_t1_ms / 1000.0,
                                     t2=self.cfg.sip.transactions_t2_ms / 1000.0,
                                     t4=self.cfg.sip.transactions_t4_ms / 1000.0)
        self.tx.request_handler = self._on_request
        self.rtp_alloc = RtpAllocator(self.cfg.rtp.bind,
                                      self.cfg.rtp.port_min,
                                      self.cfg.rtp.port_max,
                                      self.cfg.rtp.dscp)

        async def cred_loader(username: str) -> Optional[DigestCredentials]:
            r = await self.db.fetchone(
                "SELECT ha1_md5 FROM extensions WHERE number=? AND enabled=1",
                (username,),
            )
            if not r:
                return None
            return DigestCredentials(username=username, realm=self.cfg.sip.realm,
                                     password=f"ha1:{r['ha1_md5']}")

        self.registrar = Registrar(
            realm=self.cfg.sip.realm,
            location=self.location,
            cred_loader=cred_loader,
            min_expires=self.cfg.sip.registration_min_expires,
            default_expires=self.cfg.sip.registration_default_expires,
            max_expires=self.cfg.sip.registration_max_expires,
            user_agent=self.cfg.sip.user_agent,
        )

        self.dialplan = Dialplan(self.db)
        await self.dialplan.reload()

        # Transports
        await self._start_transports()

        self.b2bua = B2BUA(self.cfg, self.db, self.location, self.tx,
                           self.rtp_alloc, self.events, self.dialplan,
                           self.transports)

        # Watcher de extensiones registradas
        self.location.subscribe(lambda aor, bs: asyncio.create_task(
            self.events.publish("registrar.update", aor=aor, count=len(bs))
        ))

        log.info("SMURF iniciado · realm=%s · public_ip=%s · admin :%d/HTTPS · API /api/v1",
                 self.cfg.sip.realm,
                 self.b2bua.public_ip,
                 self.cfg.web.https_port)

    async def _start_transports(self) -> None:
        async def router(msg, ep: Endpoint, transport: Transport):
            if not self.firewall or self.firewall.check_packet(ep.host):
                await self.tx.on_message(msg, ep, transport)
        s = self.cfg.sip
        # UDP
        if s.udp_port > 0:
            t = UdpTransport(s.udp_bind, s.udp_port, router)
            await t.start(); self.transports["udp"] = t
        # TCP
        if s.tcp_port > 0:
            t = TcpTransport(s.tcp_bind, s.tcp_port, router)
            await t.start(); self.transports["tcp"] = t
        # TLS
        ssl_ctx = make_self_signed_ssl_context(self.cfg.web.tls_cert or "",
                                               self.cfg.web.tls_key or "")
        if s.tls_port > 0 and ssl_ctx:
            t = TcpTransport(s.tls_bind, s.tls_port, router, ssl_ctx=ssl_ctx)
            await t.start(); self.transports["tls"] = t
        # WS
        if s.ws_port > 0:
            t = WsTransport(s.ws_bind, s.ws_port, router)
            await t.start(); self.transports["ws"] = t
        # WSS
        if s.wss_port > 0 and ssl_ctx:
            t = WsTransport(s.wss_bind, s.wss_port, router, ssl_ctx=ssl_ctx)
            await t.start(); self.transports["wss"] = t

    async def _on_request(self, stx: ServerTransaction) -> None:
        # 1) REGISTER
        if await self.registrar.handle(stx):
            return
        # 2) PBX (INVITE/BYE/CANCEL/REFER/INFO/MESSAGE/OPTIONS)
        if await self.b2bua.handle_request(stx):
            return
        await stx.respond(405, "Method Not Allowed",
                          extra_headers={"Allow":
                                         "REGISTER, INVITE, ACK, BYE, CANCEL, OPTIONS, REFER, UPDATE, INFO, MESSAGE"})

    async def stop(self) -> None:
        log.info("Parando SMURF…")
        for t in self.transports.values():
            try: await t.stop()
            except Exception: pass
        if self.db: await self.db.close()
        self._stop.set()

    async def serve_forever(self) -> None:
        await self._stop.wait()


async def amain() -> None:
    cfg = load_config()
    setup_logging(cfg.log_level, os.path.join(cfg.storage.log_dir, "smurf.log") if cfg.storage.log_dir else None)
    server = SmurfServer(cfg)
    await server.start()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(server.stop()))
        except NotImplementedError:
            pass
    await server.serve_forever()


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
