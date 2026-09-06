"""Condition-scoped settlement rights and recoverable Polygon redemption.

The RPC signer is constructed only behind the explicit server LIVE gate. EOA and
single-owner Safe are supported; no account abstraction or manual positions are claimed.
"""

import asyncio
from web3 import Web3
from web3.exceptions import TransactionNotFound

from .config import SCALE
from .market import GAMMA, resolution
from .rules import cost

PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
ADAPTERS = {
    False: "0xAdA100Db00Ca00073811820692005400218FcE1f",
    True: "0xadA2005600Dec949baf300f4C6120000bDB6eAab",
}
ZERO = "0x" + "00" * 20


def abi(name, args, outputs=(), view=True):
    return {
        "type": "function",
        "name": name,
        "stateMutability": "view" if view else "nonpayable",
        "inputs": [{"name": f"a{i}", "type": t} for i, t in enumerate(args)],
        "outputs": [{"name": f"r{i}", "type": t} for i, t in enumerate(outputs)],
    }


TOKEN_ABI = [
    abi("balanceOf", ["address", "uint256"], ["uint256"]),
    abi("isApprovedForAll", ["address", "address"], ["bool"]),
    abi("setApprovalForAll", ["address", "bool"], view=False),
    abi("payoutDenominator", ["bytes32"], ["uint256"]),
    abi("payoutNumerators", ["bytes32", "uint256"], ["uint256"]),
]
REDEEM_ABI = [abi("redeemPositions", ["address", "bytes32", "bytes32", "uint256[]"], view=False)]
SAFE_ABI = [
    abi("getOwners", [], ["address[]"]),
    abi("getThreshold", [], ["uint256"]),
    abi(
        "execTransaction",
        [
            "address",
            "uint256",
            "bytes",
            "uint8",
            "uint256",
            "uint256",
            "uint256",
            "address",
            "address",
            "bytes",
        ],
        ["bool"],
        False,
    ),
]


