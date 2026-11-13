"""Compare refreshed ownership/control with the frozen deployed implementation."""

import asyncio
from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from nautilus_trader.common.component import TestClock

import pm_nautilus.strategy as strategy
from pm_nautilus.rules import Fees, StopLoss, Token
from pm_nautilus.strategy import Runtime
from pm_nautilus.test_runtime import TestRuntime

from refresh_eligibility_reference import refresh_eligibility as reference_refresh


DAY = 86_400_000_000_000


def make_runtime(path, runtime_type, mode="TEST"):
    clock = TestClock()
    clock.set_time(DAY)
    if mode == "LIVE":
        from test_live_recovery import factory

        runtime = runtime_type(path, mode=mode, clock=clock, live_factory=factory)
    else:
        runtime = runtime_type(path, clock=clock)
    # Drive control inputs without dispatching orders or connecting a venue.
    runtime.store.put("status", "RUNNING")
    base = Token(
        "1",
        "event-0",
        "market-0",
        "condition-0",
        "YES",
        2,
        False,
        0,
        10 * DAY,
        Fees(False),
        5_000_000,
        1_000,
        category_ids=("official",),
    )
    runtime.add_tokens(
        [
            replace(
                base,
                token_id=str(i),
                event_id=f"event-{(i - 1) // 8}",
                market_id=f"market-{(i - 1) // 2}",
                condition_id=f"condition-{(i - 1) // 2}",
            )
            for i in range(1, 65)
        ],
        official_tags=["official"],
    )
    return runtime, clock


def finance(runtime):
    return deepcopy(
        (
            runtime.business,
            runtime.store.intents(),
            runtime.cash(),
            runtime.held_cash(),
            runtime.preferences.model_dump(),
            runtime.status,
        )
    )


def observation(runtime):
    return deepcopy(
        {
            "groups": [(key, tuple(ids)) for key, ids in runtime.events.items()],
            "monitored": tuple(runtime.monitored),
            "dirty": tuple(runtime.dirty),
            "states": list(runtime._refresh_events.items()),
            "control": runtime._refresh_control,
            "scope_keys": runtime._cycle_scope_keys,
            "terminal": runtime._terminal_events,
            "time_sensitive": runtime._time_sensitive_events,
            "test_pending": getattr(runtime, "_test_preview_pending", None),
            "test_epoch": getattr(runtime, "test_market_epoch", None),
            "financial": finance(runtime),
        }
    )


