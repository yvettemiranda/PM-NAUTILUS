"""The private preflight only reads the ledger, chain, and authenticated CLOB."""

import json

import pytest
from eth_account import Account

from pm_nautilus import private_preflight as check
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


def test_readonly_ledger_does_not_create_missing_database(tmp_path):
    with pytest.raises(ValueError, match="missing"):
        check.ledger_intents(tmp_path)
    assert not (tmp_path / "LIVE").exists()


def test_collateral_response_requires_balance_and_allowance():
    assert check.collateral_summary({"balance": "2000000", "allowances": {"exchange": "1"}}) == {
        "balanceMicros": 2_000_000,
        "anyPositiveAllowance": True,
    }
    for response in ({"balance": "1"}, {"balance": "-1", "allowance": "1"}, []):
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
            return {"balance": "2000000", "allowance": "1000000"}

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
            "exchangeAllowancesMicros": {"exchange": 1_000_000},
        }

    monkeypatch.setattr(check, "ClobClient", ControlledClob)
    monkeypatch.setattr(check, "inspect", public)
    result = check.run_preflight(SETTINGS, SIGNER, FUNDER, tmp_path)
    assert result["canStartNewBuys"] is ready
    assert result["unknownOpenOrderCount"] == (not ready)
    assert result["trackedVenueOrderCount"] == 1
    assert "private_key" not in json.dumps(result)
    assert calls == ["public-rpc", "client", "balance", "open-orders"]


def test_preflight_rejects_wrong_signer_before_network(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "inspect", lambda *_: pytest.fail("No RPC on mismatch"))
    with pytest.raises(ValueError, match="Signing key"):
        check.run_preflight(SETTINGS, FUNDER, FUNDER, tmp_path)
