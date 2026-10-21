"""Narrow extension of the pinned official Polymarket V2 execution adapter.

Official signing/HTTP/WebSocket/report infrastructure is retained. PM adds durable
ownership, bounded quote buys, confirmed-only fills, and conservative terminal checks.
"""

import asyncio
import json
import os
import re
import stat
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import asdict
from decimal import Decimal
from hashlib import sha256
from time import perf_counter_ns
from types import SimpleNamespace
from urllib.parse import urlsplit

import msgspec
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.order_builder.builder import OrderBuilder
from py_clob_client_v2.order_utils import Side
from py_clob_client_v2.clob_types import (
    ApiCreds,
    MarketOrderArgsV2,
    OrderArgsV2,
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
from nautilus_trader.adapters.polymarket.common.constants import POLYMARKET_NAUTILUS_BUILDER_CODE
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.reports import OrderStatusReport, PositionStatusReport
from nautilus_trader.model.currencies import pUSD
from nautilus_trader.model.enums import OrderSide, LiquiditySide, PositionSide
from nautilus_trader.model.events import OrderFilled, OrderUpdated
from nautilus_trader.model.identifiers import ClientOrderId, VenueOrderId, TradeId
from nautilus_trader.model.objects import Money, AccountBalance

from .config import SCALE, micros
from .execution import TestExecution
from .live_account_guard import audit_open_orders, condition_inventory_matches
from .native import price, quantity, instrument_id
from .order_terms import (
    OrderTermsError,
    SellTerms,
    check_signed_buy,
    check_signed_sell,
    normalize_sell,
    exact_sdk_buy_amounts,
)


from .rules import (
    SUPPORTED_CLOB_TICKS,
    cost,
    ceil_div,
    static_reason,
    Fees,
    preview,
    plan_buy,
    arbitrate,
    signed_buy_notional,
)


class _ExactBuyOrderBuilder(OrderBuilder):
    """Only SDK BUY amount arithmetic changes; signing and fee guards stay official."""

    def get_market_order_amounts(self, side, amount, price, round_config):
        if side in ("BUY", Side.BUY):
            maker, taker = exact_sdk_buy_amounts(
                amount, price, round_config.price, round_config.amount
            )
            return Side.BUY, maker, taker
        return super().get_market_order_amounts(side, amount, price, round_config)


def checked_owned_order_result(result, venue: str, token_id: str) -> tuple[set[str], int, str]:
    """Only complete venue facts may release an owned order's reservation."""
    if (
        not isinstance(result, dict)
        or result.get("id") != venue
        or result.get("asset_id") != token_id
    ):
        raise ValueError("订单查询身份不一致")
    try:
        trades = result["associate_trades"]
        value = result["size_matched"]
        status = result["status"]
        # The official schema permits explicit null for no associated trades;
        # an absent field is never evidence that there were no fills.
        if trades is not None and (
            not isinstance(trades, list)
            or any(not isinstance(trade, str) or not trade for trade in trades)
        ):
            raise ValueError
        if not isinstance(value, str) or not value.strip():
            raise ValueError
        matched = micros(value)
        if matched < 0 or Decimal(value) * SCALE != matched:
            raise ValueError
        if not isinstance(status, str) or status not in {
            "INVALID",
            "LIVE",
            "DELAYED",
            "MATCHED",
            "UNMATCHED",
            "CANCELED",
            "CANCELED_MARKET_RESOLVED",
            "CANCELLED",
            "FILLED",
            "EXPIRED",
        }:
            raise ValueError
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise ValueError("订单查询成交事实不完整，保留额度与Event锁") from None
    return set(trades or []), matched, status


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


class _LedgerGate:
    """Full checks are exclusive; independent venue submissions remain concurrent."""

    def __init__(self):
        self.condition = asyncio.Condition()
        self.checking = False
        self.waiting_checks = 0
        self.posts = 0

    @asynccontextmanager
    async def validation(self):
        async with self.condition:
            self.waiting_checks += 1
            try:
                await self.condition.wait_for(lambda: not self.checking and not self.posts)
                self.checking = True
            finally:
                self.waiting_checks -= 1
                self.condition.notify_all()
        try:
            yield
        finally:
            await self._release(check=True)

    @asynccontextmanager
    async def post(self):
        async with self.condition:
            await self.condition.wait_for(lambda: not self.checking and not self.waiting_checks)
            self.posts += 1
        try:
            yield
        finally:
            await self._release(check=False)

    async def _release(self, *, check):
        async def cleanup():
            async with self.condition:
                if check:
                    self.checking = False
                else:
                    self.posts -= 1
                self.condition.notify_all()

        # Shutdown can cancel a task again while it waits for this lock. Finish
        # releasing ownership before propagating cancellation to its caller.
        task = asyncio.create_task(cleanup())
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
        if cancelled:
            raise asyncio.CancelledError


class _PostStateChanged(ValueError):
    """A retry was stopped after an earlier POST may already have reached the venue."""


class _BuyQuoteChanged(ValueError):
    """The old BUY limit has no executable depth in the current local book."""


class _TrackedRetryManager:
    """Mark the boundary where a submitted order may reach the venue."""

    def __init__(self, inner, execution):
        self.inner = inner
        self.execution = execution

    async def run(self, name, details, *args, **kwargs):
        if name == "submit_order" and details:
            # SDK pool acquisition can yield after the signed identity is saved.
            # Share this final boundary with full ledger validation so a new
            # check cannot be crossed by a POST queued behind the pool.
            async with self.execution._ledger_gate.post():
                self.execution._check_post_ready(details[0])
                # The retry manager can schedule a worker before it returns or
                # raises. Keep the conservative boundary used by crash recovery.
                self.execution._post_possible.add(str(details[0]))

                async def checked_attempt(*call_args, **call_kwargs):
                    try:
                        self.execution._check_post_ready(details[0])
                    except ValueError as exc:
                        if str(details[0]) in self.execution._post_possible:
                            raise _PostStateChanged(str(exc)) from exc
                        raise
                    # This marks each actual attempt, not just pool acquisition.
                    # A late worker result must never release an unknown reservation.
                    self.execution._post_possible.add(str(details[0]))
                    return await args[0](*call_args, **call_kwargs)

                return await self.inner.run(name, details, checked_attempt, *args[1:], **kwargs)
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
        if isinstance(http.builder, OrderBuilder) and not isinstance(
            http.builder, _ExactBuyOrderBuilder
        ):
            builder = http.builder
            http.builder = _ExactBuyOrderBuilder(
                builder.signer, builder.signature_type, builder.funder
            )
        n = owner.native
        super().__init__(
            asyncio.get_running_loop(), http, n.bus, n.cache, n.clock, provider, auth, config, None
        )
        self._retry_manager_pool = _TrackedRetryPool(self._retry_manager_pool, self)
        self.ready = False
        self.open_orders_clear = False
        self.reconciliation_task = None
        self._ledger_gate = _LedgerGate()
        self._ledger_validation = None
        self._ledger_ok = False
        self._reconcile_requested = asyncio.Event()
        self._ws_order_states = OrderedDict()
        self._submit_timings = {}
        self._release_verified = False
        self._pending_native = set()
        self._undelivered_native = {}
        self._pending_conversions = set()
        self._submitting = {}
        self._post_possible = set()
        self._boot_intents = set(owner.store.intents())
        self._sync_lock = asyncio.Lock()
        self._sdk_terms = {}
        self._sdk_term_locks = {}
        self._sdk_condition_tokens = {}
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
            first_terminal = not i.get("venue_terminal")
            if first_terminal:
                i["venue_terminal"] = True
                r.store.save_intent(event.client_order_id, i)
                self._reconcile_requested.set()
            return  # Await all associated trades; cancellation is not settlement.
        key = (
            f"fill:{event.client_order_id}:{event.trade_id}"
            if isinstance(event, OrderFilled)
            else str(event.id)
        )
        if isinstance(event, OrderFilled):
            if key in self._pending_native or (
                key not in self._undelivered_native
                and r.store.db.execute(
                    "SELECT 1 FROM native_events WHERE event_id=?", (key,)
                ).fetchone()
            ):
                return
            self._pending_native.add(key)
        try:
            if key not in self._undelivered_native:
                r.freeze_fill_rules(event)
                r.store.journal(event)
                self._undelivered_native[key] = event
            else:
                event = self._undelivered_native[key]
            if (
                type(event).__name__ == "OrderSubmitted"
                and str(event.client_order_id) in self._submitting
            ):
                self._submitting[str(event.client_order_id)] = True
            super()._send_order_event(event)
            self._undelivered_native.pop(key, None)
            if isinstance(event, OrderUpdated) and not event.is_quote_quantity:
                self._pending_conversions.add(str(event.client_order_id))
            elif type(event).__name__ in {
                "OrderCanceled",
                "OrderExpired",
                "OrderDenied",
                "OrderRejected",
            }:
                self._pending_conversions.discard(str(event.client_order_id))
        except Exception:
            self.ready = self.open_orders_clear = False
            self._reconcile_requested.set()
            try:
                r.store.put("status", "PAUSED")
            except Exception:
                pass  # The original journal failure remains the caller's error.
            raise
        finally:
            self._pending_native.discard(key)

    def _retry_native_delivery(self, oid):
        delivered = False
        for event in tuple(self._undelivered_native.values()):
            if str(event.client_order_id) == str(oid):
                self._send_order_event(event)
                delivered = True
        return delivered

    def _handle_ws_order_msg(self, msg, wait_for_ack):
        oid = self._cache.client_order_id(VenueOrderId(msg.id))
        i = self.owner.store.intent(oid) if oid else None
        if i:
            super()._handle_ws_order_msg(msg, wait_for_ack)
            if i["generation"] == self.owner.store.generation and not i.get("terminal"):
                state = (str(msg.type), str(msg.status), str(msg.size_matched))
                if self._ws_order_states.get(str(oid)) != state:
                    self._ws_order_states[str(oid)] = state
                    self._ws_order_states.move_to_end(str(oid))
                    while len(self._ws_order_states) > 2048:
                        self._ws_order_states.popitem(last=False)
                    self._reconcile_requested.set()

    def _handle_ws_trade_msg(self, msg, wait_for_ack):
        if self.ingest_trade(msg):
            self._reconcile_requested.set()

    def _owned_quantity_upper_bound(self, intent, order, token):
        """Native base cap, not the BUY signature's minimum received shares."""
        maximum = intent.get("base_quantity")
        if maximum is None:
            if order is None:
                raise ValueError("订单核对缺少原生订单数量上限")
            if not order.is_quote_quantity:
                maximum = micros(order.quantity.as_decimal())
            else:
                outbound = intent.get("outbound_terms", {})
                maker, tick = (
                    outbound.get("maker", intent["cash"]),
                    outbound.get("tick", token.tick),
                )
                if type(maker) is not int or maker <= 0 or tick not in SUPPORTED_CLOB_TICKS:
                    raise ValueError("订单核对缺少持久化现金或价格档位上限")
                # A better price may deliver more shares than takerAmount. This
                # is the same conservative bound persisted before a BUY POST.
                maximum = ceil_div((maker + 1) * SCALE, tick)
        if type(maximum) is not int or maximum <= 0:
            raise ValueError("订单核对份额上限无效")
        return maximum

    def ingest_trade(self, msg):
        status = getattr(msg.status, "value", msg.status)
        r = self.owner
        changed = False
        for venue in msg.get_filled_user_order_ids(self._wallet_address, self._api_key):
            oid = self._cache.client_order_id(VenueOrderId(venue))
            if oid is None:
                continue
            i = r.store.intent(oid)
            if not i or i["generation"] != r.store.generation:
                continue
            t = r.tokens[i["token_id"]]
            o = self._cache.order(oid)
            if (
                o is None
                or msg.market != t.condition_id
                or msg.get_asset_id(venue) != t.token_id
                or msg.order_side(venue) != o.side
            ):
                raise ValueError("实际成交身份与本程序意图不匹配")
            if status not in {"MATCHED", "MINED", "RETRYING", "CONFIRMED", "FAILED"}:
                raise ValueError("实际成交状态无效")
            gross_decimal = Decimal(str(msg.last_qty(venue)))
            gross = micros(gross_decimal)
            if gross <= 0 or gross_decimal * SCALE != gross:
                raise ValueError("无效真实成交数值")
            if status == "FAILED":
                # A failure can help release a reservation, so impossible failed
                # quantities must never count as final evidence. Confirmed venue
                # fills remain facts even when a separate budget guard breaches.
                if gross > self._owned_quantity_upper_bound(i, o, t):
                    raise ValueError("失败成交份额超过本程序签名订单上限")
            changed |= self._retry_native_delivery(oid)
            trade_id = TradeId(sha256(f"{msg.id}:{venue}".encode()).hexdigest()[:32])
            fill_key = f"fill:{oid}:{trade_id}"
            committed_fill = r.store.db.execute(
                "SELECT 1 FROM native_events WHERE event_id=?", (fill_key,)
            ).fetchone()
            if committed_fill and status != "CONFIRMED":
                continue  # Final committed fills cannot regress to a stale status.
            pending = set(i.get("pending_trades", []))
            before_pending = pending.copy()
            if status not in {"CONFIRMED", "FAILED"}:
                pending.add(msg.id)
            else:
                pending.discard(msg.id)
            state_changed = pending != before_pending
            if status == "FAILED" and i.get("failed_trades", {}).get(msg.id) != gross:
                i.setdefault("failed_trades", {})[msg.id] = gross
                state_changed = True
            elif status == "CONFIRMED" and msg.id in i.get("failed_trades", {}):
                del i["failed_trades"][msg.id]
                state_changed = True
            if state_changed:
                i["pending_trades"] = sorted(pending)
                r.store.save_intent(oid, i)
                changed = True
            if status != "CONFIRMED":
                continue
            if fill_key in self._pending_native or committed_fill:
                continue
            p = micros(msg.last_px(venue))
            if not 0 < p < SCALE:
                raise ValueError("无效真实成交数值")
            if o.is_quote_quantity and str(oid) not in self._pending_conversions:
                base = i.get("base_quantity")
                if not base:
                    raise ValueError("恢复订单缺少持久化的签名份额，不能猜测转换")
                self._pending_conversions.add(str(oid))
                try:
                    self._send_quote_to_base_update(o, VenueOrderId(venue), quantity(base))
                except Exception:
                    self._pending_conversions.discard(str(oid))
                    raise
            elif not o.is_quote_quantity:
                self._pending_conversions.discard(str(oid))
            if (o.side == OrderSide.BUY and p > i.get("execution_limit", i["limit"])) or (
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
            changed = True
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
        return changed

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
        started = perf_counter_ns()
        self._submit_timings[oid] = [started, started, {}]
        self._submitting[oid] = False
        try:
            await self._await_ledger_validation()
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
            timing = self._submit_timings.pop(oid, None)
            if timing is not None:
                timing[2]["total_ms"] = round((perf_counter_ns() - timing[0]) / 1_000_000, 3)
                self._log.info(f"PM submit latency {o.side.name}: {json.dumps(timing[2])}")
            self._submitting.pop(oid, None)
            self._post_possible.discard(oid)

    def _mark_submit_stage(self, order, stage):
        timing = self._submit_timings.get(str(order.client_order_id))
        if timing is not None:
            now = perf_counter_ns()
            timing[2][stage] = round((now - timing[1]) / 1_000_000, 3)
            timing[1] = now

    def _finish_failed_submit(self, order, exc, *, submitted=None):
        """Release only when the retry manager never reached the POST boundary."""
        from .storage_guard import StoragePressure
        from .market_availability import MarketUnavailable

        r = self.owner
        oid = str(order.client_order_id)
        intent = r.store.intent(oid)
        if (
            isinstance(exc, (MarketUnavailable, _BuyQuoteChanged))
            or isinstance(exc, StoragePressure)
            and order.side == OrderSide.BUY
        ) and oid not in self._post_possible:
            # A known local deferral is not an account failure. Only a proven
            # unposted order can be released; other markets' exits remain supervised.
            if intent and not intent.get("terminal"):
                reason = (
                    "盘口已变化：此买单尚未向交易所提交，等待重新判断"
                    if isinstance(exc, _BuyQuoteChanged)
                    else "市场暂不可交易：此订单尚未向交易所提交"
                    if isinstance(exc, MarketUnavailable)
                    else "存储保护：此买单尚未向交易所提交"
                )
                was_submitted = self._submitting.get(oid) if submitted is None else submitted
                if was_submitted:
                    self.generate_order_rejected(
                        order.strategy_id, order.instrument_id, order.client_order_id, reason, r.now
                    )
                else:
                    self.deny(order, reason)
            return
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
        return self._buy_winner(o, i) is not None

    def _buy_winner(self, o, i):
        """Current complete decision, including the order's own reserved cash."""
        r = self.owner
        t = r.tokens[i["token_id"]]
        cycle = r.business["cycles"].get(t.event_id)
        basic = (
            r.status == "RUNNING"
            and getattr(r, "storage_can_open", True)
            and getattr(r, "market_can_trade", lambda _: True)(t.token_id)
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
            return None
        ids = {t.token_id} if cycle else r.events.get(t.event_id, set())
        ids = {tid for tid in ids if getattr(r, "market_can_trade", lambda _: True)(tid)}
        if not ids or any(not r.books[tid].ready for tid in ids):
            return None
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
        return winner if winner and winner.token.token_id == t.token_id else None

    def _buy_quote_executable(self, i):
        r = self.owner
        token = r.tokens[i["token_id"]]
        if token.tick not in SUPPORTED_CLOB_TICKS:
            return False
        limit = min(
            i.get("execution_limit", i["limit"]) // token.tick * token.tick, SCALE - token.tick
        )
        if limit <= 0:
            return False
        terms = i.get("outbound_terms")
        notional = (
            terms["maker"]
            if terms is not None
            else signed_buy_notional(i["cash"], Fees(**i["fees"]), token.tick, limit)
        )
        return bool(
            r.books[token.token_id].ready
            and plan_buy(
                list(r.books[token.token_id].ask.external.items()),
                i["cash"],
                limit,
                token.min_size,
                Fees(**i["fees"]),
                token.tick,
                r.preferences,
                notional,
            )
        )

    def deny(self, o, reason):
        self.generate_order_denied(
            o.strategy_id, o.instrument_id, o.client_order_id, reason, self._clock.timestamp_ns()
        )

    async def _ensure_signing_metadata(self, token):
        """Refresh the public SDK caches when the runtime's official tick changes."""
        expected = (token.tick, token.neg_risk, token.fees)
        lock = self._sdk_term_locks.setdefault(token.condition_id, asyncio.Lock())
        async with lock:
            if self._sdk_terms.get(token.token_id) == expected:
                return
            known_ids = self._sdk_condition_tokens.setdefault(token.condition_id, set())
            known_ids.add(token.token_id)
            # The SDK mutates both outcome caches before this method can inspect
            # its response. An invalid/failed refresh invalidates our old proof
            # too, including a later runtime metadata rollback.
            for token_id in known_ids:
                self._sdk_terms.pop(token_id, None)
            info = await asyncio.to_thread(
                self._http_client.get_clob_market_info, token.condition_id
            )
            if not isinstance(info, dict) or type(info.get("nr", False)) is not bool:
                raise OrderTermsError("官方市场签名档位核对结果无效")
            try:
                actual = (micros(info["mts"]), info.get("nr", False))
                ids = {str(row["t"]) for row in info["t"]}
                fd = info.get("fd") or {}
                rate = Decimal(str(fd.get("r", 0)))
                exponent = Decimal(str(fd.get("e", 0)))
                if not rate.is_finite() or not exponent.is_finite() or exponent != int(exponent):
                    raise ValueError
                wanted_rate = Decimal(token.fees.rate) / SCALE
                # A zero fee has no curve, so absent exponent or SDK's zero
                # default is equivalent to the runtime's fee-free exponent=1.
                same_fees = rate == wanted_rate and (not rate or exponent == token.fees.exponent)
            except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError):
                raise OrderTermsError("官方市场签名档位核对结果无效") from None
            if token.token_id not in ids or actual != expected[:2] or not same_fees:
                raise OrderTermsError("官方签名档位/费用与当前盘口不一致，等待重新核对")
            known_ids.update(ids)
            for token_id in ids:
                self._sdk_terms[token_id] = expected

    async def _timed_preparation(self, order, stage, operation):
        started = perf_counter_ns()
        try:
            return await operation
        finally:
            timing = self._submit_timings.get(str(order.client_order_id))
            if timing is not None:
                timing[2][stage] = round((perf_counter_ns() - started) / 1_000_000, 3)

    async def _prepare_buy_signature(self, token, args):
        await self._ensure_signing_metadata(token)
        return await asyncio.to_thread(
            self._http_client.create_market_order,
            args,
            options=PartialCreateOrderOptions(
                tick_size=str(Decimal(token.tick) / SCALE), neg_risk=token.neg_risk
            ),
        )

    async def _prepare_sell_signature(self, token, terms: SellTerms):
        await self._ensure_signing_metadata(token)
        return await asyncio.to_thread(
            self._http_client.create_order,
            OrderArgsV2(
                token_id=token.token_id,
                price=float(Decimal(terms.limit) / SCALE),
                # The pinned SDK floors size * 100. A binary float such as
                # 5.02 falls below 502 there; Decimal preserves owned hundredths.
                size=Decimal(terms.quantity) / SCALE,
                side="SELL",
                expiration=0,
                builder_code=POLYMARKET_NAUTILUS_BUILDER_CODE,
            ),
            options=PartialCreateOrderOptions(
                tick_size=str(Decimal(terms.tick) / SCALE), neg_risk=token.neg_risk
            ),
        )

    async def _join_preparations(self, operations):
        # Neither operation can POST. On cancellation, wait for read/sign workers
        # before releasing SDK ownership: a late metadata write must not corrupt a
        # subsequent order's cache. Repeated cancellation must still finish cleanup.
        batch = asyncio.gather(*operations, return_exceptions=True)
        try:
            return await asyncio.shield(batch)
        except asyncio.CancelledError:
            while not batch.done():
                try:
                    await asyncio.shield(batch)
                except asyncio.CancelledError:
                    pass
            batch.result()
            raise

    def _record_outbound_terms(self, order, token, limit, maker, taker):
        i = self.owner.store.intent(order.client_order_id)
        if not i or i.get("terminal") or i["generation"] != self.owner.store.generation:
            raise OrderTermsError("签名期间订单状态已变化")
        i["outbound_terms"] = {
            "tick": token.tick,
            "neg_risk": token.neg_risk,
            "side": order.side.name,
            "limit": limit,
            "maker": maker,
            "taker": taker,
        }
        self.owner.store.save_intent(order.client_order_id, i)

    async def _submit_market_order(self, command, instrument):
        # A quote BUY keeps its cash quantity and hard price cap. Re-sign only
        # before publishing any venue identity, and at most once per dispatch.
        o = command.order
        r = self.owner
        if o.side != OrderSide.BUY or not o.is_quote_quantity:
            self.deny(o, "仅使用有现金上限的BUY市价FAK")
            return
        oid = str(o.client_order_id)
        for attempt in range(2):
            i = r.store.intent(oid)
            if not i or i.get("terminal") or i["generation"] != r.store.generation:
                raise ValueError("准备期间订单状态已变化")
            if not self._buy_quote_executable(i):
                if i.get("venue_id") or oid in self._post_possible or self._submitting.get(oid):
                    raise _BuyQuoteChanged
                winner = self._buy_winner(o, i)
                if winner is None:
                    self.deny(o, "盘口变化后买入条件已不满足")
                    return
                i["execution_limit"] = max(f.price for f in winner.fills)
                i["quantity"] = sum(f.gross for f in winner.fills)
                i.pop("outbound_terms", None)
                r.store.save_intent(oid, i)
            try:
                await self._prepare_market_buy(command, instrument)
                return
            except _BuyQuoteChanged:
                # After OrderSubmitted/venue_id, a different signature would
                # overwrite crash-recovery identity. End that unposted order
                # through the normal terminal projection instead.
                current = r.store.intent(oid)
                if (
                    attempt
                    or not current
                    or current.get("venue_id")
                    or self._submitting.get(oid)
                    or oid in self._post_possible
                ):
                    raise

    async def _prepare_market_buy(self, command, instrument):
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
        token = r.tokens[i["token_id"]]
        wallet_result, signed = await self._join_preparations(
            [
                self._timed_preparation(
                    o, "wallet_check_ms", asyncio.to_thread(self.wallet.buy_preflight)
                ),
                self._timed_preparation(o, "sign_ms", self._prepare_buy_signature(token, args)),
            ]
        )
        self._mark_submit_stage(o, "preparation_ms")
        if isinstance(wallet_result, BaseException):
            r.store.put("status", "PAUSED")
            r.store.put(
                "live_error", f"买入前钱包/赎回授权核对失败：{type(wallet_result).__name__}"
            )
            self.deny(o, "买入前钱包/赎回授权核对失败")
            return
        if isinstance(signed, BaseException):
            raise signed
        # The pinned SDK has no maxSpend option and rounds the maker amount to
        # cents. Inspect the signed facts before allowing any POST boundary.
        try:
            maker, taker = check_signed_buy(
                signed, notional, executable_limit, token.min_size, tick
            )
        except OrderTermsError as exc:
            self.deny(o, str(exc))
            return
        self._record_outbound_terms(o, token, executable_limit, maker, taker)
        if not self.buy_valid(o, i):
            self.deny(o, "签名期间控制/资格发生变化")
            return
        if not await self._check_buy_account(r.tokens[i["token_id"]]):
            self.deny(o, "账户开放挂单或同Condition份额核对未通过")
            return
        self._mark_submit_stage(o, "account_check_ms")
        i = r.store.intent(o.client_order_id)
        if not self.buy_valid(o, i):
            self.deny(o, "核对期间控制/资格发生变化")
            return
        if not self._buy_quote_executable(i):
            raise _BuyQuoteChanged
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

    async def _submit_limit_order(self, command, instrument):
        """SELL signs the same rounded rights and upward price floor as TEST."""
        o = command.order
        r = self.owner
        i = r.store.intent(o.client_order_id)
        if o.side != OrderSide.SELL or o.is_quote_quantity:
            self.deny(o, "仅使用有自有份额上限的SELL限价FAK")
            return
        token = r.tokens[i["token_id"]]
        try:
            terms = normalize_sell(i["quantity"], i["limit"], token.tick, token.min_size)
        except OrderTermsError as exc:
            self.deny(o, str(exc))
            return
        signed = await self._join_preparations(
            [self._timed_preparation(o, "sign_ms", self._prepare_sell_signature(token, terms))]
        )
        signed = signed[0]
        if isinstance(signed, BaseException):
            raise signed
        try:
            maker, taker = check_signed_sell(signed, terms)
        except OrderTermsError as exc:
            self.deny(o, str(exc))
            return
        self._record_outbound_terms(o, token, terms.limit, maker, taker)
        self.generate_order_submitted(o.strategy_id, o.instrument_id, o.client_order_id, r.now)
        venue = self._expected_venue_order_id(signed, neg_risk=token.neg_risk)
        await self._post_signed_order(
            o, signed, order_type_override="FAK", expected_venue_order_id=venue
        )

    async def _post_signed_order(self, order, signed_order, **kwargs):
        # A complete ledger check used to block the event loop here. Keep its
        # pre-POST gate while letting current quotes and confirmed fills arrive.
        await self._await_ledger_validation()
        if not self.ready:
            raise ValueError("LIVE账本/账户核对未完成，不能提交订单")
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
        self._mark_submit_stage(order, "prepare_post_ms")
        try:
            await super()._post_signed_order(order, signed_order, **kwargs)
        finally:
            self._mark_submit_stage(order, "post_ms")

    def _check_post_ready(self, client_order_id):
        r = self.owner
        i = r.store.intent(client_order_id)
        o = self._cache.order(ClientOrderId(str(client_order_id)))
        if (
            not self.ready
            or not i
            or i["generation"] != r.store.generation
            or i.get("terminal")
            or not o
            or o.is_closed
            or o.is_pending_cancel
        ):
            raise ValueError("LIVE账本/订单状态已变化，不能提交订单")
        guard = getattr(r, "storage_guard", None)
        if o.side == OrderSide.BUY and guard is not None:
            # Also runs after signing, pool waits and each HTTP retry.
            guard.require_safe()
        require_market = getattr(r, "require_market_tradeable", None)
        if require_market is not None and (
            i.get("kind") != "SETTLEMENT" or o.side != OrderSide.SELL
        ):
            # Recheck after signing, pool waits, and immediately before every retry.
            # Existing SETTLEMENT intents use their separate confirmed-rights path.
            require_market(i["token_id"])
        if o.side == OrderSide.BUY and (not self.open_orders_clear or not self.buy_valid(o, i)):
            raise ValueError("提交前资格/资金/暂停状态已变化")
        token = r.tokens[i["token_id"]]
        terms = i.get("outbound_terms")
        if (
            not isinstance(terms, dict)
            or terms.get("tick") != token.tick
            or terms.get("neg_risk") != token.neg_risk
            or terms.get("side") != o.side.name
            or i["fees"] != asdict(token.fees)
        ):
            raise ValueError("提交前市场签名档位或费用已变化")
        signed = SimpleNamespace(makerAmount=terms["maker"], takerAmount=terms["taker"])
        if o.side == OrderSide.SELL:
            current = normalize_sell(i["quantity"], i["limit"], token.tick, token.min_size)
            if current.limit != terms["limit"]:
                raise ValueError("提交前卖出价格已变化")
            check_signed_sell(signed, current)
            cycle = r.business["cycles"].get(token.event_id)
            if (
                not cycle
                or cycle["token_id"] != token.token_id
                or cycle["quantity"] < current.quantity
            ):
                raise ValueError("提交前自有卖出份额不足")
        else:
            limit = min(
                i.get("execution_limit", i["limit"]) // token.tick * token.tick, SCALE - token.tick
            )
            if terms["limit"] != limit:
                raise ValueError("提交前买入价格已变化")
            check_signed_buy(
                signed,
                signed_buy_notional(i["cash"], Fees(**i["fees"]), token.tick, limit),
                limit,
                token.min_size,
                token.tick,
            )
            if not self._buy_quote_executable(i):
                raise _BuyQuoteChanged

    def _is_unknown_submit_result(self, exc):
        # The pinned retry manager also returns None after a cancelled POST,
        # without recording an exception. The worker thread may still complete.
        # A server error likewise cannot prove the venue refused the order.
        status = getattr(exc, "status_code", None)
        return (
            exc is None
            or isinstance(exc, _PostStateChanged)
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
        condition_ids = self.owner.condition_token_ids(token.condition_id)
        if self.wallet is None or len(condition_ids) != 2:
            self.ready = False
            self.open_orders_clear = False
            self.owner.pause()
            self.owner.store.put("live_error", "同Condition实际份额与程序持仓不一致")
            return False
        # Independent fresh reads overlap; both gates must still pass. Load
        # our quantities after the awaits, since confirmed fills may arrive
        # while the RPC and paginated account-wide CLOB lookup are in flight.
        clear, actual = await asyncio.gather(
            self._check_open_orders(),
            asyncio.to_thread(self.wallet.token_balances, condition_ids),
            return_exceptions=True,
        )
        if isinstance(actual, BaseException):
            actual = {}
        if isinstance(clear, BaseException):
            raise clear  # The submit failure handler invalidates both readiness gates.
        if clear is not True:
            return False
        own = {}
        for cycle in self.owner.business["cycles"].values():
            held = self.owner.tokens.get(cycle["token_id"])
            if held and held.condition_id == token.condition_id:
                own[held.token_id] = own.get(held.token_id, 0) + cycle["quantity"]
        if not condition_inventory_matches(condition_ids, actual, own):
            self.ready = False
            self.open_orders_clear = False
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
        queries = []
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
            if oid in self._submitting and oid not in self._post_possible:
                # This process knows the durable identity has not reached POST
                # yet (for example while awaiting the SDK pool or ledger gate).
                # A reboot loses this proof and still treats the result as unknown.
                continue
            queries.append((oid, venue))

        limit = asyncio.Semaphore(4)

        async def lookup(venue):
            async with limit:
                return await asyncio.to_thread(self._http_client.get_order, venue)

        results = await asyncio.gather(
            *(lookup(venue) for _, venue in queries), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        # All DB and native state stays on this event loop. CONFIRMED WS facts
        # arriving during the REST reads must be included before releasing rights.
        await asyncio.sleep(0)
        for (oid, venue), result in zip(queries, results, strict=True):
            i = r.store.intent(oid)
            if not i or i.get("terminal") or i["generation"] != r.store.generation:
                continue
            if i.get("venue_id") != venue:
                raise ValueError("订单查询期间持久化身份已变化")
            o = self._cache.order(ClientOrderId(oid))
            if not result:
                raise ValueError("订单结果不明，保留额度与Event锁")
            associated, matched, status = checked_owned_order_result(result, venue, i["token_id"])
            rows = r.store.order_fills(oid)
            confirmed = {e.info.get("venue_trade_id") for e in rows}
            gross = sum(e.info.get("pm_gross", 0) for e in rows)
            failed = {
                key: qty for key, qty in i.get("failed_trades", {}).items() if key not in confirmed
            }
            try:
                maximum = self._owned_quantity_upper_bound(i, o, r.tokens[i["token_id"]])
                if matched > maximum or gross + sum(failed.values()) > maximum:
                    raise ValueError("订单查询累计成交或失败份额超过持久化订单上限")
            except ValueError as exc:
                # Impossible final evidence cannot release ownership. Keep all
                # confirmed fills and reservations for a later trustworthy read.
                self._invalidate_ledger()
                r.store.put("live_error", str(exc))
                raise
            # All matched quantity must be represented by finalized venue responses.
            # Missing/deferred records retain the reservation; never infer zero fill.
            if (
                i.get("pending_trades")
                or not associated.issubset(confirmed | failed.keys())
                or gross + sum(failed.values()) < matched
            ):
                continue
            if status in {
                "CANCELED",
                "CANCELED_MARKET_RESOLVED",
                "CANCELLED",
                "MATCHED",
                "FILLED",
                "EXPIRED",
            }:
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
                    r.note_terminal(i["event_id"])
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
        def owned_rights():
            rights = {}
            for c in self.owner.business["cycles"].values():
                held = c["quantity"]
                if type(held) is not int or not 0 <= held < 2**256:
                    raise ValueError("本程序持仓份额无效")
                t = self.owner.tokens[c["token_id"]]
                if t.token_id != c["token_id"]:
                    raise ValueError("本程序持仓标的身份无效")
                if held == 0 or t.condition_id in self.owner.business["settled"]:
                    continue  # Pending redemption rights are checked separately.
                rights[t.token_id] = rights.get(t.token_id, 0) + held
            return rights

        requested = owned_rights()
        if not requested:
            return []
        if self.wallet is None:
            raise ValueError("无法确认账户实际份额")
        # The Data API truncates share sizes to four decimal places. CTF balances
        # preserve all six raw-unit digits, and one fresh fixed-block batch covers
        # only our positive, unsettled rights.
        actual = await asyncio.to_thread(self.wallet.token_balances, tuple(requested))
        if (
            not isinstance(actual, dict)
            or actual.keys() != requested.keys()
            or any(type(value) is not int or not 0 <= value < 2**256 for value in actual.values())
        ):
            raise ValueError("账户实际份额查询结果无效")
        # A confirmed fill can change quantity or introduce a new token during
        # the RPC await. Verify the current complete rights, never the snapshot.
        current = owned_rights()
        if any(tid not in actual or actual[tid] < held for tid, held in current.items()):
            raise ValueError("账户实际份额不足以覆盖本程序持仓")
        reports = []
        wanted = getattr(command, "instrument_id", None)
        for tid, held in current.items():
            t = self.owner.tokens[tid]
            iid = instrument_id(t)
            if wanted is None or wanted == iid:
                now = self._clock.timestamp_ns()
                reports.append(
                    PositionStatusReport(
                        account_id=self.account_id,
                        instrument_id=iid,
                        position_side=PositionSide.LONG,
                        quantity=quantity(held),
                        report_id=UUID4(),
                        ts_last=now,
                        ts_init=now,
                    )
                )
        # Custody was freshly checked for all current rights. Reporting only those
        # rights lets the native startup batch skip N redundant wallet reads;
        # manual wallet assets never become program positions, and no cache lives
        # beyond this call.
        return reports

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
        if not ok or not (await self._validate_ledger())["ok"]:
            raise ValueError("LIVE原生执行核对失败")
        await self._check_open_orders()
        if not self._ledger_ok:
            raise ValueError("LIVE账本校验未通过")
        # The native mass-status batch reads custody alongside sync_owned. Recheck
        # the final recovered quantities before publishing readiness.
        await self.generate_position_status_reports(None)
        self.ready = True
        self.reconciliation_task = asyncio.create_task(self.maintain())

    async def _validate_ledger(self):
        async with self._ledger_gate.validation():
            task = asyncio.create_task(self.owner.validate_async())
            self._ledger_validation = task
            try:
                result = await task
                if not result["ok"]:
                    self._invalidate_ledger()
                else:
                    self._ledger_ok = True
                return result
            except (Exception, asyncio.CancelledError):
                self._invalidate_ledger()
                raise
            finally:
                if self._ledger_validation is task:
                    self._ledger_validation = None

    def _invalidate_ledger(self):
        self._ledger_ok = False
        self.ready = False
        self.open_orders_clear = False
        if not self.owner.store._closed:
            # Stop new intentions without canceling unrelated venue orders.
            self.owner.store.put("status", "PAUSED")

    async def _await_ledger_validation(self):
        task = self._ledger_validation
        if task is not None and not (await asyncio.shield(task))["ok"]:
            raise ValueError("LIVE账本校验未通过，不能提交订单")

    async def maintain(self):
        while True:
            # A signal arriving during reconciliation remains set for the next
            # round. One consumer coalesces bursts without concurrent REST syncs.
            self._reconcile_requested.clear()
            failed = False
            try:
                await self.sync_owned()
                await self.generate_position_status_reports(None)
                if not (await self._validate_ledger())["ok"]:
                    raise ValueError("LIVE账本校验未通过")
                await self._check_open_orders()
                # A concurrent API validation can fail during this network await.
                # Its failed result must not be overwritten by this older round.
                if not self._ledger_ok:
                    raise ValueError("LIVE账本校验未通过")
                self.ready = True
                if self.open_orders_clear:
                    self.owner.store.put("live_error", None)
                self.owner.drain()
            except Exception as exc:
                failed = True
                self.ready = False
                self.open_orders_clear = False
                self.owner.pause()
                self.owner.store.put("live_error", type(exc).__name__)
            if failed:
                await asyncio.sleep(5)
            else:
                try:
                    await asyncio.wait_for(self._reconcile_requested.wait(), timeout=5)
                    await asyncio.sleep(0.05)
                except TimeoutError:
                    pass

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
