"""Pre-buy wallet checks through real Web3 against a local JSON-RPC server only.

The batched fast path must issue every chain/code/Safe/approval read on each
buy, decide with the same validators, and fall back to separate fresh reads
whenever the RPC cannot answer a batch. Nothing here contacts a network.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from eth_abi import encode
from web3 import Web3

from pm_nautilus.redemption import (
    ADAPTERS,
    BATCH_READ_FAILURE_LIMIT,
    CTF,
    EXCHANGES,
    PUSD,
    PolygonWallet,
    RedemptionCheckError,
)

KEY = "0x" + "11" * 32
SIGNER = Web3().eth.account.from_key(KEY).address
SAFE = Web3.to_checksum_address("0x" + "5a" * 20)
SELECTOR = {
    name: Web3.keccak(text=signature)[:4].hex().removeprefix("0x")
    for name, signature in (
        ("owners", "getOwners()"),
        ("threshold", "getThreshold()"),
        ("approved", "isApprovedForAll(address,address)"),
    )
}
OPERATORS = {Web3.to_checksum_address(a) for a in (*ADAPTERS.values(), *EXCHANGES)}
CONTRACTS = {Web3.to_checksum_address(a) for a in (CTF, PUSD, *ADAPTERS.values())}


class Chain:
    def __init__(self, *, safe=True, batch=True, delay=0.0):
        self.batch = batch
        self.delay = delay
        self.chain_id = 137
        self.code = {address: "0x6080" for address in CONTRACTS}
        if safe:
            self.code[SAFE] = "0x6080"
        self.owners = [SIGNER]
        self.threshold = 1
        self.approved = dict.fromkeys(OPERATORS, True)
        self.posts = []  # One entry per HTTP round trip: list of JSON-RPC methods.
        self.lock = threading.Lock()

    def answer(self, request):
        method, params = request["method"], request.get("params", [])
        if method == "eth_chainId":
            result = hex(self.chain_id)
        elif method == "eth_getCode":
            result = self.code.get(Web3.to_checksum_address(params[0]), "0x")
        elif method == "eth_call":
            to = Web3.to_checksum_address(params[0]["to"])
            data = (params[0].get("data") or params[0]["input"]).removeprefix("0x")
            if self.code.get(to, "0x") == "0x":
                result = "0x"
            elif data.startswith(SELECTOR["owners"]) and to == SAFE:
                result = "0x" + encode(["address[]"], [self.owners]).hex()
            elif data.startswith(SELECTOR["threshold"]) and to == SAFE:
                result = "0x" + encode(["uint256"], [self.threshold]).hex()
            elif data.startswith(SELECTOR["approved"]) and to == Web3.to_checksum_address(CTF):
                operator = Web3.to_checksum_address("0x" + data[8 + 64 + 24 : 8 + 128])
                result = "0x" + encode(["bool"], [self.approved[operator]]).hex()
            else:
                raise AssertionError(f"unexpected eth_call {to} {data[:8]}")
        else:
            raise AssertionError(f"unexpected method {method}")
        return {"jsonrpc": "2.0", "id": request["id"], "result": result}


@pytest.fixture
def rpc():
    servers = []

    def start(chain):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests = body if isinstance(body, list) else [body]
                with chain.lock:
                    chain.posts.append([request["method"] for request in requests])
                time.sleep(chain.delay)
                if isinstance(body, list) and not chain.batch:
                    reply = {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32600, "message": "batch unsupported"},
                    }
                elif isinstance(body, list):
                    reply = [chain.answer(request) for request in reversed(requests)]
                else:
                    reply = chain.answer(body)
                data = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        safe = SAFE in chain.code
        return PolygonWallet(
            {
                "rpc_url": url,
                "private_key": KEY,
                "funder": SAFE if safe else SIGNER,
                "signature_type": 2 if safe else 0,
            }
        )

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def counts(posts):
    methods = [method for post in posts for method in post]
    return {m: methods.count(m) for m in set(methods)}


@pytest.mark.parametrize("safe", [True, False])
def test_every_buy_check_runs_fresh_in_one_round_trip(rpc, safe):
    chain = Chain(safe=safe)
    wallet = rpc(chain)
    for attempt in (1, 2):
        chain.posts.clear()
        wallet.buy_preflight()
        # Exactly one HTTP round trip carrying every check, on every buy.
        assert len(chain.posts) == 1, chain.posts
        codes = len(CONTRACTS) + (2 if safe else 1)
        assert counts(chain.posts) == {
            "eth_chainId": 1,
            "eth_getCode": codes,
            "eth_call": len(OPERATORS) + (2 if safe else 0),
        }, attempt


@pytest.mark.parametrize(
    "change,message",
    [
        (lambda c: setattr(c, "chain_id", 80002), "RPC不是Polygon主网"),
        (lambda c: c.code.pop(Web3.to_checksum_address(PUSD)), "官方合约地址没有部署代码"),
        (lambda c: c.code.__setitem__(SIGNER, "0xef01"), "签名账户含有合约或委托代码"),
        (lambda c: setattr(c, "threshold", 2), "Safe须为本签名账户可执行的单签钱包"),
        (lambda c: setattr(c, "owners", [SAFE]), "Safe须为本签名账户可执行的单签钱包"),
        (lambda c: setattr(c, "owners", [SIGNER, SAFE]), "Safe须为本签名账户可执行的单签钱包"),
        (
            lambda c: c.approved.__setitem__(Web3.to_checksum_address(EXCHANGES[0]), False),
            "交易所卖出份额授权尚未确认",
        ),
        (
            lambda c: c.approved.__setitem__(Web3.to_checksum_address(ADAPTERS[True]), False),
            "赎回适配器授权尚未确认",
        ),
    ],
)
def test_batched_findings_fail_closed_with_the_same_messages(rpc, change, message):
    chain = Chain()
    wallet = rpc(chain)
    change(chain)
    with pytest.raises(RedemptionCheckError, match=message):
        wallet.buy_preflight()
    # A valid batched answer is final: no second opinion can override it.
    assert len(chain.posts) == 1


def test_undeployed_safe_falls_back_and_keeps_the_specific_error(rpc):
    chain = Chain()
    wallet = rpc(chain)
    chain.code.pop(SAFE)  # Safe reads cannot decode inside the batch.
    with pytest.raises(RedemptionCheckError, match="资金地址不是已部署的Safe"):
        wallet.buy_preflight()
    assert isinstance(chain.posts[0], list) and len(chain.posts[0]) > 1
    assert len(chain.posts) > 1  # Separate fresh reads decided.


def test_rpc_without_batches_still_runs_every_check_separately(rpc):
    chain = Chain(batch=False)
    wallet = rpc(chain)
    for attempt in range(BATCH_READ_FAILURE_LIMIT + 2):
        chain.posts.clear()
        wallet.buy_preflight()
        batched = [post for post in chain.posts if len(post) > 1]
        assert len(batched) == (1 if attempt < BATCH_READ_FAILURE_LIMIT else 0)
        separate = counts([post for post in chain.posts if len(post) == 1])
        # Web3's validation middleware may add a chain-id read before each call.
        assert separate.pop("eth_chainId") >= 1
        assert separate == {"eth_getCode": len(CONTRACTS) + 2, "eth_call": len(OPERATORS) + 2}
    chain.approved[Web3.to_checksum_address(EXCHANGES[1])] = False
    with pytest.raises(RedemptionCheckError, match="交易所卖出份额授权尚未确认"):
        wallet.buy_preflight()


def test_injected_rpc_latency_costs_one_round_trip_instead_of_four(rpc):
    delay = 0.15
    batched = Chain(delay=delay)
    separate = Chain(delay=delay, batch=False)
    fast, slow = rpc(batched), rpc(separate)
    slow._batch_failures = BATCH_READ_FAILURE_LIMIT  # Separate path only.
    fast.buy_preflight()
    slow.buy_preflight()  # Warm both connection pools.
    started = time.perf_counter()
    fast.buy_preflight()
    fast_s = time.perf_counter() - started
    started = time.perf_counter()
    slow.buy_preflight()
    slow_s = time.perf_counter() - started
    # Safe path: chain -> codes -> owners/threshold -> approvals = 4 round trips.
    assert fast_s < 2 * delay
    assert slow_s >= 4 * delay


def test_outcome_balances_are_one_fixed_block_read_then_one_batch(rpc):
    chain = Chain()
    seen = []
    original = chain.answer

    def answer(request):
        if request["method"] == "eth_blockNumber":
            return {"jsonrpc": "2.0", "id": request["id"], "result": "0x7b"}
        if request["method"] == "eth_call":
            assert request["params"][1] == "0x7b", "balances must use the fixed block"
            data = request["params"][0].get("data") or request["params"][0]["input"]
            seen.append(int(data[-64:], 16))
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": "0x" + encode(["uint256"], [seen[-1] * 10]).hex(),
            }
        return original(request)

    chain.answer = answer
    wallet = rpc(chain)
    assert wallet.token_balances(("11", "22")) == {"11": 110, "22": 220}
    assert chain.posts == [["eth_blockNumber"], ["eth_call", "eth_call"]]
    assert sorted(seen) == [11, 22]