class PolygonWallet:
    def __init__(self, settings):
        self.w3 = Web3(Web3.HTTPProvider(settings["rpc_url"], request_kwargs={"timeout": 20}))
        self.signer = self.w3.eth.account.from_key(settings["private_key"])
        self.funder = Web3.to_checksum_address(settings["funder"])
        self.signature_type = settings["signature_type"]
        if self.signature_type not in (0, 2):
            raise ValueError("当前赎回支持 EOA(0) 与单签 Safe(2)，须先核对实际钱包类型")
        if self.signature_type == 0 and self.funder != self.signer.address:
            raise ValueError("EOA资金账户必须与签名账户相同")
        self.auto_approve = settings.get("auto_approve_redemption") is True
        self.ctf = self.w3.eth.contract(address=CTF, abi=TOKEN_ABI)

    def preflight(self):
        if self.w3.eth.chain_id != 137:
            raise ValueError("RPC不是Polygon主网")
        for address in [CTF, PUSD, *ADAPTERS.values()]:
            if not self.w3.eth.get_code(Web3.to_checksum_address(address)):
                raise ValueError("官方合约地址没有部署代码")
        if self.signature_type == 2:
            safe = self.w3.eth.contract(address=self.funder, abi=SAFE_ABI)
            if (
                safe.functions.getThreshold().call() != 1
                or self.signer.address not in safe.functions.getOwners().call()
            ):
                raise ValueError("Safe须为本签名账户可执行的单签钱包")

    def owned_balances(self, claim):
        return {
            tid: self.ctf.functions.balanceOf(self.funder, int(tid)).call()
            for tid in claim["payouts"]
        }

    def prepare(self, claim):
        self.preflight()
        balances = self.owned_balances(claim)
        if any(balances[tid] != claim["rights"].get(tid, 0) for tid in balances):
            raise ValueError("同Condition余额与本程序权利不一致，拒绝覆盖手动资产")
        condition = bytes.fromhex(claim["condition_id"][2:])
        den = self.ctf.functions.payoutDenominator(condition).call()
        if den == 0:
            raise ValueError("链上尚未正式结算")
        nums = [self.ctf.functions.payoutNumerators(condition, i).call() for i in (0, 1)]
        if [n * SCALE // den for n in nums] != list(claim["payouts"].values()):
            raise ValueError("链上结算比例与官方市场结果不一致")
        target = Web3.to_checksum_address(ADAPTERS[claim["neg_risk"]])
        approved = self.ctf.functions.isApprovedForAll(self.funder, target).call()
        operation = "REDEEM"
        if not approved:
            if not self.auto_approve:
                raise ValueError("缺少赎回适配器授权；账户配置后方可开启自动授权")
            call = self.ctf.functions.setApprovalForAll(target, True)
            target = CTF
            operation = "APPROVE"
        else:
            contract = self.w3.eth.contract(address=target, abi=REDEEM_ABI)
            call = contract.functions.redeemPositions(PUSD, bytes(32), condition, [1, 2])
        data = call._encode_transaction_data()
        if self.signature_type == 2:
            safe = self.w3.eth.contract(address=self.funder, abi=SAFE_ABI)
            # Safe's prevalidated signature is valid only when msg.sender is this
            # owner. The outer Polygon transaction is signed by that owner.
            signature = bytes.fromhex(self.signer.address[2:]).rjust(32, b"\0") + bytes(32) + b"\1"
            data = safe.functions.execTransaction(
                target, 0, bytes.fromhex(data[2:]), 0, 0, 0, 0, ZERO, ZERO, signature
            )._encode_transaction_data()
            target = self.funder
        tx = {
            "from": self.signer.address,
            "to": target,
            "data": data,
            "value": 0,
            "chainId": 137,
            "nonce": self.w3.eth.get_transaction_count(self.signer.address, "pending"),
            "gasPrice": self.w3.eth.gas_price,
        }
        tx["gas"] = self.w3.eth.estimate_gas(tx) * 12 // 10
        if self.w3.eth.get_balance(self.signer.address) < tx["gas"] * tx["gasPrice"]:
            raise ValueError("签名账户POL不足以支付链上手续费")
        signed = self.signer.sign_transaction(tx)
        return {
            "raw_tx": Web3.to_hex(signed.raw_transaction),
            "tx_hash": Web3.to_hex(signed.hash),
            "nonce": tx["nonce"],
            "operation": operation,
        }

    def broadcast(self, claim):
        try:
            result = Web3.to_hex(self.w3.eth.send_raw_transaction(claim["raw_tx"]))
            if result.lower() != claim["tx_hash"].lower():
                raise ValueError("RPC返回的交易哈希不一致")
        except ValueError as exc:
            if not any(x in str(exc).lower() for x in ("already known", "known transaction")):
                raise

    def receipt(self, claim):
        try:
            receipt = self.w3.eth.get_transaction_receipt(claim["tx_hash"])
        except TransactionNotFound:
            return None
        if self.w3.eth.block_number - receipt["blockNumber"] < 32:
            return None
        if receipt["status"] != 1:
            return {"status": "FAILED", "amount": 0}
        if claim["operation"] == "APPROVE":
            adapter = Web3.to_checksum_address(ADAPTERS[claim["neg_risk"]])
            ok = self.ctf.functions.isApprovedForAll(self.funder, adapter).call()
            return {"status": "APPROVED" if ok else "FAILED", "amount": 0}
        transfer = Web3.keccak(text="Transfer(address,address,uint256)")
        amount = 0
        for log in receipt["logs"]:
            if (
                log["address"].lower() == PUSD.lower()
                and len(log["topics"]) == 3
                and log["topics"][0] == transfer
            ):
                to = "0x" + bytes(log["topics"][2])[-20:].hex()
                if to.lower() == self.funder.lower():
                    amount += int.from_bytes(log["data"], "big")
        if any(self.owned_balances(claim).values()):
            return {"status": "FAILED", "amount": amount}
        return {"status": "CONFIRMED", "amount": amount}


class RedemptionService:
    def __init__(self, runtime, market, wallet=None):
        self.r = runtime
        self.market = market
        self.wallet = wallet
        self.busy = False

    def identify(self, token, payouts):
        r = self.r
        cycle = r.business["cycles"].get(token.event_id)
        if (
            not cycle
            or cycle["token_id"] != token.token_id
            or token.condition_id in r.business["claims"]
        ):
            return
        if r.active_intents(token.event_id):
            r.cancel_buys(token.event_id)
            return  # No redemption can race outstanding venue inventory changes.
        claim = {
            "condition_id": token.condition_id,
            "market_id": token.market_id,
            "token_id": token.token_id,
            "event_id": token.event_id,
            "neg_risk": token.neg_risk,
            "rights": {token.token_id: cycle["quantity"]},
            "payouts": payouts,
            "amount": cost(payouts[token.token_id], cycle["quantity"]),
            "cost": cycle["cost"],
            "state": "IDENTIFIED",
            "generation": r.store.generation,
            "error": None,
        }
        with r.store.transaction():
            r.business["claims"][token.condition_id] = claim
            r.business["settled"].append(token.condition_id)
            r.business["targets"] = {
                k: v for k, v in r.business["targets"].items() if v["event_id"] != token.event_id
            }
            r.store.put("business", r.business)
        r.refresh_eligibility()

    async def advance(self, claim):
        r = self.r
        if claim["generation"] != r.store.generation or claim["state"] == "CREDITED":
            return
        if r.mode == "TEST":
            if claim["state"] == "IDENTIFIED":
                claim["state"] = "CONFIRMED"  # Separate observable step; no cash yet.
                r.store.put("business", r.business)
                return
            r.settlement_fill(claim)
            return
        if claim["state"] in {"IDENTIFIED", "RETRY"}:
            prepared = await asyncio.to_thread(self.wallet.prepare, claim)
            claim.update(prepared, state="SUBMITTED", error=None)
            r.store.put("business", r.business)  # Persist hash/raw/nonce BEFORE any RPC send.
        if claim["state"] == "SUBMITTED":
            result = await asyncio.to_thread(self.wallet.receipt, claim)
            if result is None:
                await asyncio.to_thread(self.wallet.broadcast, claim)
                return  # Same raw bytes only, even after a timeout/restart.
            if result["status"] == "APPROVED":
                claim["approval_hash"] = claim.pop("tx_hash")
                claim.pop("raw_tx", None)
                claim["state"] = "IDENTIFIED"
            elif result["status"] == "FAILED":
                claim["state"] = "FAILED"
                claim["error"] = "链上执行失败或权利未全部赎回；需要核对"
            elif result["amount"] != claim["amount"]:
                claim["state"] = "FAILED"
                claim["error"] = "实际回款与自有应收不一致"
            else:
                claim["state"] = "CONFIRMED"
            r.store.put("business", r.business)
        if claim["state"] == "CONFIRMED":
            # Refresh the actual venue cash balance first. The settlement native
            # event closes own inventory; it never invents spendable LIVE cash.
            await r.client._update_account_state()
            if not claim.get("cash_included"):
                return
            claim["error"] = None
            r.store.put("business", r.business)
            r.settlement_fill(claim)

    async def run_once(self):
        if self.busy:
            return
        self.busy = True
        try:
            r = self.r
            for cycle in list(r.business["cycles"].values()):
                t = r.tokens[cycle["token_id"]]
                if t.condition_id in r.business["claims"]:
                    continue
                try:
                    raw = await self.market.get(f"{GAMMA}/markets/{t.market_id}")
                    payouts = resolution(raw, t)
                    if payouts is not None:
                        self.identify(t, payouts)
                except Exception as exc:
                    r.store.put("settlement_error", type(exc).__name__)
            for claim in list(r.business["claims"].values()):
                if claim["state"] in {"CREDITED", "FAILED"}:
                    continue
                try:
                    await self.advance(claim)
                except Exception as exc:
                    claim["error"] = (
                        str(exc)[:180] if isinstance(exc, ValueError) else type(exc).__name__
                    )
                    r.store.put("business", r.business)
        finally:
            self.busy = False
