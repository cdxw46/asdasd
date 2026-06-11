"""Outbound campaign engine.

Drives Asterisk through ARI: originates calls (respecting a concurrency
limit), plays the Spanish IVR prompt, captures DTMF, transfers to the agent
when the callee presses 1, and tracks a per-call outcome for reporting.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .ari import ARIClient, ARIError
from .config import Config
from .locuciones import LocutionStore
from .numbers import dial_string

logger = logging.getLogger(__name__)


class Outcome(str, Enum):
    PENDING = "pendiente"
    PRESSED_1 = "pulsó 1"
    TRANSFERRED = "transferida"
    TRANSFER_FAILED = "fallo transferencia"
    NO_INPUT = "sin respuesta del usuario"
    NO_ANSWER = "no contesta"
    BUSY = "comunica"
    REJECTED = "rechazada"
    FAILED = "fallida"
    CANCELLED = "cancelada"


# Outcomes that mean the customer engaged and asked for a human.
SUCCESS_OUTCOMES = {Outcome.PRESSED_1, Outcome.TRANSFERRED}


class State(str, Enum):
    QUEUED = "queued"
    DIALING = "dialing"
    ANSWERED = "answered"
    TRANSFERRING = "transferring"
    TRANSFERRED = "transferred"
    DONE = "done"


def _outcome_from_cause(cause: int | None) -> Outcome:
    # Q.850 cause codes.
    mapping = {
        16: Outcome.NO_ANSWER,   # normal clearing before answer
        17: Outcome.BUSY,        # user busy
        18: Outcome.NO_ANSWER,   # no user responding
        19: Outcome.NO_ANSWER,   # no answer
        21: Outcome.REJECTED,    # call rejected
        20: Outcome.NO_ANSWER,   # subscriber absent
        1: Outcome.FAILED,       # unallocated number
    }
    if cause is None:
        return Outcome.FAILED
    return mapping.get(cause, Outcome.FAILED)


@dataclass
class CallRecord:
    number: str
    index: int
    state: State = State.QUEUED
    outcome: Outcome | None = None
    channel_id: str | None = None
    agent_channel_id: str | None = None
    bridge_id: str | None = None
    playback_id: str | None = None
    agent_playback_id: str | None = None
    answered: bool = False
    transferred: bool = False
    finished: bool = False
    started_at: float = 0.0
    ended_at: float = 0.0
    # asyncio primitives are created when the call starts.
    done: asyncio.Event = field(default_factory=asyncio.Event)
    dtmf_event: asyncio.Event = field(default_factory=asyncio.Event)
    playback_done: asyncio.Event = field(default_factory=asyncio.Event)
    _transfer_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class Campaign:
    id: str
    chat_id: int
    records: list[CallRecord]
    cancelled: bool = False
    completed: bool = False
    created_at: float = field(default_factory=time.time)
    progress_event: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: list[asyncio.Task] = field(default_factory=list)

    def notify(self) -> None:
        self.progress_event.set()

    def snapshot(self) -> dict[str, Any]:
        total = len(self.records)
        done = sum(1 for r in self.records if r.finished)
        in_progress = sum(1 for r in self.records if r.state != State.QUEUED and not r.finished)
        counts: dict[Outcome, int] = {}
        for r in self.records:
            if r.outcome is not None:
                counts[r.outcome] = counts.get(r.outcome, 0) + 1
        return {
            "total": total,
            "done": done,
            "in_progress": in_progress,
            "queued": total - done - in_progress,
            "success": sum(1 for r in self.records if r.outcome in SUCCESS_OUTCOMES),
            "counts": counts,
            "completed": self.completed,
            "cancelled": self.cancelled,
        }


class Dialer:
    def __init__(self, ari: ARIClient, cfg: Config, locutions: LocutionStore):
        self.ari = ari
        self.cfg = cfg
        self.locutions = locutions
        self._sem = asyncio.Semaphore(cfg.max_concurrent_calls)
        self._channels: dict[str, tuple[Campaign, CallRecord, str]] = {}
        self.active: Campaign | None = None
        self.history: list[Campaign] = []

    # ----------------------------------------------------------------- campaign API
    @property
    def is_busy(self) -> bool:
        return self.active is not None and not self.active.completed

    def start_campaign(self, numbers: list[str], chat_id: int) -> Campaign:
        if self.is_busy:
            raise RuntimeError("Ya hay una campaña en curso")
        records = [CallRecord(number=n, index=i) for i, n in enumerate(numbers)]
        campaign = Campaign(id=uuid.uuid4().hex[:8], chat_id=chat_id, records=records)
        self.active = campaign
        asyncio.create_task(self._run_campaign(campaign), name=f"campaign-{campaign.id}")
        return campaign

    def cancel(self) -> bool:
        if not self.is_busy or self.active is None:
            return False
        self.active.cancelled = True
        for rec in self.active.records:
            if not rec.finished and rec.channel_id:
                asyncio.create_task(self.ari.hangup(rec.channel_id))
        return True

    async def _run_campaign(self, campaign: Campaign) -> None:
        logger.info("Campaign %s started: %d numbers", campaign.id, len(campaign.records))
        tasks = [asyncio.create_task(self._run_call(campaign, rec)) for rec in campaign.records]
        campaign.tasks = tasks
        await asyncio.gather(*tasks, return_exceptions=True)
        campaign.completed = True
        campaign.notify()
        self.history.append(campaign)
        self.history = self.history[-20:]
        logger.info("Campaign %s finished", campaign.id)

    async def _run_call(self, campaign: Campaign, record: CallRecord) -> None:
        async with self._sem:
            if campaign.cancelled:
                record.outcome = Outcome.CANCELLED
                record.finished = True
                campaign.notify()
                return

            channel_id = f"pdz-{campaign.id}-{record.index}-{uuid.uuid4().hex[:6]}"
            record.channel_id = channel_id
            record.state = State.DIALING
            record.started_at = time.time()
            self._channels[channel_id] = (campaign, record, "customer")
            campaign.notify()

            endpoint = f"PJSIP/{dial_string(record.number)}@{self.cfg.sip_endpoint}"
            try:
                await self.ari.originate(
                    endpoint=endpoint,
                    channel_id=channel_id,
                    caller_id=self.cfg.caller_id,
                    timeout=self.cfg.call_timeout,
                    app_args=f"customer,{campaign.id}",
                )
            except ARIError as exc:
                logger.warning("Originate failed for %s: %s", record.number, exc)
                record.outcome = Outcome.FAILED
                record.finished = True
                record.ended_at = time.time()
                self._channels.pop(channel_id, None)
                campaign.notify()
                return

            budget = (
                self.cfg.call_timeout
                + (self.cfg.ivr_max_repeats + 1) * (15 + self.cfg.ivr_input_timeout)
                + self.cfg.call_timeout
                + 30
            )
            try:
                await asyncio.wait_for(record.done.wait(), timeout=budget)
            except asyncio.TimeoutError:
                logger.warning("Call budget exceeded for %s", record.number)
                if record.outcome is None:
                    record.outcome = Outcome.FAILED
                await self.ari.hangup(channel_id)
                record.finished = True
            finally:
                self._channels.pop(channel_id, None)
                if record.agent_channel_id:
                    self._channels.pop(record.agent_channel_id, None)
                campaign.notify()

    # ----------------------------------------------------------------- event handling
    async def handle_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        handler = getattr(self, f"_on_{etype}", None)
        if handler is not None:
            await handler(event)

    def _lookup(self, event: dict[str, Any]) -> tuple[Campaign, CallRecord, str] | None:
        channel = event.get("channel") or {}
        channel_id = channel.get("id")
        if not channel_id:
            return None
        return self._channels.get(channel_id)

    async def _on_StasisStart(self, event: dict[str, Any]) -> None:
        channel = event.get("channel") or {}
        channel_id = channel.get("id")
        args = event.get("args") or []
        role = args[0] if args else ""

        entry = self._channels.get(channel_id)
        if entry is None:
            # Unknown channel (e.g. inbound call hitting the Stasis context).
            logger.info("StasisStart for unmanaged channel %s args=%s -> hangup", channel_id, args)
            await self.ari.hangup(channel_id)
            return

        campaign, record, kind = entry
        if kind == "agent":
            await self._on_agent_answered(campaign, record)
            return

        # Customer answered the call.
        record.answered = True
        record.state = State.ANSWERED
        campaign.notify()
        await self.ari.answer(channel_id)
        asyncio.create_task(self._run_ivr(campaign, record), name=f"ivr-{channel_id}")

    async def _run_ivr(self, campaign: Campaign, record: CallRecord) -> None:
        try:
            attempt = 0
            while attempt <= self.cfg.ivr_max_repeats and not record.finished and not record.transferred:
                attempt += 1
                playback_id = f"pb-{record.channel_id}-{attempt}"
                record.playback_id = playback_id
                record.playback_done = asyncio.Event()
                media = self.locutions.active_media("cliente", self.cfg.sound_media)
                try:
                    await self.ari.play(record.channel_id, media, playback_id)
                except ARIError as exc:
                    logger.warning("Playback failed on %s: %s", record.number, exc)
                    break

                await self._wait(record.playback_done, timeout=30)
                if record.finished or record.transferred:
                    return

                got_digit = await self._wait(record.dtmf_event, timeout=self.cfg.ivr_input_timeout)
                if record.finished or record.transferred:
                    return
                if got_digit:
                    # '1' is handled by the DTMF event handler; any other digit
                    # just replays the prompt.
                    record.dtmf_event.clear()
                    continue

            if not record.transferred and not record.finished and record.outcome is None:
                record.outcome = Outcome.NO_INPUT
                await self.ari.hangup(record.channel_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("IVR error on %s", record.number)

    @staticmethod
    async def _wait(event: asyncio.Event, timeout: float) -> bool:
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _on_ChannelDtmfReceived(self, event: dict[str, Any]) -> None:
        entry = self._lookup(event)
        if entry is None:
            return
        campaign, record, kind = entry
        if kind != "customer":
            return
        digit = event.get("digit")
        record.dtmf_event.set()
        if digit == "1":
            asyncio.create_task(self._transfer(campaign, record), name=f"xfer-{record.channel_id}")

    async def _on_PlaybackFinished(self, event: dict[str, Any]) -> None:
        playback = event.get("playback") or {}
        pb_id = playback.get("id")
        for campaign, record, _kind in list(self._channels.values()):
            if record.playback_id == pb_id:
                record.playback_done.set()
                return
            if record.agent_playback_id == pb_id:
                # Agent has heard the identification message -> bridge them in.
                await self._bridge_agent(campaign, record)
                return

    async def _transfer(self, campaign: Campaign, record: CallRecord) -> None:
        async with record._transfer_lock:
            if record.transferred or record.finished:
                return
            record.transferred = True
            record.state = State.TRANSFERRING
            record.outcome = Outcome.PRESSED_1
            campaign.notify()

        if record.playback_id:
            await self.ari.stop_playback(record.playback_id)

        bridge_id = f"br-{record.channel_id}"
        record.bridge_id = bridge_id
        agent_channel_id = f"agent-{record.channel_id}"
        record.agent_channel_id = agent_channel_id
        self._channels[agent_channel_id] = (campaign, record, "agent")

        endpoint = self.cfg.agent_dial
        try:
            await self.ari.create_bridge(bridge_id)
            await self.ari.add_to_bridge(bridge_id, record.channel_id)
            await self.ari.originate(
                endpoint=endpoint,
                channel_id=agent_channel_id,
                caller_id=record.number.lstrip("+"),
                timeout=self.cfg.call_timeout,
                app_args=f"agent,{campaign.id}",
            )
        except ARIError as exc:
            logger.warning("Transfer failed for %s: %s", record.number, exc)
            record.outcome = Outcome.TRANSFER_FAILED
            self._channels.pop(agent_channel_id, None)
            await self.ari.hangup(record.channel_id)
            campaign.notify()

    async def _on_agent_answered(self, campaign: Campaign, record: CallRecord) -> None:
        if record.finished or not record.bridge_id:
            return
        # Optional identification message played privately to the agent before
        # the customer is connected ("te paso una llamada de verificación…").
        agent_media = self.locutions.active_media("agente")
        if agent_media:
            playback_id = f"agpb-{record.channel_id}"
            record.agent_playback_id = playback_id
            try:
                await self.ari.play(record.agent_channel_id, agent_media, playback_id)
                return  # bridging happens on PlaybackFinished
            except ARIError as exc:
                logger.warning("Agent greeting failed for %s: %s", record.number, exc)
        await self._bridge_agent(campaign, record)

    async def _bridge_agent(self, campaign: Campaign, record: CallRecord) -> None:
        if record.finished or not record.bridge_id or not record.agent_channel_id:
            return
        try:
            await self.ari.add_to_bridge(record.bridge_id, record.agent_channel_id)
            record.state = State.TRANSFERRED
            record.outcome = Outcome.TRANSFERRED
            campaign.notify()
        except ARIError as exc:
            logger.warning("Could not bridge agent for %s: %s", record.number, exc)

    async def _on_ChannelDestroyed(self, event: dict[str, Any]) -> None:
        channel = event.get("channel") or {}
        channel_id = channel.get("id")
        entry = self._channels.get(channel_id)
        if entry is None:
            return
        campaign, record, kind = entry
        cause = event.get("cause")

        if kind == "agent":
            # Agent leg gone: drop the customer too if still connected.
            self._channels.pop(channel_id, None)
            if not record.finished and record.channel_id:
                await self.ari.hangup(record.channel_id)
            return

        await self._finalize(campaign, record, cause)

    async def _finalize(self, campaign: Campaign, record: CallRecord, cause: int | None) -> None:
        if record.finished:
            return
        record.finished = True
        record.state = State.DONE
        record.ended_at = time.time()
        if record.outcome is None:
            record.outcome = Outcome.NO_INPUT if record.answered else _outcome_from_cause(cause)

        # Tear down any transfer infrastructure.
        if record.agent_channel_id:
            await self.ari.hangup(record.agent_channel_id)
        if record.bridge_id:
            await self.ari.destroy_bridge(record.bridge_id)

        record.done.set()
        record.dtmf_event.set()
        record.playback_done.set()
        campaign.notify()
