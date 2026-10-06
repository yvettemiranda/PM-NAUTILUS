"""Build LIVE credentials from a confirmed signer without trading writes.

The result contains a private key and CLOB API credentials. Callers must put it
straight into the encrypted wallet vault, never a web response, log, or file.
"""

import re
from importlib import metadata
from urllib.parse import urlsplit

import httpx
from eth_account import Account
from web3 import Web3

from .preflight import PublicRPC, inspect
from .wallet_vault import SignerMaterial


DEFAULT_POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"
SDK_VERSION = "1.0.1"


class LiveSetupError(ValueError):
    """A deliberately small error safe to show without raw RPC/SDK details."""

    def __init__(self, stage: str, error_type: str):
        self.stage = stage
        self.error_type = error_type
        super().__init__(f"{stage}:{error_type}")

    def as_public(self) -> dict[str, str]:
        return {"stage": self.stage, "errorType": self.error_type}


def _validate_input(material, funder, rpc_url, auto_approve_redemption):
    if not isinstance(material, SignerMaterial):
        raise LiveSetupError("input", "INVALID_SIGNER")
    try:
        key = material.private_key
    except Exception:
        raise LiveSetupError("input", "INVALID_SIGNER") from None
    if not isinstance(key, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", key):
        raise LiveSetupError("input", "INVALID_SIGNER")
    try:
        signer = Account.from_key(key).address
    except Exception:
        raise LiveSetupError("input", "INVALID_SIGNER") from None
    try:
        claimed_signer = Web3.to_checksum_address(material.address)
        checked_funder = Web3.to_checksum_address(funder)
    except Exception:
        raise LiveSetupError("input", "INVALID_ADDRESS") from None
    if signer != claimed_signer:
        raise LiveSetupError("input", "SIGNER_MISMATCH")
    try:
        rpc = urlsplit(rpc_url)
    except Exception:
        raise LiveSetupError("input", "INVALID_RPC_URL") from None
    if rpc.scheme != "https" or not rpc.hostname:
        raise LiveSetupError("input", "INVALID_RPC_URL")
    if type(auto_approve_redemption) is not bool:
        raise LiveSetupError("input", "INVALID_REDEMPTION_SETTING")
    signature_type = 0 if signer == checked_funder else 2
    return signer, checked_funder, signature_type


def _derive_clob_credentials(key, funder, signature_type, client_factory):
    try:
        if client_factory is None:
            if metadata.version("py-clob-client-v2") != SDK_VERSION:
                raise LiveSetupError("sdk", "UNSUPPORTED_VERSION")
            from py_clob_client_v2.client import ClobClient

            client_factory = ClobClient
    except LiveSetupError:
        raise
    except Exception:
        raise LiveSetupError("sdk", "UNAVAILABLE") from None
    client = None
    try:
        client = client_factory(
            "https://clob.polymarket.com",
            chain_id=137,
            key=key,
            signature_type=signature_type,
            funder=funder,
        )
        creds = client.create_or_derive_api_key()
        values = (creds.api_key, creds.api_secret, creds.api_passphrase)
        if any(not isinstance(item, str) or not item.strip() for item in values):
            raise ValueError("Incomplete API credentials")
        return dict(zip(("api_key", "api_secret", "passphrase"), values, strict=True))
    except Exception:
        # Vendor exceptions can include headers, URLs, and signed auth details.
        raise LiveSetupError("clob", "CREDENTIAL_DERIVATION_FAILED") from None
    finally:
        # The current pinned sync SDK has no close(), but close future adapters.
        try:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        except Exception:
            pass


def build_live_settings(
    material: SignerMaterial,
    funder: str,
    *,
    rpc_url: str = DEFAULT_POLYGON_RPC,
    auto_approve_redemption: bool = False,
    inspect_wallet=None,
    client_factory=None,
    http_client=None,
) -> dict:
    """Validate EOA/single-owner Safe, then derive CLOB L2 API credentials.

    ``create_or_derive_api_key`` signs a CLOB authentication request, not an
    order. ``inspect`` performs read-only RPC calls and checks signer ownership.
    The caller must have separately confirmed the displayed signer and funder.
    Dependencies can be injected to run complete offline tests.
    """
    signer, checked_funder, signature_type = _validate_input(
        material, funder, rpc_url, auto_approve_redemption
    )
    verify = inspect if inspect_wallet is None else inspect_wallet
    try:
        if http_client is None:
            with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as transport:
                result = verify(
                    PublicRPC(transport, rpc_url), signer, checked_funder, signature_type
                )
        else:
            result = verify(PublicRPC(http_client, rpc_url), signer, checked_funder, signature_type)
        if (
            not isinstance(result, dict)
            or result.get("walletChecksPassed") is not True
            or result.get("chainId") != 137
            or result.get("signatureType") != signature_type
        ):
            raise ValueError("Incomplete wallet inspection")
    except Exception:
        # PublicRPC URL may contain a provider credential; never copy details.
        raise LiveSetupError("wallet", "OWNERSHIP_CHECK_FAILED") from None
    credentials = _derive_clob_credentials(
        material.private_key, checked_funder, signature_type, client_factory
    )
    return {
        "private_key": material.private_key,
        "api_key": credentials["api_key"],
        "api_secret": credentials["api_secret"],
        "passphrase": credentials["passphrase"],
        "funder": checked_funder,
        "signature_type": signature_type,
        "rpc_url": rpc_url,
        "auto_approve_redemption": auto_approve_redemption,
    }
