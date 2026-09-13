"""Shared Strategy, durable Event cycles, targets, stop observations and control."""

from dataclasses import asdict
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.currencies import pUSD
from nautilus_trader.model.enums import OmsType, OrderSide, TimeInForce
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.trading.strategy import Strategy

from .books import Book
from .config import SCALE, Preferences, micros
from .execution import TestExecution
from .native import Native, instrument_id, make_instrument, quantity, price
from .rules import (
    StopLoss,
    Target,
    arbitrate,
    preview,
    static_reason,
    sell_batches,
    cost,
    target_price,
)
from .store import Store

TERMINAL = {"OrderCanceled", "OrderRejected", "OrderDenied", "OrderExpired"}


class PMStrategy(Strategy):
    def __init__(self, owner):
        super().__init__(
            StrategyConfig(
                strategy_id="PM",
                order_id_tag="001",
                oms_type=OmsType.NETTING,
                use_uuid_client_order_ids=True,
            )
        )
        self.owner = owner

    def on_order_book_deltas(self, deltas):
        # All scheduling is indexed by instrument/token. No full-market iteration
        # is performed on a quote update.
        tid = self.owner.instrument_tokens.get(str(deltas.instrument_id))
        if tid is not None:
            self.owner.dirty.add(self.owner.tokens[tid].event_id)

    def on_order_filled(self, event):
        self.owner.dirty.add(
            self.owner.tokens[self.owner.store.intent(event.client_order_id)["token_id"]].event_id
        )


