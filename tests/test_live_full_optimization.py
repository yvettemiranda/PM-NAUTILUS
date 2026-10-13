"""Offline native/SDK regressions: precise terms and recoverable delivery."""

import asyncio
from dataclasses import replace
from decimal import Decimal, ROUND_DOWN
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import msgspec
import pytest
from nautilus_trader.adapters.polymarket.execution import PolymarketExecutionClient
from nautilus_trader.model.currencies import pUSD
from nautilus_trader.model.events import OrderFilled, OrderUpdated
from nautilus_trader.model.identifiers import ClientOrderId, VenueOrderId
from nautilus_trader.model.objects import AccountBalance, Money
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import MarketOrderArgsV2, PartialCreateOrderOptions
from py_clob_client_v2.order_builder.builder import OrderBuilder, ROUNDING_CONFIG
from py_clob_client_v2.order_utils import Side

from pm_nautilus.config import SCALE
from pm_nautilus.live import _ExactBuyOrderBuilder
from pm_nautilus.native import quantity
from pm_nautilus.order_terms import OrderTermsError, canonical_buy_taker, check_signed_buy
from pm_nautilus.strategy import Runtime

from test_live_recovery import (
    accept,
    assert_owned_fill,
    controlled_runtime,
    flush,
    fund,
    token,
    venue_trade,
)


def unsigned_sdk(*, tick="0.01", fee_rate=0, fee_exponent=0):
    """Run the real client's metadata/fee/balance path; stop before signing."""
    http = ClobClient("https://offline.invalid", chain_id=137)
    http.assert_level_1_auth = lambda: None
    http._ClobClient__cached_version = 2
    http._get = Mock(
        return_value={
            "mts": tick,
            "nr": False,
            "t": [{"t": "1"}],
            "fd": {"r": fee_rate, "e": fee_exponent},
        }
    )
    http._post = http._delete = Mock(side_effect=AssertionError("Network forbidden"))
    http.get_clob_market_info("0x" + "a" * 64)
    builder = _ExactBuyOrderBuilder(None)

    def build(args, options, **kwargs):
        side, maker, taker = builder.get_market_order_amounts(
            args.side, args.amount, args.price, ROUNDING_CONFIG[options.tick_size]
        )
        assert side == Side.BUY
        return SimpleNamespace(makerAmount=maker, takerAmount=taker)

    http.builder = SimpleNamespace(build_market_order=Mock(side_effect=build))
    return http


