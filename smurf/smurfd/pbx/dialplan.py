"""Dial plan basado en regex con prioridades y reglas por dirección.

La tabla `dial_plan` define entradas:
    direction: internal | inbound | outbound
    pattern: regex sobre el número marcado
    target_type: extension | queue | ivr | ringgroup | trunk | voicemail | hangup | conference
    target_value: valor del target. Soporta back-references \g<0>.
    priority: menor = se evalúa primero.
    strip_digits: nº dígitos a quitar al inicio antes de aplicar.
    prepend: prefijo a añadir.

`Dialplan.match(number, direction)` devuelve el primer DialplanRoute que
coincida.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from ..db import Database


@dataclass
class DialplanRoute:
    id: int
    name: str
    direction: str
    pattern: str
    target_type: str
    target_value: str
    priority: int
    strip_digits: int = 0
    prepend: str = ""

    def transform(self, number: str) -> str:
        n = number
        if self.strip_digits:
            n = n[self.strip_digits :]
        if self.prepend:
            n = self.prepend + n
        m = re.match(self.pattern, number)
        if m:
            try:
                return m.expand(self.target_value)
            except Exception:
                return self.target_value
        return self.target_value


class Dialplan:
    def __init__(self, db: Database):
        self.db = db
        self._cache: Optional[List[DialplanRoute]] = None

    async def reload(self) -> None:
        rows = await self.db.fetchall(
            "SELECT * FROM dial_plan WHERE enabled=1 ORDER BY priority ASC, id ASC"
        )
        self._cache = [
            DialplanRoute(
                id=r["id"], name=r["name"], direction=r["direction"],
                pattern=r["pattern"], target_type=r["target_type"],
                target_value=r["target_value"], priority=r["priority"],
                strip_digits=r.get("strip_digits", 0) or 0,
                prepend=r.get("prepend", "") or "",
            )
            for r in rows
        ]

    async def match(self, number: str, direction: str) -> Optional[DialplanRoute]:
        if self._cache is None:
            await self.reload()
        for r in self._cache or []:
            if r.direction != direction:
                continue
            try:
                if re.match(r.pattern, number):
                    return r
            except re.error:
                continue
        return None
