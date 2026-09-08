import httpx
import pytest
from eth_abi import encode
from web3 import Web3

from pm_nautilus.preflight import PublicRPC, inspect

OWNER = "0x" + "11" * 20
FUNDER = "0x" + "22" * 20


def test_public_preflight_only_sends_read_methods_and_uses_fixed_block():
    methods = []

    def handler(request):
        import json

        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        methods.append(method)
        if method == "eth_chainId":
            result = "0x89"
        elif method == "eth_blockNumber":
            result = "0x100"
        elif method == "eth_getCode":
            assert params[-1] == "0x100"
            result = "0x" if params[0] == OWNER else "0x1234"
        elif method == "eth_getBalance":
            assert params[-1] == "0x100"
            result = "0x0"
        else:
            assert method == "eth_call" and params[-1] == "0x100"
            selector = params[0]["data"][:10]
            if selector == Web3.to_hex(Web3.keccak(text="getOwners()")[:4]):
                result = Web3.to_hex(encode(["address[]"], [[OWNER]]))
            else:
                value = 1 if selector == Web3.to_hex(Web3.keccak(text="getThreshold()")[:4]) else 0
                result = Web3.to_hex(encode(["uint256"], [value]))
        return httpx.Response(200, json={"result": result})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rpc = PublicRPC(client, "https://example.invalid")
        result = inspect(rpc, OWNER, FUNDER, 2)
        assert result["walletChecksPassed"] and result["pusdBalanceMicros"] == 0
        assert result["scope"] == "PUBLIC_READ_ONLY_NOT_LIVE_ACCEPTANCE"
        with pytest.raises(ValueError, match="not read-only"):
            rpc.query("eth_sendRawTransaction", ["0x00"])
    assert set(methods) <= PublicRPC.METHODS


def test_wrong_chain_and_unsupported_wallet_fail_before_account_calls():
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"result": "0x1"}))
    ) as client:
        rpc = PublicRPC(client, "https://example.invalid")
        with pytest.raises(ValueError, match="Polygon"):
            inspect(rpc, OWNER, OWNER, 0)
        with pytest.raises(ValueError, match="supported"):
            inspect(rpc, OWNER, FUNDER, 3)
