"""Malformed controlled REST responses must never release an owned order."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from nautilus_trader.model.identifiers import ClientOrderId

from test_live_recovery import accept, controlled_runtime, flush, fund, token


COMPLETE_TERMINAL = {
    "id": "venue-1",
    "asset_id": "1",
    "associate_trades": [],
    "size_matched": "0",
    "status": "CANCELED",
}


@pytest.mark.parametrize(
    "change",
    [
        {"id": "another-order"},
        {"asset_id": "another-token"},
        {"size_matched": "-1"},
        {"size_matched": "NaN"},
        {"size_matched": "0.0000001"},
        {"size_matched": 0},
        {"associate_trades": "trade-id"},
        {"associate_trades": [None]},
        {"status": "UNKNOWN"},
        {"id": None},
        {"size_matched": None},
        {"associate_trades": "__absent__"},
    ],
)
def test_incomplete_or_mismatched_terminal_cannot_release_reservation(tmp_path, change):
    async def run():
        async with controlled_runtime(tmp_path / "malformed-order.sqlite") as runtime:
            await fund(runtime)
            t = token(runtime)
            runtime.add_tokens([t])
            oid = runtime.submit(t, "BUY", 10_000_000, 990_000, cash=1_000_000)
            await flush()
            await accept(runtime, oid)
            response = COMPLETE_TERMINAL | change
            for key, value in list(response.items()):
                if value is None or value == "__absent__":
                    response.pop(key)
            client = runtime.client
            client._http_client.get_trades = Mock(return_value=[])
            client._http_client.get_order = Mock(return_value=response)
            client._update_account_state = AsyncMock()
            with pytest.raises(ValueError, match="订单查询"):
                await client.sync_owned()
            await flush()
            intent = runtime.store.intent(oid)
            assert not intent["terminal"] and not intent.get("verified_terminal")
            assert runtime.held_cash() == 1_000_000
            assert runtime.active_intents(t.event_id)
            assert not runtime.native.cache.order(ClientOrderId(oid)).is_closed
            client._update_account_state.assert_not_awaited()
            assert not runtime._test_queue_errors

    asyncio.run(run())


@pytest.mark.parametrize("trades", [[], None, ["awaiting-trade"]])
@pytest.mark.parametrize("status", ["CANCELED", "CANCELED_MARKET_RESOLVED"])
def test_complete_zero_terminal_or_unconfirmed_match_uses_venue_facts(tmp_path, trades, status):
    async def run():
        async with controlled_runtime(tmp_path / "complete-order.sqlite") as runtime:
            await fund(runtime)
            t = token(runtime)
            runtime.add_tokens([t])
            oid = runtime.submit(t, "BUY", 10_000_000, 990_000, cash=1_000_000)
            await flush()
            await accept(runtime, oid)
            response = COMPLETE_TERMINAL | {
                "associate_trades": trades,
                "size_matched": "10" if trades else "0",
                "status": status,
            }
            client = runtime.client
            client._http_client.get_trades = Mock(return_value=[])
            client._http_client.get_order = Mock(return_value=response)
            client._update_account_state = AsyncMock()
            await client.sync_owned()
            await flush()
            intent = runtime.store.intent(oid)
            assert intent["terminal"] is (not bool(trades))
            assert runtime.held_cash() == (1_000_000 if trades else 0)
            assert not runtime._test_queue_errors

    asyncio.run(run())
