"""Stateless LIVE wallet ledger checks and the encrypted-backup upload reader.

Moved unchanged from ``app.create_app``. Nothing here holds process state;
callers pass the ledger store, data root or upload stream explicitly.
"""

import json
import os
import re
import sqlite3
from contextlib import closing


def ledger_has_activity(store, *, cancellable_metadata=None):
    for table in ("intents", "native_events"):
        if store.db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
            return True
    business = store.get("business")
    approval = store.get("redemption_approval")
    bound = store.get("live_wallet_identity")
    cancellable = (
        isinstance(bound, dict)
        and bound == cancellable_metadata
        and type(bound.get("signature_type")) is int
        and unsigned_approval_preparation(approval, cancellable_metadata)
    )
    return any(business.get(key) for key in ("cycles", "claims", "settled")) or (
        approval is not None
        and (
            not isinstance(approval, dict)
            or (approval.get("status") == "ACTIVE" and not cancellable)
            or approval.get("pending") is not None
        )
    )


def approval_identity_matches(record, metadata):
    return (
        isinstance(record, dict)
        and type(record.get("version")) is int
        and record["version"] == 1
        and record.get("identity")
        == {
            "signer": metadata["signer"],
            "funder": metadata["funder"],
            "signatureType": metadata["signature_type"],
        }
        and type(record["identity"].get("signatureType")) is int
        and isinstance(record.get("remaining"), list)
        and bool(record["remaining"])
        and all(type(value) is bool for value in record["remaining"])
    )


def unsigned_approval_failure(record, metadata):
    from .redemption import approval_error_public

    if (
        not approval_identity_matches(record, metadata)
        or record.get("status") != "FAILED"
        or "pending" not in record
        or record["pending"] is not None
        or record.get("stage") != "prepare"
        or not isinstance(record.get("errorCode"), str)
        or record.get("errorCode")
        not in {
            "INSUFFICIENT_POL",
            "PREPARATION_FAILED",
            "TRANSACTION_REVERTED",
            "APPROVAL_CHECK_FAILED",
        }
        or ("networkError" in record and record["networkError"] is not False)
    ):
        return None
    return approval_error_public(record)


def unsigned_approval_preparation(record, metadata):
    # Only the canonical record saved before creating a transaction can be
    # cancelled by the user's explicit password-confirmed empty-wallet removal.
    return (
        isinstance(metadata, dict)
        and approval_identity_matches(record, metadata)
        and set(record) == {"version", "status", "identity", "remaining", "pending"}
        and record["status"] == "ACTIVE"
        and record["pending"] is None
        and record["remaining"] in ([False], [True], [False, True])
    )


def saved_approval_diagnostic(root, metadata):
    from web3 import Web3

    from .redemption import ADAPTERS, APPROVAL_KEY, approval_error_public

    try:
        path = root / "LIVE" / "state.sqlite"
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (APPROVAL_KEY,)).fetchone()
        record = json.loads(row[0]) if row else None
        if not approval_identity_matches(record, metadata):
            return None
        if record.get("status") == "FAILED":
            error = unsigned_approval_failure(record, metadata)
            return None if error is None else {"status": "UNKNOWN", "networkError": False, **error}
        if record.get("status") != "ACTIVE":
            return None
        pending = record.get("pending")
        if (
            not isinstance(pending, dict)
            or pending.get("operation") != "APPROVE"
            or type(pending.get("neg_risk")) is not bool
            or pending["neg_risk"] != record["remaining"][0]
            or type(pending.get("nonce")) is not int
            or pending["nonce"] < 0
            or not isinstance(pending.get("operator"), str)
            or pending["operator"].lower() != ADAPTERS[pending["neg_risk"]].lower()
            or not isinstance(pending.get("raw_tx"), str)
            or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})+", pending["raw_tx"])
            or not isinstance(pending.get("tx_hash"), str)
            or not re.fullmatch(r"0x[0-9a-fA-F]{64}", pending["tx_hash"])
            or Web3.to_hex(Web3.keccak(hexstr=pending["raw_tx"])).lower()
            != pending["tx_hash"].lower()
        ):
            return None
        error = approval_error_public(record)
        return {
            "status": "PENDING",
            "standardApproved": False,
            "negRiskApproved": False,
            "pendingTxHash": pending["tx_hash"],
            "networkError": error["errorCode"] in {"RPC_RESULT_UNKNOWN", "RPC_RECEIPT_UNAVAILABLE"},
            **error,
        }
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return None


async def read_restore_upload(stream, temporary, max_bundle):
    """Read ``u16 password length | UTF-8 password | bundle`` into ``temporary``.

    Returns the password once the whole upload is durably written. The caller
    owns the temporary file and removes it whatever happens.
    """
    header = bytearray()
    password_size = None
    password_input = None
    uploaded = 0
    async for chunk in stream:
        remaining = memoryview(chunk)
        while remaining:
            if password_size is None:
                taken = min(2 - len(header), len(remaining))
                header.extend(remaining[:taken])
                remaining = remaining[taken:]
                if len(header) < 2:
                    continue
                password_size = int.from_bytes(header, "big")
                header.clear()
                if not 12 <= password_size <= 16_384:
                    raise ValueError("备份密码格式无效")
            if password_input is None:
                taken = min(password_size - len(header), len(remaining))
                header.extend(remaining[:taken])
                remaining = remaining[taken:]
                if len(header) < password_size:
                    continue
                try:
                    password_input = header.decode("utf-8")
                except UnicodeError:
                    raise ValueError("备份密码格式无效") from None
                header.clear()
            if remaining:
                uploaded += len(remaining)
                if uploaded > max_bundle:
                    raise ValueError("备份文件超过网页恢复大小限制")
                temporary.write(remaining)
                break
    if password_input is None or uploaded == 0:
        raise ValueError("备份输入格式无效")
    temporary.flush()
    os.fsync(temporary.fileno())
    return password_input
