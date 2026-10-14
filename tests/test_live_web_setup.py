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


_DEFAULT_PROFILE = object()


class DiscoveryHTTP:
    def __init__(self, profile=_DEFAULT_PROFILE, *, status=200, error=None):
        self.profile = (
            {"proxyWallet": SAFE, "name": SECRET} if profile is _DEFAULT_PROFILE else profile
        )
        self.status, self.error = status, error
        self.requests = []

    def get(self, url, *, params, headers):
        self.requests.append((url, params, headers))
        if self.error:
            raise self.error
        return SimpleNamespace(status_code=self.status, json=lambda: self.profile)


class DiscoveryRPC:
    def __init__(self, **changes):
        self.values = {
            "chain": "0x89",
            "block": "0x123",
            "signer_code": "0x",
            "factory_code": "0x01",
            "safe_code": "0x02",
            "expected": SAFE,
            "owners": [MATERIAL.address],
            "threshold": 1,
            **changes,
        }
        self.block = "latest"
        self.calls = []

    def query(self, method, params):
        self.calls.append((method, params))
        if method == "eth_chainId":
            return self.values["chain"]
        if method == "eth_blockNumber":
            return self.values["block"]
        assert method == "eth_getCode" and params[1] == self.values["block"]
        return self.values[
            "signer_code"
            if params[0] == MATERIAL.address
            else "factory_code"
            if params[0] == setup.SAFE_FACTORY
            else "safe_code"
        ]

    def call(self, address, signature, args=(), types=(), outputs=("uint256",)):
        self.calls.append(("eth_call", address, signature, self.block))
        assert self.block == self.values["block"]
        if signature == "computeProxyAddress(address)":
            assert address == setup.SAFE_FACTORY
            assert args == (MATERIAL.address,) and types == outputs == ("address",)
            return self.values["expected"]
        assert address == SAFE
        if signature == "getOwners()":
            assert outputs == ("address[]",)
            return self.values["owners"]
        assert signature == "getThreshold()"
        return self.values["threshold"]


def test_discovery_reads_profile_and_canonical_safe_at_one_block_without_auth():
    transport, rpc = DiscoveryHTTP(), DiscoveryRPC()
    funder = setup.discover_funder(
        MATERIAL.address,
        http_client=transport,
        rpc_factory=lambda client, url: rpc
        if client is transport and url == setup.DEFAULT_POLYGON_RPC
        else None,
    )
    assert funder == SAFE
    assert transport.requests == [
        (setup.PUBLIC_PROFILE_URL, {"address": MATERIAL.address}, {"Accept": "application/json"})
    ]
    assert [call[0] for call in rpc.calls] == [
        "eth_chainId",
        "eth_blockNumber",
        "eth_getCode",
        "eth_getCode",
        "eth_getCode",
        "eth_call",
        "eth_call",
        "eth_call",
    ]
    assert MATERIAL.private_key not in repr(transport.requests)


@pytest.mark.parametrize(
    "profile,status,code",
    [
        ({}, 404, "PROFILE_NOT_FOUND"),
        ({}, 200, "PROFILE_NOT_FOUND"),
        (None, 200, "PROFILE_NOT_FOUND"),
        ([], 200, "PROFILE_NOT_FOUND"),
        ({"name": SECRET}, 200, "UNSUPPORTED_PROFILE_WALLET"),
        ({"proxyWallet": SECRET}, 200, "UNSUPPORTED_PROFILE_WALLET"),
        ({"proxyWallet": "0x" + "00" * 20}, 200, "UNSUPPORTED_PROFILE_WALLET"),
        ({"proxyWallet": MATERIAL.address}, 200, "UNSUPPORTED_PROFILE_WALLET"),
        ({"proxyWallet": SAFE}, 302, "PROFILE_LOOKUP_FAILED"),
        ({"proxyWallet": SAFE}, 503, "PROFILE_LOOKUP_FAILED"),
    ],
)
def test_profile_discovery_fails_closed_without_choosing_eoa(profile, status, code):
    with pytest.raises(setup.LiveSetupError) as error:
        setup.discover_funder(
            MATERIAL.address,
            http_client=DiscoveryHTTP(profile, status=status),
            rpc_factory=lambda *_: pytest.fail("No RPC after unsupported profile"),
        )
    assert error.value.as_public() == {"stage": "discovery", "errorType": code}
    assert SECRET not in str(error.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"chain": "0x1"},
        {"block": SECRET},
        {"signer_code": "0x01"},
        {"factory_code": "0x"},
        {"safe_code": "0x"},
        {"expected": "0x" + "33" * 20},
        {"owners": []},
        {"owners": ["0x" + "33" * 20]},
        {"owners": [MATERIAL.address, SAFE]},
        {"threshold": 2},
        {"threshold": True},
    ],
)
def test_discovery_rejects_wrong_factory_deployment_chain_or_ownership(changes):
    with pytest.raises(setup.LiveSetupError) as error:
        setup.discover_funder(
            MATERIAL.address,
            http_client=DiscoveryHTTP(),
            rpc_factory=lambda *_: DiscoveryRPC(**changes),
        )
    assert error.value.as_public() == {
        "stage": "discovery",
        "errorType": "SAFE_VERIFICATION_FAILED",
    }
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("stage", ["profile", "rpc"])
def test_discovery_network_errors_never_expose_rpc_or_profile_details(stage):
    def unavailable(*_args):
        raise RuntimeError(SECRET)

    with pytest.raises(setup.LiveSetupError) as error:
        setup.discover_funder(
            MATERIAL.address,
            http_client=DiscoveryHTTP(error=RuntimeError(SECRET) if stage == "profile" else None),
            rpc_factory=unavailable,
        )
    assert error.value.stage == "discovery"
    assert SECRET not in str(error.value) + repr(error.value.as_public())


def test_default_discovery_transport_has_bounded_timeout_and_no_redirect_or_env_proxy(monkeypatch):
    options, closed = [], []

    class Transport(DiscoveryHTTP):
        def __init__(self, **kwargs):
            options.append(kwargs)
            super().__init__()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            closed.append(True)

    monkeypatch.setattr(setup.httpx, "Client", Transport)
    assert setup.discover_funder(MATERIAL.address, rpc_factory=lambda *_: DiscoveryRPC()) == SAFE
    assert options == [{"timeout": 20, "follow_redirects": False, "trust_env": False}]
    assert closed == [True]