def run_control_trace(path, runtime_type, mode="TEST"):
    runtime, clock = make_runtime(path, runtime_type, mode)
    trace = []

    def sample(label):
        before, changes = finance(runtime), runtime.store.db.total_changes
        runtime.refresh_eligibility()
        assert finance(runtime) == before, label
        assert runtime.store.db.total_changes == changes, label
        trace.append((label, observation(runtime)))
        runtime.dirty.clear()

    try:
        sample("initial")
        sample("unchanged")
        runtime.add_tokens([replace(runtime.tokens["1"], question="new metadata")])
        sample("non-group metadata")
        runtime.add_tokens([replace(runtime.tokens["2"], event_id="moved-event")])
        sample("event metadata")
        runtime.business["cycles"]["event-0"] = {
            "token_id": "1",
            "quantity": 50_000_000,
            "spent": 1_000_000,
            "budget": 2_000_000,
            "sold": False,
            "stop": asdict(StopLoss(True, 400_000)),
        }
        runtime._cycle_by_token["1"] = "event-0"
        runtime.business["targets"]["target"] = {
            "id": "target",
            "event_id": "event-0",
            "qty": 50_000_000,
            "price": 30_000,
            "created": DAY,
        }
        sample("position ownership")
        runtime.preferences.cyclePerMarket = True
        sample("scope switch with persisted position")
        runtime.store.save_intent(
            "pending",
            {
                "event_id": "event-1",
                "token_id": "9",
                "side": "BUY",
                "kind": "BUY",
                "cash": 2_000_000,
                "spent": 0,
                "terminal": False,
            },
        )
        sample("inflight frozen scope")
        runtime.business["cycles"]["event-0"]["spent"] += 10_000
        runtime.business["cycles"]["event-0"]["quantity"] += 500_000
        runtime.business["cycles"]["event-0"]["stop"]["state"] = "ARMED"
        sample("cycle and stop update")
        runtime.store.save_intent("pending", dict(runtime.store.intent("pending"), terminal=True))
        runtime.note_terminal("event-1", runtime.tokens["9"])
        sample("terminal transition between ticks")
        runtime.business["cycles"].clear()
        runtime.business["targets"].clear()
        sample("released persisted scope")
        runtime.preferences.includeIndependentMultiMarkets = True
        runtime.add_tokens(
            [replace(runtime.tokens["3"], independent_market=True, market_id="replacement-market")]
        )
        sample("independent market scope")
        runtime.add_tokens([replace(runtime.tokens["4"], condition_id="replacement-condition")])
        sample("condition metadata")
        runtime.business["settled"].append("condition-2")
        sample("settled condition")
        runtime.market_availability["7"] = {
            "status": "UNAVAILABLE",
            "marketId": "market-3",
            "conditionId": "condition-3",
        }
        sample("market unavailable")
        runtime.market_availability.clear()
        runtime.market_recovering.add("7")
        sample("recovering book")
        runtime.market_recovering.clear()
        runtime.business["banned"].append("condition:condition-4")
        runtime._time_sensitive_events.update({"condition:condition-5", "absent"})
        runtime._terminal_events.add("terminal-only")
        sample("banned terminal and time-sensitive work")
        runtime.storage_guard = SimpleNamespace(can_open=False)
        sample("storage control")
        runtime.storage_guard = None
        runtime.preferences.selectedCategoryIds = ["official"]
        runtime.preferences.allCategories = False
        runtime.official_tags.clear()
        sample("official category removed")
        runtime.official_tags.add("official")
        sample("official category restored")
        runtime.store.put("status", "PAUSED")
        sample("paused candidate ownership")
        runtime.store.put("status", "RUNNING")
        sample("running candidate restoration")
        clock.set_time(2 * DAY + 1)
        sample("progress boundary")
        runtime.preferences.maxMarketProgressPercent = 100
        sample("progress preference")
        clock.set_time(10 * DAY)
        sample("end time boundary")
        sample("empty unchanged")
        if mode == "LIVE":
            runtime.client._submit_order.assert_not_called()
            runtime.client._cancel_order.assert_not_called()
        return trace
    finally:
        runtime.close()


@pytest.mark.parametrize("runtime_type", [Runtime, TestRuntime])
def test_refresh_matches_deployed_order_dirty_and_financial_trace(
    tmp_path, monkeypatch, runtime_type
):
    with monkeypatch.context() as original:
        original.setattr(Runtime, "refresh_eligibility", reference_refresh)
        before = run_control_trace(tmp_path / "reference.sqlite", runtime_type)
    after = run_control_trace(tmp_path / "optimized.sqlite", runtime_type)
    assert after == before


def test_live_refresh_matches_deployed_trace_without_venue_calls(tmp_path, monkeypatch):
    from test_live_recovery import flush

    async def compare():
        with monkeypatch.context() as original:
            original.setattr(Runtime, "refresh_eligibility", reference_refresh)
            before = run_control_trace(tmp_path / "reference.sqlite", Runtime, "LIVE")
        await flush()
        after = run_control_trace(tmp_path / "optimized.sqlite", Runtime, "LIVE")
        await flush()
        assert after == before

    asyncio.run(compare())


@pytest.mark.parametrize("runtime_type", [Runtime, TestRuntime])
def test_refresh_matches_deployed_real_fill_and_pause_trace(tmp_path, monkeypatch, runtime_type):
    from test_hot_path_efficiency import run_random_quotes

    with monkeypatch.context() as original:
        original.setattr(Runtime, "refresh_eligibility", reference_refresh)
        before = run_random_quotes(tmp_path / "reference" / "state.sqlite", 13, runtime_type, False)
    after = run_random_quotes(tmp_path / "optimized" / "state.sqlite", 13, runtime_type, False)
    for step, (expected, actual) in enumerate(zip(before, after, strict=True)):
        assert actual == expected, step
    assert any(intent["side"] == "BUY" for _, intent in after[-1][0])
    assert any(intent["side"] == "SELL" for _, intent in after[-1][0])


