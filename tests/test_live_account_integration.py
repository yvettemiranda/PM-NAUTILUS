"""Controlled LIVE order gates; no network, keys, or real orders."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pm_nautilus.live import LiveExecution
from pm_nautilus.strategy import Runtime


def test_unknown_open_order_pauses_new_buys_without_canceling_manual_order():
    values = {}
    owner = SimpleNamespace(
        store=SimpleNamespace(
            intents=lambda **kwargs: {},
            put=lambda key, value: values.update({key: value}),
        ),
        pause=Mock(),
    )
    clob = SimpleNamespace(get_open_orders=Mock(return_value=[{"id": "manual-order"}]))
    client = SimpleNamespace(owner=owner, _http_client=clob, open_orders_clear=True)
    assert not asyncio.run(LiveExecution._check_open_orders(client))
    assert client.open_orders_clear is False
    owner.pause.assert_called_once_with()
    assert values["live_error"] == "账户存在程序外开放挂单"
    clob.get_open_orders.assert_called_once_with()


def test_buy_requires_exact_chain_holdings_for_both_outcomes():
    values = {}
    yes = SimpleNamespace(token_id="1", condition_id="condition")
    no = SimpleNamespace(token_id="2", condition_id="condition")
    owner = SimpleNamespace(
        tokens={"1": yes, "2": no},
        business={"cycles": {"event": {"token_id": "1", "quantity": 5_000_000}}},
        store=SimpleNamespace(put=lambda key, value: values.update({key: value})),
        pause=Mock(),
    )
    wallet = SimpleNamespace(token_balances=Mock(return_value={"1": 5_000_000, "2": 0}))
    client = SimpleNamespace(
        owner=owner,
        wallet=wallet,
        _check_open_orders=AsyncMock(return_value=True),
    )
    assert asyncio.run(LiveExecution._check_buy_account(client, yes))
    wallet.token_balances.return_value = {"1": 5_000_000, "2": 1}
    assert not asyncio.run(LiveExecution._check_buy_account(client, yes))
    owner.pause.assert_called_once_with()
    assert values["live_error"] == "同Condition实际份额与程序持仓不一致"
    wallet.token_balances.side_effect = OSError("private RPC URL")
    assert not asyncio.run(LiveExecution._check_buy_account(client, yes))
    assert "private" not in str(values)


def test_live_start_requires_all_wallet_open_order_reconciliation():
    runtime = SimpleNamespace(
        mode="LIVE", client=SimpleNamespace(ready=True, open_orders_clear=False)
    )
    with pytest.raises(ValueError, match="开放挂单"):
        Runtime.start(runtime)
