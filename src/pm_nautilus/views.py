"""Read-only projections for the retained PM-SMALL UI."""

from datetime import datetime, timezone
from collections import deque
from itertools import islice
from nautilus_trader.model.enums import OrderSide
from .config import SCALE, units, micros
from .rules import StopLoss, cost

RECORD_CACHE_ORDERS = 10_000  # The API's largest history page.


def iso(ns):
    return datetime.fromtimestamp(ns / 1_000_000_000, timezone.utc).isoformat()


def token_view(r, t):
    b = r.books[t.token_id]
    bids = b.bid.available() if r.mode == "TEST" else list(b.bid.external.items())
    asks = b.ask.available() if r.mode == "TEST" else list(b.ask.external.items())
    bid = max((p for p, q in bids if q), default=None) if b.ready else None
    ask = min((p for p, q in asks if q), default=None) if b.ready else None
    return {
        "tokenId": t.token_id,
        "eventId": t.event_id,
        "marketId": t.market_id,
        "eventTitle": t.event_title,
        "marketQuestion": t.question,
        "direction": t.direction,
        "marketUrl": f"https://polymarket.com/event/{t.slug}" if t.slug else None,
        "categoryLabels": list(t.category_labels),
        "bestBid": units(bid),
        "executableBuyPrice": units(ask),
        "openedAt": iso(t.opened_ns),
        "endsAt": iso(t.ends_ns),
        "progressPercent": float(t.progress(r.now)),
        "currentSellPrice": units(bid),
        "currentSellPriceStatus": "NOT_READY"
        if not b.ready
        else "NO_BID"
        if bid is None
        else "READY",
    }


