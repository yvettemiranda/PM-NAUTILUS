"""Startup custody checks exact current rights without claiming manual assets."""

import asyncio
from decimal import Decimal
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from nautilus_trader.model.identifiers import AccountId

from pm_nautilus.live import LiveExecution
from pm_nautilus.native import instrument_id


def client_with_rights(rights, balances):
    tokens = {
        tid: SimpleNamespace(token_id=tid, condition_id="0x" + f"{int(tid):064x}")
        for tid in {tid for tid, _ in rights}
    }
    cycles = {
        f"event-{n}": {"token_id": tid, "quantity": held} for n, (tid, held) in enumerate(rights)
    }
    client = SimpleNamespace(
        owner=SimpleNamespace(tokens=tokens, business={"cycles": cycles, "settled": []}),
        wallet=SimpleNamespace(token_balances=Mock(return_value=balances)),
        account_id=AccountId("POLYMARKET-001"),
        _clock=SimpleNamespace(timestamp_ns=lambda: 123),
        _fetch_user_positions=AsyncMock(side_effect=AssertionError("Data API forbidden")),
    )
    return client


@pytest.mark.parametrize("remainder", [25, 34, 81, 28, 75])
@pytest.mark.parametrize("external_shares", [0, 5_000_000])
def test_six_digit_custody_is_valid_and_external_shares_are_not_reported(
    remainder, external_shares
):
    client = client_with_rights(
        [("1", 10_000_000 + remainder)], {"1": 10_000_000 + remainder + external_shares}
    )
    reports = asyncio.run(LiveExecution.generate_position_status_reports(client, None))
    assert len(reports) == 1
    assert reports[0].quantity.as_decimal() == Decimal(10_000_000 + remainder) / 1_000_000
    client.wallet.token_balances.assert_called_once_with(("1",))
    client._fetch_user_positions.assert_not_awaited()


def test_duplicate_cycles_require_total_custody_and_report_one_total_position():
    client = client_with_rights([("1", 4_000_025), ("1", 6_000_025)], {"1": 6_000_025})
    with pytest.raises(ValueError, match="实际份额不足"):
        asyncio.run(LiveExecution.generate_position_status_reports(client, None))
    client.wallet.token_balances.return_value = {"1": 10_000_050}
    reports = asyncio.run(LiveExecution.generate_position_status_reports(client, None))
    assert len(reports) == 1
    assert reports[0].quantity.as_decimal() == Decimal("10.000050")
    assert client.wallet.token_balances.call_count == 2


def test_instrument_filter_cannot_skip_other_owned_custody():
    client = client_with_rights([("1", 5_000_025), ("2", 5_000_034)], {"1": 5_000_025, "2": 0})
    wanted = SimpleNamespace(instrument_id=instrument_id(client.owner.tokens["1"]))
    with pytest.raises(ValueError, match="实际份额不足"):
        asyncio.run(LiveExecution.generate_position_status_reports(client, wanted))
    client.wallet.token_balances.assert_called_once_with(("1", "2"))


@pytest.mark.parametrize(
    "actual",
    [
        {},
        {"1": True},
        {"1": "5000025"},
        {"1": 5_000_025.0},
        {"1": -1},
        {"1": 2**256},
        {"1": 5_000_025, "2": 1},
        None,
    ],
)
def test_ambiguous_or_tampered_custody_is_rejected(actual):
    client = client_with_rights([("1", 5_000_025)], actual)
    with pytest.raises(ValueError, match="查询结果无效"):
        asyncio.run(LiveExecution.generate_position_status_reports(client, None))


def test_custody_rpc_failure_is_not_treated_as_zero_or_a_successful_check():
    client = client_with_rights([("1", 5_000_025)], {"1": 5_000_025})
    client.wallet.token_balances.side_effect = TimeoutError("controlled failure")
    with pytest.raises(TimeoutError):
        asyncio.run(LiveExecution.generate_position_status_reports(client, None))


def test_only_positive_unsettled_tokens_are_read_and_empty_wallet_needs_no_rpc():
    client = client_with_rights([("1", 0), ("2", 5_000_034), ("3", 5_000_081)], {"2": 5_000_034})
    client.owner.business["settled"].append(client.owner.tokens["3"].condition_id)
    reports = asyncio.run(LiveExecution.generate_position_status_reports(client, None))
    assert len(reports) == 1
    client.wallet.token_balances.assert_called_once_with(("2",))
    client.owner.business["settled"].append(client.owner.tokens["2"].condition_id)
    client.wallet = None
    assert asyncio.run(LiveExecution.generate_position_status_reports(client, None)) == []
    client._fetch_user_positions.assert_not_awaited()


@pytest.mark.parametrize(
    "change", ["increase_covered", "increase_insufficient", "new_token", "settled"]
)
def test_fills_during_rpc_are_checked_against_current_complete_rights(change):
    async def run():
        client = client_with_rights([("1", 5_000_025)], {"1": 5_000_025})
        started, release = Event(), Event()

        def balances(ids):
            assert ids == ("1",)
            started.set()
            assert release.wait(3)
            return {"1": 6_000_034 if change == "increase_covered" else 5_000_025}

        client.wallet.token_balances.side_effect = balances
        task = asyncio.create_task(LiveExecution.generate_position_status_reports(client, None))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            if change.startswith("increase_"):
                client.owner.business["cycles"]["event-0"]["quantity"] = 6_000_034
            elif change == "new_token":
                client.owner.tokens["2"] = SimpleNamespace(
                    token_id="2", condition_id="0x" + "2" * 64
                )
                client.owner.business["cycles"]["new"] = {"token_id": "2", "quantity": 1}
            else:
                client.owner.business["settled"].append(client.owner.tokens["1"].condition_id)
            release.set()
            if change in {"increase_insufficient", "new_token"}:
                with pytest.raises(ValueError, match="实际份额不足"):
                    await task
            else:
                reports = await task
                if change == "settled":
                    assert reports == []
                else:
                    assert reports[0].quantity.as_decimal() == Decimal("6.000034")
            client.wallet.token_balances.assert_called_once_with(("1",))
        finally:
            release.set()
            if not task.done():
                await task

    asyncio.run(run())
