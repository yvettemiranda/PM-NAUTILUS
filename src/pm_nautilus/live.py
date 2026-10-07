"""Narrow extension of the pinned official Polymarket V2 execution adapter.

Official signing/HTTP/WebSocket/report infrastructure is retained. PM adds durable
ownership, bounded quote buys, confirmed-only fills, and conservative terminal checks.
"""

import asyncio
import json
import os
import re
import stat
from dataclasses import asdict
from decimal import Decimal
from hashlib import sha256
from urllib.parse import urlsplit

import msgspec
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import (
    ApiCreds,
    MarketOrderArgsV2,
    PartialCreateOrderOptions,
    TradeParams,
)
from nautilus_trader.adapters.polymarket.execution import PolymarketExecutionClient
from nautilus_trader.adapters.polymarket.config import PolymarketExecClientConfig
from nautilus_trader.adapters.polymarket.providers import (
    PolymarketInstrumentProvider,
    PolymarketInstrumentProviderConfig,
)
from nautilus_trader.adapters.polymarket.common.credentials import PolymarketWebSocketAuth
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.reports import OrderStatusReport
from nautilus_trader.model.currencies import pUSD
from nautilus_trader.model.enums import OrderSide, LiquiditySide
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import ClientOrderId, VenueOrderId, TradeId
from nautilus_trader.model.objects import Money, AccountBalance

from .config import SCALE, micros
from .execution import TestExecution
from .live_account_guard import audit_open_orders, condition_inventory_matches
from .native import price, quantity
from .rules import (
    SUPPORTED_CLOB_TICKS,
    cost,
    ceil_div,
    static_reason,
    Fees,
    preview,
    arbitrate,
    signed_buy_notional,
)


def validate_settings(settings):
    """Validate credentials loaded from either the legacy file or a locked web vault."""
    if not isinstance(settings, dict):
        raise ValueError("LIVE凭据文件JSON须为对象")
    for key in (
        "private_key",
        "api_key",
        "api_secret",
        "passphrase",
        "funder",
        "rpc_url",
        "signature_type",
    ):
        if key not in settings or settings[key] is None:
            raise ValueError(f"LIVE凭据缺少 {key}")
    if type(settings["signature_type"]) is not int or settings["signature_type"] not in (0, 2):
        raise ValueError("须核对实际钱包；当前支持EOA或单签Safe的完整交易/赎回路径")
    if not isinstance(settings["private_key"], str) or not re.fullmatch(
        r"0x[0-9a-fA-F]{64}", settings["private_key"]
    ):
        raise ValueError("LIVE签名密钥格式无效")
    if not isinstance(settings["funder"], str) or not re.fullmatch(
        r"0x[0-9a-fA-F]{40}", settings["funder"]
    ):
        raise ValueError("LIVE资金地址格式无效")
    if any(
        not isinstance(settings[key], str) or not settings[key].strip()
        for key in ("api_key", "api_secret", "passphrase")
    ):
        raise ValueError("LIVE CLOB API 凭据格式无效")
    try:
        rpc_url = urlsplit(settings["rpc_url"])
    except (TypeError, ValueError):
        raise ValueError("LIVE Polygon RPC URL 无效") from None
    if rpc_url.scheme != "https" or not rpc_url.hostname:
        raise ValueError("LIVE Polygon RPC 必须使用 HTTPS")
    if (
        "auto_approve_redemption" in settings
        and type(settings["auto_approve_redemption"]) is not bool
    ):
        raise ValueError("LIVE 自动赎回授权设置无效")
    return settings