@pytest.mark.parametrize("tick", [100_000, 10_000, 1_000, 100])
def test_buy_precision_all_supported_price_ticks_and_original_limit(tick):
    builder = _ExactBuyOrderBuilder(None)
    config = ROUNDING_CONFIG[str(Decimal(tick) / SCALE)]
    maker = 5_020_000
    for limit in range(tick, SCALE, tick):
        _, actual_maker, actual_taker = builder.get_market_order_amounts(
            "BUY", 5.02, float(Decimal(limit) / SCALE), config
        )
        expected = Decimal("5.02") / (Decimal(limit) / SCALE)
        expected = int(expected.quantize(Decimal(1).scaleb(-config.amount), ROUND_DOWN) * SCALE)
        assert actual_maker == maker
        assert actual_taker == expected == canonical_buy_taker(maker, limit, tick)
        signed = SimpleNamespace(makerAmount=actual_maker, takerAmount=actual_taker)
        assert check_signed_buy(signed, maker, limit, 1, tick) == (maker, expected)
        # The canonical floor must never buy the next executable price tick.
        assert maker * SCALE < expected * (limit + tick)
        with pytest.raises(OrderTermsError):
            check_signed_buy(
                SimpleNamespace(makerAmount=maker, takerAmount=expected - tick // 100),
                maker,
                limit,
                1,
                tick,
            )


def test_buy_502_and_029_keep_budget_and_official_balance_fee_adjustment():
    config = ROUNDING_CONFIG["0.01"]
    _, old_maker, _ = OrderBuilder(None).get_market_order_amounts("BUY", 5.02, 0.29, config)
    assert old_maker == 5_010_000  # Reproduce the pinned float underflow.
    http = unsigned_sdk()
    signed = http.create_market_order(
        MarketOrderArgsV2("1", amount=5.02, side="BUY", price=0.29, user_usdc_balance=6),
        PartialCreateOrderOptions(tick_size="0.01", neg_risk=False),
    )
    assert (signed.makerAmount, signed.takerAmount) == (5_020_000, 17_310_300)
    assert check_signed_buy(signed, 5_020_000, 290_000, 5_000_000, 10_000)
    # The official balance/fee adjustment still runs before exact builder floors.
    http = unsigned_sdk(fee_rate=0.04, fee_exponent=1)
    http._adjust_buy_amount_for_balance = Mock(wraps=http._adjust_buy_amount_for_balance)
    signed = http.create_market_order(
        MarketOrderArgsV2("1", amount=5.02, side="BUY", price=0.3, user_usdc_balance=5.02),
        PartialCreateOrderOptions(tick_size="0.01", neg_risk=False),
    )
    http._adjust_buy_amount_for_balance.assert_called_once()
    assert 0 < signed.makerAmount < 5_020_000
    assert signed.makerAmount % 10_000 == 0
    assert check_signed_buy(signed, 5_020_000, 300_000, 5_000_000, 10_000)
    http._post.assert_not_called()


def test_buy_canonical_floor_exception_is_narrow_and_preserves_minimum():
    maker, limit, tick = 5_020_000, 300_000, 1_000
    taker = canonical_buy_taker(maker, limit, tick)
    assert taker == 16_733_330
    signed = SimpleNamespace(makerAmount=maker, takerAmount=taker)
    with pytest.raises(OrderTermsError):
        check_signed_buy(signed, maker, limit, 1)  # Old ratio check rejected this legitimate floor.
    assert check_signed_buy(signed, maker, limit, taker, tick) == (maker, taker)
    with pytest.raises(OrderTermsError):
        check_signed_buy(signed, maker, limit, taker + 1, tick)
    for bad_maker, bad_taker in [(maker + 10_000, taker), (maker + 1, taker), (maker, taker - 1)]:
        with pytest.raises(OrderTermsError):
            check_signed_buy(
                SimpleNamespace(makerAmount=bad_maker, takerAmount=bad_taker),
                maker,
                limit,
                1,
                tick,
            )
    with pytest.raises(OrderTermsError):
        _ExactBuyOrderBuilder(None).get_market_order_amounts(
            "BUY", 5.02, 0.295, ROUNDING_CONFIG["0.01"]
        )


async def owned_order(r, *, converted=True):
    await fund(r)
    t = token(r)
    r.add_tokens([t])
    oid = r.submit(t, "BUY", 10_000_000, 990_000, cash=1_000_000)
    await flush()
    if converted:
        await accept(r, oid)
    else:
        order = r.native.cache.order(ClientOrderId(oid))
        venue = VenueOrderId("venue-1")
        r.native.cache.add_venue_order_id(order.client_order_id, venue)
        intent = r.store.intent(oid)
        intent.update(venue_id=str(venue), base_quantity=10_000_000)
        r.store.save_intent(oid, intent)
        r.client.generate_order_submitted(
            order.strategy_id, order.instrument_id, order.client_order_id, r.now
        )
        r.client.generate_order_accepted(
            order.strategy_id, order.instrument_id, order.client_order_id, venue, r.now
        )
        await flush()
    return t, oid


@pytest.mark.parametrize("conversion", [False, True])
def test_journal_failure_clears_pending_marker_and_same_trade_recovers(
    tmp_path, monkeypatch, conversion
):
    async def run():
        async with controlled_runtime(tmp_path / "write.sqlite") as r:
            t, oid = await owned_order(r, converted=not conversion)
            original = r.store.journal

            def fail(event):
                if isinstance(event, OrderUpdated if conversion else OrderFilled):
                    raise sqlite3.OperationalError("controlled durable write failure")
                return original(event)

            monkeypatch.setattr(r.store, "journal", fail)
            with pytest.raises(sqlite3.OperationalError):
                r.client.ingest_trade(venue_trade(t))
            assert not r.client._pending_native
            assert str(oid) not in r.client._pending_conversions
            assert not r.business["cycles"]
            assert not r.store.order_fills(oid)
            assert r.status == "PAUSED" and r.held_cash() == 1_000_000
            monkeypatch.setattr(r.store, "journal", original)
            assert r.client.ingest_trade(venue_trade(t))
            await flush()
            assert_owned_fill(r, t)
            assert not r.client._undelivered_native

    asyncio.run(run())


@pytest.mark.parametrize("conversion", [False, True])
def test_committed_event_dispatch_failure_recovers_original_fact(tmp_path, monkeypatch, conversion):
    async def run():
        async with controlled_runtime(tmp_path / "dispatch.sqlite") as r:
            t, oid = await owned_order(r, converted=not conversion)
            original = PolymarketExecutionClient._send_order_event
            failed = False

            def fail_once(client, event):
                nonlocal failed
                if not failed and isinstance(event, OrderUpdated if conversion else OrderFilled):
                    failed = True
                    raise RuntimeError("controlled dispatch failure after commit")
                return original(client, event)

            monkeypatch.setattr(PolymarketExecutionClient, "_send_order_event", fail_once)
            with pytest.raises(RuntimeError):
                r.client.ingest_trade(venue_trade(t))
            assert len(r.client._undelivered_native) == 1
            assert not r.client._pending_native
            assert not r.business["cycles"]
            journaled = list(r.store.events())
            committed = list(r.client._undelivered_native.values())[0]
            frozen = committed.info.get("pm_rules") if isinstance(committed, OrderFilled) else None
            r.update_preferences({"targetSellPriceMultiplier": "3"})
            assert r.client.ingest_trade(venue_trade(t))
            await flush()
            assert_owned_fill(r, t)
            assert not r.client._undelivered_native
            if frozen:
                assert r.store.order_fills(oid)[0].info["pm_rules"] == frozen
                assert [
                    (seq, event.id)
                    for seq, event in r.store.events()
                    if isinstance(event, OrderFilled)
                ] == [(seq, event.id) for seq, event in journaled if isinstance(event, OrderFilled)]

    asyncio.run(run())


@pytest.mark.parametrize(
    "change",
    [
        {"market": "0x" + "b" * 64},
        {"asset_id": "2"},
        {"side": "SELL"},
        {"size": "-10"},
        {"size": "0"},
        {"size": "0.0000001"},
        {"size": "10.000001"},
    ],
)
def test_failed_trade_invalid_identity_or_quantity_cannot_release_reservation(tmp_path, change):
    async def run():
        async with controlled_runtime(tmp_path / "failed.sqlite") as r:
            t, oid = await owned_order(r)
            payload = msgspec.to_builtins(venue_trade(t))
            payload.update(status="FAILED", **change)
            trade = msgspec.json.decode(msgspec.json.encode(payload), type=type(venue_trade(t)))
            with pytest.raises(ValueError):
                r.client.ingest_trade(trade)
            assert not r.store.intent(oid).get("failed_trades")
            r.client._http_client.get_trades = Mock(return_value=[])
            r.client._http_client.get_order = Mock(
                return_value={
                    "id": "venue-1",
                    "asset_id": t.token_id,
                    "status": "CANCELED",
                    "size_matched": "10",
                    "associate_trades": [trade.id],
                }
            )
            r.client._update_account_state = AsyncMock()
            await r.client.sync_owned()
            await flush()
            assert r.store.intent(oid)["terminal"] is False
            assert r.held_cash() == 1_000_000
            assert not r.business["cycles"] and not r._test_queue_errors

    asyncio.run(run())


def test_duplicate_confirmations_skip_intent_writes_and_fill_construction(tmp_path, monkeypatch):
    async def run():
        async with controlled_runtime(tmp_path / "duplicate.sqlite") as r:
            t, oid = await owned_order(r)
            trade = venue_trade(t)
            r.client.ingest_trade(trade)
            await flush()
            writes = []
            r.store.db.set_trace_callback(
                lambda sql: writes.append(sql)
                if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
                else None
            )
            monkeypatch.setattr(
                r.client,
                "generate_order_filled",
                Mock(side_effect=AssertionError("duplicate rebuilt")),
            )
            for _ in range(20):
                assert r.client.ingest_trade(trade) is False
            r.store.db.set_trace_callback(None)
            assert writes == []
            assert len(r.store.order_fills(oid)) == 1
            assert_owned_fill(r, t)

    asyncio.run(run())


def test_committed_confirmation_cannot_regress_or_count_twice_as_failed(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "final-status.sqlite") as r:
            t, oid = await owned_order(r)
            trade = venue_trade(t)
            r.client.ingest_trade(trade)
            await flush()
            for status in ("FAILED", "MATCHED", "MINED", "RETRYING"):
                assert r.client.ingest_trade(msgspec.structs.replace(trade, status=status)) is False
            intent = r.store.intent(oid)
            assert not intent.get("pending_trades") and not intent.get("failed_trades")
            # A pre-upgrade mixed record must not double-count this same trade
            # as 10 confirmed + 10 failed against a malformed 20-share terminal.
            intent["failed_trades"] = {trade.id: 10_000_000}
            r.store.save_intent(oid, intent)
            r.client._http_client.get_trades = Mock(return_value=[])
            r.client._http_client.get_order = Mock(
                return_value={
                    "id": "venue-1",
                    "asset_id": t.token_id,
                    "status": "CANCELED",
                    "size_matched": "20",
                    "associate_trades": [trade.id],
                }
            )
            r.client._update_account_state = AsyncMock()
            with pytest.raises(ValueError, match="累计成交或失败份额"):
                await r.client.sync_owned()
            assert r.store.intent(oid)["terminal"] is False
            assert len(r.store.order_fills(oid)) == 1
            assert_owned_fill(r, t)
            r.client._http_client.get_order.return_value["size_matched"] = "10"
            await r.client.sync_owned()
            assert (
                r.store.intent(oid)["terminal"] is True
            )  # Old same-ID FAILED is not extra quantity.
            assert_owned_fill(r, t)

    asyncio.run(run())


def test_valid_failed_trade_completes_only_after_matching_terminal_result(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "valid-failed.sqlite") as r:
            t, oid = await owned_order(r)
            trade = msgspec.structs.replace(venue_trade(t), status="FAILED")
            assert r.client.ingest_trade(trade)
            assert r.store.intent(oid)["terminal"] is False and r.held_cash() == 1_000_000
            r.client._http_client.get_trades = Mock(return_value=[])
            r.client._http_client.get_order = Mock(
                return_value={
                    "id": "venue-1",
                    "asset_id": t.token_id,
                    "status": "CANCELED",
                    "size_matched": "10",
                    "associate_trades": [trade.id],
                }
            )
            r.client._update_account_state = AsyncMock()
            await r.client.sync_owned()
            await flush()
            assert r.store.intent(oid)["terminal"] is True and r.held_cash() == 0
            assert str(oid) not in r.client._pending_conversions
            assert not r.store.order_fills(oid) and not r.business["cycles"]
            assert not r._test_queue_errors

    asyncio.run(run())


@pytest.mark.parametrize(
    "sizes,matched", [(("10", "10"), "20"), (("10", "10"), "10"), (("10",), "20")]
)
def test_cumulative_failed_or_rest_matched_above_order_cap_pauses_and_keeps_lock(
    tmp_path, sizes, matched
):
    async def run():
        async with controlled_runtime(tmp_path / "cumulative-failed.sqlite") as r:
            t, oid = await owned_order(r)
            i = r.store.intent(oid)
            i["base_quantity"] = 10_000_000
            r.store.save_intent(oid, i)
            ids = []
            for n, size in enumerate(sizes):
                trade = msgspec.structs.replace(
                    venue_trade(t), id=f"failed-{n}", status="FAILED", size=size
                )
                assert r.client.ingest_trade(trade)
                ids.append(trade.id)
            r.client.ready = r.client.open_orders_clear = True
            r.client._http_client.get_trades = Mock(return_value=[])
            r.client._http_client.get_order = Mock(
                return_value={
                    "id": "venue-1",
                    "asset_id": t.token_id,
                    "status": "CANCELED",
                    "size_matched": matched,
                    "associate_trades": ids,
                }
            )
            r.client._update_account_state = AsyncMock()
            with pytest.raises(ValueError, match="累计成交或失败份额"):
                await r.client.sync_owned()
            assert r.status == "PAUSED" and not r.client.ready and not r.client.open_orders_clear
            assert r.store.intent(oid)["terminal"] is False and r.held_cash() == 1_000_000
            assert not r.store.intent(oid).get("verified_terminal")
            assert r.active_intents(t.event_id) and not r.store.order_fills(oid)
            r.client._update_account_state.assert_not_awaited()
            assert not r._test_queue_errors

    asyncio.run(run())


@pytest.mark.parametrize(
    "first_status,second_size,valid",
    [("FAILED", "5", True), ("CONFIRMED", "5", True), ("CONFIRMED", "10", False)],
)
def test_split_final_evidence_preserves_confirmed_facts_and_enforces_total_cap(
    tmp_path, first_status, second_size, valid
):
    async def run():
        async with controlled_runtime(tmp_path / "split-final.sqlite") as r:
            t, oid = await owned_order(r)
            first = msgspec.structs.replace(
                venue_trade(t), id="first-5", size="5", status=first_status
            )
            second = msgspec.structs.replace(
                venue_trade(t), id="second-failed", size=second_size, status="FAILED"
            )
            r.client.ingest_trade(first)
            await flush()
            r.client.ingest_trade(second)
            r.client._http_client.get_trades = Mock(return_value=[])
            r.client._http_client.get_order = Mock(
                return_value={
                    "id": "venue-1",
                    "asset_id": t.token_id,
                    "status": "CANCELED",
                    "size_matched": "10",
                    "associate_trades": [first.id, second.id],
                }
            )
            r.client._update_account_state = AsyncMock()
            if valid:
                await r.client.sync_owned()
                await flush()
            else:
                with pytest.raises(ValueError, match="累计成交或失败份额"):
                    await r.client.sync_owned()
                assert r.status == "PAUSED"
            assert r.store.intent(oid)["terminal"] is valid
            if first_status == "CONFIRMED":
                assert len(r.store.order_fills(oid)) == 1
                assert r.business["cycles"][t.event_id]["quantity"] == 5_000_000
            else:
                assert not r.store.order_fills(oid) and not r.business["cycles"]
            assert not r._test_queue_errors

    asyncio.run(run())


def test_better_price_final_quantity_may_exceed_signed_minimum_within_native_base(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "better-final.sqlite") as r:
            await fund(r)
            t = replace(token(r), tick=10_000)
            r.add_tokens([t])
            oid = r.submit(t, "BUY", 32_000_000, 30_000, cash=1_000_000, execution_limit=30_000)
            await flush()
            order = r.native.cache.order(ClientOrderId(oid))
            venue = VenueOrderId("venue-1")
            r.native.cache.add_venue_order_id(order.client_order_id, venue)
            i = r.store.intent(oid)
            i.update(
                venue_id=str(venue),
                base_quantity=96_000_100,
                outbound_terms={
                    "maker": 960_000,
                    "taker": 32_000_000,
                    "tick": 10_000,
                },
            )
            r.store.save_intent(oid, i)
            r.client.generate_order_submitted(
                order.strategy_id, order.instrument_id, order.client_order_id, r.now
            )
            r.client._send_quote_to_base_update(order, venue, quantity(96_000_100))
            r.client.generate_order_accepted(
                order.strategy_id, order.instrument_id, order.client_order_id, venue, r.now
            )
            await flush()
            ids = []
            for n, (size, px) in enumerate((("10", "0.03"), ("30", "0.02"))):
                trade = msgspec.structs.replace(
                    venue_trade(t), id=f"better-{n}", size=size, price=px
                )
                r.client.ingest_trade(trade)
                await flush()
                ids.append(trade.id)
            assert r.business["cycles"][t.event_id]["quantity"] == 40_000_000
            r.client._http_client.get_trades = Mock(return_value=[])
            r.client._http_client.get_order = Mock(
                return_value={
                    "id": "venue-1",
                    "asset_id": t.token_id,
                    "status": "CANCELED",
                    "size_matched": "40",
                    "associate_trades": ids,
                }
            )
            r.client._update_account_state = AsyncMock()
            await r.client.sync_owned()
            await flush()
            assert r.store.intent(oid)["terminal"] is True
            assert len(r.store.order_fills(oid)) == 2
            assert r.business["cycles"][t.event_id]["quantity"] == 40_000_000
            assert r.validate()["ok"] and not r._test_queue_errors

    asyncio.run(run())


def test_real_buy_fill_above_original_execution_limit_is_preserved_and_pauses(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "execution-limit.sqlite") as r:
            r.update_preferences({"orderAmount": "2"})
            r.client.generate_account_state(
                [AccountBalance(Money(10, pUSD), Money(0, pUSD), Money(10, pUSD))], [], True, r.now
            )
            r.store.put("last_cash", 10_000_000)
            await flush()
            t = token(r)
            r.add_tokens([t])
            oid = r.submit(t, "BUY", 10_000_000, 990_000, cash=2_000_000, execution_limit=100_000)
            await flush()
            await accept(r, oid)
            r.client.ingest_trade(msgspec.structs.replace(venue_trade(t), price="0.101"))
            await flush()
            assert r.status == "PAUSED"
            assert "突破订单限价" in r.business["recovery_error"]
            assert r.business["cycles"][t.event_id]["quantity"] == 10_000_000
            assert r.business["cycles"][t.event_id]["spent"] == 1_010_000
            assert len(r.store.order_fills(oid)) == 1 and not r._test_queue_errors

    asyncio.run(run())


def test_indexed_buy_account_check_uses_fresh_inventory_without_token_scan(tmp_path, monkeypatch):
    async def run():
        async with controlled_runtime(tmp_path / "indexed.sqlite") as r:
            t = token(r)
            other = replace(t, token_id="2", direction="NO")
            r.add_tokens([t, other])
            r.tokens.update(
                {
                    f"irrelevant-{n}": replace(
                        t, token_id=f"irrelevant-{n}", condition_id="0x" + "b" * 64
                    )
                    for n in range(1000)
                }
            )
            monkeypatch.setattr(
                type(r.tokens),
                "values",
                lambda self: (_ for _ in ()).throw(AssertionError("metadata scan")),
            )
            r.client._check_open_orders = AsyncMock(return_value=True)
            balances = Mock(side_effect=[{"1": 0, "2": 0}, {"1": 1, "2": 0}])
            r.client.wallet = SimpleNamespace(token_balances=balances)
            assert await r.client._check_buy_account(t)
            assert not await r.client._check_buy_account(t)
            assert balances.call_count == 2
            assert balances.call_args.args[0] == ("1", "2")
            assert r.status == "PAUSED"

    asyncio.run(run())


def test_startup_twelve_owned_positions_one_fresh_wallet_read_per_batch(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "startup.sqlite") as r:
            r.client.generate_account_state(
                [AccountBalance(Money(100, pUSD), Money(0, pUSD), Money(100, pUSD))],
                [],
                True,
                r.now,
            )
            r.store.put("last_cash", 100_000_000)
            await flush()
            rows = []
            for n in range(1, 13):
                t = replace(token(r, n), condition_id="0x" + f"{n:064x}")
                r.add_tokens([t])
                oid = r.submit(t, "BUY", 10_000_000, 990_000, cash=1_000_000)
                await flush()
                order = r.native.cache.order(ClientOrderId(oid))
                venue = VenueOrderId(f"venue-{n}")
                r.native.cache.add_venue_order_id(order.client_order_id, venue)
                i = r.store.intent(oid)
                i["venue_id"] = str(venue)
                r.store.save_intent(oid, i)
                r.client.generate_order_submitted(
                    order.strategy_id, order.instrument_id, order.client_order_id, r.now
                )
                r.client._send_quote_to_base_update(order, venue, quantity(10_000_000))
                r.client.generate_order_accepted(
                    order.strategy_id, order.instrument_id, order.client_order_id, venue, r.now
                )
                await flush()
                payload = msgspec.to_builtins(venue_trade(t))
                payload.update(id=f"trade-{n}", taker_order_id=str(venue))
                r.client.ingest_trade(
                    msgspec.json.decode(msgspec.json.encode(payload), type=type(venue_trade(t)))
                )
                await flush()
                rows.append({"asset": t.token_id, "size": "10"})
            assert len(r.native.cache.positions_open()) == 12
            assert not r._test_queue_errors
            r.client.sync_owned = AsyncMock()
            r.client._fetch_user_positions = AsyncMock(
                return_value=rows + [{"asset": "manual", "size": "999"}]
            )
            assert await r.native.execution.reconcile_execution_state(timeout_secs=5)
            assert r.client._fetch_user_positions.await_count == 1
            assert len(r.native.cache.positions_open()) == 12
            # A later batch must check fresh custody, never reuse the first read.
            r.client._fetch_user_positions.return_value = rows[1:]
            with pytest.raises(ValueError, match="实际份额不足"):
                await r.client.generate_position_status_reports(SimpleNamespace(instrument_id=None))
            assert r.client._fetch_user_positions.await_count == 2
            assert not r._test_queue_errors

    asyncio.run(run())


def test_settled_rights_and_manual_wallet_assets_are_not_position_reports(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "settled-custody.sqlite") as r:
            t, _ = await owned_order(r)
            r.client.ingest_trade(venue_trade(t))
            await flush()
            assert_owned_fill(r, t)
            r.business["settled"].append(t.condition_id)
            r.client._fetch_user_positions = AsyncMock(
                return_value=[{"asset": "manual", "size": "999"}]
            )
            reports = await r.client.generate_position_status_reports(
                SimpleNamespace(instrument_id=None)
            )
            assert reports == []
            assert r.business["cycles"][t.event_id]["quantity"] == 10_000_000
            assert len(r.native.cache.positions_open()) == 1
            assert not r._test_queue_errors
            # The same missing holdings are an error while still tradable.
            r.business["settled"].clear()
            with pytest.raises(ValueError, match="实际份额不足"):
                await r.client.generate_position_status_reports(SimpleNamespace(instrument_id=None))
            assert r.client._fetch_user_positions.await_count == 2

    asyncio.run(run())


@pytest.mark.parametrize(
    "limit,tick",
    [(290_000, 10_000), (300_000, 1_000), (570_000, 10_000), (580_000, 10_000), (333_300, 100)],
)
def test_precise_buy_reaches_offline_post_and_same_test_fill(tmp_path, limit, tick):
    async def run():
        async with controlled_runtime(tmp_path / "live-precise.sqlite") as r:
            r.update_preferences({"orderAmount": "5.02"})
            r.client.generate_account_state(
                [AccountBalance(Money(10, pUSD), Money(0, pUSD), Money(10, pUSD))], [], True, r.now
            )
            r.store.put("last_cash", 10_000_000)
            await flush()
            t = replace(token(r), tick=tick)
            r.add_tokens([t])
            r.book(t.token_id, [(limit - tick, 100_000_000)], [(limit, 100_000_000)])
            oid = r.submit(t, "BUY", 1, 990_000, cash=5_020_000, execution_limit=limit)
            await flush()
            order = r.native.cache.order(ClientOrderId(oid))
            client = r.client
            client.wallet = SimpleNamespace(buy_preflight=Mock())
            client._http_client = unsigned_sdk(tick=str(Decimal(tick) / SCALE))
            client._sdk_terms[t.token_id] = (t.tick, t.neg_risk, t.fees)
            client.buy_valid = Mock(return_value=True)
            client._check_buy_account = AsyncMock(return_value=True)
            client._expected_venue_order_id = Mock(return_value=VenueOrderId("venue-1"))
            client._post_signed_order = AsyncMock()
            await client._submit_market_order(SimpleNamespace(order=order), None)
            client._post_signed_order.assert_awaited_once()
            signed = client._post_signed_order.await_args.args[1]
            assert signed.makerAmount == 5_020_000
            assert signed.takerAmount == canonical_buy_taker(5_020_000, limit, tick)
            assert r.store.intent(oid)["outbound_terms"]["limit"] == limit
            client._http_client._post.assert_not_called()
            venue = VenueOrderId("venue-1")
            r.native.cache.add_venue_order_id(order.client_order_id, venue)
            i = r.store.intent(oid)
            i.update(venue_id=str(venue), base_quantity=100_000_000)
            r.store.save_intent(oid, i)
            client._send_quote_to_base_update(order, venue, quantity(100_000_000))
            client.generate_order_accepted(
                order.strategy_id, order.instrument_id, order.client_order_id, venue, r.now
            )
            await flush()
            payload = msgspec.to_builtins(venue_trade(t))
            payload.update(
                size=str(Decimal(signed.takerAmount) / SCALE), price=str(Decimal(limit) / SCALE)
            )
            client.ingest_trade(
                msgspec.json.decode(msgspec.json.encode(payload), type=type(venue_trade(t)))
            )
            await flush()
            live_cycle = r.business["cycles"][t.event_id]
            assert live_cycle["spent"] <= 5_020_000
            assert r.validate()["ok"] and not r._test_queue_errors
            simulated = Runtime(tmp_path / "test-precise.sqlite")
            try:
                simulated.update_preferences({"orderAmount": "5.02"})
                simulated.add_tokens([t])
                simulated.book(t.token_id, [(limit - tick, 100_000_000)], [(limit, 100_000_000)])
                simulated.submit(t, "BUY", 1, 990_000, cash=5_020_000, execution_limit=limit)
                test_cycle = simulated.business["cycles"][t.event_id]
                assert test_cycle["quantity"] == live_cycle["quantity"] == signed.takerAmount
                assert test_cycle["spent"] == live_cycle["spent"]
                assert simulated.validate()["ok"]
            finally:
                simulated.close()

    asyncio.run(run())
