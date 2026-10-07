"""Controlled authorization workflow; no real wallet, RPC, or transaction."""

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from web3 import Web3

from pm_nautilus.redemption import (
    ADAPTERS,
    APPROVAL_KEY,
    CTF,
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
