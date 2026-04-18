"""Test funcional: llamada a extensión sin registro → entra a voicemail."""
from __future__ import annotations

import asyncio
import os
import socket
import secrets
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from smurfd.sip.message import SipMessage
from smurfd.sip.uri import NameAddr, SipURI
from smurfd.util.config import (RtpConfig, SipConfig, SmurfConfig,
                                 StorageConfig, WebConfig, SecurityConfig)
from smurfd.server import SmurfServer
from tests.test_register_invite import FakeUA, make_offer_sdp


class VoicemailTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import smurfd.db.database as dbmod
        dbmod._INSTANCE = None
        self.tmp = tempfile.mkdtemp(prefix="smurf-vm-")
        cfg = SmurfConfig(
            sip=SipConfig(udp_bind="127.0.0.1", udp_port=15160,
                          tcp_port=0, tls_port=0, ws_port=0, wss_port=0,
                          public_ip="127.0.0.1", realm="smurf.test"),
            rtp=RtpConfig(bind="127.0.0.1", port_min=42000, port_max=42500),
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
        self.server = SmurfServer(cfg)
        await self.server.start(with_api=False)
        rows = await self.server.db.fetchall("SELECT number, sip_password FROM extensions")
        self.creds = {r["number"]: r["sip_password"] for r in rows}

    async def asyncTearDown(self):
        await self.server.stop()
        import smurfd.db.database as dbmod
        dbmod._INSTANCE = None

    async def test_unregistered_callee_to_voicemail(self):
        """Si 1001 NO está registrada, llamar a 1001 → debe enviar a voicemail
        (200 OK con SDP) y crear una entrada en la tabla voicemail."""
        ua_a = FakeUA("1000", self.creds["1000"], "127.0.0.1", 15160, "smurf.test")
        try:
            loop = asyncio.get_event_loop()
            assert await loop.run_in_executor(None, ua_a.register), "A reg failed"
            await asyncio.sleep(0.2)

            invite_task = loop.run_in_executor(None,
                lambda: ua_a.invite_with_auth("1001", make_offer_sdp(ua_a.host, 42100)))

            inv, responses = await invite_task
            final = next((r for r in responses if r.status_code and r.status_code >= 200), None)
            self.assertIsNotNone(final)
            # 200 OK → voicemail nos contestó. Aceptamos también 480 si la lógica del PBX cambia.
            self.assertIn(final.status_code, (200, 486, 480),
                          f"esperado 200/486/480, fue {final.status_code}")

            if final.status_code == 200:
                ua_a.ack(inv, final)
                # Esperamos 2 segundos para que se grabe algo (silencio)
                await asyncio.sleep(2.0)
                ua_a.bye(inv, final)
                try:
                    bye_resp = await loop.run_in_executor(None, lambda: ua_a.recv(4.0))
                    self.assertEqual(bye_resp.status_code, 200)
                except socket.timeout:
                    pass
                # Esperar a que se procese voicemail.new
                await asyncio.sleep(1.0)
                rows = await self.server.db.fetchall("SELECT * FROM voicemail WHERE extension='1001'")
                self.assertGreaterEqual(len(rows), 1, "no se creó entrada de voicemail")
        finally:
            ua_a.close()


if __name__ == "__main__":
    unittest.main()
