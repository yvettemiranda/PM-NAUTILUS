"""Durable account nonce ownership with marker transactions and fake RPC only."""

import asyncio
import sqlite3
from copy import deepcopy
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from web3 import Web3

import pm_nautilus.redemption as redemption
from pm_nautilus.redemption import (
    RedemptionApprovalService,
    RedemptionCheckError,
    RedemptionService,
)
from pm_nautilus.store import Store
from test_redemption_approval import ControlledWallet as ApprovalWallet


class Wallet(ApprovalWallet):
    def __init__(self):
        super().__init__()
        self.claim_preparations = []
        self.claim_broadcasts = []
        self.nonce = 7
        self.result = None
        self.broadcast_error = TimeoutError("controlled ambiguity")

    def prepare(self, claim):
        self.claim_preparations.append(claim["condition_id"])
        raw = "0x" + bytes([len(self.claim_preparations), self.nonce]).hex()
        return {
            "raw_tx": raw,
            "tx_hash": Web3.to_hex(Web3.keccak(hexstr=raw)),
            "nonce": self.nonce,
            "operation": "REDEEM",
        }

    def broadcast(self, pending):
        if pending["operation"] == "APPROVE":
            return super().broadcast(pending)
        self.claim_broadcasts.append((pending["raw_tx"], pending["tx_hash"], pending["nonce"]))
        if self.broadcast_error:
            raise self.broadcast_error

    def receipt(self, pending):
        if pending["operation"] == "APPROVE":
            return super().receipt(pending)
        return self.result


def runtime(store, *, new_claims=True):
    business = store.get("business")
    if new_claims:
        business["claims"] = {
            key: {
                "condition_id": key,
                "token_id": key,
                "generation": store.generation,
                "state": "IDENTIFIED",
                "amount": 0,
                "error": None,
            }
            for key in ("first", "second")
        }
        store.put("business", business)
    return SimpleNamespace(
        mode="LIVE",
        store=store,
        business=business,
        tokens={},
        client=SimpleNamespace(_update_account_state=AsyncMock()),
        settlement_fill=Mock(),
    )


def test_unknown_claim_keeps_account_nonce_across_restart_and_later_confirmation(tmp_path):
    async def run():
        path = tmp_path / "LIVE/state.sqlite"
        wallet = Wallet()
        store = Store(path, "LIVE")
        owner = runtime(store)
        try:
            await RedemptionService(owner, None, wallet).run_once()
            assert wallet.claim_preparations == ["first"]
            assert owner.business["claims"]["first"]["state"] == "SUBMITTED"
            assert owner.business["claims"]["second"]["state"] == "IDENTIFIED"
            first_raw = wallet.claim_broadcasts[0]
        finally:
            store.close()

        store = Store(path, "LIVE")
        owner = runtime(store, new_claims=False)
        try:
            await RedemptionService(owner, None, wallet).run_once()
            assert wallet.claim_preparations == ["first"]
            assert wallet.claim_broadcasts == [first_raw, first_raw]
            approval = RedemptionApprovalService(wallet, path)
            with pytest.raises(RedemptionCheckError, match="待确认链上交易"):
                await asyncio.to_thread(approval.start)
            assert not wallet.prepared
            wallet.result = {"status": "CONFIRMED", "amount": 0}
            wallet.broadcast_error = None
            wallet.nonce = 8
            await RedemptionService(owner, None, wallet).run_once()
            assert owner.business["claims"]["first"]["state"] == "CONFIRMED"
            await RedemptionService(owner, None, wallet).run_once()
            assert wallet.claim_preparations == ["first", "second"]
            assert owner.business["claims"]["second"]["nonce"] == 8
            assert store.get(redemption._nonce_key(wallet)) == 8
            owner.settlement_fill.assert_not_called()  # Actual cash gate remains required.
        finally:
            store.close()

    asyncio.run(run())


def test_pending_approval_blocks_new_claim_and_stale_nonce_cannot_be_reused(tmp_path):
    async def run():
        path = tmp_path / "LIVE/state.sqlite"
        store = Store(path, "LIVE")
        owner = runtime(store)
        wallet = Wallet()
        try:
            approval = RedemptionApprovalService(wallet, path)
            assert (await asyncio.to_thread(approval.start))["status"] == "PENDING"
            await RedemptionService(owner, None, wallet).run_once()
            assert wallet.claim_preparations == []
            # Two finalized operator transactions consume nonces 1 and 2.
            wallet.receipt_result = {"status": "APPROVED", "amount": 0}
            await asyncio.to_thread(approval.advance)
            wallet.receipt_result = {"status": "APPROVED", "amount": 0}
            await asyncio.to_thread(approval.advance)
            wallet.nonce = 2
            await RedemptionService(owner, None, wallet).run_once()
            assert not wallet.claim_broadcasts
            assert all(
                claim["state"] == "IDENTIFIED" for claim in owner.business["claims"].values()
            )
            assert "nonce" in owner.business["claims"]["first"]["error"]
            assert store.get(redemption._nonce_key(wallet)) == 2
            another_wallet = Wallet()
            another_wallet.signer = SimpleNamespace(address="0x" + "33" * 20)
            redemption._check_prepared_nonce(store.db, another_wallet, {"nonce": 0})
        finally:
            store.close()

    asyncio.run(run())


