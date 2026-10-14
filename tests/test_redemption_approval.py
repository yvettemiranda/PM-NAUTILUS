"""Controlled authorization workflow; no real wallet, RPC, or transaction."""

import json
import sqlite3
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from web3 import Web3
from web3.exceptions import Web3RPCError

from pm_nautilus.redemption import (
    ADAPTERS,
    APPROVAL_KEY,
    CTF,
    EXCHANGES,
    PUSD,
    PolygonWallet,
    RedemptionApprovalService,
    RedemptionCheckError,
)
from pm_nautilus.store import Store


SIGNER = "0x" + "11" * 20
FUNDER = "0x" + "22" * 20


class ControlledWallet:
    def __init__(self):
        self.signer = SimpleNamespace(address=SIGNER)
        self.funder = FUNDER
        self.signature_type = 2
        self.approvals = {False: False, True: False}
        self.prepared = []
        self.broadcasts = []
        self.receipt_result = None
        self.broadcast_fails = False

    def preflight(self):
        return None

    def adapter_approvals(self):
        return dict(self.approvals)

    def prepare_approval(self, neg_risk):
        self.prepared.append(neg_risk)
        raw = "0x" + bytes([len(self.prepared), int(neg_risk)]).hex()
        return {
            "raw_tx": raw,
            "tx_hash": Web3.to_hex(Web3.keccak(hexstr=raw)),
            "nonce": len(self.prepared),
            "operation": "APPROVE",
            "neg_risk": neg_risk,
            "operator": ADAPTERS[neg_risk],
        }

    def broadcast(self, pending):
        self.broadcasts.append(pending["tx_hash"])
        if self.broadcast_fails:
            raise TimeoutError("controlled network ambiguity")

    def receipt(self, pending):
        result = self.receipt_result
        if result is not None:
            self.receipt_result = None
            if result["status"] == "APPROVED":
                self.approvals[pending["neg_risk"]] = True
        return result


def ledger(tmp_path):
    path = tmp_path / "LIVE" / "state.sqlite"
    store = Store(path, "LIVE")
    store.close()
    return path


def saved(path):
    with sqlite3.connect(path) as db:
        return json.loads(
            db.execute("SELECT value FROM meta WHERE key=?", (APPROVAL_KEY,)).fetchone()[0]
        )


def test_explicit_two_operator_approval_survives_restart_and_reuses_ambiguous_tx(tmp_path):
    path = ledger(tmp_path)
    wallet = ControlledWallet()
    service = RedemptionApprovalService(wallet, path)
    assert service.status()["status"] == "MISSING"
    assert service.advance()["status"] == "MISSING"
    assert wallet.prepared == []

    first = service.start()
    assert first["status"] == "PENDING"
    assert first["pendingTxHash"] == wallet.broadcasts[0]
    assert wallet.prepared == [False]
    assert saved(path)["pending"]["raw_tx"]

    restarted = RedemptionApprovalService(wallet, path)
    wallet.broadcast_fails = True
    ambiguous = restarted.advance()
    assert ambiguous["status"] == "PENDING" and ambiguous["networkError"] is True
    assert wallet.prepared == [False]
    assert wallet.broadcasts == [first["pendingTxHash"], first["pendingTxHash"]]

    wallet.broadcast_fails = False
    wallet.receipt_result = {"status": "APPROVED", "amount": 0}
    second = restarted.advance()
    assert second["status"] == "PENDING"
    assert second["standardApproved"] is True
    assert wallet.prepared == [False, True]
    assert second["pendingTxHash"] != first["pendingTxHash"]

    wallet.receipt_result = {"status": "APPROVED", "amount": 0}
    complete = RedemptionApprovalService(wallet, path).advance()
    assert complete["status"] == "APPROVED"
    assert complete["standardApproved"] and complete["negRiskApproved"]
    assert complete["pendingTxHash"] is None
    assert saved(path)["status"] == "COMPLETE"
    assert saved(path)["pending"] is None
    assert wallet.prepared == [False, True]


