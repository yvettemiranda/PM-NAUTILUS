"""Hot-path savings keep every decision identical; no venue traffic.

Measured on the production TEST server (2026-11-02, py-spy): rebuilding every
consumer's subscription set on each received frame and evaluating a single
dirty Event twice were each about a fifth of the busy main thread. These tests
pin that the cheaper paths act exactly like the original ones.
"""

import asyncio
from random import Random
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest
from nautilus_trader.common.component import TestClock

from pm_nautilus.books import Side
from pm_nautilus.market import FRESH_BASELINE_RECOVERY_SECONDS, PublicMarketHub
from pm_nautilus.rules import Fees, Token
from pm_nautilus.strategy import Runtime

DAY = 86_400_000_000_000


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


def runtime(path):
    clock = TestClock()
    clock.set_time(DAY)
    r = Runtime(path, clock=clock)
    r.add_tokens(tokens())
    return r, clock


def state(r):
    intents = sorted(
        (
            i["event_id"],
            i["token_id"],
            i["side"],
            i.get("cash"),
            i.get("quantity"),
            i.get("limit"),
            i.get("spent"),
            i.get("terminal"),
        )
        for i in r.store.intents().values()
    )
    cycles = {
        eid: {k: v for k, v in c.items() if k != "targets"}
        for eid, c in r.business["cycles"].items()
    }
    targets = sorted((v["event_id"], v["price"], v["qty"]) for v in r.business["targets"].values())
    books = {
        tid: (sorted(b.bid.consumed.items()), sorted(b.ask.consumed.items()))
        for tid, b in r.books.items()
    }
    return intents, cycles, targets, books, r.cash(), r.business["banned"], r.business["realized"]


@pytest.mark.parametrize("seed", range(6))
def test_single_event_fast_path_matches_original_drain_over_random_quotes(tmp_path, seed):
    fast, fast_clock = runtime(tmp_path / "fast.sqlite")
    slow, slow_clock = runtime(tmp_path / "slow.sqlite")
    slow.drain = MethodType(legacy_drain, slow)
    rng = Random(seed)
    ids = [t.token_id for t in tokens()]
    try:
        for r in (fast, slow):
            r.start()
        now = DAY
        for step in range(400):
            tid = rng.choice(ids)
            ask = rng.randrange(10, 70) * 1000
            bid = rng.randrange(max(1, ask // 2000), ask // 1000) * 1000
            bids = [(bid, rng.randrange(1, 200) * 1_000_000)]
            if rng.random() < 0.5:
                bids.append((max(1000, bid - 5000), rng.randrange(1, 200) * 1_000_000))
            asks = [(ask, rng.randrange(1, 200) * 1_000_000)]
            if rng.random() < 0.3:
                now += rng.randrange(1, 40) * 1_000_000_000
                fast_clock.set_time(now)
                slow_clock.set_time(now)
            for r in (fast, slow):
                r.book(tid, list(bids), list(asks))
            if step % 25 == 24:
                # Control ticks dirty many Events at once: the ordered path.
                for r in (fast, slow):
                    r.refresh_eligibility()
                    r.drain()
            assert state(fast) == state(slow), step
        assert fast.store.intents()  # The scenario really traded.
    finally:
        fast.close()
        slow.close()


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


recovery_service = RecoveryService


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
                    recovery_service(),
                    recovery_service({"1"}, {"1": now}),
                    recovery_service((), {"2": now - 60}),
                    recovery_service({"2"}, {"2": now - 60}, closed=True),
                }
            )
            await hub.recover_consumer_baselines(group, ws)
            hub.baseline_consumers.assert_not_called()
            hub.services.add(
                recovery_service({"1"}, {"1": now - FRESH_BASELINE_RECOVERY_SECONDS - 1})
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


def test_fast_path_keeps_stop_loss_exit_and_refreshes_like_the_original(tmp_path):
    fast, fast_clock = runtime(tmp_path / "stop-fast.sqlite")
    slow, slow_clock = runtime(tmp_path / "stop-slow.sqlite")
    slow.drain = MethodType(legacy_drain, slow)
    try:
        for r in (fast, slow):
            quote_both(r, 0)
            r.start()
        for second in range(0, 120, 5):
            for r, clock in ((fast, fast_clock), (slow, slow_clock)):
                clock.set_time(DAY + second * 1_000_000_000)
                # Collapse the bid below the stop threshold, then keep quoting.
                r.book("00", [(2_000 + second, 100_000_000)], [(20_000, 50_000_000)])
            assert state(fast) == state(slow), second
        stop = fast.business["cycles"].get("event-0", {}).get("stop", {})
        assert stop.get("state") in {"EXITING", "STOPPED", None}
    finally:
        fast.close()
        slow.close()
