"""Prepared TEST quote delivery preserves original ordering, gates and ownership."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest
from nautilus_trader.common.component import TestClock

import pm_nautilus.market as market_module
import pm_nautilus.test_quotes as quote_module
from pm_nautilus.config import micros
from pm_nautilus.market import PublicMarketHub
from pm_nautilus.strategy import Runtime
from pm_nautilus.test_market import TestMarketHub, TestMarketService, create_market_service
from pm_nautilus.test_runtime import TestRuntime

from test_lifecycle import controlled_factory
from test_test_optimization_equivalence import DAY, book_message, semantic_state, token_set


async def idle_socket(_group):
    await asyncio.Future()


@asynccontextmanager
async def worlds(path, *, count=3, live=False):
    result = []
    tokens = token_set(6, fees=True)
    try:
        for label, runtime_type, hub_type in (
            ("original", Runtime, PublicMarketHub),
            ("prepared", TestRuntime, TestMarketHub),
        ):
            hub = hub_type()
            hub.socket = idle_socket
            hub.runtime.add_tokens(tokens)
            runtimes, clocks, services = [], [], []
            for number in range(count):
                clock = TestClock()
                clock.set_time(DAY)
                runtime = runtime_type(path / label / str(number) / "state.sqlite", clock=clock)
                runtime.update_preferences({}, capital="3")
                runtime.add_tokens(tokens)
                runtimes.append(runtime)
                clocks.append(clock)
                services.append(create_market_service(runtime, hub=hub))
            live_runtime = None
            if live:
                clock = TestClock()
                clock.set_time(DAY)
                live_runtime = Runtime(
                    path / label / "LIVE" / "state.sqlite",
                    mode="LIVE",
                    clock=clock,
                    live_factory=controlled_factory,
                )
                live_runtime.add_tokens(tokens)
                services.append(create_market_service(live_runtime, hub=hub))
            result.append(
                SimpleNamespace(
                    hub=hub,
                    runtimes=runtimes,
                    clocks=clocks,
                    services=services,
                    live=live_runtime,
                )
            )
        yield result, tokens
    finally:
        for world in result:
            for service in world.services:
                await service.close()
            await world.hub.close()
            for runtime in world.runtimes:
                runtime.close()
            if world.live is not None:
                await world.live.client.shutdown()
                world.live.store.close()


def deliver(pair, msg, allowed, now=DAY):
    errors = []
    for world in pair:
        for clock in world.clocks:
            clock.set_time(now)
        try:
            world.hub.message(deepcopy(msg), allowed)
            errors.append(None)
        except Exception as exc:
            errors.append((type(exc).__name__, str(exc)))
    assert errors[0] == errors[1]
    for old, new in zip(pair[0].runtimes, pair[1].runtimes):
        assert semantic_state(old) == semantic_state(new)
    assert {tid: asdict(book) for tid, book in pair[0].hub.runtime.books.items()} == {
        tid: asdict(book) for tid, book in pair[1].hub.runtime.books.items()
    }
    return errors[0]


def test_batch_order_repeated_levels_shadow_and_paused_owned_exit_match_original(tmp_path):
    async def run():
        async with worlds(tmp_path) as (pair, tokens):
            allowed = {token.token_id for token in tokens}
            for token in tokens:
                deliver(pair, book_message(token, DAY), allowed)
            for world in pair:
                for runtime in world.runtimes:
                    runtime.start()
                    runtime.pause()
            for index, size in enumerate(("100", "100", "10", "15", "100"), 1):
                now = DAY + index * 1_000_000_000
                msg = dict(
                    event_type="price_change",
                    market=tokens[0].condition_id,
                    timestamp=str(now // 1_000_000),
                    price_changes=[
                        dict(asset_id="1", side="SELL", price="0.05", size=size),
                        dict(asset_id="1", side="SELL", price="0.05", size=size),
                        dict(asset_id="2", side="BUY", price="0.09", size="100"),
                    ],
                )
                deliver(pair, msg, allowed, now)
            assert any(len(runtime.store.intents()) > 3 for runtime in pair[1].runtimes)
            for world in pair:
                for runtime in world.runtimes:
                    assert runtime.validate()["ok"]

    asyncio.run(run())


def test_unrelated_batch_still_drains_each_consumer_once(tmp_path, monkeypatch):
    async def run():
        async with worlds(tmp_path) as (pair, tokens):
            drains = []
            for world in pair:
                for runtime in world.runtimes:
                    runtime.monitored.clear()
                    drain = Mock(wraps=runtime.drain)
                    monkeypatch.setattr(runtime, "drain", drain)
                    drains.append(drain)
            msg = dict(
                event_type="price_change",
                market=tokens[0].condition_id,
                timestamp="1",
                price_changes=[dict(asset_id="1", side="BUY", price="0.04", size="10")],
            )
            deliver(pair, msg, {"1"})
            assert all(drain.call_count == 1 for drain in drains)

    asyncio.run(run())


def test_prepared_books_preserve_paused_target_and_timed_stop_maintenance(tmp_path):
    async def run():
        async with worlds(tmp_path) as (pair, tokens):
            allowed = {token.token_id for token in tokens}
            for token in tokens:
                deliver(pair, book_message(token, DAY), allowed)
            for world in pair:
                for runtime in world.runtimes:
                    runtime.start()
                    runtime.pause()
            owned = [
                tokens[int(cycle["token_id"]) - 1]
                for cycle in pair[0].runtimes[0].business["cycles"].values()
            ]
            deliver(
                pair,
                book_message(owned[0], DAY + 1_000_000_000, bid=90_000, ask=100_000),
                allowed,
                DAY + 1_000_000_000,
            )
            deliver(
                pair,
                book_message(owned[1], DAY + 2_000_000_000, bid=15_000),
                allowed,
                DAY + 2_000_000_000,
            )
            for world in pair:
                assert all(
                    runtime.business["cycles"][owned[1].event_id]["stop"]["state"] == "ARMED"
                    for runtime in world.runtimes
                )
            deliver(
                pair,
                book_message(owned[1], DAY + 33_000_000_000, bid=14_000),
                allowed,
                DAY + 33_000_000_000,
            )
            for world in pair:
                assert all(
                    owned[0].event_id not in runtime.business["cycles"]
                    and owned[1].event_id in runtime.business["banned"]
                    and runtime.status == "PAUSED"
                    for runtime in world.runtimes
                )

    asyncio.run(run())


def test_prepared_consumer_failure_isolated_and_fail_closed_like_original(tmp_path, monkeypatch):
    async def run():
        async with worlds(tmp_path) as (pair, tokens):
            allowed = {token.token_id for token in tokens}
            for token in tokens:
                deliver(pair, book_message(token, DAY), allowed)
            for world in pair:
                for runtime in world.runtimes:
                    runtime.start()
                monkeypatch.setattr(
                    world.runtimes[0].native,
                    "feed_delta",
                    Mock(side_effect=RuntimeError("test quote failure")),
                )
            now = DAY + 1_000_000
            deliver(
                pair,
                dict(
                    event_type="price_change",
                    market=tokens[0].condition_id,
                    timestamp=str(now // 1_000_000),
                    price_changes=[dict(asset_id="1", side="BUY", price="0.04", size="90")],
                ),
                allowed,
                now,
            )
            for world in pair:
                assert world.runtimes[0].status == "PAUSED"
                assert world.services[0].scan_status["serviceError"] == "RuntimeError"
                assert all(
                    not runtime.faulted and runtime.status == "RUNNING"
                    for runtime in world.runtimes[1:]
                )

    asyncio.run(run())


@pytest.mark.parametrize("damage", ["stale", "missing", "floor", "malformed", "ignored-bad"])
def test_readiness_time_floors_and_failed_decode_follow_original(tmp_path, damage):
    async def run():
        async with worlds(tmp_path) as (pair, tokens):
            token = tokens[0]
            deliver(pair, book_message(token, DAY), {"1"})
            now = DAY + 1_000_000
            if damage == "missing":
                for world in pair:
                    world.hub.runtime.disconnect(["1"])
                msg = dict(
                    event_type="price_change",
                    market=token.condition_id,
                    timestamp=str(now // 1_000_000),
                    price_changes=[dict(asset_id="1", side="BUY", price="0.04", size="50")],
                )
            elif damage == "malformed":
                msg = dict(
                    event_type="price_change",
                    market=token.condition_id,
                    timestamp=str(now // 1_000_000),
                    price_changes=[
                        dict(asset_id="1", side="BUY", price="0.04", size="50"),
                        dict(asset_id="1", side="SELL", price="bad", size="50"),
                    ],
                )
            elif damage == "ignored-bad":
                msg = dict(
                    event_type="price_change",
                    market=token.condition_id,
                    timestamp=str(now // 1_000_000),
                    price_changes=[
                        dict(asset_id="999", side="SELL", price="bad", size="50"),
                        dict(asset_id="1", side="BUY", price="0.04", size="50"),
                    ],
                )
            else:
                msg = book_message(token, DAY - 1_000_000 if damage == "stale" else now)
                if damage == "floor":
                    for world in pair:
                        service = world.services[0]
                        service._availability_timestamp_floor["1"] = now + 1_000_000
                        service.runtime.disconnect(["1"])
            error = deliver(pair, msg, {"1"}, now)
            assert (error is not None) == (damage == "malformed")

    asyncio.run(run())


def test_test_quotes_parse_once_filter_only_touched_and_keep_live_raw(tmp_path, monkeypatch):
    async def run():
        async with worlds(tmp_path, count=10, live=True) as (pair, tokens):
            world = pair[1]
            tests, live_service = world.services[:-1], world.services[-1]
            assert all(type(service.runtime) is TestRuntime for service in tests)
            assert live_service.message.__func__ is market_module.MarketService.message
            prepared_parse, original_parse = Mock(wraps=micros), Mock(wraps=micros)
            monkeypatch.setattr(quote_module, "micros", prepared_parse)
            monkeypatch.setattr(market_module, "micros", original_parse)
            raw = book_message(tokens[0], DAY)
            world.hub.message(raw, {token.token_id for token in tokens})
            assert prepared_parse.call_count == 4  # One bid/ask price/size decode.
            assert original_parse.call_count == 4  # LIVE keeps its original parser.
            world.runtimes[0].books["1"].ask.consume(50_000, 1_000_000)
            assert all(not runtime.books["1"].ask.consumed for runtime in world.runtimes[1:])
            assert world.live.books["1"].ask.consumed == {}
            world.live.client._submit_order.assert_not_awaited()
            world.live.client._cancel_order.assert_not_awaited()

    asyncio.run(run())


def test_prepared_and_live_consumers_execute_the_same_original_message_method(tmp_path):
    async def run():
        assert TestMarketService.message is market_module.MarketService.message
        async with worlds(tmp_path, count=10, live=True) as (pair, tokens):
            world = pair[1]
            calls = []
            code = market_module.MarketService.message.__code__

            def observe(frame, event, _arg):
                if event == "call" and frame.f_code is code:
                    calls.append((frame.f_locals["self"], frame.f_locals["_prepared"]))

            previous = sys.getprofile()
            try:
                sys.setprofile(observe)
                world.hub.message(book_message(tokens[0], DAY), {"1"})
            finally:
                sys.setprofile(previous)
            assert len(calls) == 11
            assert {service for service, _prepared in calls} == set(world.services)
            assert all(
                (prepared is not None) == (service.runtime.mode == "TEST")
                for service, prepared in calls
            )

    asyncio.run(run())


def test_pure_live_source_and_consumer_never_use_prepared_values(tmp_path, monkeypatch):
    async def run():
        async with worlds(tmp_path, count=0, live=True) as (pair, tokens):
            world = pair[1]
            prepared_parse = Mock(side_effect=AssertionError("LIVE used TEST decoding"))
            monkeypatch.setattr(quote_module, "micros", prepared_parse)
            raw = book_message(tokens[0], DAY)
            world.hub.message(raw, {"1"})
            poison = SimpleNamespace(
                raw=raw,
                book_levels=Mock(side_effect=AssertionError("LIVE accepted prepared values")),
            )
            world.services[0].message(raw, {"1"}, _prepared=poison)
            prepared_parse.assert_not_called()
            poison.book_levels.assert_not_called()
            assert world.live.books["1"].ready
            assert not world.live.store.intents() and not world.live.store.events()
            world.live.client._submit_order.assert_not_awaited()
            world.live.client._cancel_order.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("override", ["message", "mutating-message", "subscriptions", "legacy"])
def test_unknown_or_overridden_consumers_keep_complete_original_dispatch(
    tmp_path, monkeypatch, override
):
    async def run():
        async with worlds(tmp_path) as (pair, tokens):
            source_sets = [world.hub.services for world in pair]
            calls = []
            try:
                for world in pair:
                    world.hub.services = list(world.services)
                    service = world.services[0]
                    if override in ("message", "mutating-message"):
                        original = service.message

                        def replacement(msg, allowed, original=original):
                            calls.append(msg)
                            original(msg, allowed)
                            if override == "mutating-message":
                                msg["asks"][0]["size"] = "7"

                        monkeypatch.setattr(service, "message", replacement)
                    elif override == "subscriptions":
                        monkeypatch.setattr(service, "desired_subscription_ids", lambda: set())
                    else:
                        # Original MarketService around a TEST runtime remains a
                        # supported legacy seam, with the original raw path.
                        service.__class__ = market_module.MarketService
                prepared_parse = Mock(wraps=micros)
                monkeypatch.setattr(quote_module, "micros", prepared_parse)
                raw = book_message(tokens[0], DAY)
                deliver(pair, raw, {token.token_id for token in tokens})
                prepared_parse.assert_not_called()
                if override in ("message", "mutating-message"):
                    assert len(calls) == 2
                if override == "mutating-message":
                    assert pair[1].runtimes[0].books["1"].ask.external == {50_000: 100_000_000}
                    assert pair[1].runtimes[1].books["1"].ask.external == {50_000: 7_000_000}
            finally:
                for world, source_set in zip(pair, source_sets):
                    world.hub.services = source_set

    asyncio.run(run())
