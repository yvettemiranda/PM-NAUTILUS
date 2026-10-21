"""One incomplete venue lookup cannot hide another order's verified terminal."""

import asyncio
from unittest.mock import AsyncMock, Mock

import msgspec
import pytest
from nautilus_trader.model.identifiers import ClientOrderId, VenueOrderId

from pm_nautilus.native import quantity

from test_live_recovery import controlled_runtime, flush, fund, token, venue_trade


async def accepted(runtime, t, venue):
    oid = runtime.submit(t, "BUY", 5_000_000, 990_000, cash=500_000)
    await flush()
    order = runtime.native.cache.order(ClientOrderId(oid))
    venue_id = VenueOrderId(venue)
    runtime.native.cache.add_venue_order_id(order.client_order_id, venue_id)
    intent = runtime.store.intent(oid)
    intent["venue_id"] = venue
    runtime.store.save_intent(oid, intent)
    runtime.client.generate_order_submitted(
        order.strategy_id, order.instrument_id, order.client_order_id, runtime.now
    )
    runtime.client._send_quote_to_base_update(order, venue_id, quantity(5_000_000))
    runtime.client.generate_order_accepted(
        order.strategy_id, order.instrument_id, order.client_order_id, venue_id, runtime.now
    )
    await flush()
    return oid


@pytest.mark.parametrize("bad_first", [True, False])
@pytest.mark.parametrize("failure", ["timeout", "identity", "overbound", "missing"])
def test_verified_terminal_progress_survives_unrelated_lookup_error(tmp_path, bad_first, failure):
    async def run():
        async with controlled_runtime(tmp_path / "independent.sqlite") as runtime:
            await fund(runtime)
            good_token, bad_token = token(runtime), token(runtime, 2)
            runtime.add_tokens([good_token, bad_token])
            rows = [(bad_token, "venue-2"), (good_token, "venue-1")]
            if not bad_first:
                rows.reverse()
            ids = {}
            for t, venue in rows:
                ids[venue] = await accepted(runtime, t, venue)
            client = runtime.client
            client.ready = client.open_orders_clear = True
            client._http_client.get_trades = Mock(return_value=[])
            client._update_account_state = AsyncMock()

            def lookup(venue):
                if venue == "venue-2" and failure == "timeout":
                    raise TimeoutError("controlled unrelated order")
                response = {
                    "id": venue,
                    "asset_id": "1" if venue == "venue-1" else "2",
                    "associate_trades": [],
                    "size_matched": "0",
                    "status": "CANCELED",
                }
                if venue == "venue-2":
                    if failure == "identity":
                        response["id"] = "another-order"
                    elif failure == "overbound":
                        response["size_matched"] = "100"
                    elif failure == "missing":
                        return None
                return response

            client._http_client.get_order = Mock(side_effect=lookup)
            with pytest.raises((ValueError, TimeoutError)):
                await client.sync_owned()
            await flush()
            good, bad = (runtime.store.intent(ids[venue]) for venue in ("venue-1", "venue-2"))
            assert good["terminal"] and good["verified_terminal"]
            assert not bad["terminal"] and not bad.get("verified_terminal")
            assert runtime.held_cash() == 500_000
            assert not runtime.active_intents("e1")
            assert runtime.active_intents("e2")
            assert not client.ready and not client.open_orders_clear
            client._update_account_state.assert_not_awaited()
            assert not runtime._test_queue_errors

            # A later complete round handles the remaining identity only; the
            # earlier terminal and released reservation are not applied twice.
            client._http_client.get_order.side_effect = lambda venue: {
                "id": venue,
                "asset_id": "2",
                "associate_trades": [],
                "size_matched": "0",
                "status": "CANCELED",
            }
            client._http_client.get_order.reset_mock()
            await client.sync_owned()
            await flush()
            client._http_client.get_order.assert_called_once_with("venue-2")
            assert runtime.held_cash() == 0
            assert runtime.store.intent(ids["venue-2"])["terminal"]
            # Recovery still requires the existing account/ledger/custody checks.
            assert not client.ready and not client.open_orders_clear
            assert not runtime._test_queue_errors

    asyncio.run(run())


def test_verified_partial_fill_keeps_position_and_releases_only_its_own_remainder(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "filled.sqlite") as runtime:
            await fund(runtime)
            t, other = token(runtime), token(runtime, 2)
            runtime.add_tokens([t, other])
            good = await accepted(runtime, t, "venue-1")
            bad = await accepted(runtime, other, "venue-2")
            client = runtime.client
            client.ingest_trade(msgspec.structs.replace(venue_trade(t), size="4"))
            await flush()
            assert runtime.held_cash() == 1_000_000
            client.ready = client.open_orders_clear = True
            client._http_client.get_trades = Mock(return_value=[])
            client._update_account_state = AsyncMock()

            def lookup(venue):
                if venue == "venue-2":
                    raise TimeoutError("controlled other order")
                return {
                    "id": venue,
                    "asset_id": "1",
                    "associate_trades": ["confirmed-trade-1"],
                    "size_matched": "4",
                    "status": "CANCELED",
                }

            client._http_client.get_order = Mock(side_effect=lookup)
            with pytest.raises(TimeoutError):
                await client.sync_owned()
            await flush()
            assert runtime.store.intent(good)["terminal"]
            assert not runtime.store.intent(bad)["terminal"]
            # The other order retains 0.50U; the confirmed 0.40U remains
            # covered locally until a successful account refresh includes it.
            assert runtime.held_cash() == 900_000
            cycle = runtime.business["cycles"][t.event_id]
            assert cycle["quantity"] == 4_000_000 and cycle["spent"] == 400_000
            assert not client.ready and not client.open_orders_clear
            assert runtime.validate()["ok"]
            assert not runtime._test_queue_errors

    asyncio.run(run())
