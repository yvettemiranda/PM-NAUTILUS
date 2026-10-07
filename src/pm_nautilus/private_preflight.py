"""Authenticated, read-only LIVE preflight. Never starts the trading runtime."""

import argparse
import asyncio
import json
import os
import re
import sqlite3
from pathlib import Path

import httpx
from eth_account import Account
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import ApiCreds, AssetType, BalanceAllowanceParams
from web3 import Web3

from .config import Preferences
from .live import load_settings
from .live_account_guard import audit_open_orders
from .preflight import EXCHANGES, PublicRPC, inspect
from .redemption import ADAPTERS, APPROVAL_KEY


def ledger_intents(data_dir):
    """Read the active LIVE journal without creating or changing a database."""
    path = Path(data_dir) / "LIVE" / "state.sqlite"
    if not path.is_file():
        raise ValueError("LIVE ledger is missing")
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("LIVE ledger failed integrity check")
        rows = db.execute("SELECT order_id,payload FROM intents").fetchall()
        intents = {}
        for order_id, payload in rows:
            row = json.loads(payload)
            if not isinstance(row, dict):
                raise ValueError("LIVE intent is invalid")
            if row.get("terminal") is False:
                intents[order_id] = row
        approval_row = db.execute("SELECT value FROM meta WHERE key=?", (APPROVAL_KEY,)).fetchone()
        approval = json.loads(approval_row[0]) if approval_row else None
        if approval is not None and (
            not isinstance(approval, dict)
            or approval.get("status") not in {"ACTIVE", "COMPLETE", "FAILED"}
        ):
            raise ValueError("LIVE redemption approval state is invalid")
        preferences_row = db.execute("SELECT value FROM meta WHERE key='preferences'").fetchone()
        if preferences_row is None:
            raise ValueError("LIVE preferences are missing")
        budget = Preferences(**json.loads(preferences_row[0])).budget
        return intents, bool(rows), approval, budget


def _nonnegative_int(value):
    if isinstance(value, bool):
        raise ValueError("Invalid amount")
    amount = int(value)
    if amount < 0 or str(amount) != str(value):
        raise ValueError("Invalid amount")
    return amount


def collateral_summary(raw):
    """Normalize the pinned CLOB balance response without copying API data."""
    if not isinstance(raw, dict):
        raise ValueError("Invalid collateral response")
    balance = _nonnegative_int(raw["balance"])
    allowances = raw.get("allowances")
    if not isinstance(allowances, dict):
        raise ValueError("Collateral allowances are missing")
    parsed = {}
    for address, value in allowances.items():
        if not isinstance(address, str) or not Web3.is_address(address):
            raise ValueError("Invalid collateral allowance address")
        normalized = address.lower()
        if normalized in parsed:
            raise ValueError("Duplicate collateral allowance address")
        parsed[normalized] = _nonnegative_int(value)
    return {"balanceMicros": balance, "allowancesMicros": parsed}