def load_settings(*, require_live_enabled: bool = True):
    if require_live_enabled and os.environ.get("PM_LIVE_ENABLED") != "true":
        raise ValueError("LIVE 未由服务器显式启用")
    path = os.environ.get("PM_LIVE_CREDENTIALS_FILE")
    if not path:
        raise ValueError("LIVE凭据文件路径未配置")
    try:
        # Check the opened inode, not a pathname that can be replaced between
        # stat() and read(). O_NONBLOCK prevents a FIFO from blocking at open().
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        with os.fdopen(os.open(path, flags), encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid not in {10001, os.geteuid()}
            ):
                raise ValueError("LIVE凭据文件须为权限0600、由UID10001或运行用户持有的普通文件")
            try:
                settings = json.load(stream)
            except (ValueError, UnicodeError):
                raise ValueError("LIVE凭据文件JSON无效") from None
    except OSError:
        raise ValueError("LIVE凭据文件无法安全读取，请检查路径、属主及文件类型") from None
    return validate_settings(settings)


def live_factory(settings, wallet):
    def create(owner):
        creds = ApiCreds(settings["api_key"], settings["api_secret"], settings["passphrase"])
        http = ClobClient(
            "https://clob.polymarket.com",
            chain_id=137,
            key=settings["private_key"],
            creds=creds,
            signature_type=settings["signature_type"],
            funder=settings["funder"],
        )
        config = PolymarketExecClientConfig(
            signature_type=settings["signature_type"],
            funder=settings["funder"],
            log_raw_ws_messages=False,
        )
        provider = PolymarketInstrumentProvider(
            http, owner.native.clock, PolymarketInstrumentProviderConfig(load_all=False)
        )
        return LiveExecution(
            owner,
            http,
            provider,
            PolymarketWebSocketAuth(creds.api_key, creds.api_secret, creds.api_passphrase),
            config,
            wallet=wallet,
        )

    return create


