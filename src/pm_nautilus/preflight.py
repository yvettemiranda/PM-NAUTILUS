"""Public-address-only checks. Never initialize a signer or trading runtime."""

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
from functools import partial

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


def _read_parallel(calls):
    # Independent reads share one fixed block. Join all workers before returning
    # or raising so the caller can safely close its HTTP client.
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="pm-preflight") as pool:
        futures = [pool.submit(call) for call in calls]
        return [future.result() for future in futures]


def inspect(rpc, signer, funder, signature_type):
    signer, funder = map(Web3.to_checksum_address, (signer, funder))
    if type(signature_type) is not int or signature_type not in (0, 2):
        raise ValueError("Only EOA(0) and single-owner Safe(2) are supported")
    if int(rpc.query("eth_chainId", []), 16) != 137:
        raise ValueError("RPC is not Polygon mainnet")
    rpc.block = rpc.query("eth_blockNumber", [])
    addresses = list(dict.fromkeys([PUSD, CTF, *ADAPTERS.values(), *EXCHANGES, signer, funder]))
    codes = dict(
        zip(
            addresses,
            _read_parallel(
                [partial(rpc.query, "eth_getCode", [address, rpc.block]) for address in addresses]
            ),
            strict=True,
        )
    )
    contracts = {
        address: codes[address] != "0x" for address in (PUSD, CTF, *ADAPTERS.values(), *EXCHANGES)
    }
    if not all(contracts.values()):
        raise ValueError("A required contract has no deployed code")
    if codes[signer] != "0x":
        raise ValueError("Signer has contract/delegation code; requires separate review")
    funder_code = codes[funder]
    if signature_type == 0:
        if signer != funder:
            raise ValueError("EOA signer and funder must match")
        if funder_code != "0x":
            raise ValueError("EOA funder must have no contract code")
    else:
        if funder_code == "0x":
            raise ValueError("Safe funder has no deployed code")
        owners, threshold = _read_parallel(
            [
                partial(rpc.call, funder, "getOwners()", outputs=("address[]",)),
                partial(rpc.call, funder, "getThreshold()"),
            ]
        )
        if threshold != 1 or len(owners) != 1 or owners[0].lower() != signer.lower():
            raise ValueError("Safe must have exactly this owner and threshold 1")
    approval_spenders = (*EXCHANGES, *ADAPTERS.values())
    facts = _read_parallel(
        [
            partial(rpc.call, PUSD, "balanceOf(address)", (funder,), ("address",)),
            partial(rpc.query, "eth_getBalance", [signer, rpc.block]),
            *[
                partial(
                    rpc.call,
                    PUSD,
                    "allowance(address,address)",
                    (funder, spender),
                    ("address", "address"),
                )
                for spender in EXCHANGES
            ],
            *[
                partial(
                    rpc.call,
                    CTF,
                    "isApprovedForAll(address,address)",
                    (funder, spender),
                    ("address", "address"),
                    ("bool",),
                )
                for spender in approval_spenders
            ],
        ]
    )
    balance, gas = facts[0], int(facts[1], 16)
    allowances = dict(zip(EXCHANGES, facts[2:4], strict=True))
    approvals = dict(zip(approval_spenders, facts[4:], strict=True))
    if any(type(value) is not bool for value in approvals.values()):
        raise ValueError("Invalid CTF approval response")
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
