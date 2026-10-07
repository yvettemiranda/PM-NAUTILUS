"""The private preflight only reads the ledger, chain, and authenticated CLOB."""

import json

import pytest
from eth_account import Account

from pm_nautilus import private_preflight as check
from pm_nautilus.preflight import EXCHANGES
from pm_nautilus.redemption import ADAPTERS, APPROVAL_KEY
from pm_nautilus.store import Store

KEY = "0x" + "11" * 32  # Disposable test key, never a real account.
SIGNER = Account.from_key(KEY).address
FUNDER = "0x" + "22" * 20
SETTINGS = {
    "private_key": KEY,
    "api_key": "controlled-key",
    "api_secret": "controlled-secret",
    "passphrase": "controlled-passphrase",
    "funder": FUNDER,
    "signature_type": 2,
    "rpc_url": "https://example.invalid",
}
ALLOWANCES = {address: "1000000" for address in EXCHANGES}


def test_readonly_ledger_does_not_create_missing_database(tmp_path):
    with pytest.raises(ValueError, match="missing"):
        check.ledger_intents(tmp_path)
    assert not (tmp_path / "LIVE").exists()


def test_collateral_response_requires_balance_and_allowance():
    assert check.collateral_summary({"balance": "2000000", "allowances": ALLOWANCES}) == {
        "balanceMicros": 2_000_000,
        "allowancesMicros": {address.lower(): 1_000_000 for address in EXCHANGES},
    }
    for response in (
        {"balance": "1"},
        {"balance": "-1", "allowances": ALLOWANCES},
        {"balance": "1", "allowance": "1"},
        {"balance": "1", "allowances": {"exchange": "1"}},
        [],
    ):
        with pytest.raises((KeyError, ValueError)):
            check.collateral_summary(response)


@pytest.mark.parametrize("open_order,ready", [("own-venue", True), ("old-manual", False)])
def test_private_preflight_only_uses_read_calls_and_checks_full_wallet(
    tmp_path, monkeypatch, open_order, ready
):
    store = Store(tmp_path / "LIVE" / "state.sqlite", mode="LIVE")
    store.save_intent(
        "own-client",
        {
            "event_id": "event",
            "token_id": "1",
            "side": "BUY",
            "terminal": False,
            "venue_id": "own-venue",
        },
    )
    store.close()
    calls = []

    class ControlledClob:
        def __init__(self, *args, **kwargs):
            calls.append("client")

        def get_balance_allowance(self, params):
            calls.append("balance")
            return {"balance": "2000000", "allowances": ALLOWANCES}

        def get_open_orders(self):
            calls.append("open-orders")
            return [{"id": open_order}]

    def public(_rpc, signer, funder, signature_type):
        calls.append("public-rpc")
        assert (signer, funder, signature_type) == (SIGNER, FUNDER, 2)
        return {
            "chainId": 137,
            "block": 1,
            "pusdBalanceMicros": 2_000_000,
            "signerPOLWei": 1,
            "exchangeAllowancesMicros": {address: 1_000_000 for address in EXCHANGES},
            "ctfApprovals": {target: True for target in ADAPTERS.values()},
        }

    monkeypatch.setattr(check, "ClobClient", ControlledClob)
    monkeypatch.setattr(check, "inspect", public)
    result = check.run_preflight(SETTINGS, SIGNER, FUNDER, tmp_path)
    assert result["canStartNewBuys"] is ready
    assert result["redemptionApprovalComplete"] is True
    assert result["redemptionApproval"]["status"] == "APPROVED"
    assert result["unknownOpenOrderCount"] == (not ready)
    assert result["trackedVenueOrderCount"] == 1
    assert "private_key" not in json.dumps(result)
    assert calls == ["public-rpc", "client", "balance", "open-orders"]


def test_preflight_rejects_wrong_signer_before_network(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "inspect", lambda *_: pytest.fail("No RPC on mismatch"))
    with pytest.raises(ValueError, match="Signing key"):
        check.run_preflight(SETTINGS, FUNDER, FUNDER, tmp_path)


