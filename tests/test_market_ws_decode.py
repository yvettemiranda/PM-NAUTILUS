"""Fast public-frame decoding preserves stdlib values and whole-frame delivery."""

import asyncio
import json
import math
import random
import struct
from copy import deepcopy
from dataclasses import replace
from types import MethodType

import pytest

import pm_nautilus.market as market
from market_ws_decode_reference import reference_receive
from test_runtime import setup


def assert_same_value(left, right):
    assert type(left) is type(right)
    if isinstance(left, float):
        assert (math.isnan(left) and math.isnan(right)) or struct.pack("!d", left) == struct.pack(
            "!d", right
        )
    elif isinstance(left, dict):
        assert list(left) == list(right)
        for key in left:
            assert_same_value(left[key], right[key])
    elif isinstance(left, list):
        assert len(left) == len(right)
        for a, b in zip(left, right, strict=True):
            assert_same_value(a, b)
    else:
        assert left == right


def book(token_id="1", timestamp="1"):
    return {
        "event_type": "book",
        "asset_id": token_id,
        "market": "condition",
        "timestamp": timestamp,
        "bids": [{"price": "0.015", "size": "100"}],
        "asks": [{"price": "0.02", "size": "50"}],
    }


def change(token_id="1", timestamp="2"):
    return {
        "event_type": "price_change",
        "market": "condition",
        "timestamp": timestamp,
        "price_changes": [{"asset_id": token_id, "side": "BUY", "price": "0.015", "size": "75"}],
    }


@pytest.mark.parametrize("encoding", [None, "utf-8", "utf-8-sig", "utf-16", "utf-32"])
@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        -0,
        2**128,
        "中文\U0001f41f\x00",
        {"z": 1, "a": [False, None, "0.01", -7]},
        book(),
        change(),
        [book(), change(), book("2")],
        [[[[["extra nesting"]]]]],
    ],
)
def test_frame_values_and_binary_encodings_match_stdlib(encoding, value):
    raw = json.dumps(value, ensure_ascii=False)
    if encoding:
        raw = raw.encode(encoding)
    assert_same_value(market._decode_market_frame(raw), json.loads(raw))


@pytest.mark.parametrize(
    "raw",
    [
        "-0.0",
        "1e999",
        "-1e999",
        "NaN",
        "Infinity",
        "-Infinity",
        '{"x":1,"y":2,"x":3}',
        '{"x":false,"x":0,"a":true,"n":null}',
        '"\\ud800"',
        '"\\udfff"',
        '"\\ud83d\\ude00"',
        '"\ud800"',
        b'"\xed\xa0\x80"',
        bytearray(b'{"v": [1, true, null]}'),
        "1" * 4300,
    ],
)
def test_extended_values_duplicate_keys_and_integer_limits_match_stdlib(raw):
    assert_same_value(market._decode_market_frame(raw), json.loads(raw))


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "[1,]",
        '{"x":1,}',
        '[{"ok":1}, {"bad":]',
        "01",
        "1.",
        "true false",
        '"unescaped\nnewline"',
        '\ufeff{"x":1}',
        b'"\xff"',
        b"\xff\xfe\x00",
        "1" * 4301,
        memoryview(b"{}"),
        17,
        None,
    ],
)
def test_decode_failures_keep_stdlib_exception_type_message_and_location(raw):
    with pytest.raises(Exception) as expected:
        json.loads(raw)
    with pytest.raises(type(expected.value)) as actual:
        market._decode_market_frame(raw)
    assert str(actual.value) == str(expected.value)
    if isinstance(expected.value, json.JSONDecodeError):
        assert (actual.value.pos, actual.value.lineno, actual.value.colno) == (
            expected.value.pos,
            expected.value.lineno,
            expected.value.colno,
        )


def test_fast_shape_is_strict_and_float_rounding_uses_original_decoder(monkeypatch):
    original = json.loads
    fallback = []

    def record(raw):
        fallback.append(raw)
        return original(raw)

    monkeypatch.setattr(market.json, "loads", record)
    ordinary = json.dumps([book(), change()])
    assert_same_value(market._decode_market_frame(ordinary), original(ordinary))
    assert not fallback
    # The typed fast decoder must reject JSON floats, rather than coerce them
    # to integers or bypass the stdlib's correctly rounded conversion.
    rng = random.Random(20261113)
    decimals = ["1.0", "-0.0", "5e-324", "1e309", "2.2250738585072012e-308"]
    decimals += [f"{rng.randrange(10**80)}e{rng.randrange(-400, 400)}" for _ in range(500)]
    for value in decimals:
        raw = '{"value":' + value + "}"
        assert_same_value(market._decode_market_frame(raw), original(raw))
        assert fallback[-1] == raw
    assert len(fallback) == len(decimals)


def test_full_200_market_snapshot_and_update_frames_use_fast_path(monkeypatch):
    original = json.loads
    snapshots = [book(str(n)) for n in range(200)]
    updates = [change(str(n)) for n in range(200)]

    def unexpected_fallback(raw):
        pytest.fail("Ordinary public snapshots and price changes must use the bounded fast path")

    monkeypatch.setattr(market.json, "loads", unexpected_fallback)
    for messages in (snapshots, updates):
        raw = json.dumps(messages, separators=(",", ":"))
        for frame in (raw, raw.encode(), bytearray(raw.encode())):
            assert_same_value(market._decode_market_frame(frame), original(frame))