def test_failed_approval_stops_and_requires_new_explicit_start(tmp_path):
    path = ledger(tmp_path)
    wallet = ControlledWallet()
    service = RedemptionApprovalService(wallet, path)
    service.start()
    wallet.receipt_result = {"status": "FAILED", "amount": 0}
    failed = service.advance()
    assert failed["status"] == "FAILED"
    assert wallet.prepared == [False]
    assert service.advance()["status"] == "FAILED"
    assert wallet.prepared == [False]
    restarted = RedemptionApprovalService(wallet, path)
    assert restarted.start()["status"] == "PENDING"
    assert wallet.prepared == [False, False]


def test_corrupt_pending_hash_or_wallet_binding_cannot_rebroadcast(tmp_path):
    path = ledger(tmp_path)
    wallet = ControlledWallet()
    service = RedemptionApprovalService(wallet, path)
    service.start()
    record = saved(path)
    record["pending"]["tx_hash"] = "0x" + "00" * 32
    with sqlite3.connect(path) as db:
        db.execute("UPDATE meta SET value=? WHERE key=?", (json.dumps(record), APPROVAL_KEY))
    calls = len(wallet.broadcasts)
    with pytest.raises(RedemptionCheckError, match="交易无效"):
        service.advance()
    assert len(wallet.broadcasts) == calls
    record["pending"]["tx_hash"] = Web3.to_hex(Web3.keccak(hexstr=record["pending"]["raw_tx"]))
    wallet.funder = "0x" + "33" * 20
    with sqlite3.connect(path) as db:
        db.execute("UPDATE meta SET value=? WHERE key=?", (json.dumps(record), APPROVAL_KEY))
    with pytest.raises(RedemptionCheckError, match="当前钱包不一致"):
        service.advance()
    assert len(wallet.broadcasts) == calls


@pytest.mark.parametrize("signature_type", [0, 2])
@pytest.mark.parametrize("neg_risk", [False, True])
def test_approval_targets_ctf_operator_for_actual_funder(signature_type, neg_risk):
    wallet = PolygonWallet.__new__(PolygonWallet)
    wallet.funder = SIGNER if signature_type == 0 else FUNDER
    wallet.signature_type = signature_type
    wallet.preflight = Mock()
    wallet._sign_contract_call = Mock(return_value={"raw_tx": "0x01", "tx_hash": "0x02"})
    is_approved = Mock(return_value=SimpleNamespace(call=Mock(return_value=False)))
    set_approval = Mock(
        return_value=SimpleNamespace(_encode_transaction_data=Mock(return_value="0x1234"))
    )
    wallet.ctf = SimpleNamespace(
        functions=SimpleNamespace(
            isApprovedForAll=is_approved,
            setApprovalForAll=set_approval,
        )
    )
    prepared = wallet.prepare_approval(neg_risk)
    is_approved.assert_called_once_with(wallet.funder, Web3.to_checksum_address(ADAPTERS[neg_risk]))
    set_approval.assert_called_once_with(Web3.to_checksum_address(ADAPTERS[neg_risk]), True)
    wallet._sign_contract_call.assert_called_once_with(CTF, "0x1234", "APPROVE")
    assert prepared["operator"] == Web3.to_checksum_address(ADAPTERS[neg_risk])
    assert prepared["neg_risk"] is neg_risk


