"""SMURF PBX core service.

Call-control logic: dialplan, extension routing, queues, groups, CDR lifecycle,
basic feature toggles (pickup/park/transfer metadata), and command bus API.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import re
import signal
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.bus import JsonCommandServer
from core.config import load_config
from core.db import Database
from core.logging_utils import configure_json_logging, get_logger

LOGGER = get_logger("pbx-core")
VALID_PRESENCE_STATUSES = {"available", "busy", "away", "dnd", "offline"}


@dataclass(slots=True)
class RouteDecision:
    status: str
    target_extension: str | None = None
    route_type: str = "extension"
    reason: str = ""
    trunk_name: str = ""


class PBXCoreService:
    def __init__(self, config_path: str | None):
        self.config = load_config(config_path)
        configure_json_logging("pbx-core", self.config.global_.log_level)
        self.db = Database(self.config.database.sqlite_path)
        self.command_server = JsonCommandServer(
            host=self.config.bus.pbx_command_host,
            port=self.config.bus.pbx_command_port,
            handler=self._handle_command,
        )
        self.shutdown_event = asyncio.Event()

        # State helpers
        self.group_rr_index: dict[str, int] = defaultdict(int)
        self.queue_rr_index: dict[str, int] = defaultdict(int)
        self.queue_least_busy_index: dict[str, int] = defaultdict(int)
        self.park_slots: dict[str, str] = {}  # slot -> call_id
        self.call_meta: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    def _active_call_by_id(self, call_id: str) -> dict[str, Any] | None:
        return self.db.fetchone(
            "SELECT call_id, from_ext, to_ext, trunk_name FROM active_calls WHERE call_id = ?",
            (call_id,),
        )

    async def run(self):
        await self.command_server.start()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.shutdown_event.set)
            except NotImplementedError:
                pass
        LOGGER.info(
            "PBX core started",
            extra={
                "extra": {
                    "command_host": self.config.bus.pbx_command_host,
                    "command_port": self.config.bus.pbx_command_port,
                }
            },
        )
        await self.shutdown_event.wait()
        await self.command_server.stop()

    def _active_calls_per_extension(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for call in self.db.list_active_calls():
            counts[str(call["from_ext"])] += 1
            counts[str(call["to_ext"])] += 1
        return counts

    def _active_calls_snapshot(self) -> list[dict[str, Any]]:
        return self.db.list_active_calls()

    def _global_call_limit_ok(self, active_calls: list[dict[str, Any]]) -> bool:
        active = active_calls
        return len(active) < int(self.config.global_.max_global_calls)

    def _active_trunk_channels(self, trunk_name: str) -> int:
        count = 0
        for call in self.db.list_active_calls():
            if str(call.get("trunk_name", "")) == trunk_name:
                count += 1
        return count

    def _extension_call_limit_ok(
        self,
        extension: str,
        active_counts: dict[str, int] | None = None,
    ) -> bool:
        ext = self.db.get_extension(extension)
        if not ext:
            return False
        max_calls = int(ext.get("max_calls", 1))
        counts = active_counts or self._active_calls_per_extension()
        current = counts.get(extension, 0)
        return current < max_calls

    def _choose_ring_group_member(self, group_number: str) -> str | None:
        groups = self.db.list_ring_groups()
        group = next((g for g in groups if g["group_number"] == group_number), None)
        if not group:
            return None
        members = [str(x) for x in group.get("members", []) if str(x)]
        if not members:
            return None
        strategy = str(group.get("strategy", "all")).lower()
        if strategy in {"all", "first"}:
            for member in members:
                if self.db.get_best_registration(member):
                    return member
            return members[0]
        if strategy == "random":
            random.shuffle(members)
            return members[0]
        if strategy == "round_robin":
            idx = self.group_rr_index[group_number] % len(members)
            self.group_rr_index[group_number] = (idx + 1) % len(members)
            return members[idx]
        return members[0]

    def _choose_queue_member(self, queue_number: str) -> str | None:
        queues = self.db.list_queues()
        queue = next((q for q in queues if q["queue_number"] == queue_number), None)
        if not queue:
            return None
        members = [str(x) for x in queue.get("members", []) if str(x)]
        if not members:
            return None
        strategy = str(queue.get("strategy", "round_robin")).lower()
        available = [m for m in members if self.db.get_best_registration(m)]
        if not available:
            available = members
        if strategy == "random":
            return random.choice(available)
        if strategy == "least_busy":
            active = self._active_calls_per_extension()
            available.sort(key=lambda m: active.get(m, 0))
            return available[0]
        if strategy == "priority":
            return available[0]
        idx = self.queue_rr_index[queue_number] % len(available)
        self.queue_rr_index[queue_number] = (idx + 1) % len(available)
        return available[idx]

    def _apply_dialplan(self, from_ext: str, to_number: str) -> RouteDecision | None:
        rules = self.db.list_dialplan_rules()
        for rule in rules:
            pattern = str(rule.get("pattern", ""))
            try:
                if not re.fullmatch(pattern, to_number):
                    continue
            except re.error:
                continue
            action = str(rule.get("action", "extension")).lower()
            target = str(rule.get("target", ""))
            if action == "extension":
                return RouteDecision(status="ok", target_extension=target)
            if action == "ring_group":
                member = self._choose_ring_group_member(target)
                if member:
                    return RouteDecision(
                        status="ok", target_extension=member, route_type="ring_group"
                    )
                return RouteDecision(status="error", reason="ring_group_empty")
            if action == "queue":
                member = self._choose_queue_member(target)
                if member:
                    return RouteDecision(
                        status="ok", target_extension=member, route_type="queue"
                    )
                return RouteDecision(status="error", reason="queue_empty")
            if action == "ivr":
                # Current implementation returns IVR pseudo extension.
                return RouteDecision(
                    status="ok", target_extension=target, route_type="ivr"
                )
            if action == "trunk":
                return RouteDecision(
                    status="ok",
                    target_extension=to_number,
                    route_type="trunk",
                    trunk_name=target,
                )
            if action == "reject":
                return RouteDecision(status="error", reason="rejected_by_dialplan")
        return None

    def _route_call(self, call_id: str, from_ext: str, to_ext: str) -> RouteDecision:
        if not call_id:
            return RouteDecision(status="error", reason="missing_call_id")
        existing = self._active_call_by_id(call_id)
        if existing:
            return RouteDecision(
                status="ok",
                target_extension=str(existing.get("to_ext", "")),
                route_type="trunk" if existing.get("trunk_name") else "extension",
                trunk_name=str(existing.get("trunk_name", "")),
            )
        active_calls = self._active_calls_snapshot()
        active_counts = self._active_calls_per_extension()
        if not self._global_call_limit_ok(active_calls):
            return RouteDecision(status="error", reason="global_call_limit")
        if not self._extension_call_limit_ok(from_ext, active_counts):
            return RouteDecision(status="error", reason="from_extension_limit")
        if from_ext == to_ext:
            return RouteDecision(status="error", reason="loop_call_forbidden")

        # Internal extension route
        ext = self.db.get_extension(to_ext)
        if ext and int(ext.get("enabled", 1)) == 1:
            if not self._extension_call_limit_ok(to_ext, active_counts):
                return RouteDecision(status="error", reason="to_extension_limit")
            self.db.start_call(call_id, from_ext, to_ext)
            self.call_meta[call_id] = {
                "from_ext": from_ext,
                "to_ext": to_ext,
                "created_at": int(time.time()),
            }
            return RouteDecision(status="ok", target_extension=to_ext)

        # Ring group direct number
        group_member = self._choose_ring_group_member(to_ext)
        if group_member:
            if not self._extension_call_limit_ok(group_member, active_counts):
                return RouteDecision(status="error", reason="group_member_limit")
            self.db.start_call(call_id, from_ext, group_member)
            self.call_meta[call_id] = {
                "from_ext": from_ext,
                "to_ext": group_member,
                "route_type": "ring_group",
                "group_number": to_ext,
                "created_at": int(time.time()),
            }
            return RouteDecision(
                status="ok", target_extension=group_member, route_type="ring_group"
            )

        # Queue direct number
        queue_member = self._choose_queue_member(to_ext)
        if queue_member:
            if not self._extension_call_limit_ok(queue_member, active_counts):
                return RouteDecision(status="error", reason="queue_member_limit")
            self.db.start_call(call_id, from_ext, queue_member)
            self.call_meta[call_id] = {
                "from_ext": from_ext,
                "to_ext": queue_member,
                "route_type": "queue",
                "queue_number": to_ext,
                "created_at": int(time.time()),
            }
            return RouteDecision(
                status="ok", target_extension=queue_member, route_type="queue"
            )

        # Dialplan
        dialed = self._apply_dialplan(from_ext, to_ext)
        if dialed:
            if dialed.status != "ok":
                return dialed
            if dialed.route_type == "trunk":
                if dialed.trunk_name:
                    trunk_cfg = next(
                        (t for t in self.db.list_trunks() if str(t.get("name", "")) == dialed.trunk_name),
                        None,
                    )
                    if trunk_cfg:
                        max_channels = int(trunk_cfg.get("max_channels", 0))
                        if (
                            max_channels > 0
                            and self._active_trunk_channels(dialed.trunk_name) >= max_channels
                        ):
                            return RouteDecision(
                                status="error",
                                reason="trunk_channel_limit",
                            )
                self.db.start_call(
                    call_id, from_ext, dialed.target_extension or to_ext, dialed.trunk_name
                )
                self.call_meta[call_id] = {
                    "from_ext": from_ext,
                    "to_ext": dialed.target_extension or to_ext,
                    "route_type": "trunk",
                    "trunk_name": dialed.trunk_name,
                    "created_at": int(time.time()),
                }
                return dialed
            target = dialed.target_extension or to_ext
            if dialed.route_type == "extension" and not self._extension_call_limit_ok(
                target, active_counts
            ):
                return RouteDecision(status="error", reason="to_extension_limit")
            self.db.start_call(call_id, from_ext, target)
            self.call_meta[call_id] = {
                "from_ext": from_ext,
                "to_ext": target,
                "route_type": dialed.route_type,
                "created_at": int(time.time()),
            }
            return dialed

        # Trunk fallback (cheapest active trunk by priority).
        trunks = self.db.list_trunks()
        active_trunks = [t for t in trunks if int(t.get("active", 1)) == 1]
        if active_trunks:
            trunk = next(
                (
                    t
                    for t in active_trunks
                    if int(t.get("max_channels", 0)) <= 0
                    or self._active_trunk_channels(str(t.get("name", "")))
                    < int(t.get("max_channels", 0))
                ),
                None,
            )
            if not trunk:
                return RouteDecision(status="error", reason="all_trunks_busy")
            trunk_name = str(trunk.get("name", ""))
            self.db.start_call(call_id, from_ext, to_ext, trunk_name=trunk_name)
            self.call_meta[call_id] = {
                "from_ext": from_ext,
                "to_ext": to_ext,
                "route_type": "trunk",
                "trunk_name": trunk_name,
                "created_at": int(time.time()),
            }
            return RouteDecision(
                status="ok",
                target_extension=to_ext,
                route_type="trunk",
                trunk_name=trunk_name,
            )
        return RouteDecision(status="error", reason="no_route")

    async def _handle_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action", "")).lower()

        if action == "ping":
            return {"ok": True, "service": "pbx-core"}

        if action == "route_call":
            call_id = str(payload.get("call_id", ""))
            from_ext = str(payload.get("from_ext", ""))
            to_ext = str(payload.get("to_ext", ""))
            if not (call_id and from_ext and to_ext):
                return {"ok": False, "status": "error", "reason": "missing_route_fields"}
            async with self._lock:
                decision = self._route_call(call_id, from_ext, to_ext)
                if decision.status == "ok":
                    return {
                        "ok": True,
                        "status": "ok",
                        "target_extension": decision.target_extension,
                        "route_type": decision.route_type,
                        "trunk_name": decision.trunk_name,
                    }
                return {
                    "ok": False,
                    "status": "error",
                    "reason": decision.reason,
                }

        if action == "ack_call":
            call_id = str(payload.get("call_id", ""))
            if call_id:
                self.db.update_call_state(call_id, "answered")
            return {"ok": True}

        if action == "end_call":
            call_id = str(payload.get("call_id", ""))
            reason = str(payload.get("reason", "normal_clear"))
            if call_id:
                self.db.end_call(call_id, reason)
                self.call_meta.pop(call_id, None)
                for slot, parked_call in list(self.park_slots.items()):
                    if parked_call == call_id:
                        self.park_slots.pop(slot, None)
            return {"ok": True}

        if action == "set_presence":
            extension = str(payload.get("extension", ""))
            status = str(payload.get("status", "available")).strip().lower()
            note = str(payload.get("note", ""))[:256]
            if not extension:
                return {"ok": False, "error": "missing extension"}
            if status not in VALID_PRESENCE_STATUSES:
                return {"ok": False, "error": "unknown_presence_status"}
            self.db.set_presence(extension, status, note)
            return {"ok": True}

        if action == "chat_send":
            from_ext = str(payload.get("from_ext", ""))
            to_ext = str(payload.get("to_ext", ""))
            message = str(payload.get("message", ""))
            if not (from_ext and to_ext and message):
                return {"ok": False, "error": "missing fields"}
            if len(message) > 4096:
                return {"ok": False, "error": "chat_message_too_long"}
            self.db.add_chat_message(from_ext, to_ext, message)
            return {"ok": True}

        if action == "park_call":
            call_id = str(payload.get("call_id", ""))
            slot = str(payload.get("slot", "701"))
            if not call_id:
                return {"ok": False, "error": "missing call_id"}
            if not slot.isdigit() or len(slot) > 6:
                return {"ok": False, "error": "invalid_slot"}
            if not self.db.fetchone(
                "SELECT call_id FROM active_calls WHERE call_id = ?",
                (call_id,),
            ):
                return {"ok": False, "error": "call_not_found"}
            self.park_slots[slot] = call_id
            self.db.update_call_state(call_id, "parked")
            return {"ok": True, "slot": slot}

        if action == "pickup_parked":
            slot = str(payload.get("slot", "701"))
            extension = str(payload.get("extension", ""))
            call_id = self.park_slots.pop(slot, "")
            if not call_id:
                return {"ok": False, "error": "slot_empty"}
            self.db.update_call_state(call_id, "answered")
            meta = self.call_meta.get(call_id, {})
            return {
                "ok": True,
                "call_id": call_id,
                "from_ext": meta.get("from_ext", ""),
                "to_ext": extension or meta.get("to_ext", ""),
            }

        if action == "pickup_call":
            target_extension = str(payload.get("target_extension", ""))
            picker_extension = str(payload.get("picker_extension", ""))
            if not target_extension or not picker_extension:
                return {"ok": False, "error": "missing_extensions"}
            active_calls = self.db.list_active_calls()
            ringing_call = next(
                (
                    c
                    for c in active_calls
                    if c.get("state") == "ringing"
                    and str(c.get("to_ext", "")) == target_extension
                ),
                None,
            )
            if not ringing_call:
                return {"ok": False, "error": "no_ringing_call"}
            self.db.update_call_state(str(ringing_call["call_id"]), "answered")
            return {
                "ok": True,
                "call_id": ringing_call["call_id"],
                "target_extension": target_extension,
                "picker_extension": picker_extension,
            }

        if action == "stats":
            return {
                "ok": True,
                "active_calls": len(self.db.list_active_calls()),
                "registrations": len(self.db.active_registrations()),
                "queue_count": len(self.db.list_queues()),
                "ring_group_count": len(self.db.list_ring_groups()),
            }

        return {"ok": False, "error": f"unknown action: {action}"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SMURF PBX core service")
    parser.add_argument("--config", default=None, help="Path to config YAML")
    return parser.parse_args()


def main():
    args = parse_args()
    service = PBXCoreService(config_path=args.config)
    asyncio.run(service.run())


if __name__ == "__main__":
    main()
