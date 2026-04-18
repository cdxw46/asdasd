"""SMURF process watchdog.

Supervises service subprocesses and restarts crashed components.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.config import load_config
from core.logging_utils import configure_json_logging, get_logger

LOGGER = get_logger("watchdog")


@dataclass(slots=True)
class ManagedService:
    name: str
    command: list[str]
    process: asyncio.subprocess.Process | None = None
    restart_count: int = 0


class WatchdogService:
    def __init__(self, config_path: str | None):
        self.config = load_config(config_path)
        configure_json_logging("watchdog", self.config.global_.log_level)
        self.shutdown_event = asyncio.Event()
        cfg_path = config_path or "/etc/smurf/config.yml"
        python_exe = sys.executable
        pbx_entry = str(ROOT_DIR / "services" / "pbx-core" / "main.py")
        media_entry = str(ROOT_DIR / "services" / "media-core" / "main.py")
        sip_entry = str(ROOT_DIR / "services" / "sip-core" / "main.py")
        api_entry = str(ROOT_DIR / "services" / "api-admin" / "main.py")
        prov_entry = str(ROOT_DIR / "services" / "provisioning" / "main.py")
        self.services = [
            ManagedService(
                name="pbx-core",
                command=[python_exe, pbx_entry, "--config", cfg_path],
            ),
            ManagedService(
                name="media-core",
                command=[python_exe, media_entry, "--config", cfg_path],
            ),
            ManagedService(
                name="sip-core",
                command=[python_exe, sip_entry, "--config", cfg_path],
            ),
            ManagedService(
                name="api-admin",
                command=[python_exe, api_entry, "--config", cfg_path],
            ),
            ManagedService(
                name="provisioning",
                command=[python_exe, prov_entry, "--config", cfg_path],
            ),
        ]
        self.by_name = {svc.name: svc for svc in self.services}
        self.dependency_order = [
            "pbx-core",
            "media-core",
            "sip-core",
            "api-admin",
            "provisioning",
        ]

    def _service_working_dir(self, svc: ManagedService) -> str:
        if len(svc.command) > 1:
            candidate = Path(svc.command[1]).resolve().parent.parent.parent
            return str(candidate)
        return str(ROOT_DIR)

    async def run(self):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.shutdown_event.set)
            except NotImplementedError:
                pass

        for name in self.dependency_order:
            svc = self.by_name[name]
            await self._start_service(svc)
            await asyncio.sleep(0.5)
        monitor_task = asyncio.create_task(self._monitor_loop())

        await self.shutdown_event.wait()
        monitor_task.cancel()
        await self._stop_all()

    async def _start_service(self, svc: ManagedService):
        LOGGER.info("starting service", extra={"extra": {"service": svc.name}})
        svc.process = await asyncio.create_subprocess_exec(
            *svc.command,
            cwd=self._service_working_dir(svc),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        asyncio.create_task(self._pipe_logs(svc))

    async def _pipe_logs(self, svc: ManagedService):
        if not svc.process or not svc.process.stdout:
            return
        while True:
            line = await svc.process.stdout.readline()
            if not line:
                break
            LOGGER.info(
                "service-log",
                extra={
                    "extra": {
                        "service": svc.name,
                        "line": line.decode("utf-8", errors="replace").rstrip(),
                    }
                },
            )

    async def _monitor_loop(self):
        while True:
            await asyncio.sleep(2)
            for svc in self.services:
                if not svc.process:
                    continue
                if svc.process.returncode is None:
                    continue
                svc.restart_count += 1
                LOGGER.warning(
                    "service crashed, restarting",
                    extra={
                        "extra": {
                            "service": svc.name,
                            "return_code": svc.process.returncode,
                            "restart_count": svc.restart_count,
                        }
                    },
                )
                await self._start_service(svc)

    async def _stop_all(self):
        for svc in self.services:
            if svc.process and svc.process.returncode is None:
                svc.process.terminate()
        await asyncio.sleep(1)
        for svc in self.services:
            if svc.process and svc.process.returncode is None:
                svc.process.kill()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SMURF watchdog service")
    parser.add_argument("--config", default=None, help="Path to config YAML")
    return parser.parse_args()


def main():
    args = parse_args()
    service = WatchdogService(config_path=args.config)
    asyncio.run(service.run())


if __name__ == "__main__":
    main()

