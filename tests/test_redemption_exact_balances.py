"""Raw CTF reads use one fresh block and reject ambiguous RPC facts."""

from threading import Barrier, Lock
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock

import pytest

from pm_nautilus.redemption import PolygonWallet, RedemptionCheckError


def wallet_with_balances(values, *, block=123):
    wallet = PolygonWallet.__new__(PolygonWallet)
    wallet.funder = "0x" + "11" * 20
    wallet.w3 = SimpleNamespace(eth=SimpleNamespace(block_number=block))
    calls = Mock(side_effect=lambda tid, block: values[tid])
    wallet.ctf = SimpleNamespace(
        functions=SimpleNamespace(
            balanceOf=lambda funder, tid: SimpleNamespace(
                call=lambda *, block_identifier: calls(tid, block_identifier)
            )
        )
    )
    return wallet, calls


def test_balances_keep_micro_units_and_query_a_fresh_block_for_each_batch():
    wallet, calls = wallet_with_balances({1: 5_000_025, 2: 0})
    eth_type = type("ControlledEth", (), {})
    block = PropertyMock(side_effect=[123, 124])
    eth_type.block_number = block
    wallet.w3.eth = eth_type()
    assert wallet.token_balances(("1", "2")) == {"1": 5_000_025, "2": 0}
    assert set(call.args for call in calls.call_args_list) == {(1, 123), (2, 123)}
    calls.reset_mock()
    calls.side_effect = lambda tid, block: {1: 5_000_034, 2: 1}[tid]
    assert wallet.token_balances(("1", "2")) == {"1": 5_000_034, "2": 1}
    assert set(call.args for call in calls.call_args_list) == {(1, 124), (2, 124)}
    assert block.call_count == 2


@pytest.mark.parametrize(
    "ids",
    [
        None,
        1,
        "12",
        b"12",
        [1],
        [True],
        [None],
        ["1", "1"],
        ["01"],
        ["-1"],
        [" 1"],
        ["1.0"],
        [str(2**256)],
        ["1" * 1000],
    ],
)
def test_invalid_or_duplicate_token_ids_do_not_reach_rpc(ids):
    wallet = PolygonWallet.__new__(PolygonWallet)
    wallet.w3 = Mock()
    wallet.ctf = Mock()
    with pytest.raises(RedemptionCheckError, match="查询对象无效"):
        wallet.token_balances(ids)
    assert not wallet.w3.mock_calls
    assert not wallet.ctf.mock_calls


def test_empty_token_batch_needs_no_wallet_or_rpc():
    wallet = PolygonWallet.__new__(PolygonWallet)
    assert wallet.token_balances(()) == {}


def test_canonical_uint256_boundaries_are_preserved():
    maximum = 2**256 - 1
    wallet, calls = wallet_with_balances({0: 0, maximum: maximum})
    assert wallet.token_balances(("0", str(maximum))) == {"0": 0, str(maximum): maximum}
    assert calls.call_count == 2


@pytest.mark.parametrize("block", [None, True, "123", -1, 123.0, 2**256])
def test_invalid_fixed_block_does_not_read_balances(block):
    wallet, calls = wallet_with_balances({1: 1}, block=block)
    with pytest.raises(RedemptionCheckError, match="查询区块无效"):
        wallet.token_balances(("1",))
    calls.assert_not_called()


@pytest.mark.parametrize("value", [None, True, "5000025", 5_000_025.0, -1, 2**256])
def test_invalid_raw_balance_is_never_converted_or_tolerated(value):
    wallet, calls = wallet_with_balances({1: value, 2: 0})
    with pytest.raises(RedemptionCheckError, match="查询结果无效"):
        wallet.token_balances(("1", "2"))
    assert calls.call_count == 2


def test_one_unknown_balance_fails_the_complete_batch():
    wallet, calls = wallet_with_balances({1: 5_000_025})
    with pytest.raises(KeyError):
        wallet.token_balances(("1", "2"))
    assert calls.call_count == 2


def test_large_token_batch_uses_the_same_block_and_four_bounded_workers():
    wallet, calls = wallet_with_balances({})
    barrier = Barrier(4)
    lock = Lock()
    active = maximum = 0
    first_four = 0

    def balance(tid, block):
        nonlocal active, maximum, first_four
        assert block == 123
        with lock:
            active += 1
            maximum = max(maximum, active)
            first_four += 1
            synchronized = first_four <= 4
        try:
            if synchronized:
                barrier.wait(timeout=3)
            return tid + 25
        finally:
            with lock:
                active -= 1

    calls.side_effect = balance
    ids = tuple(str(n) for n in range(1, 13))
    assert wallet.token_balances(ids) == {tid: int(tid) + 25 for tid in ids}
    assert calls.call_count == 12
    assert maximum == 4
