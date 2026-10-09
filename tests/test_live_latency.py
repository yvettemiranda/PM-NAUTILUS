"""Owned SELL wakeups still require confirmed fills and a verified venue terminal.

All venue responses are controlled locally; no signer, network, or live order runs.
"""

import asyncio
from unittest.mock import AsyncMock, Mock

import msgspec

from nautilus_trader.adapters.polymarket.schemas.user import (
    PolymarketUserOrder,
    PolymarketUserTrade,
)
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import ClientOrderId, VenueOrderId

from test_live import ADDRESS
from test_live_recovery import accept, controlled_runtime, flush, fund, token, venue_trade


def _sell_order_message(token):
    payload = {
        "asset_id": token.token_id,
        "associate_trades": ["sell-trade-1"],
        "created_at": "100",
        "expiration": "0",
        "id": "venue-sell-1",
        "maker_address": ADDRESS,
        "market": token.condition_id,
        "order_owner": "controlled-key",
        "order_type": "FAK",
        "original_size": "10",
        "outcome": "YES",
        "owner": "controlled-key",
        "price": "0.15",
        "side": "SELL",
        "size_matched": "4",
        "status": "CANCELED",
        "timestamp": "100",
        "type": "CANCELLATION",
        "event_type": "order",
    }
    return msgspec.json.decode(msgspec.json.encode(payload), type=PolymarketUserOrder)


def _sell_trade_message(token, status):
    payload = {
        "asset_id": token.token_id,
        "bucket_index": 0,
        "fee_rate_bps": "0",
        "id": "sell-trade-1",
        "last_update": "100",
        "maker_address": ADDRESS,
        "maker_orders": [],
        "market": token.condition_id,
        "match_time": "100",
        "outcome": "YES",
        "owner": "controlled-key",
        "price": "0.16",
        "side": "SELL",
        "size": "4",
        "status": status,
        "taker_order_id": "venue-sell-1",
        "timestamp": "100",
        "trade_owner": "controlled-key",
        "trader_side": "TAKER",
        "type": "TRADE",
        "event_type": "trade",
    }
    return msgspec.json.decode(msgspec.json.encode(payload), type=PolymarketUserTrade)


def _sell_intents(runtime):
    return {
        oid: intent for oid, intent in runtime.store.intents().items() if intent["side"] == "SELL"
    }


def test_partial_sell_waits_for_confirmed_trade_before_reselling_remaining(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "live.sqlite") as runtime:
            await fund(runtime)
            t = token(runtime)
            runtime.add_tokens([t])
            # The first bid is below the target; it lets a BUY fill establish a
            # position without dispatching a SELL before the test is ready.
            runtime.book(t.token_id, [(90_000, 100_000_000)], [(100_000, 10_000_000)])
            buy_oid = runtime.submit(t, "BUY", 10_000_000, 990_000, cash=1_000_000)
            await flush()
            await accept(runtime, buy_oid)
            runtime.client.ingest_trade(venue_trade(t, websocket=True))
            await flush()

            def get_order(venue):
                if venue == "venue-1":
                    return {
                        "asset_id": t.token_id,
                        "associate_trades": ["confirmed-trade-1"],
                        "size_matched": "10",
                        "status": "MATCHED",
                    }
                assert venue == "venue-sell-1"
                return {
                    "asset_id": t.token_id,
                    "associate_trades": ["sell-trade-1"],
                    "size_matched": "4",
                    "status": "CANCELED",
                }

            runtime.client._http_client.get_trades = Mock(return_value=[])
            runtime.client._http_client.get_order = Mock(side_effect=get_order)
            runtime.client._update_account_state = AsyncMock()
            await runtime.client.sync_owned()
            await flush()
            assert runtime.store.intent(buy_oid)["terminal"] is True
            assert runtime.business["cycles"][t.event_id]["quantity"] == 10_000_000

            # A public bid above the saved target starts one 10-share FAK SELL.
            runtime.book(t.token_id, [(160_000, 100_000_000)], [(170_000, 10_000_000)])
            await flush()
            sells = _sell_intents(runtime)
            assert len(sells) == 1
            sell_oid, sell_intent = next(iter(sells.items()))
            assert sell_intent["quantity"] == 10_000_000
            order = runtime.native.cache.order(ClientOrderId(sell_oid))
            venue = VenueOrderId("venue-sell-1")
            runtime.native.cache.add_venue_order_id(order.client_order_id, venue)
            sell_intent["venue_id"] = str(venue)
            runtime.store.save_intent(sell_oid, sell_intent)
            runtime.client.generate_order_submitted(
                order.strategy_id, order.instrument_id, order.client_order_id, runtime.now
            )
            runtime.client.generate_order_accepted(
                order.strategy_id, order.instrument_id, order.client_order_id, venue, runtime.now
            )
            await flush()

            runtime.client._reconcile_requested.clear()
            runtime.client._handle_ws_trade_msg(_sell_trade_message(t, "MATCHED"), True)
            runtime.client._handle_ws_order_msg(_sell_order_message(t), True)
            assert runtime.client._reconcile_requested.is_set()
            await flush()
            assert runtime.store.intent(sell_oid)["venue_terminal"] is True
            assert runtime.store.intent(sell_oid)["terminal"] is False
            await runtime.client.sync_owned()
            await flush()
            assert runtime.business["cycles"][t.event_id]["quantity"] == 10_000_000
            assert len(_sell_intents(runtime)) == 1
            assert runtime.store.intent(sell_oid)["terminal"] is False

            # Repeated intermediate trade and cancellation packets add no new
            # fact and must not trigger another account-wide REST round.
            runtime.client._reconcile_requested.clear()
            runtime.client._handle_ws_trade_msg(_sell_trade_message(t, "MATCHED"), True)
            runtime.client._handle_ws_order_msg(_sell_order_message(t), True)
            await flush()
            assert not runtime.client._reconcile_requested.is_set()
            assert runtime.store.intent(sell_oid)["terminal"] is False

            runtime.client._reconcile_requested.clear()
            confirmed = _sell_trade_message(t, "CONFIRMED")
            runtime.client._handle_ws_trade_msg(confirmed, True)
            assert runtime.client._reconcile_requested.is_set()
            await flush()
            assert runtime.business["cycles"][t.event_id]["quantity"] == 6_000_000
            # The venue cancellation alone still does not release the order.
            assert runtime.store.intent(sell_oid)["terminal"] is False
            assert len(_sell_intents(runtime)) == 1

            await runtime.client.sync_owned()
            await flush()
            assert runtime.store.intent(sell_oid)["terminal"] is True
            runtime.drain()
            await flush()
            sells = _sell_intents(runtime)
            assert len(sells) == 2
            remaining = [intent for oid, intent in sells.items() if oid != sell_oid]
            assert len(remaining) == 1 and remaining[0]["quantity"] == 6_000_000
            assert runtime.business["cycles"][t.event_id]["quantity"] == 6_000_000

            runtime.client._reconcile_requested.clear()
            runtime.client._handle_ws_trade_msg(confirmed, True)
            await flush()
            assert not runtime.client._reconcile_requested.is_set()
            assert len(_sell_intents(runtime)) == 2
            sell_fills = [
                event
                for _, event in runtime.store.events()
                if isinstance(event, OrderFilled) and event.order_side == OrderSide.SELL
            ]
            assert len(sell_fills) == 1
            assert not runtime._test_queue_errors

    asyncio.run(run())
