"""Tiny async AMI (Asterisk Manager Interface) client.

Used only to trigger a `pjsip reload` after the bot rewrites the dynamic
agents include file. Works across containers (unlike `asterisk -rx`).
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class AMIClient:
    def __init__(self, host: str, port: int, username: str, password: str):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self._lock = asyncio.Lock()

    async def _send(self, writer: asyncio.StreamWriter, fields: dict[str, str]) -> None:
        payload = "".join(f"{k}: {v}\r\n" for k, v in fields.items()) + "\r\n"
        writer.write(payload.encode())
        await writer.drain()

    async def reload_pjsip(self) -> bool:
        """Login, run `pjsip reload`, log off. Returns True on success."""
        async with self._lock:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port), timeout=8
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("AMI connect failed (%s:%s): %s", self.host, self.port, exc)
                return False
            try:
                await asyncio.wait_for(reader.readline(), timeout=5)  # banner
                await self._send(writer, {
                    "Action": "Login",
                    "Username": self.username,
                    "Secret": self.password,
                })
                ok = await self._read_response(reader)
                if not ok:
                    logger.warning("AMI login failed")
                    return False
                await self._send(writer, {"Action": "Command", "Command": "pjsip reload"})
                await self._read_response(reader)
                await self._send(writer, {"Action": "Logoff"})
                logger.info("AMI: pjsip reload issued")
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning("AMI error: %s", exc)
                return False
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:  # noqa: BLE001
                    pass

    async def _read_response(self, reader: asyncio.StreamReader) -> bool:
        """Read one AMI response block; True if Response: Success/Follows."""
        success = False
        try:
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                if not line:
                    break
                text = line.decode(errors="ignore").strip()
                if text.lower().startswith("response:"):
                    success = "success" in text.lower() or "follows" in text.lower()
                if text == "":
                    break
        except asyncio.TimeoutError:
            pass
        return success