@pytest.mark.parametrize("signature_type", [0, 2])
def test_signed_approval_transaction_uses_eoa_or_single_owner_safe(signature_type):
    wallet = PolygonWallet.__new__(PolygonWallet)
    wallet.funder = SIGNER if signature_type == 0 else FUNDER
    wallet.signature_type = signature_type
    signer = SimpleNamespace(
        address=SIGNER,
        sign_transaction=Mock(
            return_value=SimpleNamespace(raw_transaction=b"\x01\x02", hash=b"\x03" * 32)
        ),
    )
    wallet.signer = signer
    safe_call = Mock(
        return_value=SimpleNamespace(_encode_transaction_data=Mock(return_value="0x5678"))
    )
    safe = SimpleNamespace(functions=SimpleNamespace(execTransaction=safe_call))
    eth = SimpleNamespace(
        contract=Mock(return_value=safe),
        get_transaction_count=Mock(return_value=7),
        gas_price=100,
        estimate_gas=Mock(return_value=100_000),
        get_balance=Mock(return_value=12_000_000),
    )
    wallet.w3 = SimpleNamespace(eth=eth)
    prepared = wallet._sign_contract_call(CTF, "0x1234", "APPROVE")
    tx = signer.sign_transaction.call_args.args[0]
    assert tx["from"] == SIGNER and tx["nonce"] == 7 and tx["chainId"] == 137
    assert tx["gas"] == 120_000 and tx["gasPrice"] == 100
    if signature_type == 0:
        assert tx["to"] == Web3.to_checksum_address(CTF)
        assert tx["data"] == "0x1234"
        safe_call.assert_not_called()
    else:
        assert tx["to"] == FUNDER and tx["data"] == "0x5678"
        assert safe_call.call_args.args[:3] == (
            Web3.to_checksum_address(CTF),
            0,
            bytes.fromhex("1234"),
        )
        assert safe_call.call_args.args[-1][-1] == 1
    assert prepared["operation"] == "APPROVE"


@pytest.mark.parametrize("estimate_error", [ValueError, Web3RPCError])
def test_insufficient_pol_during_gas_estimation_is_safe_and_never_signed(estimate_error):
    wallet = PolygonWallet.__new__(PolygonWallet)
    wallet.signature_type = 0
    wallet.funder = SIGNER
    wallet.signer = SimpleNamespace(address=SIGNER, sign_transaction=Mock())
    wallet.w3 = SimpleNamespace(
        eth=SimpleNamespace(
            get_transaction_count=Mock(return_value=7),
            gas_price=100,
            get_balance=Mock(return_value=1),
            estimate_gas=Mock(side_effect=estimate_error("insufficient funds: private-rpc-marker")),
        )
    )
    with pytest.raises(RedemptionCheckError, match="POL") as raised:
        wallet._sign_contract_call(CTF, "0x1234", "APPROVE")
    assert raised.value.code == "INSUFFICIENT_POL"
    assert "private-rpc-marker" not in str(raised.value)
    wallet.signer.sign_transaction.assert_not_called()


def test_zero_pol_is_checked_before_estimation_or_signing():
    wallet = PolygonWallet.__new__(PolygonWallet)
    wallet.signature_type = 0
    wallet.funder = SIGNER
    wallet.signer = SimpleNamespace(address=SIGNER, sign_transaction=Mock())
    wallet.w3 = SimpleNamespace(
        eth=SimpleNamespace(
            get_transaction_count=Mock(return_value=7),
            gas_price=100,
            get_balance=Mock(return_value=0),
            estimate_gas=Mock(),
        )
    )
    with pytest.raises(RedemptionCheckError, match="POL"):
        wallet._sign_contract_call(CTF, "0x1234", "APPROVE")
    wallet.w3.eth.estimate_gas.assert_not_called()
    wallet.signer.sign_transaction.assert_not_called()


@pytest.mark.parametrize("error_type", [ValueError, Web3RPCError])
@pytest.mark.parametrize("message", ["already known", "known transaction"])
def test_pinned_web3_known_transaction_is_an_idempotent_broadcast(error_type, message):
    wallet = PolygonWallet.__new__(PolygonWallet)
    wallet.w3 = SimpleNamespace(
        eth=SimpleNamespace(send_raw_transaction=Mock(side_effect=error_type(message)))
    )
    wallet.broadcast({"raw_tx": "0x0102", "tx_hash": "0x" + "00" * 32})
    wallet.w3.eth.send_raw_transaction.assert_called_once_with("0x0102")


