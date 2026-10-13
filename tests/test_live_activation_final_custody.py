"""Final activation custody checks with controlled private HTTP responses only."""

import asyncio
from threading import Event
from unittest.mock import AsyncMock, Mock

import msgspec
import pytest

from test_live_recovery import accept, controlled_runtime, flush, fund, token, venue_trade


@pytest.mark.parametrize("recover_second_fill", [True, False])
def test_activation_final_custody_checks_recovered_quantity_before_ready(
    tmp_path, recover_second_fill
):
    async def run():
        async with controlled_runtime(tmp_path / "activation.sqlite") as runtime:
            await fund(runtime)
            tok = token(runtime)
            runtime.add_tokens([tok])
            oid = runtime.submit(tok, "BUY", 10_000_000, 990_000, cash=1_000_000)
            await flush()
            await accept(runtime, oid)
            first = msgspec.structs.replace(
                venue_trade(tok), id="first", size="5" if recover_second_fill else "10"
            )
            runtime.client.ingest_trade(first)
            await flush()
            second = msgspec.structs.replace(venue_trade(tok), id="second", size="5")
            custody_fetched = Event()

            def trades(**kwargs):
                # Reproduce generate_mass_status's parallel reads: custody covers
                # the original cycle before sync_owned recovers another fill.
                assert custody_fetched.wait(2)
                return [msgspec.to_builtins(second)] if recover_second_fill else []

            async def custody(**kwargs):
                custody_fetched.set()
                return [{"asset": tok.token_id, "size": "5" if recover_second_fill else "10"}]

            client = runtime.client
            client._connect = AsyncMock()
            client._http_client.get_trades = Mock(side_effect=trades)
            client._http_client.get_order = Mock(
                return_value={
                    "id": "venue-1",
                    "asset_id": tok.token_id,
                    "status": "CANCELED",
                    "size_matched": "10",
                    "associate_trades": ["first", "second"] if recover_second_fill else ["first"],
                }
            )
            client._http_client.get_balance_allowance = Mock(return_value={"balance": "0"})
            client._http_client.get_open_orders = Mock(return_value=[])
            client._fetch_user_positions = AsyncMock(side_effect=custody)
            try:
                if recover_second_fill:
                    with pytest.raises(ValueError, match="账户实际份额不足以覆盖本程序持仓"):
                        await client.activate()
                    assert not client.ready
                    assert client.reconciliation_task is None
                else:
                    await client.activate()
                    # Capture before activate's newly scheduled maintenance starts.
                    assert client.ready and client.open_orders_clear
                    assert client.reconciliation_task is not None
                assert client._fetch_user_positions.await_count == 2
                assert client._http_client.get_balance_allowance.call_count == 2
                assert runtime.business["cycles"][tok.event_id]["quantity"] == 10_000_000
                assert runtime.validate()["ok"]
                assert not runtime._test_queue_errors
            finally:
                if client.reconciliation_task:
                    client.reconciliation_task.cancel()
                    await asyncio.gather(client.reconciliation_task, return_exceptions=True)
                    client.reconciliation_task = None

    asyncio.run(run())
