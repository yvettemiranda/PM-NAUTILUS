"""Controlled LIVE redemption responses; no credentials, signing or RPC traffic."""

import asyncio
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import msgspec
import pytest
from web3 import Web3
from web3.exceptions import TransactionNotFound

from nautilus_trader.model.events import OrderFilled

from pm_nautilus.live import LiveExecution
from pm_nautilus.native import instrument_id
from pm_nautilus.redemption import PolygonWallet, RedemptionService, PUSD
from pm_nautilus.strategy import Runtime
from pm_nautilus.views import dashboard

from test_live import ADDRESS, flush
from test_live_recovery import accept, controlled_runtime, factory, fund, token, venue_trade


async def cash_response(runtime, amount):
    """Exercise the real adapter balance path with a controlled HTTP response."""
    runtime.client._http_client.get_balance_allowance = Mock(return_value={"balance": str(amount)})
    await LiveExecution._update_account_state(runtime.client)
    await flush()


def settlement_execution(runtime):
    # Only settlement orders reach the actual adapter dispatch in these tests.
    # ready=False rejects any accidental real order before the SDK can be called.
    runtime.client.ready = False
    runtime.client._submit_order = LiveExecution._submit_order.__get__(runtime.client)


async def own_settled_position(runtime, payout=1_000_000):
    await fund(runtime)
    t = token(runtime)
    runtime.add_tokens([t])
    runtime.book(t.token_id, [(90_000, 100_000_000)], [(100_000, 10_000_000)])
    oid = runtime.submit(t, "BUY", 10_000_000, 990_000, cash=1_000_000)
    await flush()
    await accept(runtime, oid)
    trade = venue_trade(t)
    runtime.client.ingest_trade(trade)
    await flush()
    runtime.client._http_client.get_trades = Mock(return_value=[msgspec.to_builtins(trade)])
    runtime.client._http_client.get_order = Mock(
        return_value={
            "asset_id": t.token_id,
            "associate_trades": [trade.id],
            "size_matched": "10",
            "status": "FILLED",
        }
    )
    runtime.client._update_account_state = AsyncMock(side_effect=partial(cash_response, runtime, 0))
    await runtime.client.sync_owned()
    await flush()
    assert not runtime.active_intents(t.event_id)
    assert runtime.cash() == 0
    service = RedemptionService(runtime, None)
    service.identify(t, {t.token_id: payout, "2": 1_000_000 - payout})
    claim = runtime.business["claims"][t.condition_id]
    assert claim["state"] == "IDENTIFIED"
    settlement_execution(runtime)
    return t, claim


class ControlledWallet:
    """Prepared bytes are markers, never a signed or broadcastable transaction."""

    def __init__(self):
        self.prepare_count = 0
        self.broadcasts = []
        self.receipt_count = 0
        self.result = None
        self.broadcast_error = None

    def prepare(self, claim):
        self.prepare_count += 1
        return {
            "raw_tx": "0x00",
            "tx_hash": "0x" + "d" * 64,
            "nonce": 7,
            "operation": "REDEEM",
        }

    def receipt(self, claim):
        self.receipt_count += 1
        return self.result

    def broadcast(self, claim):
        self.broadcasts.append((claim["raw_tx"], claim["tx_hash"], claim["nonce"]))
        if self.broadcast_error:
            raise self.broadcast_error


