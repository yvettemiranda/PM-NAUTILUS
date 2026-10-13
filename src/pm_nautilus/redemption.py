"""Condition-scoped settlement rights and recoverable Polygon redemption.

The RPC signer is constructed only behind the explicit server LIVE gate. EOA and
single-owner Safe are supported; no account abstraction or manual positions are claimed.
"""

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import closing
from functools import partial
from pathlib import Path
from threading import Lock
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
APPROVAL_KEY = "redemption_approval"
ZERO = "0x" + "00" * 20
# Stable workers let Web3's per-thread HTTP sessions reuse their connections.
_RPC_READ_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pm-polygon-read")
_ACCOUNT_LOCKS = {}
_ACCOUNT_LOCKS_GUARD = Lock()
RESOLUTION_CONCURRENCY = 4


class RedemptionCheckError(ValueError):
    """A static, application-owned message that is safe to persist for the UI."""


def _account_lock(path):
    # The process lock excludes another application process. This mutex also
    # covers authorization workers and owner-loop redemption preparations.
    key = str(Path(path).resolve())
    with _ACCOUNT_LOCKS_GUARD:
        return _ACCOUNT_LOCKS.setdefault(key, Lock())


def _nonce_key(wallet):
    address = getattr(getattr(wallet, "signer", None), "address", None)
    return f"polygon_nonce_floor:{address.lower()}" if isinstance(address, str) else None


def _nonce(pending):
    value = pending.get("nonce")
    if type(value) is not int or value < 0:
        raise RedemptionCheckError("链上交易 nonce 记录无效，须先核对原交易")
    return value


def _pending_owners(db):
    """Existing durable raw transactions are the account's nonce ownership."""
    row = db.execute("SELECT value FROM meta WHERE key='business'").fetchone()
    business = json.loads(row[0]) if row else {}
    if business.get("_storage_version") == 2:
        claims = (
            (key, json.loads(payload))
            for key, payload in db.execute(
                "SELECT key,payload FROM business_parts WHERE collection='claims'"
            )
        )
    else:
        claims = business.get("claims", {}).items()
    owners = [(("CLAIM", key), value) for key, value in claims if value.get("state") == "SUBMITTED"]
    row = db.execute("SELECT value FROM meta WHERE key=?", (APPROVAL_KEY,)).fetchone()
    approval = json.loads(row[0]) if row else None
    if approval is not None:
        if not isinstance(approval, dict):
            raise RedemptionCheckError("赎回授权记录无效，不能准备新交易")
        if approval.get("status") == "ACTIVE":
            owners.append((("APPROVAL", None), approval.get("pending")))
    return owners


def _check_account_owner(db, owner, *, preparing=False, pending=None):
    others = [(key, value) for key, value in _pending_owners(db) if key != owner]
    if preparing and others:
        raise RedemptionCheckError("账户已有待确认链上交易，须先继续核对原交易")
    if pending is not None:
        nonce = _nonce(pending)
        if any(value is None or _nonce(value) == nonce for _, value in others):
            raise RedemptionCheckError("账户待确认交易 nonce 冲突，须先核对原交易")


def _check_prepared_nonce(db, wallet, pending):
    nonce = _nonce(pending)
    key = _nonce_key(wallet)
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone() if key else None
    if row is not None:
        floor = json.loads(row[0])
        if type(floor) is not int or floor < 0 or nonce <= floor:
            raise RedemptionCheckError("RPC nonce 未超过本账户已确认交易，请稍后重新核对")


def _consume_nonce(db, wallet, pending):
    key = _nonce_key(wallet)
    nonce = _nonce(pending)
    if key is None:
        return
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    previous = json.loads(row[0]) if row else -1
    if type(previous) is not int:
        raise RedemptionCheckError("本账户已确认 nonce 记录无效")
    db.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(max(previous, nonce))),
    )


