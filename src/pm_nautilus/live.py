"""Narrow extension of the pinned official Polymarket V2 execution adapter.

Official signing/HTTP/WebSocket/report infrastructure is retained. PM adds durable
ownership, bounded quote buys, confirmed-only fills, and conservative terminal checks.
"""

import asyncio
import json
import os
from dataclasses import asdict
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

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
from .native import price, quantity
from .rules import cost, ceil_div, static_reason, Fees, preview, arbitrate


def load_settings():
    if os.environ.get("PM_LIVE_ENABLED") != "true":
        raise ValueError("LIVE 未由服务器显式启用")
    path = Path(os.environ["PM_LIVE_CREDENTIALS_FILE"])
    if path.stat().st_mode & 0o077:
        raise ValueError("LIVE凭据文件须仅属主可读写 (chmod 600)")
    settings = json.loads(path.read_text())
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
    if settings["signature_type"] not in (0, 2):
        raise ValueError("须核对实际钱包；当前支持EOA或单签Safe的完整交易/赎回路径")
    return settings


def live_factory(settings):
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
        )

    return create


class LiveExecution(PolymarketExecutionClient):
    def __init__(self, owner, http, provider, auth, config):
        self.owner = owner
        n = owner.native
        super().__init__(
            asyncio.get_running_loop(), http, n.bus, n.cache, n.clock, provider, auth, config, None
        )
        self.ready = False
        self.reconciliation_task = None
        self._release_verified = False
        self._pending_native = set()
        self._pending_conversions = set()
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
            net = gross - ceil_div(fee * SCALE, p) if o.side == OrderSide.BUY else gross
            amount = cost(p, gross) if o.side == OrderSide.BUY else cost(p, gross) - fee
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
        if not self.ready:
            self.deny(o, "LIVE核对未完成")
            return
        if o.side == OrderSide.BUY and not self.buy_valid(o, i):
            self.deny(o, "发单前资格/资金/暂停状态已变化")
            return
        await super()._submit_order(command)

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
        executable_limit = min(i["limit"] // tick * tick, SCALE - tick)
        if executable_limit <= 0:
            self.deny(o, "当前价格上限内没有合法 tick")
            return
        args = MarketOrderArgsV2(
            token_id=i["token_id"],
            amount=float(Decimal(i["cash"]) / SCALE),
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
        if not self.buy_valid(o, i):
            self.deny(o, "签名期间控制/资格发生变化")
            return
        self.generate_order_submitted(o.strategy_id, o.instrument_id, o.client_order_id, r.now)
        venue = self._expected_venue_order_id(signed, neg_risk=r.tokens[i["token_id"]].neg_risk)
        await self._post_signed_order(
            o,
            signed,
            order_type_override="FAK",
            base_quantity=quantity(int(signed.takerAmount)),
            expected_venue_order_id=venue,
        )

    async def _post_signed_order(self, order, signed_order, **kwargs):
        r = self.owner
        i = r.store.intent(order.client_order_id)
        venue = kwargs.get("expected_venue_order_id")
        if venue is None:
            self.deny(order, "无法持久化订单确定性身份")
            return
        i["venue_id"] = str(venue)
        i["submitted_ns"] = r.now
        if kwargs.get("base_quantity") is not None:
            i["base_quantity"] = micros(kwargs["base_quantity"].as_decimal())
        r.store.save_intent(order.client_order_id, i)
        self._cache.add_venue_order_id(order.client_order_id, venue)
        await super()._post_signed_order(order, signed_order, **kwargs)

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
                r.store.put("live_error", "订单结果不明，保留额度与Event锁")
                continue
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
        self.reconciliation_task = asyncio.create_task(self.maintain())

    async def maintain(self):
        while True:
            try:
                await self.sync_owned()
                await self.generate_position_status_reports(None)
                if not self.owner.validate()["ok"]:
                    raise ValueError("LIVE账本校验未通过")
                self.ready = True
                self.owner.store.put("live_error", None)
                self.owner.drain()
            except Exception as exc:
                self.ready = False
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