class _TrackedRetryManager:
    """Mark the boundary where a submitted order may reach the venue."""

    def __init__(self, inner, execution):
        self.inner = inner
        self.execution = execution

    async def run(self, name, details, *args, **kwargs):
        if name == "submit_order" and details:
            # RetryManager may schedule post_order in a worker thread. From this
            # point, cancellation cannot prove that no request reached the venue.
            self.execution._post_possible.add(str(details[0]))
        return await self.inner.run(name, details, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class _TrackedRetryPool:
    def __init__(self, inner, execution):
        self.inner = inner
        self.execution = execution

    async def acquire(self):
        return _TrackedRetryManager(await self.inner.acquire(), self.execution)

    async def release(self, manager):
        return await self.inner.release(manager.inner)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class LiveExecution(PolymarketExecutionClient):
    def __init__(self, owner, http, provider, auth, config, wallet=None):
        self.owner = owner
        self.wallet = wallet
        n = owner.native
        super().__init__(
            asyncio.get_running_loop(), http, n.bus, n.cache, n.clock, provider, auth, config, None
        )
        self._retry_manager_pool = _TrackedRetryPool(self._retry_manager_pool, self)
        self.ready = False
        self.open_orders_clear = False
        self.reconciliation_task = None
        self._release_verified = False
        self._pending_native = set()
        self._pending_conversions = set()
        self._submitting = {}
        self._post_possible = set()
        self._boot_intents = set(owner.store.intents())
        self._sync_lock = asyncio.Lock()
        # Historical snapshot only supports native journal reconstruction. No LIVE
        # order can pass until current account and own venue state are reconciled.
        amount = Money(Decimal(owner.store.get("last_cash", 0)) / SCALE, pUSD)
        self.generate_account_state(
            [AccountBalance(amount, Money(0, pUSD), amount)], [], True, n.clock.timestamp_ns()
        )

    def _send_order_event(self, event):
        r = self.owner
        i = r.store.intent(getattr(event, "client_order_id", ""))
        if not i or i["generation"] != r.store.generation:
            return
        if (
            type(event).__name__ in {"OrderCanceled", "OrderExpired"}
            and i["kind"] != "SETTLEMENT"
            and not self._release_verified
        ):
            i["venue_terminal"] = True
            r.store.save_intent(event.client_order_id, i)
            return  # Await all associated trades; cancellation is not settlement.
        if isinstance(event, OrderFilled):
            key = f"fill:{event.client_order_id}:{event.trade_id}"
            if (
                key in self._pending_native
                or r.store.db.execute(
                    "SELECT 1 FROM native_events WHERE event_id=?", (key,)
                ).fetchone()
            ):
                return
            self._pending_native.add(key)
        r.freeze_fill_rules(event)
        r.store.journal(event)
        if (
            type(event).__name__ == "OrderSubmitted"
            and str(event.client_order_id) in self._submitting
        ):
            self._submitting[str(event.client_order_id)] = True
        super()._send_order_event(event)

    def _handle_ws_order_msg(self, msg, wait_for_ack):
        oid = self._cache.client_order_id(VenueOrderId(msg.id))
        if oid and self.owner.store.intent(oid):
            super()._handle_ws_order_msg(msg, wait_for_ack)

    def _handle_ws_trade_msg(self, msg, wait_for_ack):
        self.ingest_trade(msg)

    def ingest_trade(self, msg):
        status = getattr(msg.status, "value", msg.status)
        r = self.owner
        for venue in msg.get_filled_user_order_ids(self._wallet_address, self._api_key):
            oid = self._cache.client_order_id(VenueOrderId(venue))
            if oid is None:
                continue
            i = r.store.intent(oid)
            if not i or i["generation"] != r.store.generation:
                continue
            pending = set(i.get("pending_trades", []))
            if status not in {"CONFIRMED", "FAILED"}:
                pending.add(msg.id)
            else:
                pending.discard(msg.id)
            if status == "FAILED":
                i.setdefault("failed_trades", {})[msg.id] = micros(msg.last_qty(venue))
            i["pending_trades"] = sorted(pending)
            r.store.save_intent(oid, i)
            if status != "CONFIRMED":
                continue
            t = r.tokens[i["token_id"]]
            o = self._cache.order(oid)
            if o is None or msg.market != t.condition_id or msg.get_asset_id(venue) != t.token_id:
                raise ValueError("实际成交身份与本程序意图不匹配")
            if o.is_quote_quantity and str(oid) not in self._pending_conversions:
                base = i.get("base_quantity")
                if not base:
                    raise ValueError("恢复订单缺少持久化的签名份额，不能猜测转换")
                self._pending_conversions.add(str(oid))
                self._send_quote_to_base_update(o, VenueOrderId(venue), quantity(base))
            gross = micros(msg.last_qty(venue))
            p = micros(msg.last_px(venue))
            if gross <= 0 or not 0 < p < SCALE:
                raise ValueError("无效真实成交数值")
            if (o.side == OrderSide.BUY and p > i["limit"]) or (
                o.side == OrderSide.SELL and p < i["limit"]
            ):
                r.pause()
                r.business["recovery_error"] = "实际成交突破订单限价，已暂停新买，仍记录真实回报"
            fees = Fees(**i["fees"])
            fee = fees.fee(gross, p) if msg.liquidity_side() == LiquiditySide.TAKER else 0
            # CLOB V2 fees are charged in pUSD on top of the BUY notional. The
            # reported matched shares are the shares received; do not infer a
            # smaller position by converting a cash fee into shares.
            net = gross
            amount = cost(p, gross) + fee if o.side == OrderSide.BUY else cost(p, gross) - fee
            # Real venue trade identity is stable across REST/WS/status transitions.
            trade_id = TradeId(sha256(f"{msg.id}:{venue}".encode()).hexdigest()[:32])
            self.generate_order_filled(
                strategy_id=o.strategy_id,
                instrument_id=o.instrument_id,
                client_order_id=oid,
                venue_order_id=VenueOrderId(venue),
                venue_position_id=None,
                trade_id=trade_id,
                order_side=o.side,
                order_type=o.order_type,
                last_qty=quantity(net),
                last_px=price(p),
                quote_currency=pUSD,
                commission=Money(Decimal(fee) / SCALE, pUSD),
                liquidity_side=msg.liquidity_side(),
                ts_event=int(Decimal(msg.match_time) * 1_000_000_000),
                info={
                    "pm_gross": gross,
                    "pm_net": net,
                    "pm_amount": amount,
                    "pm_fee": fee,
                    "pm_kind": i["kind"],
                    "pm_generation": i["generation"],
                    "venue_trade_id": msg.id,
                    "transaction_hash": getattr(msg, "transaction_hash", None),
                    "confirmation": "CONFIRMED",
                },
            )

    async def _submit_order(self, command):
        r = self.owner
        o = command.order
        i = r.store.intent(o.client_order_id)
        if not i:
            return
        if i["kind"] == "SETTLEMENT":
            claim = r.business["claims"].get(r.tokens[i["token_id"]].condition_id)
            if not claim or claim["state"] != "CONFIRMED":
                raise ValueError("只有实际回款确认后才能关闭赎回仓位")
            TestExecution.submit_order(self, command)
            return
        oid = str(o.client_order_id)
        self._submitting[oid] = False
        try:
            tick = r.tokens[i["token_id"]].tick
            if tick not in SUPPORTED_CLOB_TICKS:
                r.store.put("status", "PAUSED")
                r.business["recovery_error"] = "市场价格档位超出锁定SDK支持范围；持仓仍待处理"
                r.store.put("business", r.business)
                self.deny(o, "市场价格档位超出锁定SDK支持范围")
                return
            if not self.ready:
                self.deny(o, "LIVE核对未完成")
                return
            if o.side == OrderSide.BUY and not self.buy_valid(o, i):
                self.deny(o, "发单前资格/资金/暂停状态已变化")
                return
            await super()._submit_order(command)
        except (Exception, asyncio.CancelledError) as exc:
            self._finish_failed_submit(o, exc)
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            self._submitting.pop(oid, None)
            self._post_possible.discard(oid)

    def _finish_failed_submit(self, order, exc, *, submitted=None):
        """Release only when the retry manager never reached the POST boundary."""
        r = self.owner
        oid = str(order.client_order_id)
        intent = r.store.intent(oid)
        self.ready = False
        self.open_orders_clear = False
        # A failed submit must stop new buys without issuing cancel requests for
        # other in-flight orders whose venue outcome still needs reconciliation.
        r.store.put("status", "PAUSED")
        if intent and not intent.get("terminal") and oid not in self._post_possible:
            reason = f"未向交易所提交：{type(exc).__name__}"
            was_submitted = self._submitting.get(oid) if submitted is None else submitted
            if was_submitted:
                self.generate_order_rejected(
                    order.strategy_id,
                    order.instrument_id,
                    order.client_order_id,
                    reason,
                    r.now,
                )
            else:
                self.deny(order, reason)
            r.store.put("live_error", f"订单 {oid} 提交前失败：{type(exc).__name__}")
        elif intent and not intent.get("terminal"):
            # An in-flight worker can complete after the awaiting task fails.
            # Never release this reservation or resubmit the order automatically.
            r.store.put("live_error", f"订单 {oid} 结果未确认：{type(exc).__name__}")

    def buy_valid(self, o, i):
        r = self.owner
        t = r.tokens[i["token_id"]]
        cycle = r.business["cycles"].get(t.event_id)
        basic = (
            r.status == "RUNNING"
            and r.books[t.token_id].ready
            and static_reason(t, r.preferences, r.now, r.official_tags) is None
            and t.event_id not in r.business["banned"]
            and r.cash() >= r.held_cash()
            and (
                not cycle
                or (
                    cycle["token_id"] == t.token_id
                    and not cycle["sold"]
                    and cycle["stop"]["state"] == "WATCHING"
                    and cycle["spent"] + i["cash"] <= cycle["budget"]
                )
            )
            and not o.is_pending_cancel
            and not o.is_closed
        )
        if not basic or i["limit"] != r.preferences.max_price or i["fees"] != asdict(t.fees):
            return False
        ids = {t.token_id} if cycle else r.events.get(t.event_id, set())
        if not ids or any(not r.books[tid].ready for tid in ids):
            return False
        opportunities = []
        for tid in ids:
            token = r.tokens[tid]
            book = r.books[tid]
            if static_reason(token, r.preferences, r.now, r.official_tags):
                continue
            p = preview(
                token,
                r.preferences,
                list(book.bid.external.items()),
                list(book.ask.external.items()),
                i["cash"],
                cycle["budget"] if cycle else r.preferences.budget,
            )
            if p:
                opportunities.append(p)
        winner = arbitrate(opportunities, r.preferences, r.now)
        return bool(winner and winner.token.token_id == t.token_id)

    def deny(self, o, reason):
        self.generate_order_denied(
            o.strategy_id, o.instrument_id, o.client_order_id, reason, self._clock.timestamp_ns()
        )

    async def _submit_market_order(self, command, instrument):
        o = command.order
        r = self.owner
        i = r.store.intent(o.client_order_id)
        if o.side != OrderSide.BUY or not o.is_quote_quantity:
            self.deny(o, "仅使用有现金上限的BUY市价FAK")
            return
        tick = r.tokens[i["token_id"]].tick
        if tick not in SUPPORTED_CLOB_TICKS:
            self.deny(o, "锁定SDK不支持此市场价格档位")
            return
        executable_limit = min(i.get("execution_limit", i["limit"]) // tick * tick, SCALE - tick)
        if executable_limit <= 0:
            self.deny(o, "当前价格上限内没有合法 tick")
            return
        if self.wallet is None:
            self.deny(o, "LIVE钱包未连接，无法核对赎回授权")
            return
        try:
            await asyncio.to_thread(self.wallet.buy_preflight)
        except Exception as exc:
            r.store.put("status", "PAUSED")
            r.store.put("live_error", f"买入前钱包/赎回授权核对失败：{type(exc).__name__}")
            self.deny(o, "买入前钱包/赎回授权核对失败")
            return
        notional = signed_buy_notional(i["cash"], Fees(**i["fees"]), tick, executable_limit)
        if notional <= 0 or notional * SCALE // executable_limit < r.tokens[i["token_id"]].min_size:
            self.deny(o, "含费预算不足以满足交易所最小订单份额")
            return
        args = MarketOrderArgsV2(
            token_id=i["token_id"],
            amount=float(Decimal(notional) / SCALE),
            side="BUY",
            price=float(Decimal(executable_limit) / SCALE),
            order_type="FAK",
            user_usdc_balance=float(Decimal(r.cash()) / SCALE),
        )
        signed = await asyncio.to_thread(
            self._http_client.create_market_order,
            args,
            options=PartialCreateOrderOptions(neg_risk=r.tokens[i["token_id"]].neg_risk),
        )
        # The pinned SDK has no maxSpend option and rounds the maker amount to
        # cents. Inspect the signed facts before allowing any POST boundary.
        maker = int(signed.makerAmount)
        taker = int(signed.takerAmount)
        if (
            maker <= 0
            or maker > notional
            or taker < r.tokens[i["token_id"]].min_size
            or maker * SCALE > taker * executable_limit
        ):
            self.deny(o, "签名订单金额/价格/最小份额未通过含费预算核对")
            return
        if not self.buy_valid(o, i):
            self.deny(o, "签名期间控制/资格发生变化")
            return
        if not await self._check_buy_account(r.tokens[i["token_id"]]):
            self.deny(o, "账户开放挂单或同Condition份额核对未通过")
            return
        if not self.buy_valid(o, i):
            self.deny(o, "核对期间控制/资格发生变化")
            return
        self.generate_order_submitted(o.strategy_id, o.instrument_id, o.client_order_id, r.now)
        venue = self._expected_venue_order_id(signed, neg_risk=r.tokens[i["token_id"]].neg_risk)
        await self._post_signed_order(
            o,
            signed,
            order_type_override="FAK",
            # At a better execution price the venue may deliver more than the
            # signed minimum takerAmount. Nautilus needs a durable upper bound
            # until the FAK's final trades and cancellation are reconciled.
            base_quantity=quantity(ceil_div((maker + 1) * SCALE, tick)),
            expected_venue_order_id=venue,
        )

    async def _post_signed_order(self, order, signed_order, **kwargs):
        r = self.owner
        i = r.store.intent(order.client_order_id)
        venue = kwargs.get("expected_venue_order_id")
        if venue is None:
            # Submission was generated, but no venue identity means no POST is
            # permitted. Reject the submitted order so its intent is released.
            self._finish_failed_submit(
                order, ValueError("无法持久化订单确定性身份"), submitted=True
            )
            return
        i["venue_id"] = str(venue)
        i["submitted_ns"] = r.now
        if kwargs.get("base_quantity") is not None:
            i["base_quantity"] = micros(kwargs["base_quantity"].as_decimal())
        r.store.save_intent(order.client_order_id, i)
        self._cache.add_venue_order_id(order.client_order_id, venue)
        await super()._post_signed_order(order, signed_order, **kwargs)

    def _is_unknown_submit_result(self, exc):
        # The pinned retry manager also returns None after a cancelled POST,
        # without recording an exception. The worker thread may still complete.
        # A server error likewise cannot prove the venue refused the order.
        status = getattr(exc, "status_code", None)
        return (
            exc is None
            or super()._is_unknown_submit_result(exc)
            or (isinstance(status, int) and status >= 500)
        )

    def _handle_unknown_submit_result(
        self, order, expected_venue_order_id, reason, base_quantity=None
    ):
        super()._handle_unknown_submit_result(
            order, expected_venue_order_id, reason, base_quantity=base_quantity
        )
        self.ready = False
        self.open_orders_clear = False
        self.owner.store.put("status", "PAUSED")
        self.owner.store.put("live_error", f"订单 {order.client_order_id} 结果未确认")

    async def _update_account_state(self):
        confirmed_claims = {
            key
            for key, claim in self.owner.business["claims"].items()
            if claim["state"] == "CONFIRMED"
        }
        cutoff = self.owner.store.db.execute(
            "SELECT coalesce(max(seq),0) FROM native_events"
        ).fetchone()[0]
        await super()._update_account_state()
        # LiveExecutionEngine processes account messages on its queue.
        await asyncio.sleep(0)
        with self.owner.store.transaction():
            self.owner.store.put(
                "last_cash", int(Decimal(str(self._collateral_balance_pusd)) * SCALE)
            )
            self.owner.store.put("cash_covered_seq", cutoff)
            for key in confirmed_claims:
                self.owner.business["claims"][key]["cash_included"] = True
            self.owner.store.put("business", self.owner.business)

    async def sync_owned(self):
        async with self._sync_lock:
            await self._sync_owned()

    async def _check_open_orders(self):
        audit = await audit_open_orders(
            self._http_client, lambda: self.owner.store.intents(active_only=True)
        )
        self.open_orders_clear = audit.ok
        if not audit.ok:
            self.owner.pause()
            self.owner.store.put(
                "live_error",
                "账户存在程序外开放挂单" if audit.unknown_count else audit.error,
            )
        return audit.ok

    async def _check_buy_account(self, token):
        if not await self._check_open_orders():
            return False
        condition_ids = tuple(
            t.token_id for t in self.owner.tokens.values() if t.condition_id == token.condition_id
        )
        own = {}
        for cycle in self.owner.business["cycles"].values():
            held = self.owner.tokens.get(cycle["token_id"])
            if held and held.condition_id == token.condition_id:
                own[held.token_id] = own.get(held.token_id, 0) + cycle["quantity"]
        try:
            if self.wallet is None or len(condition_ids) != 2:
                raise ValueError("Incomplete inventory context")
            actual = await asyncio.to_thread(self.wallet.token_balances, condition_ids)
        except Exception:
            actual = {}
        if not condition_inventory_matches(condition_ids, actual, own):
            self.owner.pause()
            self.owner.store.put("live_error", "同Condition实际份额与程序持仓不一致")
            return False
        return True

    async def _sync_owned(self):
        r = self.owner
        intents = r.store.intents(active_only=True)
        poll_start = r.now
        if r.store.has_history():
            after_ns = min(
                [i["created"] for i in intents.values()]
                + [r.store.get("last_trade_poll", poll_start) - 300_000_000_000]
            )
            after = max(0, after_ns // 1_000_000_000 - 1)
            raw = await asyncio.to_thread(
                self._http_client.get_trades, params=TradeParams(after=after)
            )
            for trade in raw:
                self.ingest_trade(self._decoder_trade_report.decode(msgspec.json.encode(trade)))
            await asyncio.sleep(0)
            r.store.put("last_trade_poll", poll_start)
        filled = {}
        for oid in intents:
            rows = r.store.order_fills(oid)
            filled[oid] = (
                {e.info.get("venue_trade_id") for e in rows},
                sum(e.info.get("pm_gross", 0) for e in rows),
            )
        for oid, i in r.store.intents(active_only=True).items():
            if i["kind"] == "SETTLEMENT" or i.get("terminal"):
                continue
            o = self._cache.order(ClientOrderId(oid))
            venue = i.get("venue_id")
            if not venue:
                # No signed identity persisted means no post_order was permitted.
                if oid in self._boot_intents and o and not o.is_closed:
                    self.generate_order_rejected(
                        o.strategy_id,
                        o.instrument_id,
                        o.client_order_id,
                        "重启前未向交易所提交",
                        r.now,
                    )
                continue
            result = await asyncio.to_thread(self._http_client.get_order, venue)
            if not result:
                raise ValueError("订单结果不明，保留额度与Event锁")
            if str(result.get("asset_id")) != i["token_id"]:
                raise ValueError("订单查询身份不一致")
            associated = set(result.get("associate_trades") or [])
            confirmed, gross = filled.get(oid, (set(), 0))
            failed = i.get("failed_trades", {})
            # All matched quantity must be represented by finalized venue responses.
            # Missing/deferred records retain the reservation; never infer zero fill.
            matched = micros(result.get("size_matched", 0))
            status = str(result.get("status", "")).upper()
            if (
                i.get("pending_trades")
                or not associated.issubset(confirmed | failed.keys())
                or gross + sum(failed.values()) < matched
            ):
                continue
            if status in {"CANCELED", "CANCELLED", "MATCHED", "FILLED", "EXPIRED"}:
                i = r.store.intent(oid)
                i["verified_terminal"] = True
                r.store.save_intent(oid, i)
                if o and not o.is_closed:
                    self._release_verified = True
                    try:
                        self.generate_order_canceled(
                            o.strategy_id,
                            o.instrument_id,
                            o.client_order_id,
                            VenueOrderId(venue),
                            r.now,
                        )
                    finally:
                        self._release_verified = False
                elif o and o.is_closed:
                    with r.store.transaction():
                        i["terminal"] = True
                        r.store.save_intent(oid, i)
                        r.release_cycle(i["event_id"])
                        r.store.put("business", r.business)
        await asyncio.sleep(0)
        await self._update_account_state()

    async def generate_order_status_reports(self, command):
        await self.sync_owned()
        reports = []
        for oid, i in self.owner.store.intents().items():
            if not i.get("verified_terminal") or i["kind"] == "SETTLEMENT":
                continue
            o = self._cache.order(ClientOrderId(oid))
            if not o or not o.venue_order_id:
                continue
            now = self._clock.timestamp_ns()
            reports.append(
                OrderStatusReport(
                    account_id=self.account_id,
                    instrument_id=o.instrument_id,
                    venue_order_id=o.venue_order_id,
                    client_order_id=o.client_order_id,
                    order_side=o.side,
                    order_type=o.order_type,
                    time_in_force=o.time_in_force,
                    order_status=o.status,
                    quantity=o.quantity,
                    filled_qty=o.filled_qty,
                    report_id=UUID4(),
                    ts_accepted=i["created"],
                    ts_last=now,
                    ts_init=now,
                    price=getattr(o, "price", None),
                    avg_px=o.avg_px,
                )
            )
        return reports

    async def generate_fill_reports(self, command):
        # CONFIRMED facts are already sent through native OrderFilled during sync_owned.
        # Returning gross historical fills here would double-charge BUY shares/fees.
        await self.sync_owned()
        return []

    async def generate_position_status_reports(self, command):
        # The wallet may contain manual assets. Verify custody of our rights without
        # publishing whole-wallet quantities as our strategy's positions.
        positions = await self._fetch_user_positions(size_threshold=0)
        actual = {str(p["asset"]): micros(p["size"]) for p in positions}
        for c in self.owner.business["cycles"].values():
            t = self.owner.tokens[c["token_id"]]
            if (
                t.condition_id not in self.owner.business["settled"]
                and actual.get(t.token_id, 0) < c["quantity"]
            ):
                raise ValueError("账户实际份额不足以覆盖本程序持仓")
        return []

    async def generate_order_status_report(self, command):
        oid = command.client_order_id or self._cache.client_order_id(command.venue_order_id)
        if not oid or not self.owner.store.intent(oid):
            return None
        reports = await self.generate_order_status_reports(command)
        return next((x for x in reports if x.client_order_id == oid), None)

    async def activate(self):
        await self._connect()
        self._set_connected(True)
        ok = await self.owner.native.execution.reconcile_execution_state(timeout_secs=60)
        if not ok or not self.owner.validate()["ok"]:
            raise ValueError("LIVE原生执行核对失败")
        self.ready = True
        await self._check_open_orders()
        self.reconciliation_task = asyncio.create_task(self.maintain())

    async def maintain(self):
        while True:
            try:
                await self.sync_owned()
                await self.generate_position_status_reports(None)
                if not self.owner.validate()["ok"]:
                    raise ValueError("LIVE账本校验未通过")
                await self._check_open_orders()
                self.ready = True
                if self.open_orders_clear:
                    self.owner.store.put("live_error", None)
                self.owner.drain()
            except Exception as exc:
                self.ready = False
                self.open_orders_clear = False
                self.owner.pause()
                self.owner.store.put("live_error", type(exc).__name__)
            await asyncio.sleep(5)

    async def shutdown(self):
        self.ready = False
        if self.reconciliation_task:
            self.reconciliation_task.cancel()
            await asyncio.gather(self.reconciliation_task, return_exceptions=True)
            self.reconciliation_task = None
        self.owner.pause()
        engine = self.owner.native.execution
        # The pinned engine enqueues commands with call_soon_threadsafe. Let those
        # callbacks run, then dispatch all already accepted commands before waiting
        # for the adapter's network tasks. The journal retains unknown remote results.
        await asyncio.sleep(0)
        while engine.cmd_qsize():
            await asyncio.sleep(0)
        pending = [task for task in self._tasks if not task.done()]
        if pending:
            await asyncio.wait(pending, timeout=5)
        try:
            await self._disconnect()
        finally:
            try:
                await self.cancel_pending_tasks()
            finally:
                # Stopping the engine only queues sentinels. SQLite must remain open
                # until native events ahead of those sentinels have been applied.
                self.owner.native.stop()
                queues = [engine.get_cmd_queue_task(), engine.get_evt_queue_task()]
                await asyncio.gather(*(task for task in queues if task), return_exceptions=True)
                self._set_connected(False)