def portfolio_view(r):
    positions = []
    values = []
    costs = 0
    targets_by_event = {}
    for target in r.business["targets"].values():
        targets_by_event.setdefault(target["event_id"], set()).add(target["price"])
    for eid, c in r.business["cycles"].items():
        if c["quantity"] <= 0 or r.tokens[c["token_id"]].condition_id in r.business["settled"]:
            continue
        t = r.tokens[c["token_id"]]
        v = token_view(r, t)
        stop = StopLoss(**c["stop"])
        book = r.books[t.token_id]
        value = None
        sellable = None
        if book.ready:
            remaining = c["quantity"]
            value = 0
            for price, quantity in sorted(
                book.bid.available() if r.mode == "TEST" else book.bid.external.items(),
                reverse=True,
            ):
                filled = min(remaining, quantity)
                if filled <= 0:
                    continue
                value += max(0, cost(price, filled) - t.fees.fee(filled, price))
                remaining -= filled
                if remaining == 0:
                    break
            sellable = c["quantity"] - remaining
        targets = sorted(targets_by_event.get(eid, ()))
        v.update(
            quantity=units(c["quantity"]),
            averageBuyPrice=units(c["cost"] * SCALE // c["quantity"]),
            cycleBudget=units(c["budget"]),
            cycleSpent=units(c["spent"]),
            targetSellPrice=units(targets[0]) if targets else None,
            targetSellPrices=list(map(units, targets)),
            stopLossThreshold=units(stop.threshold) if stop.enabled else None,
            stopLossMultiplier=units(stop.multiplier),
            executableSellValue=units(value),
            executableSellQuantity=units(sellable),
            cycleStatus="STOP_EXITING"
            if stop.state == "EXITING"
            else "STOP_ARMED"
            if stop.state == "ARMED"
            else "EXITING"
            if c["sold"]
            else "ACCUMULATING",
        )
        positions.append(v)
        values.append(value)
        costs += c["cost"]
    claims = list(r.business["claims"].values())
    pending = [c for c in claims if c["state"] not in {"CREDITED", "FAILED"}]
    failed = [c for c in claims if c["state"] == "FAILED"]
    receivable = sum(c["amount"] for c in pending if not c.get("cash_included"))
    claim_value = sum(c["amount"] for c in pending)
    claim_cost = sum(c["cost"] for c in pending)
    value = None if None in values else sum(values)
    # A failed redemption still represents unresolved rights. Neither assume it
    # will pay nor book it as a loss while the actual chain/account result is unknown.
    total = None if value is None or failed else r.cash() + value + receivable
    held_cash = r.held_cash()
    portfolio = {
        "totalFunds": units(total),
        "realizedPnl": units(r.business["realized"]),
        "unrealizedPnl": units(
            None if value is None or failed else value + claim_value - costs - claim_cost
        ),
        "positionValue": units(value),
        "availableCash": units(r.cash() - held_cash),
        "reservedCash": units(held_cash),
        "pendingRedemption": units(receivable),
        "failedRedemption": units(sum(c["amount"] for c in failed)),
    }
    return positions, portfolio


def dashboard(r, service=None, limit=20, live_enabled=False):
    positions, portfolio = portfolio_view(r)
    # Rank and diagnose every monitored event, but only build depth-backed token
    # projections for the visible page. The page size never changes eligibility.
    summaries = []
    for eid, ids in r.events.items():
        if not ids:
            continue
        status, winner = r.evaluations.get(eid, ("INCOMPLETE", None))
        tid = winner.token.token_id if winner else min(ids)
        missing = sorted(x for x in ids if not r.books[x].ready)
        summaries.append(
            {
                "eventId": eid,
                "status": status,
                "winner": winner,
                "tokenId": tid,
                "ids": ids,
                "progressPercent": float(r.tokens[tid].progress(r.now)),
                "pendingReason": ("AWAITING_FULL_BOOKS" if missing else "AWAITING_EVALUATION")
                if status == "INCOMPLETE"
                else None,
                "missingBookTokenIds": missing,
            }
        )
    direction = 1 if r.preferences.candidateSortDirection == "ASC" else -1
    summaries.sort(
        key=lambda e: (e["status"] != "READY", e["progressPercent"] * direction, e["eventId"])
    )
    active_events = {i["event_id"] for i in r.store.intents(active_only=True).values()}
    events = []
    for summary in summaries[:limit]:
        eid, ids = summary["eventId"], summary["ids"]
        status, winner = summary["status"], summary["winner"]
        tid = summary["tokenId"]
        t = r.tokens[tid]
        v = token_view(r, t)
        outcomes = [
            (v if x == tid else token_view(r, r.tokens[x]))
            | {"isWinner": bool(winner and x == tid)}
            for x in sorted(ids)
        ]
        events.append(
            {
                "eventId": eid,
                "eventTitle": t.event_title,
                "marketUrl": v["marketUrl"],
                "progressPercent": v["progressPercent"],
                "openedAt": v["openedAt"],
                "endsAt": v["endsAt"],
                "resultCount": t.result_count,
                "tokenCount": len(ids),
                "eligibleTokenCount": len(ids),
                "marketCount": len({r.tokens[x].market_id for x in ids}),
                "status": status,
                "pendingReason": summary["pendingReason"],
                "missingBookTokenIds": summary["missingBookTokenIds"],
                "locked": eid in r.business["cycles"] or eid in active_events,
                "winner": v if winner else None,
                "representative": v,
                "outcomes": outcomes,
            }
        )
    scan = dict(service.scan_status) if service else r.store.get("scan", {})
    scan.update(
        events=events,
        eventCount=sum(e["status"] == "READY" for e in summaries),
        displayEventCount=len(summaries),
        pendingEventCount=sum(e["status"] == "INCOMPLETE" for e in summaries),
        tokenCount=len(r.monitored),
        diagnostics={
            "availableCategories": service.categories if service else r.store.get("categories", []),
            "eventCount": scan.get("eventCountScanned", 0),
            "streams": service.stream_diagnostics() if service else None,
            "pendingEvents": [
                {k: e[k] for k in ("eventId", "pendingReason", "missingBookTokenIds")}
                for e in summaries
                if e["status"] == "INCOMPLETE"
            ],
        },
    )
    return {
        "version": "0.1.0",
        "generation": r.store.generation,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "executionMode": r.mode,
        "liveExecutionEnabled": live_enabled,
        "strategy": {
            "status": r.status,
            "initialCapital": units(r.store.get("initial_capital")),
            "availableCash": units(r.cash()),
        },
        "capitalEditable": r.mode == "TEST" and r.status == "PAUSED" and not r.store.has_history(),
        "preferences": r.preferences.public(),
        "positions": positions,
        "marketScan": scan,
        "portfolio": portfolio,
        "redemptions": [
            {
                k: v
                for k, v in c.items()
                if k in {"condition_id", "state", "amount", "error", "tx_hash"}
            }
            for c in r.business["claims"].values()
        ],
    }


class _RecordProjection:
    """Incremental read-only journal projection, discarded on generation change."""

    def __init__(self, generation, capacity):
        self.generation = generation
        self.capacity = capacity
        self.cursor = 0
        self.groups = {}
        self.order_ids = deque()
        self.total_count = 0
        self.qty = {}
        self.basis = {}
        self.incomplete = False

    def update(self, r):
        head = r.store.db.execute("SELECT max(seq) FROM native_events").fetchone()[0] or 0
        if head == self.cursor:
            return
        # Scan only the appended rowid range. The kind index would rescan every
        # historical fill and create a sort even for an unchanged UI refresh.
        for seq, payload in r.store.db.execute(
            "SELECT seq,payload FROM native_events NOT INDEXED "
            "WHERE seq>? AND seq<=? AND kind='OrderFilled' ORDER BY seq",
            (self.cursor, head),
        ):
            e = r.store.serializer.deserialize(payload)
            order_id = str(e.client_order_id)
            group = self.groups.get(order_id)
            intent = group["intent"] if group else r.store.intent(e.client_order_id)
            if intent:
                # Once the window is full, a missing group can also be a late
                # fill of an evicted order. Distinguish it by its indexed journal
                # identity without retaining every historical order ID in RAM.
                retired = (
                    group is None
                    and self.total_count >= self.capacity
                    and bool(
                        r.store.db.execute(
                            "SELECT 1 FROM native_events WHERE kind='OrderFilled' "
                            "AND json_extract(CAST(payload AS TEXT),'$.client_order_id')=? "
                            "AND seq<? LIMIT 1",
                            (order_id, seq),
                        ).fetchone()
                    )
                )
                self.apply(e, intent, retired)
            else:
                # A missing identity cannot be silently cached forever. Rebuild
                # next time so a subsequently repaired journal remains readable.
                self.incomplete = True
        self.cursor = head

    def apply(self, e, intent, retired=False):
        key = intent["token_id"]
        q = micros(e.last_qty.as_decimal())
        amount = e.info["pm_amount"]
        before = self.qty.get(key, 0)
        pnl = None
        if e.order_side == OrderSide.BUY:
            kind = "OPEN" if before == 0 else "ADD"
            self.qty[key] = before + q
            self.basis[key] = self.basis.get(key, 0) + amount
        else:
            kind = (
                "SETTLEMENT"
                if intent["kind"] == "SETTLEMENT"
                else "CLOSE"
                if q == before
                else "PARTIAL_CLOSE"
            )
            used = (
                self.basis.get(key, 0)
                if q == before
                else self.basis.get(key, 0) * q // max(before, 1)
            )
            pnl = amount - used
            self.qty[key] = before - q
            self.basis[key] = self.basis.get(key, 0) - used
        if self.qty[key] == 0:
            self.qty.pop(key)
            self.basis.pop(key, None)
        if retired:
            # Preserve the original first-fill ordering and total count while
            # still applying the new fill to future cost-basis calculations.
            return
        group_key = str(e.client_order_id)
        gross = e.info.get("pm_gross", q)
        if group_key in self.groups:
            group = self.groups[group_key]
            group["quantity_micros"] += q
            group["amount_micros"] += amount
            group["gross"] += gross
            group["weighted"] += micros(e.last_px.as_decimal()) * gross
            group["pnl"] += pnl or 0
            row = group["row"]
            row.update(
                quantity=units(group["quantity_micros"]),
                amount=units(group["amount_micros"]),
                price=units(group["weighted"] // group["gross"]),
                realizedPnl=units(group["pnl"]) if pnl is not None else None,
            )
            if e.order_side == OrderSide.SELL:
                row["type"] = kind
            return
        row = {
            "id": group_key,
            "type": kind,
            "price": str(e.last_px),
            "quantity": units(q),
            "amount": units(amount),
            "realizedPnl": units(pnl),
            "occurredAt": iso(e.ts_event),
            "winningOutcome": None,
        }
        if len(self.order_ids) == self.capacity:
            self.groups.pop(self.order_ids.popleft())
        self.groups[group_key] = {
            "row": row,
            "intent": {"token_id": key, "kind": intent.get("kind")},
            "quantity_micros": q,
            "amount_micros": amount,
            "gross": gross,
            "weighted": micros(e.last_px.as_decimal()) * gross,
            "pnl": pnl or 0,
        }
        self.order_ids.append(group_key)
        self.total_count += 1

    def view(self, r, limit):
        out = []
        for order_id in islice(reversed(self.order_ids), limit):
            group = self.groups[order_id]
            t = r.tokens[group["intent"]["token_id"]]
            # Discovery metadata can change without another fill. Refresh it for
            # each visible row and never expose a mutable cached record.
            out.append(
                group["row"]
                | {
                    "eventTitle": t.event_title,
                    "marketQuestion": t.question,
                    "marketUrl": f"https://polymarket.com/event/{t.slug}" if t.slug else None,
                    "direction": t.direction,
                }
            )
        return {"records": out, "totalCount": self.total_count}


def records(r, limit):
    generation = r.store.generation
    projection = getattr(r, "_record_projection", None)
    if (
        projection is None
        or projection.generation != generation
        or projection.incomplete
        or projection.capacity < limit
    ):
        projection = _RecordProjection(generation, max(RECORD_CACHE_ORDERS, limit))
        r._record_projection = projection
    try:
        projection.update(r)
        return projection.view(r, limit)
    except Exception:
        # An interrupted projection must not retain partially applied fill state.
        r._record_projection = None
        raise
