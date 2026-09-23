"""Authenticated, read-only LIVE preflight. Never starts the trading runtime."""

import argparse
import asyncio
import json
import os
import sqlite3
from pathlib import Path

import httpx
from eth_account import Account
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import ApiCreds, AssetType, BalanceAllowanceParams
from web3 import Web3

from .live import load_settings
from .live_account_guard import audit_open_orders
from .preflight import PublicRPC, inspect


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
        return intents, bool(rows)


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
    allowances = raw.get("allowances", raw.get("allowance"))
    if isinstance(allowances, dict):
        values = allowances.values()
    elif allowances is not None:
        values = (allowances,)
    else:
        raise ValueError("Collateral allowance is missing")
    parsed = [_nonnegative_int(value) for value in values]
    return {"balanceMicros": balance, "anyPositiveAllowance": any(x > 0 for x in parsed)}


def run_preflight(settings, expected_signer, expected_funder, data_dir):
    signer = Account.from_key(settings["private_key"]).address
    funder = Web3.to_checksum_address(settings["funder"])
    if signer.lower() != Web3.to_checksum_address(expected_signer).lower():
        raise ValueError("Signing key does not match expected address")
    if funder.lower() != Web3.to_checksum_address(expected_funder).lower():
        raise ValueError("Funder does not match expected address")
    intents, has_history = ledger_intents(data_dir)
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
    funded = collateral["balanceMicros"] > 0 and public["pusdBalanceMicros"] > 0
    gas_available = public["signerPOLWei"] > 0
    allowance = collateral["anyPositiveAllowance"] and any(
        amount > 0 for amount in public["exchangeAllowancesMicros"].values()
    )
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
        "signerPOLWei": public["signerPOLWei"],
        "signerGasAvailable": gas_available,
        "collateralAllowancePresent": allowance,
        "canStartNewBuys": open_orders.ok and funded and allowance and gas_available,
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