def test_cancelled_prepare_joins_twice_then_saves_on_owner_loop_before_unlock(tmp_path):
    async def run():
        store = Store(tmp_path / "LIVE/state.sqlite", "LIVE")
        owner = runtime(store)
        wallet = Wallet()
        entered, release, completed = Event(), Event(), Event()
        prepare = wallet.prepare
        owner_thread = get_ident()
        writes = []
        save = store.put

        def gated_prepare(claim):
            entered.set()
            try:
                assert release.wait(5)
                return prepare(claim)
            finally:
                completed.set()

        def observed_save(*args, **kwargs):
            writes.append(get_ident())
            return save(*args, **kwargs)

        wallet.prepare = gated_prepare
        store.put = observed_save
        first = owner.business["claims"]["first"]
        second = owner.business["claims"]["second"]
        task = asyncio.create_task(RedemptionService(owner, None, wallet).advance(first))
        following = None
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            following = asyncio.create_task(RedemptionService(owner, None, wallet).advance(second))
            for _ in range(10):
                await asyncio.sleep(0)
            assert not task.done() and not following.done() and not completed.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            await asyncio.wait_for(following, 5)
            assert completed.is_set()
            assert store.get("business")["claims"]["first"]["state"] == "SUBMITTED"
            assert wallet.claim_preparations == ["first"] and not wallet.claim_broadcasts
            assert writes and set(writes) == {owner_thread}
        finally:
            release.set()
            await asyncio.gather(task, *(x for x in (following,) if x), return_exceptions=True)
            store.close()

    asyncio.run(run())


def test_resolution_queries_are_bounded_and_cancellation_joins_all_reads(tmp_path):
    async def run():
        store = Store(tmp_path / "TEST.sqlite", "TEST")
        owner = runtime(store)
        owner.mode = "TEST"
        owner.business["claims"] = {}
        owner.business["cycles"] = {str(i): {"token_id": str(i)} for i in range(12)}
        owner.tokens = {
            str(i): SimpleNamespace(condition_id=str(i), market_id=str(i)) for i in range(12)
        }
        active = 0
        peak = 0
        finished = 0
        full = asyncio.Event()

        async def get(_url):
            nonlocal active, peak, finished
            active += 1
            peak = max(peak, active)
            if active == redemption.RESOLUTION_CONCURRENCY:
                full.set()
            try:
                await asyncio.Future()
            finally:
                active -= 1
                finished += 1

        service = RedemptionService(owner, SimpleNamespace(get=get))
        task = asyncio.create_task(service.run_once())
        try:
            await asyncio.wait_for(full.wait(), 3)
            assert peak == redemption.RESOLUTION_CONCURRENCY
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
            assert active == 0 and finished == peak and not service.busy
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            store.close()

    asyncio.run(run())


def test_failed_prepared_save_rolls_back_and_never_broadcasts_then_retry_is_durable(tmp_path):
    async def run():
        store = Store(tmp_path / "LIVE/state.sqlite", "LIVE")
        owner = runtime(store)
        wallet = Wallet()
        service = RedemptionService(owner, None, wallet)
        claim = owner.business["claims"]["first"]
        try:
            store.db.execute("""
                CREATE TRIGGER fail_preparation BEFORE UPDATE ON business_parts
                WHEN NEW.collection='claims' AND NEW.key='first'
                  AND json_extract(NEW.payload,'$.state')='SUBMITTED'
                BEGIN SELECT RAISE(ROLLBACK, 'controlled durable write failure'); END
            """)
            with pytest.raises(sqlite3.IntegrityError):
                await service.advance(claim)
            assert not wallet.claim_broadcasts
            assert claim["state"] == "IDENTIFIED" and "raw_tx" not in claim
            assert store.get("business")["claims"]["first"]["state"] == "IDENTIFIED"
            assert not store.db.in_transaction
            store.db.execute("DROP TRIGGER fail_preparation")
            with pytest.raises(TimeoutError):
                await service.advance(claim)
            assert claim["state"] == "SUBMITTED"
            assert (
                store.get("business")["claims"]["first"]["raw_tx"] == wallet.claim_broadcasts[0][0]
            )
            assert len(wallet.claim_broadcasts) == 1
        finally:
            store.close()

    asyncio.run(run())