@pytest.mark.parametrize("depth", [5, 50, 9998, 10010])
def test_deep_shapes_always_fall_back_to_original_recursion_behavior(monkeypatch, depth):
    original = json.loads
    calls = []

    def record(raw):
        calls.append(True)
        return original(raw)

    monkeypatch.setattr(market.json, "loads", record)
    raw = "[" * depth + "0" + "]" * depth
    try:
        expected = original(raw)
    except Exception as expected:
        with pytest.raises(type(expected)) as actual:
            market._decode_market_frame(raw)
        assert str(actual.value) == str(expected)
    else:
        actual = market._decode_market_frame(raw)
        # Avoid the test's own recursive equality at the interpreter limit.
        for _ in range(depth):
            assert type(actual) is type(expected) is list
            assert len(actual) == len(expected) == 1
            actual, expected = actual[0], expected[0]
        assert actual == expected == 0
    assert calls == [True]


def test_receive_keeps_frame_and_message_order_subscription_yields_and_heartbeat(
    tmp_path, monkeypatch
):
    async def replay(root, receive):
        runtime, _, token = setup(root)
        runtime.add_tokens([replace(token, token_id="2")])
        service = market.MarketService(runtime)
        group = ("1", "2")
        service.active_groups[group] = set(group)
        service.stream_state(group, "AWAITING_BOOKS")
        service.receive = MethodType(receive, service)
        trace = []
        original_message = service.message
        real_sleep = asyncio.sleep
        calls = 0

        def message(msg, allowed):
            nonlocal calls
            calls += 1
            trace.append(("message", deepcopy(msg), sorted(allowed)))
            original_message(msg, allowed)
            if calls == 1:
                asyncio.get_running_loop().call_soon(
                    lambda: service.active_groups[group].discard("2")
                )

        async def yielding(delay):
            if delay == 0:
                trace.append(("yield", calls))
            await real_sleep(delay)

        service.message = message
        frames = ["PING", "PONG", json.dumps([book(), book("2")]), json.dumps(change()).encode()]
        waiting = asyncio.Event()

        class Socket:
            async def send(self, raw):
                trace.append(("send", raw))

            async def recv(self):
                if frames:
                    return frames.pop(0)
                waiting.set()
                await asyncio.Event().wait()

        monkeypatch.setattr(market.asyncio, "sleep", yielding)
        task = asyncio.create_task(service.receive(Socket(), group))
        try:
            await asyncio.wait_for(waiting.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert service.streams[group]["lastPongAt"]
            result = {
                "trace": trace,
                "books": {
                    tid: (b.ready, b.timestamp, b.bid.available(), b.ask.available())
                    for tid, b in runtime.books.items()
                },
                "business": deepcopy(runtime.business),
                "cash": runtime.cash(),
                "state": service.streams[group]["state"],
            }
            assert trace[:2] == [("send", "PING"), ("send", "PONG")]
            assert [entry[-1] for entry in trace if entry[0] == "message"] == [
                ["1", "2"],
                ["1"],
                ["1"],
            ]
            return result
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            monkeypatch.setattr(market.asyncio, "sleep", real_sleep)
            await service.close()
            runtime.close()

    async def run():
        baseline = await replay(tmp_path / "old", reference_receive)
        candidate = await replay(tmp_path / "new", market.MarketService.receive)
        assert candidate == baseline

    asyncio.run(run())


@pytest.mark.parametrize("receive", [reference_receive, market.MarketService.receive])
def test_malformed_late_frame_value_never_applies_earlier_messages(tmp_path, receive):
    async def run():
        runtime, _, _ = setup(tmp_path)
        service = market.MarketService(runtime)
        group = ("1",)
        service.stream_state(group, "AWAITING_BOOKS")
        called = []
        service.message = lambda *args: called.append(args)

        class Socket:
            async def send(self, raw):
                pass

            async def recv(self):
                return "[" + json.dumps(book()) + ',{"broken":]'

        try:
            before = (deepcopy(runtime.business), runtime.cash(), runtime.store.db.total_changes)
            with pytest.raises(json.JSONDecodeError):
                await receive(service, Socket(), group)
            assert called == []
            assert (runtime.business, runtime.cash(), runtime.store.db.total_changes) == before
        finally:
            await service.close()
            runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("receive", [reference_receive, market.MarketService.receive])
def test_cancellation_between_messages_does_not_apply_rest_of_frame(tmp_path, receive):
    async def run():
        runtime, _, _ = setup(tmp_path)
        service = market.MarketService(runtime)
        group = ("1",)
        service.stream_state(group, "AWAITING_BOOKS")
        called = []

        def message(msg, allowed):
            called.append(msg)
            asyncio.current_task().cancel()

        service.message = message

        class Socket:
            async def send(self, raw):
                pass

            async def recv(self):
                return json.dumps([book(), change()])

        try:
            task = asyncio.create_task(receive(service, Socket(), group))
            with pytest.raises(asyncio.CancelledError):
                await task
            assert called == [book()]
        finally:
            await service.close()
            runtime.close()

    asyncio.run(run())
