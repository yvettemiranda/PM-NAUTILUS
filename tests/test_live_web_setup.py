"""LIVE web import only prepares credentials after wallet ownership checks."""

import json
from types import SimpleNamespace

import pytest

from pm_nautilus import live_web_setup as setup
from pm_nautilus.preflight import PublicRPC
from pm_nautilus.wallet_vault import SignerMaterial, derive_signer


MATERIAL = derive_signer("private_key", "0x" + "11" * 32)  # Disposable test key.
SAFE = "0x" + "22" * 20
SECRET = "secret-must-not-leak"


def verified_wallet(expected_signature_type, calls):
    def inspect(rpc, signer, funder, signature_type):
        calls.append(("inspect", signer, funder, signature_type))
        assert isinstance(rpc, PublicRPC)
        assert rpc.url == setup.DEFAULT_POLYGON_RPC
        assert signature_type == expected_signature_type
        return {
            "walletChecksPassed": True,
            "chainId": 137,
            "signatureType": signature_type,
        }

    return inspect


def client_factory(calls):
    class Client:
        def __init__(self, host, *, chain_id, key, signature_type, funder):
            calls.append(("client", host, chain_id, key, signature_type, funder))

        def create_or_derive_api_key(self):
            calls.append(("derive",))
            return SimpleNamespace(
                api_key="test-api-key",
                api_secret="test-api-secret",
                api_passphrase="test-passphrase",
            )

    return Client


@pytest.mark.parametrize(("funder", "signature_type"), [(MATERIAL.address, 0), (SAFE, 2)])
def test_settings_match_existing_live_schema_after_readonly_ownership_check(funder, signature_type):
    calls = []
    result = setup.build_live_settings(
        MATERIAL,
        funder,
        inspect_wallet=verified_wallet(signature_type, calls),
        client_factory=client_factory(calls),
        http_client=object(),
    )

    assert result == {
        "private_key": MATERIAL.private_key,
        "api_key": "test-api-key",
        "api_secret": "test-api-secret",
        "passphrase": "test-passphrase",
        "funder": funder,
        "signature_type": signature_type,
        "rpc_url": setup.DEFAULT_POLYGON_RPC,
        "auto_approve_redemption": False,
    }
    assert [call[0] for call in calls] == ["inspect", "client", "derive"]
    assert calls[1] == (
        "client",
        "https://clob.polymarket.com",
        137,
        MATERIAL.private_key,
        signature_type,
        funder,
    )


def test_invalid_signer_is_rejected_before_any_network_or_auth():
    mismatched = SignerMaterial(SAFE, MATERIAL.private_key)
    with pytest.raises(setup.LiveSetupError) as error:
        setup.build_live_settings(
            mismatched,
            SAFE,
            inspect_wallet=lambda *_: pytest.fail("No RPC for mismatched signer"),
            client_factory=lambda *_args, **_kwargs: pytest.fail("No CLOB auth"),
            http_client=object(),
        )
    assert error.value.as_public() == {"stage": "input", "errorType": "SIGNER_MISMATCH"}
    assert MATERIAL.private_key not in str(error.value)


def test_wallet_inspection_failure_stops_before_clob_auth_and_hides_rpc_exception():
    def failed(*_):
        raise RuntimeError(SECRET)

    with pytest.raises(setup.LiveSetupError) as error:
        setup.build_live_settings(
            MATERIAL,
            SAFE,
            inspect_wallet=failed,
            client_factory=lambda *_args, **_kwargs: pytest.fail("No CLOB auth"),
            http_client=object(),
        )
    assert error.value.as_public() == {"stage": "wallet", "errorType": "OWNERSHIP_CHECK_FAILED"}
    assert SECRET not in str(error.value) + json.dumps(error.value.as_public())


def test_incomplete_inspection_cannot_become_a_pass():
    with pytest.raises(setup.LiveSetupError) as error:
        setup.build_live_settings(
            MATERIAL,
            MATERIAL.address,
            inspect_wallet=lambda *_: {"walletChecksPassed": True, "chainId": 137},
            client_factory=lambda *_args, **_kwargs: pytest.fail("No CLOB auth"),
            http_client=object(),
        )
    assert error.value.stage == "wallet"


def test_clob_failure_hides_vendor_exception_and_does_not_return_settings():
    class FailingClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def create_or_derive_api_key(self):
            raise RuntimeError(SECRET)

    with pytest.raises(setup.LiveSetupError) as error:
        setup.build_live_settings(
            MATERIAL,
            MATERIAL.address,
            inspect_wallet=verified_wallet(0, []),
            client_factory=FailingClient,
            http_client=object(),
        )
    assert error.value.as_public() == {
        "stage": "clob",
        "errorType": "CREDENTIAL_DERIVATION_FAILED",
    }
    assert SECRET not in str(error.value) + repr(error.value.as_public())


def test_incomplete_api_credentials_are_rejected():
    class IncompleteClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def create_or_derive_api_key(self):
            return SimpleNamespace(api_key="", api_secret="test", api_passphrase="test")

    with pytest.raises(setup.LiveSetupError) as error:
        setup.build_live_settings(
            MATERIAL,
            MATERIAL.address,
            inspect_wallet=verified_wallet(0, []),
            client_factory=IncompleteClient,
            http_client=object(),
        )
    assert error.value.stage == "clob"
