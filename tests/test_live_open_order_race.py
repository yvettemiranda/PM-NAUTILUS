"""Controlled CLOB/journal timing; no network, keys, or orders."""

import asyncio
from threading import Event

import pytest

from pm_nautilus.live_account_guard import audit_open_orders


class DelayedClob:
    def __init__(self, *responses):
        self.responses = responses
        self.started = Event()
        self.release = Event()
        self.calls = 0

    def get_open_orders(self):
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            assert self.release.wait(timeout=5), "test did not release the first CLOB read"
        return self.responses[self.calls - 1]


async def audit_during_change(clob, journal, change):
    task = asyncio.create_task(audit_open_orders(clob, lambda: journal["intents"]))
    try:
        assert await asyncio.to_thread(clob.started.wait, 5)
        change(journal)
    finally:
        clob.release.set()
    return await task


def test_order_persisted_during_clob_query_is_not_mistaken_for_manual_order():
    clob = DelayedClob([{"id": "new-program-order"}])
    journal = {"intents": {}}

    def persist(journal):
        journal["intents"] = {"client-id": {"terminal": False, "venue_id": "new-program-order"}}

    audit = asyncio.run(audit_during_change(clob, journal, persist))
    assert audit.ok and audit.unknown_count == 0
    assert clob.calls == 1


@pytest.mark.parametrize(
    ("second_response", "expected_ok"),
    [([], True), ([{"id": "formerly-owned"}], False)],
)
def test_order_terminal_during_clob_query_requires_fresh_remote_confirmation(
    second_response, expected_ok
):
    clob = DelayedClob([{"id": "formerly-owned"}], second_response)
    journal = {"intents": {"client-id": {"terminal": False, "venue_id": "formerly-owned"}}}

    def finish(journal):
        journal["intents"]["client-id"]["terminal"] = True

    audit = asyncio.run(audit_during_change(clob, journal, finish))
    assert audit.ok is expected_ok
    assert audit.unknown_order_ids == (() if expected_ok else ("formerly-owned",))
    assert clob.calls == 2


@pytest.mark.parametrize(
    ("second_response", "expected_ok"),
    [([], True), ([{"id": "short-lived"}], False)],
)
def test_order_created_and_terminalized_during_query_is_never_whitelisted(
    second_response, expected_ok
):
    clob = DelayedClob([{"id": "short-lived"}], second_response)
    journal = {"intents": {}}

    def create_and_finish(journal):
        journal["intents"] = {"client-id": {"terminal": True, "venue_id": "short-lived"}}

    audit = asyncio.run(audit_during_change(clob, journal, create_and_finish))
    assert audit.ok is expected_ok
    assert audit.unknown_order_ids == (() if expected_ok else ("short-lived",))
    assert clob.calls == 2


def test_incomplete_second_clob_response_fails_closed():
    clob = DelayedClob([{"id": "manual"}], [{"asset_id": "missing-id"}])
    journal = {"intents": {}}
    audit = asyncio.run(audit_during_change(clob, journal, lambda _: None))
    assert not audit.ok
    assert audit.error == "INVALID_OPEN_ORDER_RESPONSE"
    assert clob.calls == 2
