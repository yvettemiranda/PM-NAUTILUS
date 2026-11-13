"""Full-field immutable metadata reuse preserves scan publication and ownership."""

import asyncio
from copy import deepcopy
from dataclasses import asdict, fields, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
import weakref

import httpx
import pytest
from nautilus_trader.common.component import TestClock

from pm_nautilus.market import MarketService, PublicMarketHub, _PublicRuntime, normalize
from pm_nautilus.rules import Fees
from pm_nautilus.strategy import Runtime
from pm_nautilus.test_runtime import TestRuntime

from market_token_reuse_reference import reference_public_add_tokens, reference_scan
from test_live_recovery import factory, flush
from test_market import category_response, event, market_option


def public_owner(token):
    hub = SimpleNamespace(_scan_tick_versions=None, _tick_versions={}, services=())
    owner = _PublicRuntime(hub)
    owner.tokens[token.token_id] = token
    owner.books[token.token_id].snapshot([(10_000, 20_000_000)], [(20_000, 30_000_000)], 100)
    calls = []

    def disconnect(ids):
        calls.append(tuple(sorted(ids)))
        _PublicRuntime.disconnect(owner, ids)

    owner.disconnect = disconnect
    return owner, calls


def test_public_publication_compares_every_token_field_against_frozen_reference():
    original = normalize(event())[0]
    for field in fields(original):
        value = getattr(original, field.name)
        if isinstance(value, Fees):
            value = Fees(True, 20_000, 2)
        elif isinstance(value, bool):
            value = not value
        elif isinstance(value, int):
            value += 1
        elif isinstance(value, tuple):
            value += ("new-category",)
        elif value is None:
            value = 123
        else:
            value += "-changed"
        changed = replace(original, **{field.name: value})
        results = []
        for publish in (reference_public_add_tokens, _PublicRuntime.add_tokens):
            owner, calls = public_owner(original)
            publish(owner, [deepcopy(original), changed])
            results.append(
                (list(owner.tokens.items()), asdict(owner.books[original.token_id]), calls)
            )
            assert owner.tokens[changed.token_id] == changed
        assert results[0] == results[1], field.name


@pytest.mark.parametrize("tick_race", [False, True])
def test_equal_publication_reuses_object_after_ws_tick_race_resolution(tick_race):
    scanned = normalize(event())[0]
    current = replace(scanned, tick=10_000) if tick_race else scanned
    source, calls = public_owner(current)
    if tick_race:
        source.hub._scan_tick_versions = {current.token_id: 0}
        source.hub._tick_versions = {current.token_id: 1}
    candidate = deepcopy(scanned)
    assert candidate is not current
    source.add_tokens([candidate])
    assert source.tokens[current.token_id] is current
    assert source.books[current.token_id].ready
    assert not calls


def test_multiple_tick_updates_in_one_publication_keep_original_disconnect():
    token = normalize(event())[0]
    results = []
    for publish in (reference_public_add_tokens, _PublicRuntime.add_tokens):
        owner, calls = public_owner(token)
        publish(owner, [replace(token, tick=10_000), deepcopy(token)])
        results.append((list(owner.tokens.items()), asdict(owner.books[token.token_id]), calls))
        assert calls == [(token.token_id,)]
    assert results[0] == results[1]


def scan_events():
    first = event(11)
    first["markets"] = [market_option(11)]
    independent = event(12)
    independent["markets"] = [market_option(12), market_option(13)]
    augmented = event(14)
    augmented.update(negRisk=True, negRiskAugmented=True)
    augmented["markets"] = [
        market_option(14, groupItemTitle="Named outcome"),
        market_option(15, groupItemTitle="Other", negRiskOther=True),
    ]
    return [first, independent, augmented]


