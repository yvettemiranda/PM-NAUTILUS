"""Read-only projections for the retained PM-SMALL UI."""

from datetime import datetime, timezone
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.enums import OrderSide
from .config import SCALE, units, micros
from .rules import StopLoss, cost


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


def dashboard(r, service=None, limit=20, live_enabled=False):
    positions = []
    values = []
    costs = 0
    for eid, c in r.business["cycles"].items():
        if c["quantity"] <= 0 or r.tokens[c["token_id"]].condition_id in r.business["settled"]:
            continue
        t = r.tokens[c["token_id"]]
        v = token_view(r, t)
        stop = StopLoss(**c["stop"])
        bid = micros(v["bestBid"]) if v["bestBid"] is not None else None
        value = (
            None
            if not r.books[t.token_id].ready
            else max(0, cost(bid or 0, c["quantity"]) - t.fees.fee(c["quantity"], bid or 0))
        )
        targets = sorted(
            {x["price"] for x in r.business["targets"].values() if x["event_id"] == eid}
        )
        v.update(
            quantity=units(c["quantity"]),
            averageBuyPrice=units(c["cost"] * SCALE // c["quantity"]),
            cycleBudget=units(c["budget"]),
            cycleSpent=units(c["spent"]),
            targetSellPrice=units(targets[0]) if targets else None,
            targetSellPrices=list(map(units, targets)),
            stopLossThreshold=units(stop.threshold) if stop.enabled else None,
            stopLossMultiplier=units(stop.multiplier),
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
    pending = [c for c in r.business["claims"].values() if c["state"] != "CREDITED"]
    receivable = sum(c["amount"] for c in pending if not c.get("cash_included"))
    claim_value = sum(c["amount"] for c in pending)
    claim_cost = sum(c["cost"] for c in pending)
    value = None if None in values else sum(values)
    total = None if value is None else r.cash() + value + receivable
    events = []
    for eid, ids in r.events.items():
        if not ids:
            continue
        status, winner = r.evaluations.get(eid, ("INCOMPLETE", None))
        tid = winner.token.token_id if winner else min(ids)
        t = r.tokens[tid]
        v = token_view(r, t)
        outcomes = [
            token_view(r, r.tokens[x]) | {"isWinner": bool(winner and x == tid)}
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
                "locked": eid in r.business["cycles"] or bool(r.active_intents(eid)),
                "winner": v if winner else None,
                "representative": v,
                "outcomes": outcomes,
            }
        )
    direction = 1 if r.preferences.candidateSortDirection == "ASC" else -1
    events.sort(
        key=lambda e: (e["status"] != "READY", e["progressPercent"] * direction, e["eventId"])
    )
    scan = dict(service.scan_status) if service else r.store.get("scan", {})
    scan.update(
        events=events[:limit],
        eventCount=sum(e["status"] == "READY" for e in events),
        displayEventCount=len(events),
        pendingEventCount=sum(e["status"] == "INCOMPLETE" for e in events),
        tokenCount=len(r.monitored),
        diagnostics={
            "availableCategories": service.categories if service else r.store.get("categories", []),
            "eventCount": scan.get("eventCountScanned", 0),
        },
    )
    return {
        "version": "0.1.0",
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
        "portfolio": {
            "totalFunds": units(total),
            "realizedPnl": units(r.business["realized"]),
            "unrealizedPnl": units(
                None if value is None else value + claim_value - costs - claim_cost
            ),
            "positionValue": units(value),
            "availableCash": units(r.cash() - r.held_cash()),
            "reservedCash": units(r.held_cash()),
            "pendingRedemption": units(receivable),
        },
        "redemptions": [
            {
                k: v
                for k, v in c.items()
                if k in {"condition_id", "state", "amount", "error", "tx_hash"}
            }
            for c in r.business["claims"].values()
        ],
    }


def records(r, limit):
    out = []
    qty = {}
    basis = {}
    for _, e in r.store.events():
        if not isinstance(e, OrderFilled):
            continue
        i = r.store.intent(e.client_order_id)
        if not i:
            continue
        t = r.tokens[i["token_id"]]
        key = t.token_id
        q = micros(e.last_qty.as_decimal())
        amount = e.info["pm_amount"]
        before = qty.get(key, 0)
        pnl = None
        if e.order_side == OrderSide.BUY:
            kind = "OPEN" if before == 0 else "ADD"
            qty[key] = before + q
            basis[key] = basis.get(key, 0) + amount
        else:
            kind = (
                "SETTLEMENT"
                if i["kind"] == "SETTLEMENT"
                else "CLOSE"
                if q == before
                else "PARTIAL_CLOSE"
            )
            used = basis.get(key, 0) if q == before else basis.get(key, 0) * q // max(before, 1)
            pnl = amount - used
            qty[key] = before - q
            basis[key] = basis.get(key, 0) - used
        out.append(
            {
                "id": str(e.trade_id),
                "type": kind,
                "eventTitle": t.event_title,
                "marketQuestion": t.question,
                "marketUrl": f"https://polymarket.com/event/{t.slug}" if t.slug else None,
                "direction": t.direction,
                "price": str(e.last_px),
                "quantity": units(q),
                "amount": units(amount),
                "realizedPnl": units(pnl),
                "occurredAt": iso(e.ts_event),
                "winningOutcome": None,
            }
        )
    return {"records": list(reversed(out))[:limit], "totalCount": len(out)}
