"""Queue and ring-group strategy helpers for SMURF PBX."""

from __future__ import annotations

import random
from collections import defaultdict


class StrategySelector:
    """Selection engine for queue and group member strategies."""

    def __init__(self):
        self.group_rr_index: dict[str, int] = defaultdict(int)
        self.queue_rr_index: dict[str, int] = defaultdict(int)

    def choose_group_member(
        self,
        group_number: str,
        members: list[str],
        strategy: str,
        is_registered: callable,
    ) -> str | None:
        if not members:
            return None
        mode = strategy.lower().strip()
        if mode in {"all", "first"}:
            for member in members:
                if is_registered(member):
                    return member
            return members[0]
        if mode == "random":
            pool = members[:]
            random.shuffle(pool)
            return pool[0]
        if mode == "round_robin":
            idx = self.group_rr_index[group_number] % len(members)
            self.group_rr_index[group_number] = (idx + 1) % len(members)
            return members[idx]
        return members[0]

    def choose_queue_member(
        self,
        queue_number: str,
        members: list[str],
        strategy: str,
        is_registered: callable,
        active_calls: dict[str, int],
    ) -> str | None:
        if not members:
            return None
        mode = strategy.lower().strip()
        available = [m for m in members if is_registered(m)]
        if not available:
            available = members
        if mode == "random":
            return random.choice(available)
        if mode == "least_busy":
            available.sort(key=lambda m: active_calls.get(m, 0))
            return available[0]
        if mode == "priority":
            return available[0]
        idx = self.queue_rr_index[queue_number] % len(available)
        self.queue_rr_index[queue_number] = (idx + 1) % len(available)
        return available[idx]