def test_unknown_transaction_is_not_mistaken_for_known_transaction():
    wallet = PolygonWallet.__new__(PolygonWallet)
    wallet.w3 = SimpleNamespace(
        eth=SimpleNamespace(
            send_raw_transaction=Mock(side_effect=Web3RPCError("unknown transaction"))
        )
    )
    with pytest.raises(Web3RPCError):
        wallet.broadcast({"raw_tx": "0x0102", "tx_hash": "0x" + "00" * 32})


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (Web3RPCError("insufficient funds: private-rpc-marker"), "INSUFFICIENT_POL"),
        (Web3RPCError("execution reverted: private-rpc-marker"), "TRANSACTION_REVERTED"),
        (
            Web3RPCError("execution reverted: insufficient funds in contract"),
            "TRANSACTION_REVERTED",
        ),
        (TimeoutError("private-rpc-marker"), "PREPARATION_FAILED"),
    ],
)
def test_preparation_failure_is_durable_safe_and_retryable_without_pending(tmp_path, error, code):
    path = ledger(tmp_path)
    wallet = ControlledWallet()
    prepare = wallet.prepare_approval
    wallet.prepare_approval = Mock(side_effect=error)
    failed = RedemptionApprovalService(wallet, path).start()
    assert failed["status"] == "FAILED" and failed["pendingTxHash"] is None
    assert failed["errorCode"] == code and failed["stage"] == "prepare"
    assert failed["networkError"] is False
    assert not wallet.prepared and not wallet.broadcasts
    record = saved(path)
    assert record["status"] == "FAILED" and record["pending"] is None
    assert "private-rpc-marker" not in json.dumps(record)
    resumed = RedemptionApprovalService(wallet, path)
    assert resumed.status()["errorCode"] == code
    wallet.prepare_approval = prepare
    pending = resumed.start()
    assert pending["status"] == "PENDING" and pending["errorCode"] is None
    assert len(wallet.prepared) == 1 and len(wallet.broadcasts) == 1


@pytest.mark.parametrize(
    ("error", "code", "network_error"),
    [
        (Web3RPCError("transaction underpriced: private-rpc-marker"), "TX_FEE_REJECTED", False),
        (ValueError("nonce too low: private-rpc-marker"), "TX_NONCE_REJECTED", False),
        (Web3RPCError("insufficient funds: private-rpc-marker"), "INSUFFICIENT_POL", False),
        (Web3RPCError("invalid sender: private-rpc-marker"), "RPC_REJECTED", False),
        (TimeoutError("private-rpc-marker"), "RPC_RESULT_UNKNOWN", True),
    ],
)
def test_rejected_or_unknown_submission_preserves_exact_pending_and_nonce(
    tmp_path, error, code, network_error
):
    path = ledger(tmp_path)
    wallet = ControlledWallet()
    wallet.broadcast = Mock(side_effect=error)
    first = RedemptionApprovalService(wallet, path).start()
    assert first["status"] == "PENDING" and first["errorCode"] == code
    assert first["networkError"] is network_error and first["stage"] == "broadcast"
    original = saved(path)["pending"]
    assert "private-rpc-marker" not in json.dumps(saved(path))
    restarted = RedemptionApprovalService(wallet, path)
    assert restarted.status()["errorCode"] == code
    second = restarted.start()
    assert second["pendingTxHash"] == first["pendingTxHash"]
    assert saved(path)["pending"] == original and wallet.prepared == [False]
    assert wallet.broadcast.call_args_list[0].args[0] == wallet.broadcast.call_args_list[1].args[0]
    wallet.broadcast.side_effect = None
    recovered = restarted.start()
    assert recovered["errorCode"] is None and recovered["networkError"] is False
    assert saved(path)["pending"] == original and wallet.prepared == [False]


def test_receipt_rpc_failure_retains_pending_and_durable_safe_diagnostic(tmp_path):
    path = ledger(tmp_path)
    wallet = ControlledWallet()
    service = RedemptionApprovalService(wallet, path)
    service.start()
    original = saved(path)["pending"]
    wallet.receipt = Mock(side_effect=TimeoutError("private-rpc-marker"))
    result = service.advance()
    assert result["errorCode"] == "RPC_RECEIPT_UNAVAILABLE" and result["stage"] == "receipt"
    assert result["networkError"] is True and saved(path)["pending"] == original
    assert wallet.prepared == [False] and len(wallet.broadcasts) == 1
    assert "private-rpc-marker" not in json.dumps(saved(path))


