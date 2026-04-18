"""PBX feature server for event stream and webhooks."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from pathlib import Path
from typing import Any

import httpx

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.bus import JsonCommandServer
from core.config import load_config
from core.db import Database
from core.logging_utils import configure_json_logging, get_logger

LOGGER = get_logger("pbx-feature-server")


class FeatureServer:
    def __init__(self, config_path: str | None):
        self.config = load_config(config_path)
        configure_json_logging("pbx-feature-server", self.config.global_.log_level)
        self.db = Database(self.config.database.sqlite_path)
        self.shutdown_event = asyncio.Event()
        self.server = JsonCommandServer(
            host=self.config.bus.pbx_event_host,
            port=self.config.bus.pbx_event_port,
            handler=self._handle_event,
        )
        self.clients: list[asyncio.StreamWriter] = []

    async def run(self):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.shutdown_event.set)
            except NotImplementedError:
                pass

        await self.server.start()
        LOGGER.info(
            "pbx-feature-server started",
            extra={
                "extra": {
                    "host": self.config.bus.pbx_event_host,
                    "port": self.config.bus.pbx_event_port,
                }
            },
        )
        await self.shutdown_event.wait()
        await self.server.stop()

    async def _notify_webhooks(self, event: dict[str, Any]):
        hooks = self.db.fetchall(
            "SELECT * FROM webhooks WHERE active = 1 AND event_name = ?",
            (event.get("event", ""),),
        )
        if not hooks:
            return
        async with httpx.AsyncClient(timeout=5.0) as client:
            for hook in hooks:
                try:
                    await client.post(str(hook["target_url"]), json=event)
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning(
                        "webhook delivery failed",
                        extra={
                            "extra": {
                                "target_url": hook["target_url"],
                                "error": str(exc),
                            }
                        },
                    )

    async def _handle_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        event_name = str(payload.get("event", ""))
        if not event_name:
            return {"ok": False, "error": "missing event"}
        # Optional persistent event storage could be added here.
        await self._notify_webhooks(payload)
        return {"ok": True}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SMURF PBX feature server")
    parser.add_argument("--config", default=None, help="Path to config YAML")
    return parser.parse_args()


def main():
    args = parse_args()
    service = FeatureServer(config_path=args.config)
    asyncio.run(service.run())


if __name__ == "__main__":
    main()