@pytest.mark.parametrize("scope", [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize("finish", ["success", "failed", "cancelled", "tick_race"])
def test_scan_reuse_matches_atomic_frozen_scan_and_all_market_scopes(
    tmp_path, monkeypatch, scope, finish
):
    async def run(reference):
        raw = scan_events()
        stage = "first"
        next_page, release = asyncio.Event(), asyncio.Event()
        staged = []

        def observe_normalize(*args, **kwargs):
            result = normalize(*args, **kwargs)
            if stage == "repeat":
                staged.extend(weakref.ref(token) for token in result)
            return result

        async def handler(request):
            category = category_response(request)
            if category is not None:
                return category
            if request.url.params.get("after_cursor") == "tail":
                if stage == "repeat":
                    next_page.set()
                    await release.wait()
                    if finish == "failed":
                        return httpx.Response(400)
                return httpx.Response(200, json={"events": []})
            return httpx.Response(200, json={"events": raw, "next_cursor": "tail"})

        clock = TestClock()
        clock.set_time(1)
        runtime = TestRuntime(tmp_path / str(reference) / "state.sqlite", clock=clock)
        runtime.preferences.includeIndependentMultiMarkets = scope[0]
        runtime.preferences.includeAugmentedNegRisk = scope[1]
        hub = PublicMarketHub(httpx.MockTransport(handler))
        service = MarketService(runtime, hub=hub)
        hub.sync_subscriptions = AsyncMock()
        task = None
        try:
            with monkeypatch.context() as variant:
                if reference:
                    variant.setattr(MarketService, "scan", reference_scan)
                    variant.setattr(_PublicRuntime, "add_tokens", reference_public_add_tokens)
                    variant.setattr("market_token_reuse_reference.normalize", observe_normalize)
                else:
                    variant.setattr("pm_nautilus.market.normalize", observe_normalize)
                await service.scan()
                original = dict(hub.runtime.tokens)
                stored = runtime.store.tokens()
                financial = deepcopy((runtime.business, runtime.store.intents(), runtime.cash()))
                stage = "repeat"
                hub.discovered_at = None
                task = asyncio.create_task(service.scan())
                await asyncio.wait_for(next_page.wait(), 2)
                # Even while a complete page is staged, neither publication nor
                # any financial metadata may change before the final page.
                assert hub.runtime.tokens == original
                assert runtime.store.tokens() == stored
                assert all(hub.runtime.tokens[tid] is token for tid, token in original.items())
                assert staged
                if reference:
                    assert all(token() is not None for token in staged)
                else:
                    # The loop's final local `t` may retain one candidate. All
                    # other equal Tokens are released while later pages await.
                    assert sum(token() is not None for token in staged) <= 1
                if finish == "cancelled":
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    if finish == "tick_race":
                        tid = "22"
                        current = replace(hub.runtime.tokens[tid], tick=10_000)
                        hub.runtime.tokens[tid] = current
                        hub._tick_versions[tid] = 1
                    release.set()
                    await task
                assert (runtime.business, runtime.store.intents(), runtime.cash()) == financial
                if finish in {"failed", "cancelled"}:
                    assert hub.runtime.tokens == original and runtime.store.tokens() == stored
                    assert hub.discovery_version == 1
                else:
                    assert hub.discovery_version == 2
                    if not reference:
                        for tid, token in original.items():
                            if finish != "tick_race" or tid != "22":
                                assert hub.runtime.tokens[tid] is token
                                assert runtime.tokens[tid] is token
                return (
                    list(hub.runtime.tokens.items()),
                    list(runtime.tokens.items()),
                    runtime.store.tokens(),
                    list(runtime.events.items()),
                    runtime.monitored,
                    hub.discovery_version,
                    hub._published_market_scope,
                    hub.discovery_status["checkedMarketCount"],
                    hub.discovery_status["error"],
                    hub.discovery_status["consecutiveFailures"],
                )
        finally:
            release.set()
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await service.close()
            await hub.close()
            runtime.close()

    async def compare():
        assert await run(True) == await run(False)

    asyncio.run(compare())


def test_scan_scope_and_metadata_changes_match_frozen_publication(tmp_path, monkeypatch):
    async def trace(reference):
        raw = scan_events()

        def handler(request):
            category = category_response(request)
            return category if category is not None else httpx.Response(200, json={"events": raw})

        runtime = TestRuntime(tmp_path / str(reference) / "state.sqlite", clock=TestClock())
        hub = PublicMarketHub(httpx.MockTransport(handler))
        service = MarketService(runtime, hub=hub)
        hub.sync_subscriptions = AsyncMock()
        financial = deepcopy((runtime.business, runtime.store.intents(), runtime.cash()))
        result = []
        try:
            with monkeypatch.context() as variant:
                if reference:
                    variant.setattr(MarketService, "scan", reference_scan)
                    variant.setattr(_PublicRuntime, "add_tokens", reference_public_add_tokens)
                for index, scope in enumerate(
                    [(False, False), (True, False), (True, True), (False, True), (False, False)]
                ):
                    runtime.preferences.includeIndependentMultiMarkets = scope[0]
                    runtime.preferences.includeAugmentedNegRisk = scope[1]
                    if index == 2:
                        raw[0]["markets"][0]["question"] = "Updated official metadata"
                    if index == 4:
                        raw[0]["markets"][0].pop("question")
                    hub.discovered_at = None
                    await service.scan()
                    assert (runtime.business, runtime.store.intents(), runtime.cash()) == financial
                    result.append(
                        (
                            list(hub.runtime.tokens.items()),
                            list(runtime.tokens.items()),
                            runtime.store.tokens(),
                            list(runtime.events.items()),
                            runtime.monitored,
                            hub.discovery_version,
                            hub._published_market_scope,
                        )
                    )
            return result
        finally:
            await service.close()
            await hub.close()
            runtime.close()

    async def compare():
        assert await trace(True) == await trace(False)

    asyncio.run(compare())


@pytest.mark.parametrize("mode", ["TEST", "LIVE"])
def test_repeated_publication_keeps_consumer_metadata_shared_without_financial_change(
    tmp_path, mode
):
    async def run():
        clock = TestClock()
        runtime = Runtime(tmp_path / "state.sqlite", mode=mode, clock=clock, live_factory=factory)
        hub = PublicMarketHub(httpx.MockTransport(lambda request: httpx.Response(500)))
        service = MarketService(runtime, hub=hub)
        hub.sync_subscriptions = AsyncMock()
        tokens = normalize(event())
        try:
            hub.runtime.add_tokens(tokens)
            hub.discovery_version = 1
            hub.apply_discovery(service)
            financial = deepcopy((runtime.business, runtime.store.intents(), runtime.cash()))
            hub.runtime.add_tokens(deepcopy(tokens))
            hub.discovery_version = 2
            hub.apply_discovery(service)
            assert (runtime.business, runtime.store.intents(), runtime.cash()) == financial
            assert all(
                hub.runtime.tokens[t.token_id] is runtime.tokens[t.token_id] is t for t in tokens
            )
            if mode == "LIVE":
                runtime.client._submit_order.assert_not_called()
                runtime.client._cancel_order.assert_not_called()
        finally:
            await service.close()
            await hub.close()
            runtime.close()
            if mode == "LIVE":
                await flush()

    asyncio.run(run())