def test_confirmation_and_nonce_floor_commit_together_or_keep_original_pending(tmp_path):
    async def run():
        path = tmp_path / "LIVE/state.sqlite"
        store = Store(path, "LIVE")
        owner = runtime(store)
        wallet = Wallet()
        service = RedemptionService(owner, None, wallet)
        first = owner.business["claims"]["first"]
        try:
            with pytest.raises(TimeoutError):
                await service.advance(first)
            wallet.result = {"status": "CONFIRMED", "amount": 0}
            store.db.execute("""
                CREATE TRIGGER fail_confirmation BEFORE UPDATE ON business_parts
                WHEN NEW.collection='claims' AND NEW.key='first'
                  AND json_extract(NEW.payload,'$.state')='CONFIRMED'
                BEGIN SELECT RAISE(ROLLBACK, 'controlled confirmation write failure'); END
            """)
            with pytest.raises(sqlite3.IntegrityError):
                await service.advance(first)
            assert first["state"] == "SUBMITTED"
            assert store.get(redemption._nonce_key(wallet)) is None
            await service.advance(owner.business["claims"]["second"])
            assert wallet.claim_preparations == ["first"]
            store.db.execute("DROP TRIGGER fail_confirmation")
        finally:
            store.close()
        store = Store(path, "LIVE")
        owner = runtime(store, new_claims=False)
        try:
            await RedemptionService(owner, None, wallet).advance(owner.business["claims"]["first"])
            assert store.get("business")["claims"]["first"]["state"] == "CONFIRMED"
            assert store.get(redemption._nonce_key(wallet)) == 7
            assert wallet.claim_preparations == ["first"]
        finally:
            store.close()

    asyncio.run(run())


def test_failed_resolution_identification_restores_targets_and_retries_without_duplicates(tmp_path):
    store = Store(tmp_path / "LIVE/state.sqlite", "LIVE")
    business = store.get("business")
    business["cycles"] = {"event": {"token_id": "1", "quantity": 10_000_000, "cost": 100_000}}
    business["targets"] = {
        "target": {"event_id": "event", "price": 500_000, "remaining_qty": 10_000_000}
    }
    store.put("business", business)
    owner = SimpleNamespace(
        store=store,
        business=business,
        active_intents=Mock(return_value={}),
        cancel_buys=Mock(),
        refresh_eligibility=Mock(),
    )
    token = SimpleNamespace(
        event_id="event",
        token_id="1",
        condition_id="condition",
        market_id="market",
        neg_risk=False,
    )
    payouts = {"1": 1_000_000, "2": 0}
    before = deepcopy(business)
    try:
        store.db.execute("""
            CREATE TRIGGER fail_identification BEFORE INSERT ON business_parts
            WHEN NEW.collection='claims'
            BEGIN SELECT RAISE(ROLLBACK, 'controlled identification failure'); END
        """)
        with pytest.raises(sqlite3.IntegrityError):
            RedemptionService(owner, None).identify(token, payouts)
        assert owner.business == before and store.get("business") == before
        owner.refresh_eligibility.assert_not_called()
        store.db.execute("DROP TRIGGER fail_identification")
        service = RedemptionService(owner, None)
        service.identify(token, payouts)
        service.identify(token, payouts)
        assert owner.business["settled"] == ["condition"]
        assert list(owner.business["claims"]) == ["condition"]
        assert not owner.business["targets"]
        assert owner.business == store.get("business")
    finally:
        store.close()


def test_failed_test_confirmation_cannot_skip_durable_identified_stage(tmp_path):
    async def run():
        store = Store(tmp_path / "TEST.sqlite")
        owner = runtime(store)
        owner.mode = "TEST"
        claim = owner.business["claims"]["first"]
        try:
            store.db.execute("""
                CREATE TRIGGER fail_test_confirmation BEFORE UPDATE ON business_parts
                WHEN NEW.collection='claims' AND json_extract(NEW.payload,'$.state')='CONFIRMED'
                BEGIN SELECT RAISE(ROLLBACK, 'controlled test confirmation failure'); END
            """)
            with pytest.raises(sqlite3.IntegrityError):
                await RedemptionService(owner, None).advance(claim)
            assert claim["state"] == "IDENTIFIED"
            assert store.get("business")["claims"]["first"]["state"] == "IDENTIFIED"
            owner.settlement_fill.assert_not_called()
            store.db.execute("DROP TRIGGER fail_test_confirmation")
            await RedemptionService(owner, None).advance(claim)
            assert claim["state"] == "CONFIRMED"
            owner.settlement_fill.assert_not_called()
        finally:
            store.close()

    asyncio.run(run())


def test_authorization_save_failure_is_not_broadcast_and_resume_reloads_durable_state(tmp_path):
    store = Store(tmp_path / "LIVE/state.sqlite", "LIVE")
    wallet = ApprovalWallet()
    service = RedemptionApprovalService(wallet, store.path)
    try:
        store.db.execute("""
            CREATE TRIGGER fail_authorization BEFORE INSERT ON meta
            WHEN NEW.key='redemption_approval'
              AND json_type(NEW.value,'$.pending')='object'
            BEGIN SELECT RAISE(ROLLBACK, 'controlled authorization failure'); END
        """)
        with pytest.raises(RedemptionCheckError, match="未广播新交易"):
            service.start()
        assert wallet.prepared == [False] and wallet.broadcasts == []
        saved = store.get(redemption.APPROVAL_KEY)
        assert saved["status"] == "ACTIVE" and saved["pending"] is None
        store.db.execute("DROP TRIGGER fail_authorization")
        assert RedemptionApprovalService(wallet, store.path).advance()["status"] == "PENDING"
        assert len(wallet.broadcasts) == 1
        assert store.get(redemption.APPROVAL_KEY)["pending"]["tx_hash"] == wallet.broadcasts[0]
    finally:
        store.close()
