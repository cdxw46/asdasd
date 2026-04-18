"""SMURF provisioning service.

Dedicated HTTP provisioning endpoint for IP phones and bootstrapping.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import uvicorn
from fastapi import FastAPI, HTTPException, Response

from core.config import load_config
from core.db import Database
from core.logging_utils import configure_json_logging, get_logger

LOGGER = get_logger("provisioning")


class ProvisioningService:
    def __init__(self, config_path: str | None):
        self.config = load_config(config_path)
        configure_json_logging("provisioning", self.config.global_.log_level)
        self.db = Database(self.config.database.sqlite_path)
        self.app = FastAPI(
            title="SMURF Provisioning",
            version="1.0.0",
            description="Dynamic phone provisioning service",
        )
        self._configure_routes()

    def _configure_routes(self):
        @self.app.get("/health")
        async def health():
            return {"status": "ok", "service": "provisioning"}

        @self.app.get("/vendors")
        async def vendors():
            tpl_dir = Path(self.config.provisioning.templates_path)
            tpl_dir.mkdir(parents=True, exist_ok=True)
            vendor_names = sorted([p.stem for p in tpl_dir.glob("*.tpl")])
            return {"items": vendor_names}

        @self.app.get("/template/{vendor}")
        async def template(vendor: str):
            tpl_file = Path(self.config.provisioning.templates_path) / (
                f"{vendor.lower()}.tpl"
            )
            if not tpl_file.exists():
                raise HTTPException(status_code=404, detail="Template not found")
            return Response(tpl_file.read_text(encoding="utf-8"), media_type="text/plain")

        @self.app.get("/config/{vendor}/{extension}.cfg")
        async def config(vendor: str, extension: str):
            ext = self.db.get_extension(extension)
            if not ext:
                raise HTTPException(status_code=404, detail="Unknown extension")
            tpl_file = Path(self.config.provisioning.templates_path) / (
                f"{vendor.lower()}.tpl"
            )
            if not tpl_file.exists():
                raise HTTPException(status_code=404, detail="Template not found")
            template = tpl_file.read_text(encoding="utf-8")
            rendered = template.format(
                EXTENSION=extension,
                DISPLAY_NAME=ext["display_name"],
                AUTH_USER=ext["auth_username"],
                AUTH_PASS=ext["auth_password"],
                SIP_SERVER=self.config.global_.domain,
                SIP_UDP_PORT=self.config.sip.udp_port,
                SIP_TLS_PORT=self.config.sip.tls_port,
                PROVISIONING_URL=self.config.provisioning.base_url,
            )
            return Response(rendered, media_type="text/plain")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SMURF provisioning service")
    parser.add_argument("--config", default=None, help="Path to config YAML")
    return parser.parse_args()


def main():
    args = parse_args()
    service = ProvisioningService(config_path=args.config)
    uvicorn.run(
        service.app,
        host=service.config.provisioning.host,
        port=service.config.provisioning.port,
        log_level=service.config.global_.log_level.lower(),
    )


if __name__ == "__main__":
    main()