def assert_rights_preserved(runtime, t, claim):
    assert not runtime._test_queue_errors, runtime._test_queue_errors
    assert claim["state"] != "CREDITED"
    assert runtime.business["cycles"][t.event_id]["quantity"] == 10_000_000
    positions = runtime.native.cache.positions_open(instrument_id=instrument_id(t))
    assert sum(p.quantity.as_decimal() for p in positions) == 10
    assert runtime.cash() == 0
    assert dashboard(runtime)["portfolio"]["pendingRedemption"] == str(claim["amount"] // 1_000_000)


def settlement_fills(runtime):
    return [
        event
        for _, event in runtime.store.events()
        if isinstance(event, OrderFilled) and event.info.get("pm_kind") == "SETTLEMENT"
    ]


@pytest.mark.parametrize("payout", [0, 500_000, 1_000_000])
def test_live_wait_restart_and_idempotent_actual_credit(tmp_path, payout):
    async def run():
        path = tmp_path / "live.sqlite"
        wallet = ControlledWallet()
        async with controlled_runtime(path) as runtime:
            t, claim = await own_settled_position(runtime, payout)
            service = RedemptionService(runtime, None, wallet)
            service.identify(t, {t.token_id: payout, "2": 1_000_000 - payout})
            assert len(runtime.business["claims"]) == 1
            await service.advance(claim)
            assert claim["state"] == "SUBMITTED"
            assert_rights_preserved(runtime, t, claim)
            assert not settlement_fills(runtime)
            assert wallet.prepare_count == 1
            persisted = runtime.store.get("business")["claims"][t.condition_id]
            assert persisted["raw_tx"] == "0x00"
            assert persisted["tx_hash"] == "0x" + "d" * 64

        async with controlled_runtime(path) as restored:
            await flush()
            settlement_execution(restored)
            claim = restored.business["claims"][t.condition_id]
            service = RedemptionService(restored, None, wallet)
            await service.advance(claim)
            assert wallet.prepare_count == 1
            assert len(wallet.broadcasts) == 2
            assert wallet.broadcasts[0] == wallet.broadcasts[1]
            assert_rights_preserved(restored, t, claim)
            wallet.result = {"status": "CONFIRMED", "amount": claim["amount"]}
            restored.client._update_account_state = AsyncMock(
                side_effect=partial(cash_response, restored, claim["amount"])
            )
            await service.advance(claim)
            await service.advance(claim)  # Even before the native queue has drained.
            await flush()
            assert not restored._test_queue_errors, restored._test_queue_errors
            assert claim["state"] == "CREDITED"
            assert len(settlement_fills(restored)) == 1
            assert not restored.business["cycles"]
            assert restored.cash() == claim["amount"]
            assert restored.status == "PAUSED"
            assert restored.validate()["ok"]
            assert dashboard(restored)["portfolio"]["pendingRedemption"] == "0"
            old_calls = (wallet.prepare_count, wallet.receipt_count, len(wallet.broadcasts))
            await service.advance(claim)
            await service.run_once()
            assert (wallet.prepare_count, wallet.receipt_count, len(wallet.broadcasts)) == old_calls
            assert len(settlement_fills(restored)) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "result,error",
    [
        ({"status": "FAILED", "amount": 0}, "链上执行失败"),
        ({"status": "CONFIRMED", "amount": 9_000_000}, "实际回款与自有应收不一致"),
    ],
)
def test_live_failed_or_wrong_amount_retains_rights_without_credit(tmp_path, result, error):
    async def run():
        async with controlled_runtime(tmp_path / "live.sqlite") as runtime:
            t, claim = await own_settled_position(runtime)
            wallet = ControlledWallet()
            wallet.result = result
            service = RedemptionService(runtime, None, wallet)
            await service.advance(claim)
            await flush()
            assert claim["state"] == "FAILED"
            assert error in claim["error"]
            assert_rights_preserved(runtime, t, claim)
            assert not settlement_fills(runtime)
            calls = wallet.receipt_count
            await service.advance(claim)
            await service.run_once()
            assert wallet.prepare_count == 1 and wallet.receipt_count == calls

    asyncio.run(run())


def test_receipt_confirmed_but_cash_refresh_fails_then_recovers_after_restart(tmp_path):
    async def run():
        path = tmp_path / "live.sqlite"
        wallet = ControlledWallet()
        async with controlled_runtime(path) as runtime:
            t, claim = await own_settled_position(runtime)
            wallet.result = {"status": "CONFIRMED", "amount": claim["amount"]}
            service = RedemptionService(runtime, None, wallet)
            runtime.client._update_account_state = AsyncMock(side_effect=TimeoutError("controlled"))
            await service.run_once()
            assert claim["state"] == "CONFIRMED"
            assert claim["error"] == "TimeoutError"
            assert_rights_preserved(runtime, t, claim)
            assert not settlement_fills(runtime)

        async with controlled_runtime(path) as restored:
            await flush()
            settlement_execution(restored)
            claim = restored.business["claims"][t.condition_id]
            calls = wallet.receipt_count
            restored.client._update_account_state = AsyncMock(
                side_effect=partial(cash_response, restored, claim["amount"])
            )
            service = RedemptionService(restored, None, wallet)
            await service.run_once()
            await flush()
            assert not restored._test_queue_errors, restored._test_queue_errors
            assert claim["state"] == "CREDITED"
            assert wallet.prepare_count == 1 and wallet.receipt_count == calls
            assert len(settlement_fills(restored)) == 1
            assert restored.cash() == claim["amount"]

    asyncio.run(run())


def test_ambiguous_broadcast_persists_identity_and_never_prepares_replacement(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "live.sqlite") as runtime:
            t, claim = await own_settled_position(runtime)
            wallet = ControlledWallet()
            wallet.broadcast_error = TimeoutError("controlled")
            service = RedemptionService(runtime, None, wallet)
            await service.run_once()
            assert claim["state"] == "SUBMITTED"
            saved = runtime.store.get("business")["claims"][t.condition_id]
            assert saved["raw_tx"] == "0x00" and saved["nonce"] == 7
            assert_rights_preserved(runtime, t, claim)
            wallet.broadcast_error = None
            await service.run_once()
            assert wallet.prepare_count == 1
            assert wallet.broadcasts[0] == wallet.broadcasts[1]
            assert not settlement_fills(runtime)

    asyncio.run(run())


