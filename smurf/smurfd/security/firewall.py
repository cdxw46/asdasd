"""Fail2ban interno + rate limiting de mensajes SIP por IP.

Funciona en RAM con persistencia opcional en `banned_ips`. Cualquier
mensaje SIP entrante pasa por `check_packet(ip)`. Cuando se detecta auth
fallida (401 repetido, REGISTER inválido, INVITE rechazado), se llama a
`record_failure(ip, reason)`. Si supera el umbral, la IP queda bloqueada
durante `ban_sec` segundos.

Adicionalmente intenta usar `iptables -A INPUT -s <ip> -j DROP` si está
disponible (con sudo). Si no, sólo mantiene la lista en memoria y rechaza
los paquetes silenciosamente.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional, Tuple

from ..util.logger import get_logger

log = get_logger("security.fw")


class Firewall:
    def __init__(self, db=None, *, window_sec: int = 300,
                 max_failures: int = 8, ban_sec: int = 3600,
                 pps_limit: int = 200, use_iptables: bool = True):
        self.db = db
        self.window_sec = window_sec
        self.max_failures = max_failures
        self.ban_sec = ban_sec
        self.pps_limit = pps_limit
        self.use_iptables = use_iptables and shutil.which("iptables") is not None
        self._failures: Dict[str, Deque[float]] = defaultdict(deque)
        self._banned: Dict[str, float] = {}
        self._packets: Dict[str, Deque[float]] = defaultdict(deque)
        self._whitelist = {"127.0.0.1", "::1"}

    def whitelist(self, ip: str) -> None:
        self._whitelist.add(ip)

    async def init_from_db(self) -> None:
        if not self.db:
            return
        rows = await self.db.fetchall("SELECT ip, until FROM banned_ips")
        now = time.time()
        for r in rows:
            if r["until"] > now:
                self._banned[r["ip"]] = r["until"]

    def is_banned(self, ip: str) -> bool:
        if ip in self._whitelist:
            return False
        until = self._banned.get(ip)
        if until is None:
            return False
        if until < time.time():
            self._banned.pop(ip, None)
            return False
        return True

    def check_packet(self, ip: str) -> bool:
        """Devuelve True si el paquete debe procesarse, False si se descarta."""
        if ip in self._whitelist:
            return True
        if self.is_banned(ip):
            return False
        now = time.time()
        q = self._packets[ip]
        q.append(now)
        cutoff = now - 1.0
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) > self.pps_limit:
            asyncio.create_task(self._ban(ip, "pps-flood"))
            return False
        return True

    async def record_failure(self, ip: str, reason: str = "") -> None:
        if ip in self._whitelist:
            return
        now = time.time()
        q = self._failures[ip]
        q.append(now)
        cutoff = now - self.window_sec
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= self.max_failures:
            await self._ban(ip, reason or "auth-failures")

    async def _ban(self, ip: str, reason: str) -> None:
        if ip in self._whitelist or ip in self._banned:
            return
        until = time.time() + self.ban_sec
        self._banned[ip] = until
        log.warning("Bloqueando IP %s (%s) hasta %s", ip, reason,
                    time.strftime("%H:%M:%S", time.localtime(until)))
        if self.db:
            try:
                await self.db.execute(
                    "INSERT OR REPLACE INTO banned_ips(ip,until,reason) VALUES(?,?,?)",
                    (ip, until, reason),
                )
            except Exception:
                log.exception("persistir ban falló")
        if self.use_iptables:
            try:
                subprocess.run(
                    ["sudo", "-n", "iptables", "-I", "INPUT", "-s", ip, "-j", "DROP"],
                    check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=2,
                )
            except Exception:
                pass
        asyncio.get_event_loop().call_later(self.ban_sec, lambda: self._unban(ip))

    def _unban(self, ip: str) -> None:
        self._banned.pop(ip, None)
        if self.use_iptables:
            try:
                subprocess.run(
                    ["sudo", "-n", "iptables", "-D", "INPUT", "-s", ip, "-j", "DROP"],
                    check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=2,
                )
            except Exception:
                pass
