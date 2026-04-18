"""Diálogos SIP (RFC 3261 §12) y construcción de requests dentro de un diálogo.

Un diálogo se identifica por (Call-ID, local-tag, remote-tag). Mientras está
"early" (provisional con remote tag) se identifica con tag remoto provisional.
Cuando se confirma con un 2xx, pasa a confirmado.

Aquí no hacemos el routing/forking: eso lo hace el B2BUA. Esta clase sólo
mantiene el estado de un diálogo y construye requests in-dialog (ACK, BYE,
re-INVITE, REFER, NOTIFY, INFO, UPDATE).
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .message import SipMessage, Via
from .uri import NameAddr, SipURI


def gen_tag() -> str:
    return secrets.token_hex(6)


def gen_call_id(host: str = "smurf.local") -> str:
    return f"{secrets.token_hex(12)}@{host}"


@dataclass
class Dialog:
    call_id: str
    local_tag: str
    remote_tag: str
    local_uri: SipURI
    remote_uri: SipURI
    local_target: SipURI            # nuestro Contact
    remote_target: SipURI           # Contact del peer
    route_set: List[NameAddr] = field(default_factory=list)
    secure: bool = False
    local_seq: int = 0
    remote_seq: int = 0
    is_uac: bool = True
    confirmed: bool = False

    @property
    def id(self) -> Tuple[str, str, str]:
        return (self.call_id, self.local_tag, self.remote_tag)

    def next_local_seq(self) -> int:
        self.local_seq += 1
        return self.local_seq

    def build_request(self, method: str, body: bytes = b"",
                      content_type: Optional[str] = None,
                      cseq: Optional[int] = None) -> SipMessage:
        msg = SipMessage(is_request=True, method=method,
                         request_uri=SipURI.parse(str(self.remote_target)))
        from_na = NameAddr(uri=self.local_uri); from_na.parameters["tag"] = self.local_tag
        to_na = NameAddr(uri=self.remote_uri); to_na.parameters["tag"] = self.remote_tag
        msg.set("From", str(from_na))
        msg.set("To", str(to_na))
        msg.set("Call-ID", self.call_id)
        n = cseq if cseq is not None else self.next_local_seq()
        msg.set("CSeq", f"{n} {method}")
        msg.set("Max-Forwards", "70")
        for r in self.route_set:
            msg.add("Route", str(r))
        contact = NameAddr(uri=self.local_target)
        msg.set("Contact", str(contact))
        if body:
            msg.set("Content-Type", content_type or "application/octet-stream")
            msg.set("Content-Length", str(len(body)))
            msg.body = body
        else:
            msg.set("Content-Length", "0")
        return msg

    @classmethod
    def from_uas_2xx(cls, request: SipMessage, response: SipMessage,
                     local_target: SipURI) -> "Dialog":
        """RFC 3261 §12.1.1: crea diálogo confirmado desde el lado UAS."""
        to = response.to_header
        from_h = response.from_header
        local_tag = to.parameters.get("tag", "")
        remote_tag = from_h.parameters.get("tag", "")
        contacts = request.contacts()
        remote_target = contacts[0].uri if contacts else from_h.uri
        rr = [NameAddr.parse(v) for v in request.get_all("Record-Route")]
        n_cseq, _ = request.cseq
        return cls(
            call_id=request.call_id,
            local_tag=local_tag,
            remote_tag=remote_tag,
            local_uri=to.uri,
            remote_uri=from_h.uri,
            local_target=local_target,
            remote_target=remote_target,
            route_set=rr,            # tal cual recibido
            local_seq=0,
            remote_seq=n_cseq,
            is_uac=False,
            confirmed=True,
        )

    @classmethod
    def from_uac_2xx(cls, request: SipMessage, response: SipMessage,
                     local_target: SipURI) -> "Dialog":
        """RFC 3261 §12.1.2: crea diálogo confirmado desde el lado UAC."""
        to = response.to_header
        from_h = response.from_header
        local_tag = from_h.parameters.get("tag", "")
        remote_tag = to.parameters.get("tag", "")
        contacts = response.contacts()
        remote_target = contacts[0].uri if contacts else to.uri
        rr = [NameAddr.parse(v) for v in response.get_all("Record-Route")]
        rr.reverse()  # UAC invierte el route set
        n_cseq, _ = request.cseq
        return cls(
            call_id=request.call_id,
            local_tag=local_tag,
            remote_tag=remote_tag,
            local_uri=from_h.uri,
            remote_uri=to.uri,
            local_target=local_target,
            remote_target=remote_target,
            route_set=rr,
            local_seq=n_cseq,
            remote_seq=0,
            is_uac=True,
            confirmed=True,
        )

    def build_ack(self, response: SipMessage) -> SipMessage:
        """ACK para un 2xx final dentro del diálogo (RFC 3261 §13.2.2.4)."""
        msg = SipMessage(is_request=True, method="ACK",
                         request_uri=SipURI.parse(str(self.remote_target)))
        from_na = NameAddr(uri=self.local_uri); from_na.parameters["tag"] = self.local_tag
        to_na = NameAddr(uri=self.remote_uri); to_na.parameters["tag"] = self.remote_tag
        msg.set("From", str(from_na))
        msg.set("To", str(to_na))
        msg.set("Call-ID", self.call_id)
        n, _ = response.cseq
        msg.set("CSeq", f"{n} ACK")
        msg.set("Max-Forwards", "70")
        for r in self.route_set:
            msg.add("Route", str(r))
        msg.set("Contact", str(NameAddr(uri=self.local_target)))
        msg.set("Content-Length", "0")
        return msg