def test_cash_arrived_before_native_settlement_is_not_counted_twice(tmp_path):
    async def run():
        async with controlled_runtime(tmp_path / "live.sqlite") as runtime:
            _, claim = await own_settled_position(runtime)
            wallet = ControlledWallet()
            wallet.result = {"status": "CONFIRMED", "amount": claim["amount"]}
            runtime.client._update_account_state = AsyncMock(
                side_effect=partial(cash_response, runtime, claim["amount"])
            )
            await RedemptionService(runtime, None, wallet).advance(claim)
            # A UI request can run after the balance reply but before the native
            # settlement command/fill queues finish. Already-arrived cash is not
            # also an unpaid receivable during this window.
            assert runtime.cash() == 10_000_000
            assert dashboard(runtime)["portfolio"]["totalFunds"] == "10"
            await flush()
            assert claim["state"] == "CREDITED"

    asyncio.run(run())


def test_crash_after_local_settlement_intent_recovers_without_second_redemption(tmp_path):
    async def run():
        path = tmp_path / "live.sqlite"
        wallet = ControlledWallet()
        runtime = Runtime(path, mode="LIVE", live_factory=factory)
        t, claim = await own_settled_position(runtime)
        wallet.result = {"status": "CONFIRMED", "amount": claim["amount"]}
        runtime.client._update_account_state = AsyncMock(
            side_effect=partial(cash_response, runtime, claim["amount"])
        )
        await RedemptionService(runtime, None, wallet).advance(claim)
        assert runtime.active_intents(t.event_id)
        assert not settlement_fills(runtime)
        # Interrupt the command queue before local settlement produces native
        # fills. Chain receipt, actual cash, and local OrderInitialized are durable.
        runtime.native.execution.kill()
        runtime.native.stop()
        await flush()
        runtime.store.close()

        async with controlled_runtime(path) as restored:
            await flush()
            settlement_execution(restored)
            claim = restored.business["claims"][t.condition_id]
            restored.client._update_account_state = AsyncMock(
                side_effect=partial(cash_response, restored, claim["amount"])
            )
            service = RedemptionService(restored, None, wallet)
            await service.run_once()
            await flush()
            assert not restored._test_queue_errors, restored._test_queue_errors
            assert claim["state"] == "CREDITED"
            assert len(settlement_fills(restored)) == 1
            assert not restored.business["cycles"]
            assert restored.cash() == 10_000_000
            assert wallet.prepare_count == 1
            assert wallet.receipt_count == 1

    asyncio.run(run())


@pytest.mark.parametrize("balances", [{"1": 11_000_000, "2": 0}, {"1": 10_000_000, "2": 1}])
def test_wallet_rejects_manual_same_condition_assets_before_signing(balances):
    # Bypass __init__: no credential file, private key, HTTP provider or signer.
    wallet = PolygonWallet.__new__(PolygonWallet)
    wallet.preflight = Mock()
    wallet.owned_balances = Mock(return_value=balances)
    wallet.signer = SimpleNamespace(
        sign_transaction=Mock(side_effect=AssertionError("must not sign"))
    )
    claim = {"rights": {"1": 10_000_000}, "payouts": {"1": 1_000_000, "2": 0}}
    with pytest.raises(ValueError, match="拒绝覆盖手动资产"):
        wallet.prepare(claim)
    wallet.signer.sign_transaction.assert_not_called()


def test_receipt_requires_confirmations_actual_pusd_transfer_and_burned_rights():
    wallet = PolygonWallet.__new__(PolygonWallet)
    wallet.funder = ADDRESS
    wallet.owned_balances = Mock(return_value={"1": 0, "2": 0})
    amount = 10_000_000
    receipt = {
        "blockNumber": 100,
        "status": 1,
        "logs": [
            {
                "address": PUSD,
                "topics": [
                    Web3.keccak(text="Transfer(address,address,uint256)"),
                    bytes(32),
                    bytes.fromhex(ADDRESS[2:]).rjust(32, b"\0"),
                ],
                "data": amount.to_bytes(32, "big"),
            }
        ],
    }
    eth = SimpleNamespace(block_number=131, get_transaction_receipt=Mock(return_value=receipt))
    wallet.w3 = SimpleNamespace(eth=eth)
    claim = {"tx_hash": "0x" + "d" * 64, "operation": "REDEEM"}
    assert wallet.receipt(claim) is None
    eth.block_number = 132
    assert wallet.receipt(claim) == {"status": "CONFIRMED", "amount": amount}
    wallet.owned_balances.return_value = {"1": 1, "2": 0}
    assert wallet.receipt(claim) == {"status": "FAILED", "amount": amount}
    receipt["status"] = 0
    assert wallet.receipt(claim) == {"status": "FAILED", "amount": 0}
    eth.get_transaction_receipt.side_effect = TransactionNotFound("controlled")
    assert wallet.receipt(claim) is None