@pytest.mark.parametrize("runtime_type", [Runtime, TestRuntime])
def test_same_members_with_changed_iteration_rebuild_exact_old_order(
    tmp_path, monkeypatch, runtime_type
):
    class CollidingId(str):
        def __hash__(self):
            return 1

    runtime, _ = make_runtime(tmp_path / "state.sqlite", runtime_type)
    try:
        base = runtime.tokens["1"]
        runtime.tokens.clear()
        runtime.add_tokens([replace(base, token_id=CollidingId(str(i))) for i in range(101, 129)])
        before, signature = runtime.events, runtime._eligibility_group_signature
        candidates = list(runtime.tokens.items())[::-1]
        monkeypatch.setattr(runtime, "_eligibility_candidates", lambda: candidates)
        assert tuple({tid for tid, _ in candidates}) != signature[0]
        runtime.refresh_eligibility()
        assert runtime.events == before and runtime.events is not before
        actual = [(key, tuple(ids)) for key, ids in runtime.events.items()]
        reference_refresh(runtime)
        assert actual == [(key, tuple(ids)) for key, ids in runtime.events.items()]
    finally:
        runtime.close()


@pytest.mark.parametrize("runtime_type", [Runtime, TestRuntime])
def test_unchanged_groups_reuse_allocations_but_recheck_every_token(
    tmp_path, monkeypatch, runtime_type
):
    runtime, _ = make_runtime(tmp_path / "state.sqlite", runtime_type)
    try:
        runtime.refresh_eligibility()
        groups, states = runtime.events, runtime._refresh_events
        static = Mock(wraps=strategy.static_reason)
        scope = Mock(wraps=runtime.cycle_id)
        monkeypatch.setattr(strategy, "static_reason", static)
        monkeypatch.setattr(runtime, "cycle_id", scope)
        runtime.refresh_eligibility()
        assert static.call_count == len(runtime.tokens)
        assert scope.call_count == len(runtime.tokens)
        assert runtime.events is groups
        assert all(runtime.events[key] is ids for key, ids in groups.items())
        assert all(runtime._refresh_events[key] is states[key] for key in states)
        assert len({id(state) for state in runtime._refresh_events.values()}) == 1
        # Replacing the owner mapping cannot reuse evidence for the old object.
        runtime.events = {}
        runtime.refresh_eligibility()
        assert runtime.events == groups and runtime.events is not groups
    finally:
        runtime.close()


@pytest.mark.parametrize("failure", ["scope", "control", "intents"])
def test_failed_refresh_never_publishes_group_cache(tmp_path, monkeypatch, failure):
    runtime, _ = make_runtime(tmp_path / "state.sqlite", Runtime)
    try:
        old_signature = runtime._eligibility_group_signature
        runtime.tokens["1"] = replace(runtime.tokens["1"], event_id="new-event")
        with monkeypatch.context() as failing:

            def fail(*args, **kwargs):
                raise RuntimeError("controlled refresh failure")

            if failure == "scope":
                failing.setattr(runtime, "cycle_id", fail)
            elif failure == "control":
                failing.setattr(runtime, "cash", fail)
            else:
                # held_cash reads intents first; fail at the later index build.
                original, calls = runtime.store.intents, 0

                def intents(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    return fail() if calls == 2 else original(*args, **kwargs)

                failing.setattr(runtime.store, "intents", intents)
            with pytest.raises(RuntimeError, match="controlled refresh failure"):
                runtime.refresh_eligibility()
        assert runtime._eligibility_group_signature is None
        assert runtime._eligibility_group_events is None
        # Also exercise rollback to the formerly cached inputs after failure.
        runtime.tokens["1"] = replace(runtime.tokens["1"], event_id="event-0")
        runtime.refresh_eligibility()
        assert runtime._eligibility_group_signature == old_signature
        assert "new-event" not in runtime.events
        assert "1" in runtime.events["event-0"]
    finally:
        runtime.close()
