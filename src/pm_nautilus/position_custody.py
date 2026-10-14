"""Exact, read-only CTF custody and proof of resolved, worthless external tokens.

Every fact is read at one Polygon block. Public position display quantities and
prices are never evidence of custody or a redemption entitlement.
"""

from collections.abc import Iterable
import re

from web3 import Web3

from .preflight import _read_parallel
from .redemption import CTF, PUSD


USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
COLLATERALS = (PUSD, USDC_E)
UINT256_MAX = (1 << 256) - 1
NULL_PARENT = bytes(32)


def token_id(value):
    """Require one canonical decimal uint256 token identity."""
    if (
        type(value) is not str
        or not re.fullmatch(r"(?:0|[1-9][0-9]*)", value)
        or len(value) > 78
        or int(value) > UINT256_MAX
    ):
        raise ValueError("Invalid CTF token identity")
    return value


def _uint(value):
    if type(value) is not int or not 0 <= value <= UINT256_MAX:
        raise ValueError("Invalid CTF integer response")
    return value


class PositionCustody:
    def __init__(self, rpc, funder):
        if not isinstance(funder, str) or not Web3.is_address(funder):
            raise ValueError("Invalid custody wallet")
        self.rpc = rpc
        self.funder = Web3.to_checksum_address(funder)
        self.block = None

    def _anchor(self):
        if self.block is not None:
            return
        if int(self.rpc.query("eth_chainId", []), 16) != 137:
            raise ValueError("Custody RPC is not Polygon")
        block = self.rpc.query("eth_blockNumber", [])
        if not isinstance(block, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", block):
            raise ValueError("Invalid custody block")
        self.rpc.block = block
        code = self.rpc.query("eth_getCode", [CTF, block])
        if not isinstance(code, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})+", code):
            raise ValueError("CTF contract is not deployed")
        self.block = block

    def balances(self, token_ids: Iterable[str]):
        if isinstance(token_ids, (str, bytes)):
            raise ValueError("Invalid custody token collection")
        ids = tuple(dict.fromkeys(token_id(value) for value in token_ids))
        if not ids:
            return {}
        self._anchor()
        amounts = _read_parallel(
            [
                lambda tid=tid: self.rpc.call(
                    CTF,
                    "balanceOf(address,uint256)",
                    (self.funder, int(tid)),
                    ("address", "uint256"),
                )
                for tid in ids
            ]
        )
        return dict(zip(ids, map(_uint, amounts), strict=True))

    def is_resolved_zero(self, row):
        """Ignore only a proven CTF binary loss, without burning or claiming it."""
        if not isinstance(row, dict):
            raise ValueError("Invalid external position metadata")
        if row.get("negative_risk") is not False:
            return False
        tid = token_id(row.get("token_id"))
        opposite = token_id(row.get("opposite_token_id"))
        if tid == opposite:
            raise ValueError("External outcome identities are duplicated")
        condition_raw, index = row.get("condition_id"), row.get("outcome_index")
        if (
            not isinstance(condition_raw, str)
            or not re.fullmatch(r"0x[0-9a-fA-F]{64}", condition_raw)
            or type(index) is not int
            or index not in (0, 1)
        ):
            raise ValueError("Incomplete external binary position metadata")
        condition = bytes.fromhex(condition_raw[2:])
        self._anchor()
        slots = _uint(
            self.rpc.call(CTF, "getOutcomeSlotCount(bytes32)", (condition,), ("bytes32",))
        )
        if slots != 2:
            return False
        collections = _read_parallel(
            [
                lambda outcome=outcome: self.rpc.call(
                    CTF,
                    "getCollectionId(bytes32,bytes32,uint256)",
                    (NULL_PARENT, condition, 1 << outcome),
                    ("bytes32", "bytes32", "uint256"),
                    ("bytes32",),
                )
                for outcome in (0, 1)
            ]
        )
        if any(not isinstance(value, bytes) or len(value) != 32 for value in collections):
            raise ValueError("Invalid CTF collection identity")
        positions = _read_parallel(
            [
                lambda collateral=collateral, collection=collection: self.rpc.call(
                    CTF,
                    "getPositionId(address,bytes32)",
                    (collateral, collection),
                    ("address", "bytes32"),
                )
                for collateral in COLLATERALS
                for collection in collections
            ]
        )
        positions = list(map(_uint, positions))
        matches = sum(
            pair[index] == int(tid) and pair[1 - index] == int(opposite)
            for pair in (positions[:2], positions[2:])
        )
        if matches != 1:
            return False
        payouts = _read_parallel(
            [
                lambda: self.rpc.call(
                    CTF, "payoutDenominator(bytes32)", (condition,), ("bytes32",)
                ),
                *[
                    lambda outcome=outcome: self.rpc.call(
                        CTF,
                        "payoutNumerators(bytes32,uint256)",
                        (condition, outcome),
                        ("bytes32", "uint256"),
                    )
                    for outcome in (0, 1)
                ],
            ]
        )
        denominator, *numerators = map(_uint, payouts)
        if denominator == 0:
            return False
        if sum(numerators) != denominator:
            raise ValueError("Invalid CTF resolution payouts")
        if numerators[index] != 0:
            return False
        # A positive opposite balance remains an obligation to reconcile. Never
        # use an API price or a rounded payout to dismiss that other entitlement.
        return self.balances((opposite,))[opposite] == 0