def test_status_uses_fixed_message_instead_of_stored_free_form_text(tmp_path):
    path = ledger(tmp_path)
    wallet = ControlledWallet()
    service = RedemptionApprovalService(wallet, path)
    service.start()
    record = saved(path)
    record.update(errorCode="TX_FEE_REJECTED", stage="broadcast", errorMessage="private-rpc-marker")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE meta SET value=? WHERE key=?", (json.dumps(record), APPROVAL_KEY))
    result = service.status()
    assert result["errorCode"] == "TX_FEE_REJECTED"
    assert "private-rpc-marker" not in json.dumps(result)


def test_legacy_active_without_pending_can_record_pol_failure_and_retry(tmp_path):
    path = ledger(tmp_path)
    wallet = ControlledWallet()
    service = RedemptionApprovalService(wallet, path)
    legacy = {
        "version": 1,
        "status": "ACTIVE",
        "identity": service._identity(),
        "remaining": [False, True],
        "pending": None,
    }
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO meta VALUES (?,?)", (APPROVAL_KEY, json.dumps(legacy)))
        generation = db.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0]
        preferences = db.execute("SELECT value FROM meta WHERE key='preferences'").fetchone()[0]
    prepare = wallet.prepare_approval
    wallet.prepare_approval = Mock(
        side_effect=RedemptionCheckError("POL不足", code="INSUFFICIENT_POL")
    )
    failed = service.start()
    assert failed["status"] == "FAILED" and failed["errorCode"] == "INSUFFICIENT_POL"
    assert saved(path)["pending"] is None and not wallet.broadcasts
    wallet.prepare_approval = prepare
    assert RedemptionApprovalService(wallet, path).start()["status"] == "PENDING"
    assert wallet.prepared == [False] and len(wallet.broadcasts) == 1
    with sqlite3.connect(path) as db:
        assert (
            db.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0] == generation
        )
        assert (
            db.execute("SELECT value FROM meta WHERE key='preferences'").fetchone()[0]
            == preferences
        )
        assert db.execute("SELECT count(*) FROM native_events").fetchone()[0] == 0


def test_safe_ownership_requires_exactly_one_owner():
    wallet = PolygonWallet.__new__(PolygonWallet)
    wallet.funder = FUNDER
    wallet.signature_type = 2
    wallet.signer = SimpleNamespace(address=SIGNER)
    owners = Mock(return_value=SimpleNamespace(call=Mock(return_value=[SIGNER, FUNDER])))
    threshold = Mock(return_value=SimpleNamespace(call=Mock(return_value=1)))
    safe = SimpleNamespace(functions=SimpleNamespace(getOwners=owners, getThreshold=threshold))
    eth = SimpleNamespace(
        chain_id=137,
        get_code=Mock(side_effect=lambda address: b"" if address == SIGNER else b"\x01"),
        contract=Mock(return_value=safe),
    )
    wallet.w3 = SimpleNamespace(eth=eth)
    with pytest.raises(RedemptionCheckError, match="单签"):
        wallet.preflight()


def test_eoa_preflight_reads_independent_code_in_parallel_without_reusing_old_facts():
    wallet = PolygonWallet.__new__(PolygonWallet)
    wallet.funder = SIGNER
    wallet.signature_type = 0
    wallet.signer = SimpleNamespace(address=SIGNER)
    code_barrier = Barrier(2)
    first_two = {Web3.to_checksum_address(CTF), Web3.to_checksum_address(PUSD)}

    def get_code(address):
        if address in first_two:
            code_barrier.wait(timeout=3)
        return b"" if address == SIGNER else b"\x01"

    eth = SimpleNamespace(chain_id=137, get_code=Mock(side_effect=get_code))
    wallet.w3 = SimpleNamespace(eth=eth)
    wallet.preflight()
    addresses = [call.args[0] for call in eth.get_code.call_args_list]
    assert len(addresses) == 5  # Four contracts plus the shared EOA signer/funder.
    assert addresses.count(SIGNER) == 1
    eth.chain_id = 1
    with pytest.raises(RedemptionCheckError, match="Polygon主网"):
        wallet.preflight()
    assert eth.get_code.call_count == 5
    eth.chain_id = 137
    eth.get_code.side_effect = lambda address: b"" if address in {SIGNER, CTF} else b"\x01"
    with pytest.raises(RedemptionCheckError, match="官方合约地址"):
        wallet.preflight()


