"""Hot-path savings keep every decision identical; no venue traffic.

Measured on the production TEST server (2026-11-02, py-spy): rebuilding every
consumer's subscription set on each received frame and evaluating a single
dirty Event twice were each about a fifth of the busy main thread. These tests
pin that the cheaper paths act exactly like the original ones.
"""

import asyncio
from contextlib import contextmanager
from itertools import count
from random import Random
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from nautilus_trader.common.component import TestClock

from pm_nautilus.books import Side
from pm_nautilus.market import FRESH_BASELINE_RECOVERY_SECONDS, PublicMarketHub
from pm_nautilus.rules import Fees, Token
import pm_nautilus.strategy as strategy_module
from pm_nautilus.strategy import Runtime
from pm_nautilus.test_runtime import TestRuntime

DAY = 86_400_000_000_000
# Production TEST accounts run TestRuntime (PAUSED deferral, preview tracking).
BOTH_RUNTIMES = pytest.mark.parametrize(
    "cls", [Runtime, TestRuntime], ids=["Runtime", "TestRuntime"]
)


def legacy_drain(self):
    """The drain before the single-Event fast path, kept verbatim as reference."""
    if self.faulted:
        return
    pending = self.dirty
    self.dirty = set()
    for eid in pending:
        self.evaluate(eid, dispatch=False)

    def key(eid):
        ready = self.evaluations.get(eid, ("NO_WINNER", None))[0] == "READY"
        ts = [self.tokens[x] for x in self.events.get(eid, set())]
        progress = min((t.progress(self.now) for t in ts), default=0)
        return (
            not ready,
            progress * (1 if self.preferences.candidateSortDirection == "ASC" else -1),
            eid,
        )

    for eid in sorted(pending, key=key):
        self.evaluate(eid)
    exits = [eid for eid in self.dirty if eid in self.business["cycles"]]
    self.dirty = set()
    for eid in exits:
        self.evaluate(eid)


def tokens():
    out = []
    for event in range(4):
        for outcome, direction in enumerate(("YES", "NO")):
            out.append(
                Token(
                    f"{event}{outcome}",
                    f"event-{event}",
                    f"market-{event}",
                    f"condition-{event}",
                    direction,
                    2,
                    False,
                    event * DAY // 10,
                    (10 + event) * DAY,
                    Fees(event % 2 == 1, 40_000 if event % 2 else 0),
                    5_000_000,
                    1000,
                )
            )
    return out


def runtime(path, cls=Runtime):
    clock = TestClock()
    clock.set_time(DAY)
    r = cls(path, clock=clock)
    r.add_tokens(tokens())
    return r, clock


def state(r):
    intents = sorted(
        (oid, {k: v for k, v in i.items() if k != "generation"})
        for oid, i in r.store.intents().items()
    )
    books = {
        tid: (sorted(b.bid.consumed.items()), sorted(b.ask.consumed.items()))
        for tid, b in r.books.items()
    }
    business = {k: v for k, v in r.business.items() if k != "cursor"}
    # What the page shows and what is still pending, not only what traded.
    evaluations = {
        eid: (status, winner and winner.token.token_id)
        for eid, (status, winner) in r.evaluations.items()
    }
    return (
        intents,
        business,
        books,
        r.cash(),
        r.status,
        evaluations,
        sorted(r.dirty),
        sorted(r._time_sensitive_events),
        sorted(getattr(r, "_test_preview_pending", ())),
    )


@contextmanager
def drain_variant(legacy):
    """Repeat client order IDs (and trade IDs derived from them) per run.

    The legacy variant replaces Runtime.drain itself, so TestRuntime's PAUSED
    deferral still wraps whichever drain is under test.
    """
    with pytest.MonkeyPatch.context() as patch:
        counter = count()
        patch.setattr(
            strategy_module, "uuid4", lambda: SimpleNamespace(hex=f"{next(counter):032x}")
        )
        if legacy:
            patch.setattr(Runtime, "drain", legacy_drain)
        yield


