"""Cliente AMI: origina llamadas en Asterisk y traduce los eventos a
resultados de alto nivel para el bot."""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Awaitable, Callable

from panoramisk import Manager

log = logging.getLogger("p1.ami")

# Codigos "Reason" de OriginateResponse de Asterisk -> resultado legible.
ORIGINATE_REASON = {
    "0": "FAILED",       # no se pudo originar
    "1": "NO_ANSWER",    # colgado / sin respuesta
    "3": "NO_ANSWER",
    "4": "ANSWERED",     # contesto (el detalle real llega por UserEvent)
    "5": "BUSY",         # ocupado
    "8": "UNREACHABLE",  # congestion / no alcanzable
}

# Resultado -> texto que se muestra en Telegram
RESULT_LABELS = {
    "ANSWERED": "📞 Contestó",
    "PRESSED1": "✅ Pulsó 1 (transferido)",
    "TRANSFER_END": "↪️ Transferencia finalizada",
    "NO_INPUT": "🔇 Contestó pero no pulsó nada",
    "INVALID": "⚠️ Pulsó una tecla no válida",
    "NO_ANSWER": "📵 No contestó",
    "BUSY": "⛔ Comunicando",
    "UNREACHABLE": "🚫 No alcanzable",
    "FAILED": "❌ Error al llamar",
}

ResultCallback = Callable[[str, str, dict], Awaitable[None] | None]


class Dialer:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        secret: str,
        trunk_endpoint: str,
        ivr_context: str,
        dial_timeout: int,
        on_result: ResultCallback,
    ) -> None:
        self.trunk_endpoint = trunk_endpoint
        self.ivr_context = ivr_context
        self.dial_timeout = dial_timeout
        self.on_result = on_result
        # ActionID -> target en curso
        self._pending: dict[str, str] = {}
        # target que ya ha "contestado" (para no pisar el resultado fino con el de originate)
        self._answered: set[str] = set()

        self.manager = Manager(
            host=host,
            port=port,
            username=username,
            secret=secret,
            ping_delay=10,
        )

    async def connect(self) -> None:
        self.manager.register_event("UserEvent", self._on_userevent)
        self.manager.register_event("OriginateResponse", self._on_originate_response)
        await self.manager.connect()
        log.info("Conectado al AMI de Asterisk")

    def close(self) -> None:
        try:
            self.manager.close()
        except Exception:  # noqa: BLE001
            pass

    async def originate(self, number: str, campaign_id: str) -> None:
        action_id = uuid.uuid4().hex
        self._pending[action_id] = number
        action = {
            "Action": "Originate",
            "ActionID": action_id,
            "Channel": f"PJSIP/{number}@{self.trunk_endpoint}",
            "Context": self.ivr_context,
            "Exten": "s",
            "Priority": 1,
            "Async": "true",
            "Timeout": self.dial_timeout * 1000,
            "Variable": [f"CAMPAIGN={campaign_id}", f"TARGET={number}"],
        }
        log.info("Originate -> %s (campana %s)", number, campaign_id)
        await self.manager.send_action(action)

    async def _emit(self, target: str, result: str, extra: dict) -> None:
        res = self.on_result(target, result, extra)
        if asyncio.iscoroutine(res):
            await res

    async def _on_userevent(self, manager, message) -> None:  # noqa: ANN001
        if message.get("UserEvent") != "CallResult":
            return
        target = (message.get("Target") or "").strip()
        result = (message.get("Result") or "").strip()
        if not target or not result:
            return
        if result == "ANSWERED":
            self._answered.add(target)
        extra = {}
        if message.get("DialStatus"):
            extra["DialStatus"] = message.get("DialStatus").strip()
        await self._emit(target, result, extra)

    async def _on_originate_response(self, manager, message) -> None:  # noqa: ANN001
        action_id = message.get("ActionID")
        target = self._pending.pop(action_id, None)
        if not target:
            return
        # Si ya contesto, los UserEvent mandan; no degradamos el resultado.
        if target in self._answered:
            self._answered.discard(target)
            return
        response = message.get("Response", "")
        reason = str(message.get("Reason", "")).strip()
        if response == "Success":
            result = "ANSWERED"
        else:
            result = ORIGINATE_REASON.get(reason, "FAILED")
        await self._emit(target, result, {"Reason": reason})