def test_safe_preflight_reads_owner_and_threshold_in_parallel():
    wallet = PolygonWallet.__new__(PolygonWallet)
    wallet.funder = FUNDER
    wallet.signature_type = 2
    wallet.signer = SimpleNamespace(address=SIGNER)
    authority_barrier = Barrier(2)

    def authority(value):
        authority_barrier.wait(timeout=3)
        return value

    safe = SimpleNamespace(
        functions=SimpleNamespace(
            getOwners=lambda: SimpleNamespace(call=lambda: authority([SIGNER])),
            getThreshold=lambda: SimpleNamespace(call=lambda: authority(1)),
        )
    )
    eth = SimpleNamespace(
        chain_id=137,
        get_code=Mock(side_effect=lambda address: b"" if address == SIGNER else b"\x01"),
        contract=Mock(return_value=safe),
    )
    wallet.w3 = SimpleNamespace(eth=eth)
    wallet.preflight()
    assert eth.get_code.call_count == 6


def test_approval_and_outcome_balances_are_fresh_parallel_reads_that_fail_closed():
    wallet = PolygonWallet.__new__(PolygonWallet)
    wallet.funder = FUNDER
    wallet.w3 = SimpleNamespace(eth=SimpleNamespace(block_number=123))
    wallet.preflight = Mock()
    approval_barrier = Barrier(4)
    balance_barrier = Barrier(2)
    approval_values = {
        Web3.to_checksum_address(address): True for address in (*ADAPTERS.values(), *EXCHANGES)
    }

    def approval(operator):
        approval_barrier.wait(timeout=3)
        return approval_values[operator]

    balances = {1: 5_000_000, 2: 0}

    def balance(token_id):
        balance_barrier.wait(timeout=3)
        return balances[token_id]

    wallet.ctf = SimpleNamespace(
        functions=SimpleNamespace(
            isApprovedForAll=lambda _funder, operator: SimpleNamespace(
                call=lambda: approval(operator)
            ),
            balanceOf=lambda _funder, token_id: SimpleNamespace(
                call=lambda *, block_identifier: balance(token_id)
                if block_identifier == 123
                else pytest.fail("balance read used a different block")
            ),
        )
    )
    wallet.buy_preflight()
    assert wallet.token_balances(("1", "2")) == {"1": 5_000_000, "2": 0}
    approval_values[Web3.to_checksum_address(ADAPTERS[True])] = False
    with pytest.raises(RedemptionCheckError, match="授权尚未确认"):
        wallet.buy_preflight()
    approval_values[Web3.to_checksum_address(ADAPTERS[True])] = True
    approval_values.pop(Web3.to_checksum_address(ADAPTERS[False]))
    with pytest.raises(KeyError):
        wallet.buy_preflight()
    balances.pop(2)
    with pytest.raises(KeyError):
        wallet.token_balances(("1", "2"))


@pytest.mark.parametrize("missing_exchange", EXCHANGES)
def test_buy_preflight_rejects_missing_exchange_sell_approval_in_one_parallel_batch(
    missing_exchange,
):
    wallet = PolygonWallet.__new__(PolygonWallet)
    wallet.funder = FUNDER
    wallet.preflight = Mock()
    operators = []
    wallet.ctf = SimpleNamespace(
        functions=SimpleNamespace(
            isApprovedForAll=lambda funder, operator: SimpleNamespace(
                call=lambda: operator.lower() != missing_exchange.lower()
            )
        )
    )

    def batch(calls):
        operators.append(len(calls))
        return [call() for call in calls]

    wallet._parallel_reads = batch
    with pytest.raises(RedemptionCheckError, match="交易所卖出份额授权"):
        wallet.buy_preflight()
    wallet.preflight.assert_called_once()
    assert operators == [4]
