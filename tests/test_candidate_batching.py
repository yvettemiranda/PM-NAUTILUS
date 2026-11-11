"""Candidate batching: positions act on every quote, candidates once per interval."""

from dataclasses import replace

import pytest

import pm_nautilus.market as market_module
from pm_nautilus.app import _apply_pacing
from pm_nautilus.strategy import Runtime
from pm_nautilus.test_runtime import TestRuntime

from test_runtime import setup

SECOND = 1_000_000_000


@pytest.fixture
def batching(monkeypatch):
    monkeypatch.setattr(Runtime, "CANDIDATE_BATCH_NS", 2 * SECOND)


def two_markets(tmp_path, clock, token):
    other = replace(
        token, token_id="2", event_id="event-2", market_id="market-2", condition_id="c2"
    )
    r = TestRuntime(tmp_path / "t.sqlite", clock=clock)
    r.add_tokens([token, other])
    r.update_preferences({"maxBuyPriceCents": "3", "orderAmount": "2"})
    return r, other


def buys(r):
    return sorted(i["token_id"] for i in r.store.intents().values() if i["side"] == "BUY")


def test_candidates_wait_for_the_batch_but_positions_sell_at_once(tmp_path, batching):
    reference, clock, token = setup(tmp_path / "reference")
    reference.close()
    r, other = two_markets(tmp_path, clock, token)
    try:
        now = clock.timestamp_ns()
        r.start()  # START evaluates every candidate at once (books not ready yet).
        r.book(token.token_id, [(15_000, 100_000_000)], [(20_000, 100_000_000)], 1)
        assert buys(r) == []  # Within the interval: candidate waits.
        clock.set_time(now + 2 * SECOND)
        r.book(other.token_id, [(10, 1)], [(900_000, 1)], 2)  # Any later quote flushes.
        assert buys(r) == [token.token_id]
        # Held position: a target-price bid sells on that very quote.
        r.book(token.token_id, [(500_000, 1_000_000_000)], [(510_000, 100_000_000)], 3)
        assert any(i["side"] == "SELL" for i in r.store.intents().values())
    finally:
        r.close()


def test_control_tick_flushes_waiting_candidates(tmp_path, batching):
    reference, clock, token = setup(tmp_path / "reference")
    reference.close()
    r, other = two_markets(tmp_path, clock, token)
    try:
        now = clock.timestamp_ns()
        r.start()
        r.book(token.token_id, [(15_000, 100_000_000)], [(20_000, 100_000_000)], 1)
        assert buys(r) == []
        clock.set_time(now + 3 * SECOND)
        r.refresh_eligibility()
        r.drain()
        assert buys(r) == [token.token_id]
    finally:
        r.close()


def test_batching_off_by_default_evaluates_every_quote(tmp_path):
    reference, clock, token = setup(tmp_path / "reference")
    reference.close()
    r, other = two_markets(tmp_path, clock, token)
    try:
        assert Runtime.CANDIDATE_BATCH_NS == 0
        r.start()
        r.book(token.token_id, [(15_000, 100_000_000)], [(20_000, 100_000_000)], 1)
        assert buys(r) == [token.token_id]
    finally:
        r.close()


def test_pacing_environment_is_validated(monkeypatch):
    monkeypatch.setattr(market_module, "DISCOVERY_SECONDS", 300)
    monkeypatch.setattr(Runtime, "CANDIDATE_BATCH_NS", 0)
    _apply_pacing({})
    assert market_module.DISCOVERY_SECONDS == 300 and Runtime.CANDIDATE_BATCH_NS == 0
    _apply_pacing({"PM_DISCOVERY_SECONDS": "900", "PM_CANDIDATE_BATCH_SECONDS": "1.5"})
    assert market_module.DISCOVERY_SECONDS == 900
    assert Runtime.CANDIDATE_BATCH_NS == 1_500_000_000 and TestRuntime.CANDIDATE_BATCH_NS
    for bad in ({"PM_DISCOVERY_SECONDS": "5"}, {"PM_CANDIDATE_BATCH_SECONDS": "60"}):
        with pytest.raises(ValueError):
            _apply_pacing(bad)