class Runtime:
    def __init__(self, path: str | Path, mode="TEST", clock=None, live_factory=None):
        self.store = Store(path, mode)
        self.mode = mode
        self.preferences = Preferences(**self.store.get("preferences"))
        self.tokens = self.store.tokens()
        self.books = self.store.books()
        self.business = self.store.get("business")
        self.official_tags = set(self.store.get("official_tags", []))
        self.monitored = set()
        self.events = {}
        self.instrument_tokens = {str(instrument_id(t)): t.token_id for t in self.tokens.values()}
        self.dirty = set()
        self.evaluations = {}
        self.replaying = False
        self.faulted = None
        self.native = Native(self, clock)
        self.strategy = PMStrategy(self)
        self.client = TestExecution(self) if mode == "TEST" else live_factory(self)
        if mode == "TEST":
            self.client.account()
        self.native.register(self.strategy, self.client)
        self.native.bus.subscribe("data.book.deltas.*", self.strategy.on_order_book_deltas)
        self.recover_projection()
        if mode == "TEST":
            self.client.account()
        self.refresh_eligibility()

    @property
    def now(self):
        return self.native.clock.timestamp_ns()

    @property
    def status(self):
        return self.store.get("status")

    def test_cash(self):
        initial = self.store.get("initial_capital")
        if getattr(self, "_cash_initial", None) != initial:
            self._cash_initial = initial
            self._cash_seq = 0
            self._cash_value = initial
        cash = self._cash_value
        for seq, e in self.store.events(self._cash_seq):
            if isinstance(e, OrderFilled):
                cash += e.info.get("pm_amount", 0) * (-1 if e.order_side == OrderSide.BUY else 1)
            self._cash_seq = seq
        self._cash_value = cash
        return cash

    def cash(self):
        account = self.native.cache.account(self.client.account_id)
        if account is None:
            return 0
        balance = account.balance_free(pUSD)
        return micros(balance.as_decimal()) if balance else 0

    def held_cash(self):
        reserved = sum(
            i["cash"] - i.get("spent", 0)
            for i in self.store.intents(active_only=True).values()
            if i["side"] == "BUY" and not i.get("terminal", False)
        )
        if self.mode == "LIVE":
            reserved += sum(
                e.info.get("pm_amount", 0)
                for _, e in self.store.events(self.store.get("cash_covered_seq", 0))
                if isinstance(e, OrderFilled) and e.order_side == OrderSide.BUY
            )
        return reserved

    def add_tokens(self, tokens, official_tags=None):
        # One metadata commit per scan, not one FULL fsync per discovered token.
        # Unchanged metadata must not repeatedly rebuild native instruments.
        with self.store.transaction():
            for t in tokens:
                if self.tokens.get(t.token_id) == t:
                    continue
                self.tokens[t.token_id] = t
                self.instrument_tokens[str(instrument_id(t))] = t.token_id
                self.store.save_token(t)
                self.books.setdefault(t.token_id, Book())
                self.native.data.process(make_instrument(t))
        if official_tags is not None:
            self.official_tags = set(official_tags)
            self.store.put("official_tags", sorted(self.official_tags))
        self.refresh_eligibility()

    def refresh_eligibility(self):
        self.monitored = {
            tid
            for tid, t in self.tokens.items()
            if static_reason(t, self.preferences, self.now, self.official_tags) is None
            and t.condition_id not in self.business["settled"]
        }
        self.events = {}
        for tid in self.monitored:
            self.events.setdefault(self.tokens[tid].event_id, set()).add(tid)
        for cycle in self.business["cycles"].values():
            self.monitored.add(cycle["token_id"])
        self.dirty.update(self.events)

    def book(self, token_id, bids, asks, timestamp=None):
        b = self.books[token_id]
        if not b.snapshot(bids, asks, self.now if timestamp is None else timestamp):
            return
        self.store.save_book(token_id, b)
        self.native.feed(self.tokens[token_id], b)
        self.dirty.add(self.tokens[token_id].event_id)
        self.drain()

    def disconnect(self, token_ids):
        for tid in token_ids:
            if tid in self.books:
                self.books[tid].disconnect()
                self.dirty.add(self.tokens[tid].event_id)

    def recover_projection(self):
        # Rebuild missing application effects from committed native events.
        for seq, e in self.store.events(self.business["cursor"]):
            self.project(seq, e)
        # Unsigned initialized orders have no venue side effect in TEST. The native
        # journal replays fills before releasing their reservations.
        if self.mode == "TEST" or any(
            i["kind"] == "SETTLEMENT" for i in self.store.intents(active_only=True).values()
        ):
            for oid, i in self.store.intents(active_only=True).items():
                if self.mode != "TEST" and i["kind"] != "SETTLEMENT":
                    continue
                o = self.native.cache.order(ClientOrderId(oid))
                if o is None or o.is_closed:
                    i["terminal"] = True
                    self.store.save_intent(oid, i)
                elif not i.get("terminal"):
                    from nautilus_trader.model.enums import OrderStatus

                    if o.status == OrderStatus.INITIALIZED:
                        self.client.generate_order_denied(
                            o.strategy_id,
                            o.instrument_id,
                            o.client_order_id,
                            "重启恢复：未提交模拟订单",
                            self.now,
                        )
                    else:
                        TestExecution.cancel_order(
                            self.client,
                            type("Cancel", (), {"client_order_id": o.client_order_id})(),
                        )

    def on_native(self, event):
        if self.replaying:
            return
        intent = self.store.intent(getattr(event, "client_order_id", ""))
        if intent is None:
            return  # Never claim external/manual orders.
        if intent["generation"] != self.store.generation:
            return
        seq = self.store.journal(event)
        if seq > self.business["cursor"] and not self.faulted:
            self.project(seq, event)

    def freeze_fill_rules(self, event):
        if isinstance(event, OrderFilled) and event.order_side == OrderSide.BUY:
            intent = self.store.intent(event.client_order_id)
            t = self.tokens[intent["token_id"]]
            event.info.setdefault(
                "pm_rules",
                {
                    "budget": self.preferences.budget,
                    "stop_enabled": self.preferences.stopLossEnabled,
                    "stop_multiplier": micros(self.preferences.stopLossMultiplier),
                    "target": target_price(
                        micros(event.last_px.as_decimal()), t.tick, self.preferences
                    ),
                },
            )

    def project(self, seq, event):
        intent = self.store.intent(getattr(event, "client_order_id", ""))
        if intent is None:
            return
        tid = intent["token_id"]
        t = self.tokens[tid]
        previous_business = deepcopy(self.business)
        previous_book = deepcopy(self.books[tid])
        try:
            self._project_transaction(seq, event, intent, t)
        except Exception as exc:
            self.business.clear()
            self.business.update(previous_business)
            self.books[tid] = previous_book
            self.faulted = f"业务投影未提交，等待重启重放: {type(exc).__name__}"
            self.store.put("status", "PAUSED")
            raise

    def _project_transaction(self, seq, event, intent, t):
        with self.store.transaction():
            if isinstance(event, OrderFilled):
                self.apply_fill(event, intent, t)
            if type(event).__name__ in TERMINAL:
                intent["terminal"] = True
            elif isinstance(event, OrderFilled):
                o = self.native.cache.order(event.client_order_id)
                intent["terminal"] = (
                    bool(intent.get("verified_terminal"))
                    if self.mode == "LIVE" and intent["kind"] != "SETTLEMENT"
                    else o.is_closed
                    if o
                    else False
                )
            self.store.save_intent(event.client_order_id, intent)
            self.business["cursor"] = seq
            self.release_cycle(t.event_id)
            self.store.put("business", self.business)

    def apply_fill(self, e, intent, t):
        info = e.info or {}
        qty = micros(e.last_qty.as_decimal())
        p = micros(e.last_px.as_decimal())
        gross = info.get("pm_gross", qty)
        amount = info.get(
            "pm_amount",
            cost(p, qty)
            + (
                micros(e.commission.as_decimal())
                if e.order_side == OrderSide.BUY
                else -micros(e.commission.as_decimal())
            ),
        )
        cycles = self.business["cycles"]
        targets = self.business["targets"]
        cycle = cycles.get(t.event_id)
        if e.order_side == OrderSide.BUY:
            frozen = info.get("pm_rules", {})
            if cycle is None:
                cycle = {
                    "id": str(e.trade_id),
                    "token_id": t.token_id,
                    "budget": frozen.get("budget", self.preferences.budget),
                    "spent": 0,
                    "quantity": 0,
                    "cost": 0,
                    "sold": False,
                    "stop": asdict(
                        StopLoss(
                            frozen.get("stop_enabled", self.preferences.stopLossEnabled),
                            frozen.get(
                                "stop_multiplier", micros(self.preferences.stopLossMultiplier)
                            ),
                        )
                    ),
                }
                cycles[t.event_id] = cycle
            if cycle["token_id"] != t.token_id:
                raise ValueError("同Event出现兄弟成交冲突")
            cycle["spent"] += amount
            cycle["quantity"] += qty
            cycle["cost"] += amount
            intent["spent"] = intent.get("spent", 0) + amount
            if cycle["spent"] > cycle["budget"]:
                raise ValueError("实际成交超过冻结预算")
            stop = StopLoss(**cycle["stop"])
            stop.add(p, gross)
            cycle["stop"] = asdict(stop)
            key = f"{e.client_order_id}:{e.trade_id}"
            targets[key] = {
                "id": key,
                "event_id": t.event_id,
                "token_id": t.token_id,
                "price": frozen.get("target", target_price(p, t.tick, self.preferences)),
                "qty": qty,
                "created": e.ts_event,
            }
        else:
            if cycle is None or qty > cycle["quantity"]:
                raise ValueError("卖出超过本程序仓位")
            allocated_cost = (
                cycle["cost"]
                if qty == cycle["quantity"]
                else cycle["cost"] * qty // cycle["quantity"]
            )
            cycle["quantity"] -= qty
            cycle["cost"] -= allocated_cost
            cycle["sold"] = True
            self.business["realized"] += amount - allocated_cost
            if intent["kind"] == "SETTLEMENT":
                claim = self.business["claims"][t.condition_id]
                claim["state"] = "CREDITED"
                claim["credited_event"] = str(e.trade_id)
            remain = qty
            ids = intent.get("targets", list(targets))
            for key in ids:
                target = targets.get(key)
                if target is None or target["event_id"] != t.event_id:
                    continue
                used = min(remain, target["qty"])
                target["qty"] -= used
                remain -= used
                if target["qty"] == 0:
                    del targets[key]
                if not remain:
                    break
        if self.mode == "TEST" and intent["kind"] != "SETTLEMENT":
            b = self.books[t.token_id]
            (b.ask if e.order_side == OrderSide.BUY else b.bid).consume(p, gross)
            self.store.save_book(t.token_id, b)
        self.dirty.add(t.event_id)

    def release_cycle(self, eid):
        cycle = self.business["cycles"].get(eid)
        if cycle is None or cycle["quantity"]:
            return
        active = bool(self.active_intents(eid))
        if active:
            return
        if cycle["stop"]["state"] == "EXITING" and eid not in self.business["banned"]:
            self.business["banned"].append(eid)
        self.business["cycles"].pop(eid, None)
        self.business["targets"] = {
            k: v for k, v in self.business["targets"].items() if v["event_id"] != eid
        }

    def active_intents(self, eid):
        return self.store.intents(active_only=True, event_id=eid)

    def evaluate(self, eid, dispatch=True):
        self.evaluations[eid] = "NO_WINNER", None
        cycle = self.business["cycles"].get(eid)
        ids = self.events.get(eid, set())
        active = self.active_intents(eid)
        winner = None
        status = "NO_WINNER"
        if cycle:
            t = self.tokens[cycle["token_id"]]
            b = self.books[t.token_id]
            if t.condition_id in self.business["settled"]:
                self.evaluations[eid] = "NO_WINNER", None
                return
            stop = StopLoss(**cycle["stop"])
            available_bids = (
                b.bid.available() if self.mode == "TEST" else list(b.bid.external.items())
            )
            bid = max((p for p, q in available_bids if q), default=None)
            stop.observe(bid, b.bid.version, self.now, b.ready)
            if dispatch:
                cycle["stop"] = asdict(stop)
            if dispatch and stop.state == "EXITING":
                if eid not in self.business["banned"]:
                    self.business["banned"].append(eid)
                self.cancel_buys(eid)
                self.business["targets"] = {
                    k: v for k, v in self.business["targets"].items() if v["event_id"] != eid
                }
            if dispatch:
                self.store.put("business", self.business)
            if b.ready and not any(i["side"] == "SELL" for i in active.values()):
                if stop.state == "EXITING" and bid and cycle["quantity"] >= t.min_size:
                    if dispatch:
                        self.submit(
                            t,
                            "SELL",
                            cycle["quantity"],
                            min(p for p, q in available_bids if q),
                            kind="STOP",
                        )
                    return
                targets = [
                    Target(v["id"], v["price"], v["qty"], v["created"])
                    for v in self.business["targets"].values()
                    if v["event_id"] == eid
                ]
                batches = sell_batches(available_bids, targets, t.min_size, t.fees)
                if batches:
                    batch = batches[0]
                    if dispatch:
                        self.submit(
                            t,
                            "SELL",
                            batch.quantity,
                            batch.limit,
                            targets=[x.id for x in batch.targets],
                        )
                    return
            if cycle["sold"] or stop.state in ("ARMED", "EXITING", "STOPPED"):
                self.evaluations[eid] = "NO_WINNER", None
                return
            ids = {t.token_id} if t.token_id in self.events.get(eid, set()) else set()
        elif any(not self.books[tid].ready for tid in ids):
            self.evaluations[eid] = "INCOMPLETE", None
            return
        if eid in self.business["banned"] or active:
            self.evaluations[eid] = "NO_WINNER", None
            return
        budget = cycle["budget"] if cycle else self.preferences.budget
        remaining = budget - (cycle["spent"] if cycle else 0)
        cash = self.cash() - self.held_cash()
        if not cycle and cash < budget:
            self.evaluations[eid] = "NO_WINNER", None
            return
        opportunities = []
        for tid in ids:
            t = self.tokens[tid]
            b = self.books[tid]
            if not b.ready or static_reason(t, self.preferences, self.now, self.official_tags):
                continue
            bid = b.bid.available() if self.mode == "TEST" else list(b.bid.external.items())
            ask = b.ask.available() if self.mode == "TEST" else list(b.ask.external.items())
            p = preview(t, self.preferences, bid, ask, min(cash, remaining), budget)
            if p:
                opportunities.append(p)
        winner = arbitrate(opportunities, self.preferences, self.now)
        if winner:
            status = "READY"
        self.evaluations[eid] = status, winner
        if dispatch and winner and self.status == "RUNNING":
            # Evaluation is synchronous with persistence and dispatch; no await may
            # be inserted here without repeating the complete decision.
            self.submit(
                winner.token,
                "BUY",
                sum(f.gross for f in winner.fills),
                self.preferences.max_price,
                cash=min(cash, remaining),
            )

    def drain(self):
        if self.faulted:
            return
        # One pass plus exits created by fills. Do not turn zero-time liquidity into
        # an unbounded new-cycle loop. A later event/control tick permits re-entry.
        pending = self.dirty
        self.dirty = set()
        # Current readiness must be established before cross-Event cash allocation.
        # Preview pass has no order, stop, budget or journal side effects.
        for eid in pending:
            self.evaluate(eid, dispatch=False)

        def key(eid):
            ready = self.evaluations.get(eid, ("NO_WINNER", None))[0] == "READY"
            ts = [self.tokens[x] for x in self.events.get(eid, set())]
            progress = min((t.progress(self.now) for t in ts), default=0)
            return (
                not ready,
                progress * (1 if self.preferences.candidateSortDirection == "ASC" else -1),
                eid,
            )

        for eid in sorted(pending, key=key):
            self.evaluate(eid)
        exits = [eid for eid in self.dirty if eid in self.business["cycles"]]
        self.dirty = set()
        for eid in exits:
            self.evaluate(eid)

    def submit(self, t, side, qty, limit, cash=0, kind="TARGET", targets=None):
        if side == "BUY":
            kind = "BUY"
        oid = ClientOrderId(f"PM-{self.mode}-{uuid4().hex}")
        intent = {
            "event_id": t.event_id,
            "token_id": t.token_id,
            "side": side,
            "cash": cash,
            "spent": 0,
            "quantity": qty,
            "limit": limit,
            "kind": kind,
            "targets": targets or [],
            "generation": self.store.generation,
            "created": self.now,
            "terminal": False,
            "fees": asdict(t.fees),
        }
        if side == "BUY":
            order = self.strategy.order_factory.market(
                instrument_id(t),
                OrderSide.BUY,
                quantity(cash),
                quote_quantity=True,
                time_in_force=TimeInForce.IOC,
                client_order_id=oid,
            )
        else:
            order = self.strategy.order_factory.limit(
                instrument_id(t),
                OrderSide.SELL,
                quantity(qty),
                price(max(1, limit)),
                time_in_force=TimeInForce.IOC,
                client_order_id=oid,
            )
        self.store.save_intent(oid, intent)
        self.store.journal(order.init_event)
        self.strategy.submit_order(order)
        return str(oid)

    def settlement_fill(self, claim):
        if claim["state"] == "CREDITED" or self.active_intents(claim["event_id"]):
            return
        t = self.tokens[claim["token_id"]]
        self.submit(
            t, "SELL", claim["rights"][t.token_id], claim["payouts"][t.token_id], kind="SETTLEMENT"
        )

    def cancel_buys(self, eid=None):
        count = 0
        for oid, intent in self.store.intents(active_only=True, event_id=eid).items():
            if (
                intent["side"] == "BUY"
                and not intent.get("terminal", False)
                and (eid is None or intent["event_id"] == eid)
            ):
                order = self.native.cache.order(ClientOrderId(oid))
                if order and not order.is_closed:
                    self.strategy.cancel_order(order)
                    count += 1
        return count

    def start(self):
        if self.mode == "LIVE" and not self.client.ready:
            raise ValueError("LIVE 尚未完成实际账户核对")
        result = self.validate()
        if not result["ok"]:
            raise ValueError("账本核对失败，不能启动: " + ", ".join(result["errors"]))
        self.store.put("status", "RUNNING")
        self.refresh_eligibility()
        self.drain()

    def pause(self):
        self.store.put("status", "PAUSED")
        self.cancel_buys()

    def update_preferences(self, changes, capital=None):
        if self.faulted:
            raise ValueError(self.faulted)
        new = Preferences(**(self.preferences.model_dump() | changes))
        if new.budget < self.preferences.budget and any(
            i["side"] == "BUY"
            and not i.get("terminal")
            and i["event_id"] not in self.business["cycles"]
            for i in self.store.intents(active_only=True).values()
        ):
            raise ValueError("在途首笔买单尚未确认，需等待终态后再降低每轮金额")
        initial = self.store.get("initial_capital")
        if capital is not None:
            if self.mode != "TEST":
                raise ValueError("LIVE使用实际余额，不接受模拟初始资金")
            capital = micros(capital)
            if not 0 < capital <= 1_000_000 * SCALE:
                raise ValueError("初始资金无效")
            if capital != initial and (self.status != "PAUSED" or self.store.has_history()):
                raise ValueError("仅暂停且无交易历史时可改初始资金")
            initial = capital
        if self.mode == "TEST" and new.budget > initial:
            raise ValueError("每轮金额不得超过总模拟资金")
        with self.store.transaction():
            self.store.put("preferences", new.model_dump(mode="json"))
            if self.mode == "TEST":
                self.store.put("initial_capital", initial)
        self.preferences = new
        count = self.cancel_buys()
        self.refresh_eligibility()
        if self.mode == "TEST":
            self.client.account()
        self.drain()
        return count

    def validate(self):
        errors = []
        if self.faulted:
            errors.append(self.faulted)
        if self.business.get("recovery_error"):
            errors.append(self.business["recovery_error"])
        if self.store.db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            errors.append("SQLite integrity")
        for eid, c in self.business["cycles"].items():
            t = self.tokens[c["token_id"]]
            positions = self.native.cache.positions_open(instrument_id=instrument_id(t))
            native = sum(micros(p.quantity.as_decimal()) for p in positions)
            if native != c["quantity"]:
                errors.append(f"{eid}:框架与周期数量不一致")
            if c["spent"] > c["budget"] or c["cost"] < 0:
                errors.append(f"{eid}:预算/成本不一致")
            target_qty = sum(
                x["qty"] for x in self.business["targets"].values() if x["event_id"] == eid
            )
            if (
                t.condition_id not in self.business["settled"]
                and c["stop"]["state"] not in ("EXITING", "STOPPED")
                and target_qty != c["quantity"]
            ):
                errors.append(f"{eid}:目标覆盖不一致")
        cycle_instruments = {
            str(instrument_id(self.tokens[c["token_id"]])) for c in self.business["cycles"].values()
        }
        for position in self.native.cache.positions_open():
            if str(position.instrument_id) not in cycle_instruments:
                errors.append(f"{position.instrument_id}:框架持仓缺少业务周期")
        if self.mode == "TEST" and self.test_cash() < 0:
            errors.append("模拟现金为负")
        if self.mode == "TEST" and self.test_cash() != self.cash():
            errors.append("模拟现金与原生成交事件不一致")
        if self.held_cash() > self.cash():
            errors.append("在途现金占用超过可用余额")
        return {"ok": not errors, "errors": errors, "mode": self.mode}

    def close(self):
        self.pause()
        self.native.stop()
        self.store.close()
