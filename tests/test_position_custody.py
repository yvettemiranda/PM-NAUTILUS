"""Read-only custody proof fixtures; no wallet, signatures, broadcasts or HTTP."""

from copy import deepcopy

import pytest

from pm_nautilus.position_custody import (
    COLLATERALS,
    CTF,
    NULL_PARENT,
    PositionCustody,
    UINT256_MAX,
    token_id,
)
from test_live_readiness import Client, Response, check, page, statuses


FUNDER = "0x" + "22" * 20
CONDITION = "0x" + "ab" * 32


def position(**changes):
    return {
        "token_id": "123",
        "opposite_token_id": "456",
        "negative_risk": False,
        "outcome_index": 0,
        "condition_id": CONDITION,
        # Deliberately misleading API fields never prove payout or custody.
        "current_size": "999",
        "current_price": 999,
        "current_value": 999,
        "redeemable": False,
        **changes,
    }


class ReadRPC:
    def __init__(self, collateral=COLLATERALS[0]):
        self.block = "latest"
        self.chain = "0x89"
        self.slot_count = 2
        self.denominator = 1
        self.numerators = [0, 1]
        self.balance_map = {123: 1_000_025, 456: 0}
        self.collections = [bytes([1]) * 32, bytes([2]) * 32]
        self.collateral = collateral
        self.queries = []
        self.calls = []
        self.fail_signature = None

    def query(self, method, params):
        self.queries.append((method, params))
        if method == "eth_chainId":
            return self.chain
        if method == "eth_blockNumber":
            return "0x123"
        if method == "eth_getCode":
            assert params == [CTF, "0x123"]
            return "0x01"
        pytest.fail("Only chain ID, block and code reads are allowed")

    def call(self, address, signature, args=(), types=(), outputs=("uint256",)):
        self.calls.append((signature, args, self.block))
        assert address == CTF
        if signature == self.fail_signature:
            raise TimeoutError("private-rpc-details")
        if signature == "balanceOf(address,uint256)":
            assert args[0] == FUNDER
            return self.balance_map.get(args[1], 0)
        assert bytes.fromhex(CONDITION[2:]) in args or signature == "getPositionId(address,bytes32)"
        if signature == "getOutcomeSlotCount(bytes32)":
            return self.slot_count
        if signature == "getCollectionId(bytes32,bytes32,uint256)":
            assert args[0] == NULL_PARENT and args[2] in (1, 2)
            return self.collections[args[2] - 1]
        if signature == "getPositionId(address,bytes32)":
            index = self.collections.index(args[1])
            return ([123, 456] if args[0] == self.collateral else [789, 999])[index]
        if signature == "payoutDenominator(bytes32)":
            return self.denominator
        if signature == "payoutNumerators(bytes32,uint256)":
            return self.numerators[args[1]]
        pytest.fail("Only fixed CTF view functions are allowed")


@pytest.mark.parametrize("collateral", COLLATERALS)
def test_zero_loss_is_proved_for_supported_collateral_at_one_fresh_block(collateral):
    rpc = ReadRPC(collateral)
    reader = PositionCustody(rpc, FUNDER)
    assert reader.balances(["123", "123", "456"]) == {"123": 1_000_025, "456": 0}
    assert reader.is_resolved_zero(position()) is True
    assert [method for method, _ in rpc.queries] == [
        "eth_chainId",
        "eth_blockNumber",
        "eth_getCode",
    ]
    assert all(block == "0x123" for _, _, block in rpc.calls)
    # A different invocation reads a new block; nothing survives this reader.
    PositionCustody(ReadRPC(), FUNDER).balances(["123"])


def test_winner_with_zero_display_price_cannot_be_dismissed():
    rpc = ReadRPC()
    rpc.numerators = [1, 0]
    reader = PositionCustody(rpc, FUNDER)
    assert (
        reader.is_resolved_zero(position(current_price=0, current_value=0, redeemable=True))
        is False
    )


@pytest.mark.parametrize("collateral", COLLATERALS)
def test_either_binary_outcome_can_be_a_proven_loss(collateral):
    rpc = ReadRPC(collateral)
    rpc.numerators = [1, 0]
    rpc.balance_map = {123: 0, 456: 1_000_081}
    assert (
        PositionCustody(rpc, FUNDER).is_resolved_zero(
            position(token_id="456", opposite_token_id="123", outcome_index=1)
        )
        is True
    )


def test_unknown_collateral_cannot_use_a_supported_redemption_proof():
    assert PositionCustody(ReadRPC("0x" + "33" * 20), FUNDER).is_resolved_zero(position()) is False


def test_bare_string_is_not_a_set_of_custody_token_ids():
    with pytest.raises(ValueError, match="collection"):
        PositionCustody(ReadRPC(), FUNDER).balances("123")


