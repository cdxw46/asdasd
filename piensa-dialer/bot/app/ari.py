"""Minimal async Asterisk REST Interface (ARI) client built on aiohttp.

Covers exactly what the dialer needs: originate channels, answer, play
media, create bridges, add/remove channels, hangup, and a resilient
websocket event stream with auto-reconnect.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class ARIError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"ARI HTTP {status}: {message}")
        self.status = status


class ARIClient:
    def __init__(self, base_url: str, username: str, password: str, app: str):
        self._rest = base_url.rstrip("/")
        self._auth = aiohttp.BasicAuth(username, password)
        self._username = username
        self._password = password
        self._app = app
        self._session: aiohttp.ClientSession | None = None
        self._ws_task: asyncio.Task | None = None
        self._handler: EventHandler | None = None
        self._closing = False

    # ------------------------------------------------------------------ lifecycle
    async def connect(self, handler: EventHandler) -> None:
        self._handler = handler
        self._closing = False
        self._session = aiohttp.ClientSession(auth=self._auth)
        self._ws_task = asyncio.create_task(self._ws_loop(), name="ari-ws")

    async def close(self) -> None:
        self._closing = True
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()

    async def wait_until_ready(self, timeout: float = 60.0) -> None:
        """Block until Asterisk answers REST calls (it boots slower than us)."""
        deadline = asyncio.get_event_loop().time() + timeout
        last_err: Exception | None = None
        while asyncio.get_event_loop().time() < deadline:
            try:
                await self.get("/asterisk/info")
                return
            except Exception as exc:  # noqa: BLE001 - retry loop
                last_err = exc
                await asyncio.sleep(2)
        raise RuntimeError(f"ARI not ready after {timeout}s: {last_err}")

    # ------------------------------------------------------------------ websocket
    async def _ws_loop(self) -> None:
        url = f"{self._rest}/events?app={self._app}&subscribeAll=true"
        backoff = 1
        while not self._closing:
            try:
                assert self._session is not None
                async with self._session.ws_connect(url, heartbeat=30) as ws:
                    logger.info("ARI websocket connected (app=%s)", self._app)
                    backoff = 1
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._dispatch(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect loop
                if self._closing:
                    break
                logger.warning("ARI websocket error: %s (reconnecting in %ss)", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _dispatch(self, raw: str) -> None:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Could not decode ARI event: %s", raw[:200])
            return
        if self._handler is not None:
            try:
                await self._handler(event)
            except Exception:  # noqa: BLE001 - never kill the ws loop
                logger.exception("Error handling ARI event %s", event.get("type"))

    # ------------------------------------------------------------------ REST
    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        assert self._session is not None, "connect() must be called first"
        url = self._rest + path
        async with self._session.request(method, url, **kwargs) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise ARIError(resp.status, text)
            if not text:
                return None
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text

    async def get(self, path: str, **params: Any) -> Any:
        return await self._request("GET", path, params={k: v for k, v in params.items() if v is not None})

    async def post(self, path: str, **params: Any) -> Any:
        return await self._request("POST", path, params={k: v for k, v in params.items() if v is not None})

    async def delete(self, path: str, **params: Any) -> Any:
        return await self._request("DELETE", path, params={k: v for k, v in params.items() if v is not None})

    # ------------------------------------------------------------------ helpers
    async def originate(
        self,
        *,
        endpoint: str,
        channel_id: str,
        caller_id: str,
        timeout: int,
        app_args: str,
        variables: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        body = {"variables": variables or {}}
        return await self._request(
            "POST",
            "/channels",
            params={
                "endpoint": endpoint,
                "app": self._app,
                "appArgs": app_args,
                "callerId": caller_id,
                "timeout": str(timeout),
                "channelId": channel_id,
            },
            json=body,
        )

    async def answer(self, channel_id: str) -> None:
        await self.post(f"/channels/{channel_id}/answer")

    async def play(self, channel_id: str, media: str, playback_id: str) -> dict[str, Any]:
        return await self.post(f"/channels/{channel_id}/play", media=media, playbackId=playback_id)

    async def stop_playback(self, playback_id: str) -> None:
        try:
            await self.delete(f"/playbacks/{playback_id}")
        except ARIError as exc:
            if exc.status != 404:
                raise

    async def hangup(self, channel_id: str, reason: str = "normal") -> None:
        try:
            await self.delete(f"/channels/{channel_id}", reason=reason)
        except ARIError as exc:
            if exc.status != 404:
                raise

    async def create_bridge(self, bridge_id: str) -> dict[str, Any]:
        return await self.post("/bridges", type="mixing", bridgeId=bridge_id)

    async def add_to_bridge(self, bridge_id: str, channel_id: str) -> None:
        await self.post(f"/bridges/{bridge_id}/addChannel", channel=channel_id)

    async def destroy_bridge(self, bridge_id: str) -> None:
        try:
            await self.delete(f"/bridges/{bridge_id}")
        except ARIError as exc:
            if exc.status != 404:
                raise
