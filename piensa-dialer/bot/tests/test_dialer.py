import asyncio

import pytest

from app.dialer import Campaign, CallRecord, Outcome, _outcome_from_cause


def make_campaign(n=3):
    records = [CallRecord(number=f"+3460000000{i}", index=i) for i in range(n)]
    return Campaign(id="test1234", chat_id=1, records=records)


def test_outcome_from_cause_mapping():
    assert _outcome_from_cause(17) == Outcome.BUSY
    assert _outcome_from_cause(19) == Outcome.NO_ANSWER
    assert _outcome_from_cause(21) == Outcome.REJECTED
    assert _outcome_from_cause(None) == Outcome.FAILED
    assert _outcome_from_cause(999) == Outcome.FAILED


def test_snapshot_counts():
    c = make_campaign(3)
    c.records[0].outcome = Outcome.TRANSFERRED
    c.records[0].finished = True
    c.records[1].outcome = Outcome.NO_ANSWER
    c.records[1].finished = True
    s = c.snapshot()
    assert s["total"] == 3
    assert s["done"] == 2
    assert s["queued"] == 1
    assert s["success"] == 1
    assert s["counts"][Outcome.TRANSFERRED] == 1
    assert s["counts"][Outcome.NO_ANSWER] == 1


def test_pressed_one_counts_as_success():
    c = make_campaign(2)
    c.records[0].outcome = Outcome.PRESSED_1
    assert c.snapshot()["success"] == 1


def test_notify_sets_event():
    c = make_campaign(1)
    assert not c.progress_event.is_set()
    c.notify()
    assert c.progress_event.is_set()
