"""Independent financial equivalence against the unchanged single-account engine."""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from nautilus_trader.common.component import TestClock

from pm_nautilus.market import MarketService, PublicMarketHub
from pm_nautilus.rules import Fees, Token
from pm_nautilus.strategy import Runtime
from pm_nautilus.test_runtime import TestRuntime, create_runtime
from pm_nautilus.test_market import create_market_service
from pm_nautilus.views import dashboard

from test_market_efficiency import IdleSocket


DAY = 86_400_000_000_000


def token_set(count=8, *, siblings=True, fees=False):
    return [
        Token(
            str(index + 1),
            f"event-{index // 2 if siblings else index}",
            f"market-{index // 2 if siblings else index}",
            f"condition-{index // 2 if siblings else index}",
            "YES" if index % 2 == 0 else "NO",
            2,
            False,
            0,
            10 * DAY,
            Fees(fees, 40_000 if fees else 0),
            5_000_000,
            1_000,
        )
        for index in range(count)
    ]


def book_message(token, now, *, bid=40_000, ask=50_000, depth=100_000_000, levels=1):
    def value(micros):
        return f"{micros / 1_000_000:.6f}"

    return {
        "event_type": "book",
        "asset_id": token.token_id,
        "market": token.condition_id,
        "timestamp": str(now // 1_000_000),
        "bids": [
            {"price": value(bid - index * 1_000), "size": value(depth)} for index in range(levels)
        ],
        "asks": [
            {"price": value(ask + index * 1_000), "size": value(depth)} for index in range(levels)
        ],
    }


def semantic_state(runtime):
    """Normalize only generated identities, preserving every financial payload."""
    journal = [
        json.loads(runtime.store.serializer.serialize(event)) for _, event in runtime.store.events()
    ]
    aliases = {runtime.store.generation: "GENERATION"}
    for index, payload in enumerate(journal):
        aliases[payload["event_id"]] = f"EVENT-{index}"
        for field, label in (
            ("client_order_id", "ORDER"),
            ("trade_id", "TRADE"),
            ("position_id", "POSITION"),
        ):
            value = payload.get(field)
            if value is not None and value not in aliases:
                aliases[value] = (
                    f"{label}-{sum(x.startswith(label + '-') for x in aliases.values())}"
                )
    replacements = sorted(aliases.items(), key=lambda item: -len(item[0]))

    def canonical(value):
        if isinstance(value, dict):
            return {canonical(key): canonical(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [canonical(item) for item in value]
        if isinstance(value, str):
            for raw, normalized in replacements:
                value = value.replace(raw, normalized)
            return value
        return value

    # A deferred evaluator may not have allocated a lazy BookCache entry yet.
    # Its durable shadow is still part of the exact logical account state.
    logical_books = runtime.store.books()
    logical_books.update(runtime.books)
    result = {
        "cash": runtime.cash(),
        "status": runtime.status,
        "business": runtime.business,
        "preferences": runtime.store.get("preferences"),
        "initialCapital": runtime.store.get("initial_capital"),
        "intents": runtime.store.intents(),
        "journal": journal,
        # Lazy allocation of an entirely empty, never observed cache entry is
        # scheduling detail. Every observed level and shadow quantity is exact.
        "books": {
            tid: asdict(book)
            for tid, book in logical_books.items()
            if book.ready
            or book.bid.external
            or book.ask.external
            or book.bid.consumed
            or book.ask.consumed
        },
        "positions": sorted(
            (str(position.instrument_id), str(position.quantity.as_decimal()), position.avg_px_open)
            for position in runtime.native.cache.positions_open()
        ),
    }
    # Dict key ordering is unimportant; list ordering/native journal ordering is not.
    return json.loads(json.dumps(canonical(result), sort_keys=True))


@asynccontextmanager
async def paired_runtimes(tmp_path, *, fees=False):
    pair = []
    tokens = token_set(fees=fees)
    try:
        for label, runtime_type in (("original", Runtime), ("optimized", TestRuntime)):
            clock = TestClock()
            clock.set_time(DAY)
            path = tmp_path / label / "state.sqlite"
            runtime = runtime_type(path, clock=clock)
            runtime.update_preferences({}, capital="2")
            runtime.add_tokens(tokens)
            pair.append(
                SimpleNamespace(
                    runtime=runtime,
                    clock=clock,
                    path=path,
                    runtime_type=runtime_type,
                    service=MarketService(runtime),
                )
            )
        yield pair, tokens
    finally:
        for item in pair:
            await item.service.close()
            item.runtime.close()


def apply_pair(pair, token, now, **quote):
    for item in pair:
        item.clock.set_time(now)
        item.service.message(book_message(token, now, **quote), {token.token_id})
    assert semantic_state(pair[0].runtime) == semantic_state(pair[1].runtime)


@pytest.mark.parametrize("fees", [False, True])
def test_paused_target_and_stop_exits_and_restart_match_original_engine(tmp_path, fees):
    async def run():
        async with paired_runtimes(tmp_path, fees=fees) as (pair, tokens):
            for token in tokens:
                apply_pair(pair, token, DAY)
            # The candidate must be based on the newest quote at START, including
            # a sibling that changed after the paused preview had first existed.
            for item in pair:
                dashboard(item.runtime, item.service, limit=20)
            apply_pair(pair, tokens[0], DAY + 1_000_000, bid=40_000, ask=60_000)
            for item in pair:
                item.runtime.start()
                item.runtime.pause()
            assert semantic_state(pair[0].runtime) == semantic_state(pair[1].runtime)
            assert pair[0].runtime.business["cycles"]
            assert pair[0].runtime.business["cycles"]["event-0"]["token_id"] == "2"
            owned = [
                tokens[int(cycle["token_id"]) - 1]
                for cycle in pair[0].runtime.business["cycles"].values()
            ]
            # PAUSED keeps exits and stops operating with the exact old rules.
            apply_pair(pair, owned[0], DAY + 1_000_000_000, bid=90_000, ask=100_000)
            assert owned[0].event_id not in pair[0].runtime.business["cycles"]
            apply_pair(pair, owned[1], DAY + 2_000_000_000, bid=15_000)
            assert pair[0].runtime.business["cycles"][owned[1].event_id]["stop"]["state"] == "ARMED"
            apply_pair(pair, owned[1], DAY + 33_000_000_000, bid=14_000)
            assert owned[1].event_id in pair[0].runtime.business["banned"]
            for item in pair:
                assert item.runtime.validate()["ok"]
                await item.service.close()
                item.runtime.close()
                item.runtime = item.runtime_type(item.path, clock=item.clock)
                item.service = MarketService(item.runtime)
                assert item.runtime.status == "PAUSED"
                assert item.runtime.validate()["ok"]
            assert semantic_state(pair[0].runtime) == semantic_state(pair[1].runtime)
            for token in owned:
                apply_pair(pair, token, DAY + 34_000_000_000, bid=40_000)

    asyncio.run(run())


def test_market_preview_is_current_read_only_and_start_uses_latest_ordering(tmp_path):
    async def run():
        async with paired_runtimes(tmp_path) as (pair, tokens):
            for token in tokens:
                apply_pair(pair, token, DAY)
            original = semantic_state(pair[1].runtime)
            for item in pair:
                data = dashboard(item.runtime, item.service, limit=20)
                assert data["marketScan"]["eventCount"] == 4
            assert semantic_state(pair[1].runtime) == original
            apply_pair(pair, tokens[0], DAY + 1_000_000, bid=30_000, ask=60_000)
            for item in pair:
                before = semantic_state(item.runtime)
                data = dashboard(item.runtime, item.service, limit=20)
                assert semantic_state(item.runtime) == before
                first = next(
                    event
                    for event in data["marketScan"]["events"]
                    if event["eventId"] == tokens[0].event_id
                )
                assert first["winner"]["tokenId"] == "2"
            for item in pair:
                item.runtime.start()
            assert semantic_state(pair[0].runtime) == semantic_state(pair[1].runtime)
            assert pair[1].runtime.business["cycles"][tokens[0].event_id]["token_id"] == "2"

    asyncio.run(run())


async def idle_socket():
    await asyncio.Future()


async def replay_workload(
    runtime_type, hub_type, root, running_accounts, messages=600, *, token_count=642
):
    tokens = token_set(token_count, siblings=False)
    allowed = {token.token_id for token in tokens}
    hub = hub_type()
    hub.runtime.add_tokens(tokens)
    accounts, services, clocks = [], [], []
    try:
        for number in range(10):
            clock = TestClock()
            clock.set_time(DAY)
            runtime = runtime_type(root / str(number + 1) / "state.sqlite", clock=clock)
            runtime.update_preferences({}, capital="3")
            runtime.add_tokens(tokens)
            accounts.append(runtime)
            clocks.append(clock)
            services.append(create_market_service(runtime, hub=hub))
        for token in tokens:
            hub.message(book_message(token, DAY, levels=20), allowed)
        for runtime in accounts[:running_accounts]:
            runtime.start()
        # These stand in only for already-open transport tasks: production hub
        # subscription/control logic runs, with no external connection at all.
        for index in range(0, len(tokens), 200):
            group = (f"fixture-{index // 200}",)
            hub.active_groups[group] = {token.token_id for token in tokens[index : index + 200]}
            hub.tasks[group] = asyncio.create_task(idle_socket())
            hub.streams[group] = {"state": "READY", "error": None}
            hub.sockets[group] = IdleSocket()
        await services[0].sync_subscriptions()
        plans_before = getattr(hub, "plan_runs", 0), getattr(hub, "plan_skips", 0)
        writes_before = sum(runtime.store.db.total_changes for runtime in accounts)
        sync_requests = 0
        initial_orders = sum(len(runtime.store.intents()) for runtime in accounts)
        cpu_started, wall_started = time.process_time(), time.perf_counter()
        for index in range(messages):
            token = tokens[index % len(tokens)]
            now = DAY + (index + 1) * 1_000_000
            for clock in clocks:
                clock.set_time(now)
            message = {
                "event_type": "price_change",
                "market": token.condition_id,
                "timestamp": str(now // 1_000_000),
                "price_changes": [
                    {
                        "asset_id": token.token_id,
                        "side": "SELL",
                        "price": "0.05",
                        "size": str(101 + index % 20),
                    }
                ],
            }
            hub.message(message, allowed)
            if index % 100 == 0:
                for runtime in accounts:
                    runtime.refresh_eligibility()
                    runtime.drain()
                for service in services:
                    await service.sync_subscriptions()
                    sync_requests += 1
            await asyncio.sleep(0)
        cpu_seconds, wall_seconds = (
            time.process_time() - cpu_started,
            time.perf_counter() - wall_started,
        )
        writes = sum(runtime.store.db.total_changes for runtime in accounts) - writes_before
        state = [semantic_state(runtime) for runtime in accounts]
        for runtime in accounts:
            assert runtime.validate()["ok"]
        return state, {
            "cpuSeconds": round(cpu_seconds, 6),
            "wallSeconds": round(wall_seconds, 6),
            "sqliteWrites": writes,
            "messages": messages,
            "consumerDeliveries": messages * 10,
            "subscriptionSyncRequests": sync_requests,
            "subscriptionPlans": (
                hub.plan_runs - plans_before[0] if hasattr(hub, "plan_runs") else sync_requests
            ),
            "subscriptionPlanSkips": getattr(hub, "plan_skips", 0) - plans_before[1],
            "initialOrders": initial_orders,
            "ordersCreatedDuringReplay": sum(len(runtime.store.intents()) for runtime in accounts)
            - initial_orders,
        }
    finally:
        for service in services:
            await service.close()
        await hub.close()
        for runtime in accounts:
            runtime.close()


@pytest.mark.parametrize("running_accounts", [0, 1, 2, 10])
def test_ten_accounts_642_token_replay_preserves_every_financial_result(tmp_path, running_accounts):
    async def run():
        from pm_nautilus.test_market import TestMarketHub

        original, original_metrics = await replay_workload(
            Runtime, PublicMarketHub, tmp_path / "original", running_accounts
        )
        optimized, optimized_metrics = await replay_workload(
            TestRuntime, TestMarketHub, tmp_path / "optimized", running_accounts
        )
        assert optimized == original
        assert optimized_metrics["subscriptionPlanSkips"] > 0
        metrics = {
            "accounts": 10,
            "tokens": 642,
            "levelsPerSide": 20,
            "runningAccounts": running_accounts,
            "original": original_metrics,
            "optimized": optimized_metrics,
            "cpuReductionPercent": round(
                (1 - optimized_metrics["cpuSeconds"] / original_metrics["cpuSeconds"]) * 100, 2
            ),
        }
        print("test_optimization_benchmark=" + json.dumps(metrics, sort_keys=True))

    asyncio.run(run())


def test_factory_keeps_live_on_the_original_runtime(tmp_path):
    from unittest.mock import Mock, patch

    with patch(
        "pm_nautilus.test_runtime.Runtime", new=Mock(return_value="original-live")
    ) as original:
        assert create_runtime(tmp_path / "LIVE.sqlite", mode="LIVE") == "original-live"
        assert original.call_args.args[1] == "LIVE"
    with pytest.raises(ValueError, match="仅适用于TEST"):
        TestRuntime(tmp_path / "rejected.sqlite", mode="LIVE")
