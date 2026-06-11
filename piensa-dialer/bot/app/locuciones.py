"""Locution (audio prompt) library.

Stores multiple prompts as 8 kHz mono WAVs in the Asterisk-readable sounds
dir, plus a small JSON index. There are two roles:

* ``cliente``  — message played to the person we call.
* ``agente``   — short message played to the agent on transfer (identifies
                 the call before bridging the customer in).

Each role has one "active" locution used by the dialer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass

from . import tts

logger = logging.getLogger(__name__)

ROLES = ("cliente", "agente")


@dataclass
class Locution:
    id: str
    name: str
    role: str
    stem: str          # file name without extension (also the Asterisk media key)
    source: str        # "mp3" | "tts" | "default"
    created: float

    @property
    def media(self) -> str:
        return f"sound:custom/{self.stem}"


class LocutionStore:
    def __init__(self, sounds_dir: str, lang: str = "es"):
        self.dir = sounds_dir
        self.lang = lang
        self.index_path = os.path.join(sounds_dir, "locuciones.json")
        self._items: dict[str, Locution] = {}
        self._active: dict[str, str | None] = {"cliente": None, "agente": None}
        self._lock = asyncio.Lock()
        os.makedirs(sounds_dir, exist_ok=True)
        self._load()

    # --------------------------------------------------------------- persistence
    def _load(self) -> None:
        if not os.path.exists(self.index_path):
            return
        try:
            with open(self.index_path, encoding="utf-8") as fh:
                data = json.load(fh)
            for raw in data.get("items", []):
                loc = Locution(**raw)
                if os.path.exists(self._wav(loc.stem)):
                    self._items[loc.id] = loc
            self._active.update(data.get("active", {}))
            # Drop active pointers to locutions that no longer exist.
            for role in ROLES:
                if self._active.get(role) not in self._items:
                    self._active[role] = None
        except Exception:  # noqa: BLE001
            logger.exception("Could not load locution index; starting empty")

    def _save(self) -> None:
        data = {
            "items": [asdict(loc) for loc in self._items.values()],
            "active": self._active,
        }
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.index_path)

    def _wav(self, stem: str) -> str:
        return os.path.join(self.dir, f"{stem}.wav")

    # --------------------------------------------------------------- queries
    def list(self, role: str | None = None) -> list[Locution]:
        items = [l for l in self._items.values() if role is None or l.role == role]
        return sorted(items, key=lambda l: l.created)

    def get(self, loc_id: str) -> Locution | None:
        return self._items.get(loc_id)

    def active(self, role: str) -> Locution | None:
        loc_id = self._active.get(role)
        return self._items.get(loc_id) if loc_id else None

    def active_media(self, role: str, fallback: str | None = None) -> str | None:
        loc = self.active(role)
        if loc is not None:
            return loc.media
        return fallback

    # --------------------------------------------------------------- mutations
    async def add_from_file(self, src_path: str, name: str, role: str = "cliente") -> Locution:
        loc_id = uuid.uuid4().hex[:8]
        stem = f"loc_{role}_{loc_id}"
        async with self._lock:
            await tts.to_asterisk_wav(src_path, self._wav(stem))
            loc = Locution(id=loc_id, name=name[:64] or stem, role=role, stem=stem,
                           source="mp3", created=time.time())
            self._items[loc.id] = loc
            if self._active.get(role) is None:
                self._active[role] = loc.id
            self._save()
        logger.info("Added locution %s (%s) role=%s", loc.id, loc.name, role)
        return loc

    async def add_from_text(self, text: str, name: str, role: str = "cliente") -> Locution:
        loc_id = uuid.uuid4().hex[:8]
        stem = f"loc_{role}_{loc_id}"
        async with self._lock:
            await tts.synthesize(text, self.lang, self._wav(stem))
            loc = Locution(id=loc_id, name=name[:64] or stem, role=role, stem=stem,
                           source="tts", created=time.time())
            self._items[loc.id] = loc
            if self._active.get(role) is None:
                self._active[role] = loc.id
            self._save()
        logger.info("Added TTS locution %s (%s) role=%s", loc.id, loc.name, role)
        return loc

    async def set_active(self, loc_id: str) -> bool:
        async with self._lock:
            loc = self._items.get(loc_id)
            if not loc:
                return False
            self._active[loc.role] = loc_id
            self._save()
            return True

    async def delete(self, loc_id: str) -> bool:
        async with self._lock:
            loc = self._items.pop(loc_id, None)
            if not loc:
                return False
            try:
                os.remove(self._wav(loc.stem))
            except FileNotFoundError:
                pass
            for role in ROLES:
                if self._active.get(role) == loc_id:
                    self._active[role] = None
            self._save()
            return True

    async def register_default(self, stem: str, name: str = "Aviso por defecto") -> Locution:
        """Register the built-in TTS prompt (already on disk) as a cliente locution."""
        async with self._lock:
            for loc in self._items.values():
                if loc.stem == stem:
                    return loc
            loc = Locution(id="default", name=name, role="cliente", stem=stem,
                           source="default", created=0.0)
            self._items[loc.id] = loc
            if self._active.get("cliente") is None:
                self._active["cliente"] = loc.id
            self._save()
            return loc