def test_tiny_positive_payout_cannot_be_rounded_into_zero_entitlement():
    rpc = ReadRPC()
    rpc.denominator = UINT256_MAX
    rpc.numerators = [1, UINT256_MAX - 1]
    assert PositionCustody(rpc, FUNDER).is_resolved_zero(position()) is False


def test_opposite_positive_entitlement_still_blocks_external_loss_filter():
    rpc = ReadRPC()
    rpc.balance_map[456] = 1
    assert PositionCustody(rpc, FUNDER).is_resolved_zero(position()) is False


@pytest.mark.parametrize("change", [{"negative_risk": True}, {"negative_risk": None}])
def test_unsupported_or_unconfirmed_market_type_is_never_ignored(change):
    rpc = ReadRPC()
    assert PositionCustody(rpc, FUNDER).is_resolved_zero(position(**change)) is False
    assert not rpc.queries and not rpc.calls


@pytest.mark.parametrize("slots,denominator", [(3, 1), (0, 1), (2, 0)])
def test_nonbinary_or_unresolved_condition_remains_blocked(slots, denominator):
    rpc = ReadRPC()
    rpc.slot_count, rpc.denominator = slots, denominator
    assert PositionCustody(rpc, FUNDER).is_resolved_zero(position()) is False


@pytest.mark.parametrize("numerators", [[0, 0], [0, 2], [False, 1], [-1, 2]])
def test_invalid_resolution_is_not_proof(numerators):
    rpc = ReadRPC()
    rpc.numerators = numerators
    with pytest.raises(ValueError):
        PositionCustody(rpc, FUNDER).is_resolved_zero(position())


@pytest.mark.parametrize(
    "changes",
    [
        {"token_id": "999"},
        {"opposite_token_id": "789"},
        {"outcome_index": 1},
    ],
)
def test_token_pair_condition_and_index_must_match_chain_derivation(changes):
    assert PositionCustody(ReadRPC(), FUNDER).is_resolved_zero(position(**changes)) is False


@pytest.mark.parametrize(
    "changes",
    [
        {"opposite_token_id": None},
        {"opposite_token_id": "123"},
        {"condition_id": None},
        {"condition_id": "0x12"},
        {"outcome_index": True},
        {"outcome_index": "0"},
        {"outcome_index": 2},
    ],
)
def test_incomplete_external_identity_fails_closed(changes):
    with pytest.raises(ValueError):
        PositionCustody(ReadRPC(), FUNDER).is_resolved_zero(position(**changes))


@pytest.mark.parametrize("value", [None, True, 123, "01", "-1", "1e5", str(1 << 256)])
def test_token_identity_is_bounded_and_canonical(value):
    with pytest.raises(ValueError):
        token_id(value)
    assert token_id(str(UINT256_MAX)) == str(UINT256_MAX)


@pytest.mark.parametrize("amount", [True, -1, 1 << 256, "100", 1.0])
def test_custody_never_coerces_invalid_chain_quantity(amount):
    rpc = ReadRPC()
    rpc.balance_map[123] = amount
    with pytest.raises(ValueError):
        PositionCustody(rpc, FUNDER).balances(["123"])


def test_rpc_failure_is_unknown_without_using_api_zero_price():
    rpc = ReadRPC()
    rpc.fail_signature = "payoutDenominator(bytes32)"
    with pytest.raises(TimeoutError):
        PositionCustody(rpc, FUNDER).is_resolved_zero(position(current_price=0, redeemable=True))


def test_wrong_chain_or_incomplete_collection_never_creates_proof():
    rpc = ReadRPC()
    rpc.chain = "0x1"
    with pytest.raises(ValueError, match="Polygon"):
        PositionCustody(rpc, FUNDER).balances(["123"])
    rpc = ReadRPC()
    rpc.collections[0] = b"short"
    with pytest.raises(ValueError, match="collection"):
        PositionCustody(rpc, FUNDER).is_resolved_zero(position())


def test_position_metadata_is_not_mutated_and_contains_no_nonce_or_signature_path():
    row = position()
    original = deepcopy(row)
    assert PositionCustody(ReadRPC(), FUNDER).is_resolved_zero(row)
    assert row == original


def test_readiness_uses_full_chain_zero_proof_without_claiming_or_mutating_api_row():
    row = position(proxy_wallet=FUNDER)
    before = deepcopy(row)
    rpc = ReadRPC()
    result = check(
        Client(Response({"blocked": False}), Response(page([row]))),
        known_positions={},
        position_reader=PositionCustody(rpc, FUNDER),
    )
    assert result["ready"] is True
    assert statuses(result)["positions"] == "pass"
    assert row == before
    assert all(block == "0x123" for _, _, block in rpc.calls)