async def _finish_rpc(operation, *, cleanup_cancelled_result=None):
    """Join uninterruptible RPC workers before releasing lifecycle ownership."""
    task = asyncio.create_task(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled() and task.exception() is None and cleanup_cancelled_result:
            cleanup_cancelled_result(task.result())
        raise


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
        if type(self.signature_type) is not int or self.signature_type not in (0, 2):
            raise RedemptionCheckError("当前赎回支持 EOA(0) 与单签 Safe(2)，须先核对实际钱包类型")
        if self.signature_type == 0 and self.funder != self.signer.address:
            raise RedemptionCheckError("EOA资金账户必须与签名账户相同")
        self.auto_approve = settings.get("auto_approve_redemption") is True
        self.ctf = self.w3.eth.contract(address=CTF, abi=TOKEN_ABI)

    @staticmethod
    def _parallel_reads(calls):
        """Read independent RPC facts concurrently, with no cache or write calls."""
        if not calls:
            return []
        # Web3's HTTPProvider keeps a separate requests session per worker thread.
        # Bound concurrency avoids a large burst against a rate-limited Polygon RPC.
        futures = [_RPC_READ_POOL.submit(call) for call in calls]
        wait(futures)
        return [future.result() for future in futures]

    def preflight(self):
        if self.w3.eth.chain_id != 137:
            raise RedemptionCheckError("RPC不是Polygon主网")
        contracts = [
            Web3.to_checksum_address(address) for address in (CTF, PUSD, *ADAPTERS.values())
        ]
        # EOA signer and funder are the same address; one fresh read covers both.
        addresses = list(dict.fromkeys([*contracts, self.signer.address, self.funder]))
        codes = dict(
            zip(
                addresses,
                self._parallel_reads(
                    [partial(self.w3.eth.get_code, address) for address in addresses]
                ),
                strict=True,
            )
        )
        if any(not codes[address] for address in contracts):
            raise RedemptionCheckError("官方合约地址没有部署代码")
        if codes[self.signer.address]:
            raise RedemptionCheckError("签名账户含有合约或委托代码，需先单独核对")
        if self.signature_type == 2:
            if not codes[self.funder]:
                raise RedemptionCheckError("资金地址不是已部署的Safe")
            safe = self.w3.eth.contract(address=self.funder, abi=SAFE_ABI)
            owners, threshold = self._parallel_reads(
                [safe.functions.getOwners().call, safe.functions.getThreshold().call]
            )
            if (
                threshold != 1
                or len(owners) != 1
                or owners[0].lower() != self.signer.address.lower()
            ):
                raise RedemptionCheckError("Safe须为本签名账户可执行的单签钱包")
        elif codes[self.funder]:
            raise RedemptionCheckError("EOA资金地址含有合约代码")

    def adapter_approvals(self):
        operators = tuple(ADAPTERS.items())
        values = self._parallel_reads(
            [
                self.ctf.functions.isApprovedForAll(
                    self.funder, Web3.to_checksum_address(operator)
                ).call
                for _, operator in operators
            ]
        )
        approvals = {
            neg_risk: value for (neg_risk, _), value in zip(operators, values, strict=True)
        }
        if any(type(value) is not bool for value in approvals.values()):
            raise RedemptionCheckError("赎回授权查询结果无效")
        return approvals

    def buy_preflight(self):
        self.preflight()
        if not all(self.adapter_approvals().values()):
            raise RedemptionCheckError("赎回适配器授权尚未确认，不能新买入")

    def owned_balances(self, claim):
        return self.token_balances(claim["payouts"])

    def token_balances(self, token_ids):
        """Read both outcomes from the CTF contract before a new LIVE buy."""
        ids = tuple(token_ids)
        balances = self._parallel_reads(
            [self.ctf.functions.balanceOf(self.funder, int(tid)).call for tid in ids]
        )
        return dict(zip(ids, balances, strict=True))

    def prepare(self, claim):
        self.preflight()
        balances = self.owned_balances(claim)
        if any(balances[tid] != claim["rights"].get(tid, 0) for tid in balances):
            raise RedemptionCheckError("同Condition余额与本程序权利不一致，拒绝覆盖手动资产")
        condition = bytes.fromhex(claim["condition_id"][2:])
        den = self.ctf.functions.payoutDenominator(condition).call()
        if den == 0:
            raise RedemptionCheckError("链上尚未正式结算")
        nums = [self.ctf.functions.payoutNumerators(condition, i).call() for i in (0, 1)]
        if [n * SCALE // den for n in nums] != list(claim["payouts"].values()):
            raise RedemptionCheckError("链上结算比例与官方市场结果不一致")
        target = Web3.to_checksum_address(ADAPTERS[claim["neg_risk"]])
        approved = self.ctf.functions.isApprovedForAll(self.funder, target).call()
        operation = "REDEEM"
        if not approved:
            if not self.auto_approve:
                raise RedemptionCheckError("缺少赎回适配器授权；账户配置后方可开启自动授权")
            call = self.ctf.functions.setApprovalForAll(target, True)
            target = CTF
            operation = "APPROVE"
        else:
            contract = self.w3.eth.contract(address=target, abi=REDEEM_ABI)
            call = contract.functions.redeemPositions(PUSD, bytes(32), condition, [1, 2])
        return self._sign_contract_call(target, call._encode_transaction_data(), operation)

    def prepare_approval(self, neg_risk):
        if type(neg_risk) is not bool:
            raise RedemptionCheckError("赎回授权对象无效")
        self.preflight()
        target = Web3.to_checksum_address(ADAPTERS[neg_risk])
        approved = self.ctf.functions.isApprovedForAll(self.funder, target).call()
        if type(approved) is not bool:
            raise RedemptionCheckError("赎回授权查询结果无效")
        if approved:
            return None
        call = self.ctf.functions.setApprovalForAll(target, True)
        return {
            **self._sign_contract_call(CTF, call._encode_transaction_data(), "APPROVE"),
            "neg_risk": neg_risk,
            "operator": target,
        }

    def _sign_contract_call(self, target, data, operation):
        target = Web3.to_checksum_address(target)
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
            raise RedemptionCheckError("签名账户POL不足以支付链上手续费")
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
                raise RedemptionCheckError("RPC返回的交易哈希不一致")
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


class RedemptionApprovalService:
    """Explicit, durable authorization of both CTF redemption operators."""

    def __init__(self, wallet, ledger_path):
        self.wallet = wallet
        self.ledger_path = Path(ledger_path)

    def _identity(self):
        return {
            "signer": self.wallet.signer.address,
            "funder": self.wallet.funder,
            "signatureType": self.wallet.signature_type,
        }

    def _read(self):
        try:
            with closing(
                sqlite3.connect(self.ledger_path.resolve().as_uri() + "?mode=rw", uri=True)
            ) as db:
                row = db.execute("SELECT value FROM meta WHERE key=?", (APPROVAL_KEY,)).fetchone()
            record = json.loads(row[0]) if row else None
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise RedemptionCheckError("赎回授权记录无法读取") from exc
        if record is not None:
            if (
                not isinstance(record, dict)
                or record.get("version") != 1
                or record.get("status") not in {"ACTIVE", "COMPLETE", "FAILED"}
                or record.get("identity") != self._identity()
                or not isinstance(record.get("remaining"), list)
                or any(type(value) is not bool for value in record["remaining"])
            ):
                raise RedemptionCheckError("赎回授权记录与当前钱包不一致")
            pending = record.get("pending")
            if pending is not None:
                if (
                    not isinstance(pending, dict)
                    or pending.get("operation") != "APPROVE"
                    or type(pending.get("neg_risk")) is not bool
                    or not record["remaining"]
                    or pending["neg_risk"] != record["remaining"][0]
                    or pending.get("operator", "").lower() != ADAPTERS[pending["neg_risk"]].lower()
                    or not isinstance(pending.get("raw_tx"), str)
                    or not isinstance(pending.get("tx_hash"), str)
                ):
                    raise RedemptionCheckError("待确认赎回授权记录无效")
                try:
                    if (
                        Web3.to_hex(Web3.keccak(hexstr=pending["raw_tx"])).lower()
                        != pending["tx_hash"].lower()
                    ):
                        raise ValueError("Transaction hash mismatch")
                except (ValueError, TypeError):
                    raise RedemptionCheckError("待确认赎回授权交易无效") from None
        return record

    def _write(self, record, *, consumed=None):
        payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        try:
            with closing(
                sqlite3.connect(self.ledger_path.resolve().as_uri() + "?mode=rw", uri=True)
            ) as db:
                db.execute("PRAGMA synchronous=FULL")
                with db:
                    if consumed is not None:
                        _consume_nonce(db, self.wallet, consumed)
                    db.execute(
                        "INSERT INTO meta(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (APPROVAL_KEY, payload),
                    )
        except (OSError, sqlite3.Error) as exc:
            raise RedemptionCheckError("赎回授权记录无法保存，未广播新交易") from exc

    def _public(self, record, approvals, *, network_error=False):
        pending = record.get("pending") if record else None
        if record and record["status"] == "FAILED":
            status = "FAILED"
        elif pending:
            status = "PENDING"
        elif all(approvals.values()) and (record is None or record["status"] == "COMPLETE"):
            status = "APPROVED"
        else:
            status = "MISSING"
        return {
            "status": status,
            "standardApproved": approvals[False],
            "negRiskApproved": approvals[True],
            "pendingTxHash": pending["tx_hash"] if pending else None,
            "networkError": network_error,
        }

    def status(self):
        self.wallet.preflight()
        record = self._read()
        approvals = self.wallet.adapter_approvals()
        return self._public(record, approvals)

    def start(self):
        """Only call after a separate explicit approval action for both operators."""
        with _account_lock(self.ledger_path):
            return self._start()

    def _check_owner(self, *, preparing=False, pending=None):
        with closing(
            sqlite3.connect(self.ledger_path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as db:
            db.execute("BEGIN")
            _check_account_owner(db, ("APPROVAL", None), preparing=preparing, pending=pending)
            if preparing and pending is not None:
                _check_prepared_nonce(db, self.wallet, pending)

    def _start(self):
        self.wallet.preflight()
        record = self._read()
        if record is not None and record["status"] == "ACTIVE":
            return self._advance()
        self._check_owner(preparing=True)
        approvals = self.wallet.adapter_approvals()
        if all(approvals.values()) and (record is None or record["status"] != "ACTIVE"):
            record = {
                "version": 1,
                "status": "COMPLETE",
                "identity": self._identity(),
                "remaining": [],
                "pending": None,
            }
            self._write(record)
            return self._public(record, approvals)
        record = {
            "version": 1,
            "status": "ACTIVE",
            "identity": self._identity(),
            "remaining": [neg_risk for neg_risk in (False, True) if not approvals[neg_risk]],
            "pending": None,
        }
        self._write(record)
        return self._advance()

    def advance(self):
        """Resume a previously authorized transaction without replacing its nonce."""
        with _account_lock(self.ledger_path):
            return self._advance()

    def _advance(self):
        self.wallet.preflight()
        record = self._read()
        approvals = self.wallet.adapter_approvals()
        if record is None or record["status"] != "ACTIVE":
            return self._public(record, approvals)
        pending = record["pending"]
        if pending:
            try:
                result = self.wallet.receipt(pending)
            except Exception:
                return self._public(record, approvals, network_error=True)
            if result is None:
                self._check_owner(pending=pending)
                try:
                    self.wallet.broadcast(pending)
                except Exception:
                    return self._public(record, approvals, network_error=True)
                return self._public(record, approvals)
            if result.get("status") == "FAILED":
                record["status"] = "FAILED"
                self._write(record, consumed=pending)
                return self._public(record, approvals)
            if result.get("status") != "APPROVED":
                raise RedemptionCheckError("赎回授权回执无效")
            record["pending"] = None
            record["remaining"].pop(0)
            self._write(record, consumed=pending)
            approvals = self.wallet.adapter_approvals()
        while record["remaining"]:
            neg_risk = record["remaining"][0]
            if approvals[neg_risk]:
                record["remaining"].pop(0)
                self._write(record)
                continue
            self._check_owner(preparing=True)
            prepared = self.wallet.prepare_approval(neg_risk)
            if prepared is None:
                approvals = self.wallet.adapter_approvals()
                if not approvals[neg_risk]:
                    raise RedemptionCheckError("赎回授权查询结果不一致")
                continue
            record["pending"] = prepared
            self._check_owner(preparing=True, pending=prepared)
            self._write(record)
            try:
                self.wallet.broadcast(prepared)
            except Exception:
                return self._public(record, approvals, network_error=True)
            return self._public(record, approvals)
        approvals = self.wallet.adapter_approvals()
        if not all(approvals.values()):
            raise RedemptionCheckError("赎回授权链上结果不完整")
        record["status"] = "COMPLETE"
        self._write(record)
        return self._public(record, approvals)


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
        targets = r.business["targets"]
        settled_length = len(r.business["settled"])
        try:
            with r.store.transaction():
                r.business["claims"][token.condition_id] = claim
                r.business["settled"].append(token.condition_id)
                r.business["targets"] = {
                    k: v for k, v in targets.items() if v["event_id"] != token.event_id
                }
                r.store.put("business", r.business)
        except BaseException:
            r.business["claims"].pop(token.condition_id, None)
            del r.business["settled"][settled_length:]
            r.business["targets"] = targets
            raise
        r.refresh_eligibility()

    async def advance(self, claim):
        r = self.r
        if claim["generation"] != r.store.generation or claim["state"] == "CREDITED":
            return
        if r.mode == "TEST":
            if claim["state"] == "IDENTIFIED":
                # Separate observable step; no cash yet. Failed persistence
                # must leave both projections in the original state.
                self._save_claim(claim, dict(claim, state="CONFIRMED"))
                return
            r.settlement_fill(claim)
            return
        lock = _account_lock(r.store.path)
        await _finish_rpc(
            asyncio.to_thread(lock.acquire), cleanup_cancelled_result=lambda _: lock.release()
        )
        try:
            await self._advance_live(claim)
        finally:
            lock.release()

    def _save_claim(self, claim, change, *, consumed=None):
        before = dict(claim)
        try:
            with self.r.store.transaction():
                claim.clear()
                claim.update(change)
                if consumed is not None:
                    _consume_nonce(self.r.store.db, self.wallet, consumed)
                self.r.store.put("business", self.r.business)
        except BaseException:
            claim.clear()
            claim.update(before)
            raise

    async def _advance_live(self, claim):
        r = self.r
        if claim["generation"] != r.store.generation or claim["state"] in {"CREDITED", "FAILED"}:
            return
        owner = ("CLAIM", claim["condition_id"])
        if claim["state"] in {"IDENTIFIED", "RETRY"}:
            if any(key != owner for key, _ in _pending_owners(r.store.db)):
                return

            def adopt(prepared):
                _check_prepared_nonce(r.store.db, self.wallet, prepared)
                self._save_claim(claim, dict(claim, **prepared, state="SUBMITTED", error=None))

            prepared = await _finish_rpc(
                asyncio.to_thread(self.wallet.prepare, dict(claim)), cleanup_cancelled_result=adopt
            )
            # Adoption runs on the owner loop, including cancellation after the
            # worker signs. A failed save cannot be followed by a broadcast.
            adopt(prepared)
        if claim["state"] == "SUBMITTED":
            result = await _finish_rpc(asyncio.to_thread(self.wallet.receipt, dict(claim)))
            if result is None:
                _check_account_owner(r.store.db, owner, pending=claim)
                await _finish_rpc(asyncio.to_thread(self.wallet.broadcast, dict(claim)))
                return  # Same raw bytes only, even after a timeout/restart.
            updated = dict(claim)
            if result["status"] == "APPROVED":
                updated["approval_hash"] = updated.pop("tx_hash")
                updated.pop("raw_tx", None)
                updated["state"] = "IDENTIFIED"
            elif result["status"] == "FAILED":
                updated["state"] = "FAILED"
                updated["error"] = "链上执行失败或权利未全部赎回；需要核对"
            elif result["status"] != "CONFIRMED":
                raise RedemptionCheckError("赎回交易回执无效")
            elif result["amount"] != claim["amount"]:
                updated["state"] = "FAILED"
                updated["error"] = "实际回款与自有应收不一致"
            else:
                updated["state"] = "CONFIRMED"
            # The receipt confirms consumption of the account nonce even when
            # asset or amount checks correctly prevent ledger credit.
            self._save_claim(claim, updated, consumed=dict(claim))
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
            generation = r.store.generation
            tokens = iter(
                r.tokens[cycle["token_id"]]
                for cycle in list(r.business["cycles"].values())
                if r.tokens[cycle["token_id"]].condition_id not in r.business["claims"]
            )

            async def query_resolutions():
                for token in tokens:
                    try:
                        raw = await self.market.get(f"{GAMMA}/markets/{token.market_id}")
                        payouts = resolution(raw, token)
                        if payouts is not None and r.store.generation == generation:
                            self.identify(token, payouts)
                    except Exception as exc:
                        r.store.put("settlement_error", type(exc).__name__)

            queries = [
                asyncio.create_task(query_resolutions()) for _ in range(RESOLUTION_CONCURRENCY)
            ]
            try:
                await asyncio.gather(*queries)
            finally:
                for task in queries:
                    task.cancel()
                await asyncio.gather(*queries, return_exceptions=True)
            for claim in list(r.business["claims"].values()):
                if claim["state"] in {"CREDITED", "FAILED"}:
                    continue
                try:
                    await self.advance(claim)
                except Exception as exc:
                    claim["error"] = (
                        str(exc) if isinstance(exc, RedemptionCheckError) else type(exc).__name__
                    )
                    r.store.put("business", r.business)
        finally:
            self.busy = False
