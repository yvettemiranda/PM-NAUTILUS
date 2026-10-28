"""Read-only projections for the retained PM-SMALL UI."""

from datetime import datetime, timezone
import asyncio
import sqlite3
from contextlib import closing
from collections import deque
from itertools import chain, islice
from nautilus_trader.model.enums import OrderSide
from .config import SCALE, units, micros
from .rules import StopLoss, cost

RECORD_CACHE_ORDERS = 10_000  # The API's largest history page.
PROJECTION_BATCH_ROWS = 128


def iso(ns):
    return datetime.fromtimestamp(ns / 1_000_000_000, timezone.utc).isoformat()


def market_availability(r, token_id):
    states = getattr(r, "market_availability", {})
    state = states.get(token_id)
    if state is None and (token := r.tokens.get(token_id)) is not None:
        state = next(
            (
                item | {"tokenId": token_id}
                for item in states.values()
                if item.get("status") == "UNAVAILABLE"
                and item.get("marketId") == token.market_id
                and item.get("conditionId") == token.condition_id
            ),
            None,
        )
    return dict(state) if isinstance(state, dict) and state.get("status") == "UNAVAILABLE" else None


def token_view(r, t):
    b = r.books[t.token_id]
    unavailable = market_availability(r, t.token_id)
    ready = b.ready and t.token_id not in getattr(r, "market_recovering", set())
    bids = b.bid.available() if r.mode == "TEST" else list(b.bid.external.items())
    asks = b.ask.available() if r.mode == "TEST" else list(b.ask.external.items())
    bid = max((p for p, q in bids if q), default=None) if ready and not unavailable else None
    ask = min((p for p, q in asks if q), default=None) if ready and not unavailable else None
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
        "marketAvailability": unavailable,
        "currentSellPriceStatus": "UNAVAILABLE"
        if unavailable
        else "NOT_READY"
        if not ready
        else "NO_BID"
        if bid is None
        else "READY",
    }


