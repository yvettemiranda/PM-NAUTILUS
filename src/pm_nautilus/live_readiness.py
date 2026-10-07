"""Read-only checks for a proposed LIVE wallet, before enabling trading.

This module only calls the existing authenticated preflight and two official
public GET endpoints. A failed or incomplete check never means ``ready``.
"""

from decimal import Decimal, InvalidOperation
from collections.abc import Collection, Mapping
import re
from typing import Any

import httpx
from web3 import Web3

from .private_preflight import run_preflight


GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
POSITIONS_URL = "https://data-api.polymarket.com/v2/positions"


def _check(name: str, status: str, message: str) -> dict[str, str]:
    return {"id": name, "status": status, "message": message}


def _unknown_approval() -> dict[str, Any]:
    return {
        "status": "UNKNOWN",
        "standardApproved": None,
        "negRiskApproved": None,
        "pendingTxHash": None,
        "networkError": False,
    }


def _get_json(client: Any, url: str, *, params: dict | None = None) -> Any:
    response = client.get(url, params=params, headers={"Accept": "application/json"})
    # A redirect could send the request to a different host. Do not follow it
    # or treat a 3xx response as a successful check.
    if response.status_code != 200:
        raise ValueError("Unexpected HTTP status")
    return response.json()


def _private_check(settings, signer, funder, data_dir, preflight):
    try:
        result = preflight(settings, signer, funder, data_dir)
    except Exception:
        return _check(
            "account", "unknown", "账户检查失败，请核对钱包、网络与实盘记录。"
        ), _unknown_approval()
    if not isinstance(result, dict) or type(result.get("canStartNewBuys")) is not bool:
        return _check("account", "unknown", "账户检查结果不完整，请稍后重试。"), _unknown_approval()
    approval = result.get("redemptionApproval")
    if (
        not isinstance(approval, dict)
        or approval.get("status") not in {"MISSING", "PENDING", "FAILED", "APPROVED"}
        or type(approval.get("standardApproved")) is not bool
        or type(approval.get("negRiskApproved")) is not bool
        or (
            approval.get("pendingTxHash") is not None
            and (
                not isinstance(approval["pendingTxHash"], str)
                or not re.fullmatch(r"0x[0-9a-fA-F]{64}", approval["pendingTxHash"])
            )
        )
    ):
        return _check("account", "unknown", "赎回授权核对结果不完整。"), _unknown_approval()
    approval = {
        "status": approval["status"],
        "standardApproved": approval["standardApproved"],
        "negRiskApproved": approval["negRiskApproved"],
        "pendingTxHash": approval.get("pendingTxHash"),
        "networkError": False,
    }
    if not result["canStartNewBuys"]:
        if type(result.get("unknownOpenOrderCount")) is int and result["unknownOpenOrderCount"] > 0:
            return _check("account", "blocked", "账户存在本程序以外的挂单，请先核对。"), approval
        if result.get("redemptionApprovalComplete") is False:
            return _check("account", "blocked", "赎回适配器授权尚未确认。"), approval
        if result.get("collateralBalanceSufficient") is False:
            return _check("account", "blocked", "pUSD 余额不足以覆盖已保存的每轮金额。"), approval
        if result.get("collateralAllowancePresent") is False:
            return _check(
                "account", "blocked", "pUSD 交易授权额度不足以覆盖已保存的每轮金额。"
            ), approval
        if result.get("signerGasAvailable") is False:
            return _check("account", "blocked", "签名钱包的 Polygon POL 余额不足。"), approval
        return _check(
            "account", "blocked", "账户资金、授权或手续费余额尚未满足实盘要求。"
        ), approval
    if result.get("redemptionApprovalComplete") is not True:
        return _check("account", "unknown", "赎回授权核对结果不完整。"), _unknown_approval()
    if approval["status"] != "APPROVED" or not (
        approval["standardApproved"] and approval["negRiskApproved"]
    ):
        return _check("account", "unknown", "赎回授权核对结果不一致。"), _unknown_approval()
    return _check("account", "pass", "账户只读核对通过。"), approval


def _geoblock_check(client) -> dict[str, str]:
    try:
        result = _get_json(client, GEOBLOCK_URL)
        if not isinstance(result, dict) or type(result.get("blocked")) is not bool:
            raise ValueError("Invalid geoblock response")
    except Exception:
        return _check("region", "unknown", "无法确认服务器所在地是否允许交易，请稍后重试。")
    if result["blocked"]:
        return _check("region", "blocked", "服务器所在地不允许通过 API 下单。")
    return _check("region", "pass", "服务器所在地允许通过 API 下单。")


def _position_size(row: Any, funder: str) -> Decimal:
    if not isinstance(row, dict):
        raise ValueError("Invalid position")
    wallet = row.get("proxy_wallet")
    if (
        not isinstance(wallet, str)
        or not Web3.is_address(wallet)
        or wallet.lower() != funder.lower()
    ):
        raise ValueError("Position belongs to a different wallet")
    raw = row.get("current_size")
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ValueError("Invalid position size")
    try:
        size = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ValueError("Invalid position size") from exc
    if not size.is_finite() or size < 0:
        raise ValueError("Invalid position size")
    return size


def _position_micros(row: Any, funder: str) -> int:
    micros = _position_size(row, funder) * 1_000_000
    if micros != micros.to_integral_value():
        raise ValueError("Position has unsupported precision")
    return int(micros)


