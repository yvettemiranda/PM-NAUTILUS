"""Public-address-only checks. Never initialize a signer or trading runtime."""

import argparse
import json
import os

import httpx
from eth_abi import decode, encode
from web3 import Web3

from .redemption import ADAPTERS, CTF, PUSD

EXCHANGES = (
    "0xE111180000d2663C0091e4f400237545B87B996B",
    "0xe2222d279d744050d28e00520010520000310F59",
)


class PublicRPC:
    METHODS = frozenset(
        {"eth_chainId", "eth_blockNumber", "eth_getCode", "eth_call", "eth_getBalance"}
    )

    def __init__(self, client, url):
        self.client, self.url = client, url
        self.block = "latest"

    def query(self, method, params):
        if method not in self.METHODS:
            raise ValueError("RPC method is not read-only")
        response = self.client.post(
            self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload or not isinstance(payload.get("result"), str):
            raise ValueError("RPC query failed")
        return payload["result"]

    def call(self, address, signature, args=(), types=(), outputs=("uint256",)):
        data = Web3.keccak(text=signature)[:4] + encode(types, args)
        result = self.query("eth_call", [{"to": address, "data": Web3.to_hex(data)}, self.block])
        return decode(outputs, bytes.fromhex(result[2:]))[0]


def inspect(rpc, signer, funder, signature_type):
    signer, funder = map(Web3.to_checksum_address, (signer, funder))
    if signature_type not in (0, 2):
        raise ValueError("Only EOA(0) and single-owner Safe(2) are supported")
    if int(rpc.query("eth_chainId", []), 16) != 137:
        raise ValueError("RPC is not Polygon mainnet")
    rpc.block = rpc.query("eth_blockNumber", [])
    contracts = {
        address: rpc.query("eth_getCode", [address, rpc.block]) != "0x"
        for address in (PUSD, CTF, *ADAPTERS.values(), *EXCHANGES)
    }
    if not all(contracts.values()):
        raise ValueError("A required contract has no deployed code")
    signer_code = rpc.query("eth_getCode", [signer, rpc.block])
    if signer_code != "0x":
        raise ValueError("Signer has contract/delegation code; requires separate review")
    if signature_type == 0:
        if signer != funder:
            raise ValueError("EOA signer and funder must match")
    else:
        owners = rpc.call(funder, "getOwners()", outputs=("address[]",))
        threshold = rpc.call(funder, "getThreshold()")
        if threshold != 1 or len(owners) != 1 or owners[0].lower() != signer.lower():
            raise ValueError("Safe must have exactly this owner and threshold 1")
    balance = rpc.call(PUSD, "balanceOf(address)", (funder,), ("address",))
    gas = int(rpc.query("eth_getBalance", [signer, rpc.block]), 16)
    allowances = {
        spender: rpc.call(
            PUSD, "allowance(address,address)", (funder, spender), ("address", "address")
        )
        for spender in EXCHANGES
    }
    approvals = {
        spender: rpc.call(
            CTF,
            "isApprovedForAll(address,address)",
            (funder, spender),
            ("address", "address"),
            ("bool",),
        )
        for spender in (*EXCHANGES, *ADAPTERS.values())
    }
    return {
        "scope": "PUBLIC_READ_ONLY_NOT_LIVE_ACCEPTANCE",
        "block": int(rpc.block, 16),
        "chainId": 137,
        "signatureType": signature_type,
        "walletChecksPassed": True,
        "contractsHaveCode": contracts,
        "pusdBalanceMicros": balance,
        "signerPOLWei": gas,
        "exchangeAllowancesMicros": allowances,
        "ctfApprovals": approvals,
        "unverified": [
            "CLOB authentication",
            "open orders",
            "position ownership reconciliation",
            "contract implementation identity",
            "real fills",
            "real redemption",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signer", required=True)
    parser.add_argument("--funder", required=True)
    parser.add_argument("--signature-type", required=True, type=int, choices=(0, 2))
    args = parser.parse_args()
    # A provider URL may contain an API key: never print it or exception details.
    url = os.environ.get("PM_PREFLIGHT_RPC_URL", "https://polygon-bor-rpc.publicnode.com")
    try:
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            result = inspect(PublicRPC(client, url), args.signer, args.funder, args.signature_type)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "errorType": type(exc).__name__,
                    "message": "只读检查失败；未进行签名或链上写入。",
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