def run_preflight(settings, expected_signer, expected_funder, data_dir):
    signer = Account.from_key(settings["private_key"]).address
    funder = Web3.to_checksum_address(settings["funder"])
    if signer.lower() != Web3.to_checksum_address(expected_signer).lower():
        raise ValueError("Signing key does not match expected address")
    if funder.lower() != Web3.to_checksum_address(expected_funder).lower():
        raise ValueError("Funder does not match expected address")
    intents, has_history, approval_state, budget = ledger_intents(data_dir)
    if approval_state is not None and (
        approval_state.get("version") != 1
        or approval_state.get("identity")
        != {"signer": signer, "funder": funder, "signatureType": settings["signature_type"]}
        or (
            approval_state.get("status") == "COMPLETE"
            and (approval_state.get("pending") is not None or approval_state.get("remaining") != [])
        )
    ):
        raise ValueError("Redemption approval record does not match wallet")
    with httpx.Client(timeout=20, follow_redirects=False) as transport:
        public = inspect(
            PublicRPC(transport, settings["rpc_url"]),
            signer,
            funder,
            settings["signature_type"],
        )
    clob = ClobClient(
        "https://clob.polymarket.com",
        chain_id=137,
        key=settings["private_key"],
        creds=ApiCreds(settings["api_key"], settings["api_secret"], settings["passphrase"]),
        signature_type=settings["signature_type"],
        funder=funder,
    )
    collateral = collateral_summary(
        clob.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    )
    open_orders = asyncio.run(audit_open_orders(clob, intents))
    if open_orders.error:
        raise ValueError(open_orders.error)
    funded = collateral["balanceMicros"] >= budget and public["pusdBalanceMicros"] >= budget
    gas_available = public["signerPOLWei"] > 0
    # The saved per-Event budget already includes the maximum cash trading fee.
    # Both exchange paths must be funded because the next eligible token may be
    # either standard or negative-risk. An unidentified positive allowance is
    # not sufficient evidence for either path.
    allowance = all(
        collateral["allowancesMicros"].get(exchange.lower(), 0) >= budget
        and public["exchangeAllowancesMicros"].get(exchange, 0) >= budget
        for exchange in EXCHANGES
    )
    approvals = public.get("ctfApprovals")
    if not isinstance(approvals, dict) or any(
        type(approvals.get(target)) is not bool for target in ADAPTERS.values()
    ):
        raise ValueError("CTF redemption approval inspection is incomplete")
    redemption_approved = all(approvals[target] for target in ADAPTERS.values())
    approval_settled = approval_state is None or approval_state["status"] == "COMPLETE"
    pending = approval_state.get("pending") if approval_state else None
    if approval_state is not None and approval_state["status"] == "FAILED":
        approval_status = "FAILED"
    elif approval_state is not None and approval_state["status"] == "ACTIVE" and pending:
        approval_status = "PENDING"
    elif redemption_approved and approval_settled:
        approval_status = "APPROVED"
    else:
        approval_status = "MISSING"
    pending_hash = pending.get("tx_hash") if isinstance(pending, dict) else None
    if pending_hash is not None and (
        not isinstance(pending_hash, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", pending_hash)
    ):
        raise ValueError("Redemption approval transaction identity is invalid")
    return {
        "scope": "PRIVATE_READ_ONLY_NO_ORDER_SIGNATURES_OR_WRITES",
        "signer": signer,
        "funder": funder,
        "signatureType": settings["signature_type"],
        "chainId": public["chainId"],
        "rpcBlock": public["block"],
        "ledgerHasHistory": has_history,
        "trackedVenueOrderCount": sum(bool(intent.get("venue_id")) for intent in intents.values()),
        "unknownOpenOrderCount": open_orders.unknown_count,
        "onchainCollateralMicros": public["pusdBalanceMicros"],
        "clobCollateralMicros": collateral["balanceMicros"],
        "nextOrderAmountMicros": budget,
        "collateralBalanceSufficient": funded,
        "signerPOLWei": public["signerPOLWei"],
        "signerGasAvailable": gas_available,
        "collateralAllowancePresent": allowance,
        "redemptionApprovalComplete": redemption_approved and approval_settled,
        "redemptionApproval": {
            "status": approval_status,
            "standardApproved": approvals[ADAPTERS[False]],
            "negRiskApproved": approvals[ADAPTERS[True]],
            "pendingTxHash": pending_hash,
            "networkError": False,
        },
        "canStartNewBuys": (
            open_orders.ok
            and funded
            and allowance
            and gas_available
            and redemption_approved
            and approval_settled
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-signer", required=True)
    parser.add_argument("--expected-funder", required=True)
    parser.add_argument("--data-dir", default=os.environ.get("PM_DATA_DIR", "runtime"))
    args = parser.parse_args()
    try:
        result = run_preflight(
            load_settings(require_live_enabled=False),
            args.expected_signer,
            args.expected_funder,
            args.data_dir,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "errorType": type(exc).__name__,
                    "message": "私有只读预检失败；未签署订单或发送交易。",
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(1) from None
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    if not result["canStartNewBuys"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
