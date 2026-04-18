"""Internal JSON-over-TCP bus primitives for SMURF."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

from .logging_utils import get_logger

LOGGER = get_logger("core.bus")

JsonDict = dict[str, Any]
CommandHandler = Callable[[JsonDict], Awaitable[JsonDict]]


class JsonCommandServer:
    """Line-oriented JSON request/response server over TCP."""

    def __init__(self, host: str, port: int, handler: CommandHandler):
        self.host = host
        self.port = port
        self.handler = handler
        self.server: asyncio.AbstractServer | None = None

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                raw = line.decode("utf-8", errors="replace").strip()
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                    if not isinstance(payload, dict):
                        raise ValueError("command payload must be an object")
                except Exception as exc:  # noqa: BLE001
                    response: JsonDict = {"ok": False, "error": f"invalid_json: {exc}"}
                else:
                    try:
                        response = await self.handler(payload)
                    except Exception as exc:  # noqa: BLE001
                        LOGGER.exception("command handler crashed")
                        response = {"ok": False, "error": f"handler_error: {exc}"}
                writer.write((json.dumps(response) + "\n").encode("utf-8"))
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            LOGGER.debug("bus client disconnected: %s", peer)

    async def start(self):
        self.server = await asyncio.start_server(self._client, self.host, self.port)
        LOGGER.info("json command server listening on %s:%s", self.host, self.port)

    async def run_forever(self):
        await self.start()
        assert self.server is not None
        async with self.server:
            await self.server.serve_forever()

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None


class JsonCommandClient:
    """Simple request/response client for the JSON command server."""

    def __init__(self, host: str, port: int, timeout: float = 2.0):
        self.host = host
        self.port = port
        self.timeout = timeout

    async def request(self, payload: JsonDict) -> JsonDict:
        return await send_command(
            host=self.host,
            port=self.port,
            payload=payload,
            timeout=self.timeout,
        )

    async def call(self, payload: JsonDict) -> JsonDict:
        """Alias used by some services."""
        return await self.request(payload)


class JsonEventClient:
    """Best-effort JSON line emitter to an event endpoint."""

    def __init__(self, host: str, port: int, timeout: float = 2.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self):
        if self._writer is not None:
            return
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=self.timeout,
        )

    async def send(self, payload: JsonDict):
        if self._writer is None:
            await self.connect()
        assert self._writer is not None
        self._writer.write((json.dumps(payload) + "\n").encode("utf-8"))
        await self._writer.drain()

    async def close(self):
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()
            self._writer = None
            self._reader = None


async def send_command(
    host: str,
    port: int,
    payload: JsonDict,
    timeout: float = 2.0,
) -> JsonDict:
    """Send one JSON command and receive one JSON response."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port),
        timeout=timeout,
    )
    try:
        writer.write((json.dumps(payload) + "\n").encode("utf-8"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        if not line:
            return {"ok": False, "error": "empty_response"}
        response = json.loads(line.decode("utf-8", errors="replace"))
        if not isinstance(response, dict):
            return {"ok": False, "error": "invalid_response_shape"}
        return response
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"invalid_response_json: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"bus_error: {exc}"}
    finally:
        writer.close()
        await writer.wait_closed()


# Compatibility aliases
JsonLineServer = JsonCommandServer
JsonRpcClient = JsonCommandClient
