"""Registrar SIP (RFC 3261 §10) con servicio de localización en memoria.

Las binding (extensión SIP → Contact URI + endpoint de transporte + expira)
se guardan en RAM porque las consultas son ultra-frecuentes en el ruteo de
llamadas; la base de datos guarda sólo las extensiones (con su password
HA1 y configuración). Las bindings se reconstruyen tras un restart porque
los REGISTER llegan periódicamente.

Reglas implementadas:
    * Autenticación digest obligatoria (MD5 o SHA-256) salvo trunks IP.
    * Manejo de "Contact: *" + Expires: 0 → borrar todas las bindings.
    * Min-Expires (423 si el cliente pide menos del mínimo).
    * "Path" header almacenado para SIP-over-WebSocket (RFC 3327).
    * Detección automática de NAT (rport, received) y uso del 5-tuple.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional

from ..util.logger import get_logger
from .auth import (DigestCredentials, build_challenge, parse_auth_header,
                   verify_response)
from .message import SipMessage, make_response, Via
from .transaction import ServerTransaction
from .transport import Endpoint
from .uri import NameAddr, SipURI

log = get_logger("sip.registrar")


@dataclass
class Binding:
    aor: str                      # sip:1001@smurf.local
    contact_uri: SipURI
    endpoint: Endpoint
    expires_at: float
    cseq: int = 0
    user_agent: str = ""
    instance: Optional[str] = None  # +sip.instance
    path: List[str] = field(default_factory=list)
    call_id: str = ""

    def is_alive(self) -> bool:
        return self.expires_at > time.time()

    def remaining(self) -> int:
        return max(0, int(self.expires_at - time.time()))


# Cargador de credenciales asincrónico (lo provee la capa de DB)
CredentialsLoader = Callable[[str], Awaitable[Optional[DigestCredentials]]]


class LocationService:
    """Mapa AOR → lista de Binding (puede haber varios contactos por extensión)."""

    def __init__(self) -> None:
        self._bindings: Dict[str, List[Binding]] = {}
        self._lock = asyncio.Lock()
        self._listeners: List[Callable[[str, List[Binding]], None]] = []

    def subscribe(self, cb: Callable[[str, List[Binding]], None]) -> None:
        self._listeners.append(cb)

    def _notify(self, aor: str) -> None:
        bs = self.get(aor)
        for cb in self._listeners:
            try:
                cb(aor, bs)
            except Exception:
                log.exception("listener de registro falló")

    def get(self, aor: str) -> List[Binding]:
        aor = aor.lower()
        bs = [b for b in self._bindings.get(aor, []) if b.is_alive()]
        if not bs and aor in self._bindings:
            self._bindings.pop(aor, None)
        return bs

    def all(self) -> Dict[str, List[Binding]]:
        out: Dict[str, List[Binding]] = {}
        for aor, lst in list(self._bindings.items()):
            alive = [b for b in lst if b.is_alive()]
            if alive:
                out[aor] = alive
            else:
                self._bindings.pop(aor, None)
        return out

    async def upsert(self, b: Binding) -> None:
        async with self._lock:
            lst = self._bindings.setdefault(b.aor, [])
            replaced = False
            key = (b.instance, str(b.contact_uri).lower(), b.call_id)
            for i, existing in enumerate(lst):
                ek = (existing.instance, str(existing.contact_uri).lower(), existing.call_id)
                if (b.instance and existing.instance == b.instance) or ek == key:
                    lst[i] = b
                    replaced = True
                    break
            if not replaced:
                lst.append(b)
            self._bindings[b.aor] = [x for x in lst if x.is_alive()]
        self._notify(b.aor)

    async def remove(self, aor: str, contact_uri: Optional[SipURI] = None,
                     instance: Optional[str] = None) -> None:
        aor = aor.lower()
        async with self._lock:
            lst = self._bindings.get(aor, [])
            if contact_uri is None and instance is None:
                self._bindings.pop(aor, None)
            else:
                cu = str(contact_uri).lower() if contact_uri else None
                lst = [b for b in lst
                       if not ((cu and str(b.contact_uri).lower() == cu) or
                               (instance and b.instance == instance))]
                if lst:
                    self._bindings[aor] = lst
                else:
                    self._bindings.pop(aor, None)
        self._notify(aor)


class Registrar:
    def __init__(self, realm: str, location: LocationService,
                 cred_loader: CredentialsLoader,
                 min_expires: int = 60, default_expires: int = 3600,
                 max_expires: int = 7200, opaque_secret: str = "smurf",
                 user_agent: str = "SMURF-PBX/1.0"):
        self.realm = realm
        self.location = location
        self.cred_loader = cred_loader
        self.min_expires = min_expires
        self.default_expires = default_expires
        self.max_expires = max_expires
        self.opaque_secret = opaque_secret
        self.user_agent = user_agent

    async def handle(self, stx: ServerTransaction) -> bool:
        """Devuelve True si el mensaje era un REGISTER y fue manejado."""
        req = stx.request
        if req.method != "REGISTER":
            return False
        to = req.to_header
        aor_uri = to.uri
        username = aor_uri.user or ""
        if not username:
            await stx.respond(400, "Missing user in To URI")
            return True

        creds = await self.cred_loader(username)
        if creds is None:
            await stx.respond(404, "Extension not found")
            return True

        auth_value = req.get("Authorization") or req.get("Proxy-Authorization")
        if not auth_value:
            await stx.respond(401, "Unauthorized",
                              extra_headers={"WWW-Authenticate":
                                             build_challenge(self.realm, "MD5",
                                                             opaque_secret=self.opaque_secret)})
            return True
        ok, stale = verify_response(req.method or "", req.body, auth_value, creds,
                                    opaque_secret=self.opaque_secret)
        if not ok:
            await stx.respond(401, "Unauthorized",
                              extra_headers={"WWW-Authenticate":
                                             build_challenge(self.realm, "MD5",
                                                             stale=stale,
                                                             opaque_secret=self.opaque_secret)})
            return True

        contacts = req.contacts()
        expires_default = self._parse_expires_header(req, self.default_expires)
        n_cseq, _ = req.cseq
        aor = aor_uri.aor

        is_wildcard_remove = (
            len(contacts) == 1
            and contacts[0].uri.host == ""
            and "*" in contacts[0].parameters
            and expires_default == 0
        )
        if is_wildcard_remove:
            await self.location.remove(aor)
            await self._respond_with_bindings(stx, aor)
            return True

        v = req.via_top()
        ep = stx.endpoint
        if v and v.received:
            ep = Endpoint(stx.transport.name, v.received,
                          int(v.rport) if v.rport and v.rport.isdigit() else (v.port or 5060))

        for c in contacts:
            exp = self._parse_contact_expires(c, expires_default)
            if 0 < exp < self.min_expires:
                await stx.respond(423, "Interval Too Brief",
                                  extra_headers={"Min-Expires": str(self.min_expires)})
                return True
            exp = min(exp, self.max_expires) if exp > 0 else 0

            instance = c.parameters.get("+sip.instance")
            contact_uri = c.uri
            if contact_uri.host in ("", "0.0.0.0", "127.0.0.1") and stx.transport.name in ("ws", "wss"):
                contact_uri = SipURI.parse(str(contact_uri))
                contact_uri.host = ep.host
                contact_uri.port = ep.port
                contact_uri.parameters.setdefault("transport", stx.transport.name)

            if exp == 0:
                await self.location.remove(aor, contact_uri=c.uri, instance=instance)
            else:
                b = Binding(
                    aor=aor,
                    contact_uri=contact_uri,
                    endpoint=ep,
                    expires_at=time.time() + exp,
                    cseq=n_cseq,
                    user_agent=req.get("User-Agent", "") or "",
                    instance=instance,
                    path=req.get_all("Path"),
                    call_id=req.call_id,
                )
                await self.location.upsert(b)

        await self._respond_with_bindings(stx, aor)
        return True

    async def _respond_with_bindings(self, stx: ServerTransaction, aor: str) -> None:
        from .dialog import gen_tag
        resp = make_response(stx.request, 200, "OK", to_tag=gen_tag())
        for b in self.location.get(aor):
            params = f";expires={b.remaining()}"
            if b.instance:
                params += f';+sip.instance="{b.instance}"'
            resp.add("Contact", f"<{b.contact_uri}>{params}")
        resp.set("Date", time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime()))
        resp.set("Server", self.user_agent)
        await stx.send_response(resp)

    def _parse_expires_header(self, req: SipMessage, default: int) -> int:
        v = req.get("Expires")
        if v is None:
            return default
        try:
            return max(0, int(v.strip()))
        except ValueError:
            return default

    def _parse_contact_expires(self, c: NameAddr, default: int) -> int:
        v = c.parameters.get("expires")
        if v is None:
            return default
        try:
            return max(0, int(v))
        except ValueError:
            return default