def run_random_quotes(path, seed, cls, legacy):
    with drain_variant(legacy):
        r, clock = runtime(path, cls)
        rng = Random(seed)
        ids = [t.token_id for t in tokens()]
        states = []
        try:
            r.start()
            now = DAY
            for step in range(400):
                if step == 250:
                    # PAUSED still sells and watches stops; TEST defers the rest.
                    r.pause()
                tid = rng.choice(ids)
                ask = rng.randrange(10, 70) * 1000
                bid = rng.randrange(max(1, ask // 2000), ask // 1000) * 1000
                bids = [(bid, rng.randrange(1, 200) * 1_000_000)]
                if rng.random() < 0.5:
                    bids.append((max(1000, bid - 5000), rng.randrange(1, 200) * 1_000_000))
                asks = [(ask, rng.randrange(1, 200) * 1_000_000)]
                if rng.random() < 0.3:
                    now += rng.randrange(1, 40) * 1_000_000_000
                    clock.set_time(now)
                r.book(tid, bids, asks)
                if step % 25 == 24:
                    # Control ticks dirty many Events at once: the ordered path.
                    r.refresh_eligibility()
                    r.drain()
                states.append(state(r))
            for tid in ids:
                # Bids far above every target: owned positions sell in both runs.
                r.book(tid, [(500_000, 1_000_000_000)], [(510_000, 100_000_000)])
                states.append(state(r))
            return states
        finally:
            r.close()


@BOTH_RUNTIMES
@pytest.mark.parametrize("seed", range(6))
def test_single_event_fast_path_matches_original_drain_over_random_quotes(tmp_path, cls, seed):
    fast = run_random_quotes(tmp_path / "fast" / "s.sqlite", seed, cls, legacy=False)
    slow = run_random_quotes(tmp_path / "slow" / "s.sqlite", seed, cls, legacy=True)
    for step, (a, b) in enumerate(zip(fast, slow, strict=True)):
        assert a == b, step
    assert fast[-1][0]  # The scenario really traded.
    assert any(i["side"] == "SELL" for _, i in fast[-1][0])  # ... and sold.


def quote_both(r, event):
    for outcome in (0, 1):
        r.book(f"{event}{outcome}", [(15_000, 100_000_000)], [(20_000, 50_000_000)])


def test_single_dirty_event_is_evaluated_once_and_several_keep_the_ordered_prepass(
    tmp_path, monkeypatch
):
    r, _ = runtime(tmp_path / "count.sqlite")
    try:
        quote_both(r, 0)
        quote_both(r, 1)
        evaluate = Mock(wraps=r.evaluate)
        monkeypatch.setattr(r, "evaluate", evaluate)
        r.dirty = {"event-0"}
        r.drain()
        assert [c.kwargs.get("dispatch", True) for c in evaluate.call_args_list] == [True]
        evaluate.reset_mock()
        r.dirty = {"event-0", "event-1"}
        r.drain()
        dispatch = [c.kwargs.get("dispatch", True) for c in evaluate.call_args_list]
        assert dispatch == [False, False, True, True]
    finally:
        r.close()


def test_preview_cache_hit_does_not_rebuild_depth_lists(tmp_path, monkeypatch):
    r, _ = runtime(tmp_path / "depth.sqlite")
    try:
        quote_both(r, 0)
        first = r.evaluations["event-0"]
        assert first[0] == "READY"
        original = Side.available
        calls = []

        def counted(side):
            calls.append(side)
            return original(side)

        monkeypatch.setattr(Side, "available", counted)
        r.evaluate("event-0", dispatch=False)
        assert calls == [] and r.evaluations["event-0"] == first
        r.books["00"].ask.consume(20_000, 1_000_000)  # Consumption invalidates the key.
        r.evaluate("event-0", dispatch=False)
        assert len(calls) == 2  # Bid and Ask of the changed outcome only.
    finally:
        r.close()


class RecoveryService:
    def __init__(self, pending=(), requested=None, closed=False):
        self.closed = closed
        self._availability_fresh_pending = set(pending)
        self._availability_fresh_requested_at = dict(requested or {})


def test_frame_without_due_baseline_skips_consumer_subscription_rebuilds():
    async def run():
        hub = PublicMarketHub()
        group = ("public-1",)
        hub.active_groups[group] = {"1", "2"}
        hub.streams[group] = {"state": "READY", "error": None}
        hub.baseline_consumers = Mock(return_value=[])
        ws = SimpleNamespace(send=Mock())
        now = asyncio.get_running_loop().time()
        try:
            # No pending request, a fresh one, and a stale entry no longer pending.
            hub.services.update(
                {
                    RecoveryService(),
                    RecoveryService({"1"}, {"1": now}),
                    RecoveryService((), {"2": now - 60}),
                    RecoveryService({"2"}, {"2": now - 60}, closed=True),
                }
            )
            await hub.recover_consumer_baselines(group, ws)
            hub.baseline_consumers.assert_not_called()
            hub.services.add(
                RecoveryService({"1"}, {"1": now - FRESH_BASELINE_RECOVERY_SECONDS - 1})
            )
            await hub.recover_consumer_baselines(group, ws)
            hub.baseline_consumers.assert_called_once_with(group)
            ws.send.assert_not_called()  # Mocked consumers own nothing due here.
        finally:
            hub.services.clear()
            await hub.close()

    asyncio.run(run())


def test_membership_view_matches_sorted_subscription_ids():
    async def run():
        hub = PublicMarketHub()
        try:
            group = ("public-1",)
            hub.active_groups[group] = {"3", "1", "2"}
            assert set(hub.subscription_members(group)) == set(hub.subscription_ids(group))
            assert hub.subscription_ids(group) == ("1", "2", "3")
            assert tuple(hub.subscription_members(("missing",))) == hub.subscription_ids(
                ("missing",)
            )
        finally:
            await hub.close()

    asyncio.run(run())


def run_stop_scenario(path, cls, legacy):
    with drain_variant(legacy):
        r, clock = runtime(path, cls)
        states = []
        try:
            quote_both(r, 0)
            r.start()
            for second in range(0, 120, 5):
                clock.set_time(DAY + second * 1_000_000_000)
                # Collapse both outcomes' bids below the stop threshold, whichever
                # one was bought, then keep quoting past the 30 s confirmation.
                # The bid stays on tick (so the STOP can fill); its size changes
                # the book version that the confirmation requires.
                for tid in ("00", "01"):
                    r.book(tid, [(2_000, (100 + second) * 1_000_000)], [(20_000, 50_000_000)])
                states.append(state(r))
            return states
        finally:
            r.close()


@BOTH_RUNTIMES
def test_fast_path_keeps_stop_loss_exit_like_the_original(tmp_path, cls):
    fast = run_stop_scenario(tmp_path / "fast" / "s.sqlite", cls, legacy=False)
    slow = run_stop_scenario(tmp_path / "slow" / "s.sqlite", cls, legacy=True)
    assert fast == slow
    # The scenario really bought, exited through the stop-loss and released.
    business = fast[-1][1]
    assert "event-0" in business["banned"] and "event-0" not in business["cycles"]
    assert any(i["side"] == "SELL" and i["kind"] == "STOP" for _, i in fast[-1][0])