@pytest.mark.parametrize("approval_status", [None, "ACTIVE", "FAILED", "COMPLETE"])
def test_missing_adapter_or_unconfirmed_authorization_blocks_new_buys(
    tmp_path, monkeypatch, approval_status
):
    store = Store(tmp_path / "LIVE" / "state.sqlite", mode="LIVE")
    if approval_status is not None:
        store.put(
            APPROVAL_KEY,
            {
                "version": 1,
                "status": approval_status,
                "identity": {
                    "signer": SIGNER,
                    "funder": FUNDER,
                    "signatureType": 2,
                },
                "remaining": [],
                "pending": None,
            },
        )
    store.close()

    class ControlledClob:
        def __init__(self, *args, **kwargs):
            pass

        def get_balance_allowance(self, params):
            return {"balance": "2000000", "allowances": ALLOWANCES}

        def get_open_orders(self):
            return []

    monkeypatch.setattr(check, "ClobClient", ControlledClob)
    monkeypatch.setattr(
        check,
        "inspect",
        lambda *_: {
            "chainId": 137,
            "block": 1,
            "pusdBalanceMicros": 2_000_000,
            "signerPOLWei": 1,
            "exchangeAllowancesMicros": {address: 1_000_000 for address in EXCHANGES},
            "ctfApprovals": {target: approval_status is not None for target in ADAPTERS.values()},
        },
    )
    result = check.run_preflight(SETTINGS, SIGNER, FUNDER, tmp_path)
    ready = approval_status == "COMPLETE"
    assert result["redemptionApprovalComplete"] is ready
    assert result["canStartNewBuys"] is ready
    assert (
        result["redemptionApproval"]["status"]
        == {
            None: "MISSING",
            "ACTIVE": "MISSING",
            "FAILED": "FAILED",
            "COMPLETE": "APPROVED",
        }[approval_status]
    )


@pytest.mark.parametrize(
    "clob_balance,onchain_balance,clob_allowances,onchain_allowances",
    [
        (1_999_999, 2_000_000, (2_000_000, 2_000_000), (2_000_000, 2_000_000)),
        (2_000_000, 1_999_999, (2_000_000, 2_000_000), (2_000_000, 2_000_000)),
        (2_000_000, 2_000_000, (1_999_999, 2_000_000), (2_000_000, 2_000_000)),
        (2_000_000, 2_000_000, (2_000_000, 2_000_000), (2_000_000, 1_999_999)),
    ],
)
def test_private_preflight_requires_saved_order_amount_on_both_exchange_paths(
    tmp_path,
    monkeypatch,
    clob_balance,
    onchain_balance,
    clob_allowances,
    onchain_allowances,
):
    store = Store(tmp_path / "LIVE" / "state.sqlite", mode="LIVE")
    store.put("preferences", store.get("preferences") | {"orderAmount": "2"})
    store.close()

    class ControlledClob:
        def __init__(self, *args, **kwargs):
            pass

        def get_balance_allowance(self, params):
            return {
                "balance": str(clob_balance),
                "allowances": dict(zip(EXCHANGES, map(str, clob_allowances), strict=True)),
            }

        def get_open_orders(self):
            return []

    monkeypatch.setattr(check, "ClobClient", ControlledClob)
    monkeypatch.setattr(
        check,
        "inspect",
        lambda *_: {
            "chainId": 137,
            "block": 1,
            "pusdBalanceMicros": onchain_balance,
            "signerPOLWei": 1,
            "exchangeAllowancesMicros": dict(zip(EXCHANGES, onchain_allowances, strict=True)),
            "ctfApprovals": {target: True for target in ADAPTERS.values()},
        },
    )
    result = check.run_preflight(SETTINGS, SIGNER, FUNDER, tmp_path)
    assert result["nextOrderAmountMicros"] == 2_000_000
    assert result["canStartNewBuys"] is False
    assert result["collateralBalanceSufficient"] is (
        clob_balance >= 2_000_000 and onchain_balance >= 2_000_000
    )
    assert result["collateralAllowancePresent"] is (
        all(value >= 2_000_000 for value in clob_allowances + onchain_allowances)
    )
