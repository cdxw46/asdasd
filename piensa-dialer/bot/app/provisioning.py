"""HTTP provisioning server for Zoiper QR auto-configuration.

Zoiper scans a QR that points to ``<base>/prov/<token>.xml``; this server
returns the account XML and Zoiper configures itself. The agent's phone must
be able to reach this URL (open the provisioning port on the firewall).
"""
from __future__ import annotations

import logging

from aiohttp import web

from .agentes import AgentStore

logger = logging.getLogger(__name__)


class ProvisioningServer:
    def __init__(self, agents: AgentStore, sip_domain: str, port: int):
        self.agents = agents
        self.sip_domain = sip_domain
        self.port = port
        self._runner: web.AppRunner | None = None

    async def _handle(self, request: web.Request) -> web.Response:
        token = request.match_info["token"]
        agent = self.agents.by_token(token)
        if agent is None:
            return web.Response(status=404, text="not found")
        try:
            transport = int(request.query.get("t", "0"))
        except ValueError:
            transport = 0
        xml = self.agents.provisioning_xml(agent, self.sip_domain, transport)
        logger.info("Provisioned agent %s via QR token", agent.sip_user)
        return web.Response(text=xml, content_type="application/xml")

    async def _health(self, request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/prov/{token}.xml", self._handle)
        app.router.add_get("/prov/{token}", self._handle)
        app.router.add_get("/healthz", self._health)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await site.start()
        logger.info("Provisioning server listening on :%s", self.port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