def portfolio_view(r, position_limit=None):
    positions = []
    valued_rows = []
    unvalued_rows = []
    values = []
    costs = 0
    known_costs = 0
    unpriced_costs = 0
    unavailable_count = 0
    not_ready_count = 0
    settled = set(r.business["settled"])
    targets_by_event = {}
    for target in r.business["targets"].values() if position_limit != 0 else ():
        targets_by_event.setdefault(target["event_id"], set()).add(target["price"])
    for eid, c in r.business["cycles"].items():
        if c["quantity"] <= 0 or r.tokens[c["token_id"]].condition_id in settled:
            continue
        t = r.tokens[c["token_id"]]
        book = r.books[t.token_id]
        unavailable = market_availability(r, t.token_id)
        ready = book.ready and t.token_id not in getattr(r, "market_recovering", set())
        unavailable_count += bool(unavailable)
        not_ready_count += not unavailable and not ready
        value = None
        sellable = None
        if ready and not unavailable:
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
        if value is None:
            unpriced_costs += c["cost"]
        else:
            known_costs += c["cost"]
        # Partition the complete owned universe before limiting the visible page.
        # Both partitions preserve their existing cycle order. A ready empty book
        # is a known zero value and remains with the normally priced positions.
        if position_limit != 0:
            rows = unvalued_rows if value is None else valued_rows
            rows.append((eid, c, t, value, sellable))
        values.append(value)
        costs += c["cost"]
    rows = chain(valued_rows, unvalued_rows)
    visible = rows if position_limit is None else islice(rows, max(0, position_limit))
    for eid, c, t, value, sellable in visible:
        v = token_view(r, t)
        stop = StopLoss(**c["stop"])
        targets = sorted(targets_by_event.get(eid, ()))
        v.update(
            quantity=units(c["quantity"]),
            averageBuyPrice=units(c["cost"] * SCALE // c["quantity"]),
            remainingCost=units(c["cost"]),
            unrealizedPnl=units(None if value is None else value - c["cost"]),
            estimatedUnrealizedPnl=units(0 if value is None else value - c["cost"]),
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
    claims = list(r.business["claims"].values())
    pending = [c for c in claims if c["state"] not in {"CREDITED", "FAILED"}]
    failed = [c for c in claims if c["state"] == "FAILED"]
    receivable = sum(c["amount"] for c in pending if not c.get("cash_included"))
    claim_value = sum(c["amount"] for c in pending)
    claim_cost = sum(c["cost"] for c in pending)
    value = None if None in values else sum(values)
    known_value = sum(v for v in values if v is not None)
    failed_cost = sum(c["cost"] for c in failed)
    failed_cost_fallback = sum(c["cost"] for c in failed if not c.get("cash_included"))
    # Strict totals retain their original unknown semantics for analytics. The
    # separate display estimate carries unpriced owned rights at remaining cost;
    # this is neither a quote nor confirmed cash, profit or redemption proceeds.
    cash = r.cash()
    total = None if value is None or failed else cash + value + receivable
    estimated_position = known_value + unpriced_costs
    estimated_total = cash + estimated_position + receivable + failed_cost_fallback
    known_unrealized = known_value - known_costs + claim_value - claim_cost
    held_cash = r.held_cash()
    portfolio = {
        "totalFunds": units(total),
        "realizedPnl": units(r.business["realized"]),
        "unrealizedPnl": units(
            None if value is None or failed else value + claim_value - costs - claim_cost
        ),
        "positionValue": units(value),
        "knownPositionValue": units(known_value),
        "unpricedPositionCost": units(unpriced_costs),
        "estimatedPositionValue": units(estimated_position),
        "estimatedTotalFunds": units(estimated_total),
        "knownUnrealizedPnl": units(known_unrealized),
        "knownTotalPnl": units(r.business["realized"] + known_unrealized),
        "valuedPositionCount": sum(v is not None for v in values),
        "unvaluedPositionCount": sum(v is None for v in values),
        "unavailablePositionCount": unavailable_count,
        "notReadyPositionCount": not_ready_count,
        "availableCash": units(cash - held_cash),
        "reservedCash": units(held_cash),
        "pendingRedemption": units(receivable),
        "failedRedemption": units(sum(c["amount"] for c in failed)),
        "failedRedemptionCost": units(failed_cost),
        "failedRedemptionCostFallback": units(failed_cost_fallback),
        "failedRedemptionCount": len(failed),
        "failedRedemptionCashIncludedCount": sum(bool(c.get("cash_included")) for c in failed),
    }
    return positions, portfolio


def dashboard(r, service=None, limit=20, live_enabled=False, position_limit=None):
    test_preview = getattr(r, "refresh_test_market_preview", None) if r.mode == "TEST" else None
    if limit and test_preview is not None:
        test_preview()
    pending_previews = r.test_market_pending_events() if test_preview is not None else ()
    positions, portfolio = portfolio_view(r, position_limit)
    settled = set(r.business["settled"])
    position_cycles = [
        c
        for c in r.business["cycles"].values()
        if c["quantity"] > 0 and r.tokens[c["token_id"]].condition_id not in settled
    ]
    # Rank and diagnose every monitored event, but only build depth-backed token
    # projections for the visible page. The page size never changes eligibility.
    summaries = []
    for eid, ids in r.events.items():
        if not ids:
            continue
        status, winner = r.evaluations.get(eid, ("INCOMPLETE", None))
        if eid in pending_previews:
            status, winner = "INCOMPLETE", None
        tid = winner.token.token_id if winner else min(ids)
        missing = sorted(x for x in ids if not r.books[x].ready)
        summaries.append(
            {
                "eventId": eid,
                "status": status,
                "winner": winner,
                "tokenId": tid,
                "ids": ids,
                "progressPercent": float(r.tokens[tid].progress(r.now)) if limit else 0,
                "pendingReason": ("AWAITING_FULL_BOOKS" if missing else "AWAITING_EVALUATION")
                if status == "INCOMPLETE"
                else None,
                "missingBookTokenIds": missing,
            }
        )
    direction = 1 if r.preferences.candidateSortDirection == "ASC" else -1
    if limit:
        summaries.sort(
            key=lambda e: (e["status"] != "READY", e["progressPercent"] * direction, e["eventId"])
        )
    active_events = (
        {i["event_id"] for i in r.store.intents(active_only=True).values()} if limit else set()
    )
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
                if limit and e["status"] == "INCOMPLETE"
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
        "positionCount": len(position_cycles),
        "positionBooksReady": portfolio["unvaluedPositionCount"] == 0,
        **{
            key: portfolio[key]
            for key in (
                "valuedPositionCount",
                "unvaluedPositionCount",
                "unavailablePositionCount",
                "notReadyPositionCount",
            )
        },
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
            self.apply_payload(r, seq, payload)
        self.cursor = head

    def apply_payload(self, r, seq, payload):
        e = r.store.serializer.deserialize(payload)
        order_id = str(e.client_order_id)
        group = self.groups.get(order_id)
        intent = group["intent"] if group else r.store.intent(e.client_order_id)
        if intent:
            # A late fill of an evicted order must not become a new history row.
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
            self.apply(e, intent, retired, seq)
        else:
            # Rebuild if a missing durable identity is subsequently repaired.
            self.incomplete = True

    async def update_async(self, r):
        head = r.store.db.execute("SELECT max(seq) FROM native_events").fetchone()[0] or 0
        while self.cursor < head:
            rows = r.store.db.execute(
                "SELECT seq,CASE WHEN kind='OrderFilled' THEN payload END "
                "FROM native_events NOT INDEXED "
                "WHERE seq>? AND seq<=? ORDER BY seq LIMIT ?",
                (self.cursor, head, PROJECTION_BATCH_ROWS),
            ).fetchall()
            for seq, payload in rows:
                if payload is not None:
                    self.apply_payload(r, seq, payload)
            self.cursor = rows[-1][0] if len(rows) == PROJECTION_BATCH_ROWS else head
            # Yield after bounded work, retaining all cost-basis history. Neither
            # the writable connection nor native objects move to another thread.
            await asyncio.sleep(0)
            if r.store._closed or r.store.generation != self.generation:
                raise ValueError("交易历史构建期间账本已更改，请重新读取")

    def apply(self, e, intent, retired=False, seq=0):
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
            "first_seq": seq,
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
            out.append(self.row(r, order_id))
        return {"records": out, "totalCount": self.total_count}

    def row(self, r, order_id, paged=False):
        group = self.groups[order_id]
        t = r.tokens[group["intent"]["token_id"]]
        # Refresh mutable discovery metadata without exposing cached records.
        row = group["row"] | {
            "eventTitle": t.event_title,
            "marketQuestion": t.question,
            "marketUrl": f"https://polymarket.com/event/{t.slug}" if t.slug else None,
            "direction": t.direction,
        }
        return row | {"firstSeq": group["first_seq"]} if paged else row


def records(r, limit):
    lock = getattr(r, "_record_projection_lock", None)
    if lock is not None and lock.locked():
        raise ValueError("交易历史正在更新，请重新读取")
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


async def records_async(r, limit):
    lock = getattr(r, "_record_projection_lock", None)
    if lock is None:
        lock = r._record_projection_lock = asyncio.Lock()
    async with lock:
        generation = r.store.generation
        projection = getattr(r, "_record_projection", None)
        if (
            projection is None
            or projection.generation != generation
            or projection.incomplete
            or projection.capacity < limit
        ):
            projection = _RecordProjection(generation, max(RECORD_CACHE_ORDERS, limit))
        try:
            await projection.update_async(r)
            if r.store._closed or generation != r.store.generation:
                raise ValueError("交易历史构建期间账本已更改，请重新读取")
            result = projection.view(r, limit)
            r._record_projection = projection
            return result
        except BaseException:
            # Cancellation/lifecycle changes must never publish partial basis.
            r._record_projection = None
            raise


class _SelectedRecords(_RecordProjection):
    """Replay all cost basis while retaining only one requested history page."""

    def __init__(self, generation, selected):
        super().__init__(generation, max(1, len(selected)))
        self.selected = set(selected)

    def apply_payload(self, r, seq, payload):
        event = r.store.serializer.deserialize(payload)
        intent = r.store.intent(event.client_order_id)
        if intent:
            self.apply(event, intent, str(event.client_order_id) not in self.selected, seq)


def _history_ids(path, head, before, limit, since):
    # This connection is independent of the writable runtime connection and
    # always closed, including cancellation of the awaiting HTTP request.
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        groups = (
            "SELECT i.order_id,min(e.seq) AS first_seq FROM native_events e "
            "JOIN intents i ON i.order_id=json_extract(CAST(e.payload AS TEXT),'$.client_order_id') "
            "WHERE e.kind='OrderFilled' AND e.seq<=? GROUP BY i.order_id "
        )
        page = (
            db.execute(
                groups + "HAVING min(e.seq)<? ORDER BY first_seq DESC LIMIT ?",
                (head, before, limit),
            ).fetchall()
            if before
            else []
        )
        changed = (
            db.execute(
                "SELECT i.order_id FROM native_events e NOT INDEXED JOIN intents i "
                "ON i.order_id=json_extract(CAST(e.payload AS TEXT),'$.client_order_id') "
                "WHERE e.kind='OrderFilled' AND e.seq>? AND e.seq<=? GROUP BY i.order_id",
                (since, head),
            ).fetchall()
            if since is not None and since < head
            else []
        )
        return [row[0] for row in page], [row[0] for row in changed]


def _history_cursor(value, generation):
    if value is None:
        return None
    try:
        owner, number = value.rsplit(":", 1)
        number = int(number)
        if owner != generation or not 0 <= number <= 2**63 - 1:
            raise ValueError
        return number
    except (AttributeError, ValueError):
        raise ValueError("交易历史游标已失效，请重新读取") from None


async def records_page_async(r, limit=20, cursor=None, since=None):
    """Stable first-fill keyset pages and incremental updates, without a 10k cap."""
    await records_async(r, min(limit, RECORD_CACHE_ORDERS))
    async with r._record_projection_lock:
        generation = r.store.generation
        before = _history_cursor(cursor, generation)
        since_seq = _history_cursor(since, generation)
        projection = r._record_projection
        if projection is None or projection.generation != generation:
            raise ValueError("交易历史构建期间账本已更改，请重新读取")
        head = projection.cursor
        if since_seq is not None and since_seq > head:
            raise ValueError("交易历史游标已失效，请重新读取")
        page_ids, changed_ids = await asyncio.to_thread(
            _history_ids, r.store.path, head, before, limit, since_seq
        )
        if before is None:
            page_ids = list(islice(reversed(projection.order_ids), limit))
        selected = tuple(dict.fromkeys([*page_ids, *changed_ids]))
        missing = tuple(oid for oid in selected if oid not in projection.groups)
        extra = None
        try:
            if missing:
                key = (generation, head, missing)
                cache = getattr(r, "_record_page_cache", {})
                extra = cache.get(key)
                if extra is None:
                    extra = _SelectedRecords(generation, missing)
                    # Use the captured journal head, including late fills of
                    # older orders, while yielding after bounded owner-loop work.
                    while extra.cursor < head:
                        rows = r.store.db.execute(
                            "SELECT seq,CASE WHEN kind='OrderFilled' THEN payload END "
                            "FROM native_events NOT INDEXED WHERE seq>? AND seq<=? "
                            "ORDER BY seq LIMIT ?",
                            (extra.cursor, head, PROJECTION_BATCH_ROWS),
                        ).fetchall()
                        for seq, payload in rows:
                            if payload is not None:
                                extra.apply_payload(r, seq, payload)
                        extra.cursor = rows[-1][0] if len(rows) == PROJECTION_BATCH_ROWS else head
                        await asyncio.sleep(0)
                        if r.store._closed or r.store.generation != generation:
                            raise ValueError("交易历史构建期间账本已更改，请重新读取")
                    extra.qty.clear()
                    extra.basis.clear()
                    cache = {k: v for k, v in cache.items() if k[:2] == key[:2]}
                    if len(cache) >= 8:
                        cache.pop(next(iter(cache)))
                    cache[key] = extra
                    r._record_page_cache = cache
            if r.store._closed or r.store.generation != generation:
                raise ValueError("交易历史构建期间账本已更改，请重新读取")

            def row(oid):
                owner = projection if oid in projection.groups else extra
                return owner.row(r, oid, paged=True)

            page = [row(oid) for oid in page_ids]
            return {
                "records": page,
                "changes": [row(oid) for oid in changed_ids],
                "totalCount": projection.total_count,
                "generation": generation,
                "head": f"{generation}:{head}",
                "nextCursor": f"{generation}:{page[-1]['firstSeq']}" if page else None,
            }
        except BaseException:
            r._record_page_cache = {}
            raise