def _token_id(row: dict) -> str:
    token_id = row.get("token_id")
    if type(token_id) is not str or not re.fullmatch(r"[0-9]+", token_id):
        raise ValueError("Invalid position token")
    return token_id


def _positions_check(
    client,
    funder: str,
    known_token_ids: Collection[str] | None,
    known_positions: Mapping[str, int] | None,
) -> dict[str, str]:
    # The V2 OPEN set includes settled but unredeemed positions. The explicit
    # zero floor avoids the API's default 0.1-share dust filter. Follow the
    # cursor until the server certifies that the book is exhausted.
    params = {
        "user": funder,
        "status": "OPEN",
        "limit": 1000,
        "filter_type": "TOKENS",
        "filter_amount": 0,
        "include_archived": "true",
    }
    seen_cursors: set[str] = set()
    observed: dict[str, int] = {}
    try:
        if not Web3.is_address(funder):
            raise ValueError("Invalid wallet")
        if known_token_ids is None:
            known = frozenset()
        else:
            if isinstance(known_token_ids, (str, bytes)) or not isinstance(
                known_token_ids, Collection
            ):
                raise ValueError("Invalid owned token set")
            known = frozenset(known_token_ids)
            if any(
                type(token_id) is not str or not re.fullmatch(r"[0-9]+", token_id)
                for token_id in known
            ):
                raise ValueError("Invalid owned token ID")
        expected = None
        if known_positions is not None:
            if not isinstance(known_positions, Mapping):
                raise ValueError("Invalid owned positions")
            expected = dict(known_positions)
            if any(
                type(token_id) is not str
                or not re.fullmatch(r"[0-9]+", token_id)
                or type(quantity) is not int
                or quantity < 0
                for token_id, quantity in expected.items()
            ):
                raise ValueError("Invalid owned position quantity")
            if known_token_ids is not None and known != expected.keys():
                raise ValueError("Owned token inputs disagree")
        for _ in range(100):
            payload = _get_json(client, POSITIONS_URL, params=params)
            if not isinstance(payload, dict):
                raise ValueError("Invalid positions response")
            rows, page = payload.get("data"), payload.get("pagination")
            if not isinstance(rows, list) or not isinstance(page, dict):
                raise ValueError("Invalid positions page")
            if type(page.get("has_more")) is not bool:
                raise ValueError("Invalid positions pagination")
            for row in rows:
                amount = _position_micros(row, funder)
                if amount == 0:
                    continue
                token_id = _token_id(row)
                observed[token_id] = observed.get(token_id, 0) + amount
            if not page["has_more"]:
                if expected is not None:
                    positive_expected = {
                        token_id: amount for token_id, amount in expected.items() if amount > 0
                    }
                    if observed != positive_expected:
                        return _check(
                            "positions", "blocked", "实际持仓与程序记录不一致，需先核对。"
                        )
                    return _check("positions", "pass", "实际持仓与程序记录一致。")
                if observed and not known:
                    return _check("positions", "blocked", "钱包里已有持仓，需先核对归属。")
                if observed.keys() - known:
                    return _check("positions", "blocked", "钱包里有程序未记录的持仓，需先核对。")
                if known:
                    return _check("positions", "pass", "未发现程序记录以外的持仓。")
                return _check("positions", "pass", "未发现钱包原有持仓。")
            cursor = page.get("next_cursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                raise ValueError("Invalid positions cursor")
            seen_cursors.add(cursor)
            # Keep the same wallet anchor; the signed cursor carries the
            # original status, sort, and page size.
            params = {"user": funder, "cursor": cursor}
        raise ValueError("Position pagination exceeded safety bound")
    except Exception:
        return _check("positions", "unknown", "无法完整核对钱包原有持仓，请稍后重试。")


def run_readiness(
    settings,
    expected_signer: str,
    expected_funder: str,
    data_dir,
    *,
    private_check=None,
    http_client=None,
    known_token_ids: Collection[str] | None = None,
    known_positions: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Return a sanitized pre-LIVE checklist; never submit/sign an order.

    ``settings`` is an already validated LIVE settings dictionary. The public
    expected addresses must have been confirmed by the owner. Dependencies are
    injectable for offline tests. The caller must run this in the server's
    actual trading environment so the geoblock GET reflects that egress IP.
    ``known_token_ids`` may be supplied from the read-only LIVE business ledger
    during restart. Only tokens with those IDs can pass the public positions
    check; first activation defaults to blocking every pre-existing position.
    ``known_positions`` is stronger: expected 1e-6 share counts per token from
    the ledger must equal the full paginated public wallet inventory.
    """
    preflight = private_check if private_check is not None else run_preflight
    account, approval = _private_check(
        settings, expected_signer, expected_funder, data_dir, preflight
    )
    if http_client is None:
        # Direct outbound requests from this host, no environment proxy and no
        # redirect; the result must describe this server's real trade origin.
        with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
            region = _geoblock_check(client)
            positions = _positions_check(client, expected_funder, known_token_ids, known_positions)
    else:
        region = _geoblock_check(http_client)
        positions = _positions_check(http_client, expected_funder, known_token_ids, known_positions)
    checks = [account, region, positions]
    return {
        "scope": "READ_ONLY_NO_ORDER_SIGNATURES_OR_WRITES",
        "ready": all(item["status"] == "pass" for item in checks),
        "checks": checks,
        "redemptionApproval": approval,
    }
