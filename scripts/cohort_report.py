"""Read one PM-NAUTILUS ledger and report buy-fill cohorts as JSON."""

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import msgspec
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.serialization.serializer import MsgSpecSerializer

from pm_nautilus.config import SCALE, micros, units
from pm_nautilus.rules import Fees, cost
from pm_nautilus.store import Store


NS_PER_SECOND = 1_000_000_000
NS_PER_DAY = 86_400 * NS_PER_SECOND
ONE_CENT = SCALE // 100
EXIT_KINDS = {"TARGET", "STOP", "SETTLEMENT"}


def _stamp(path):
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@contextmanager
def _snapshot(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError("expected an existing state.sqlite file")
    wal, shm = (path.with_name(path.name + suffix) for suffix in ("-wal", "-shm"))
    has_wal, has_shm = wal.exists(), shm.exists()
    if has_wal != has_shm:
        raise ValueError("incomplete WAL/SHM pair; supply a clean SQLite snapshot")
    with path.open("rb") as stream:
        stream.seek(18)
        wal_format = stream.read(2) == b"\x02\x02"
    # On an idle WAL-format DB, SQLite mode=ro can create empty sidecars.
    # Immutable mode is safe only for a stable main file with no WAL to read.
    immutable = wal_format and not has_wal
    before = _stamp(path) if immutable else None
    uri = path.as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
    with closing(sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        yield db
    if immutable and (wal.exists() or shm.exists() or _stamp(path) != before):
        raise ValueError("ledger changed during the read; rerun on a consistent SQLite snapshot")


def _meta(db, key):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def _timestamp(ns):
    return datetime.fromtimestamp(ns // NS_PER_SECOND, timezone.utc) if ns > 0 else None


def _iso_week(ns):
    stamp = _timestamp(ns)
    if stamp is None:
        return None
    year, week, _ = stamp.isocalendar()
    return f"{year}-W{week:02}"


def _divide(total, weights):
    """Divide integer units by quantity, keeping every rounding remainder."""
    denominator = sum(weights)
    prefix = previous = 0
    for weight in weights:
        prefix += weight
        share = total * prefix // denominator
        yield share - previous
        previous = share


def _ratio(numerator, denominator):
    return format(Decimal(numerator) / denominator, ".6f") if denominator else None


@dataclass
class _Lot:
    cohort: tuple
    quantity: int
    remaining: int
    cost: int
    bought_at: int
    target: int | None
    estimated: bool = False

    @property
    def remaining_cost(self):
        return self.cost - self.cost * (self.quantity - self.remaining) // self.quantity

    def close(self, quantity):
        before = self.remaining_cost
        self.remaining -= quantity
        return before - self.remaining_cost


def _tokens_for_report(db):
    cache = {}

    def token(token_id):
        if token_id not in cache:
            row = db.execute("SELECT payload FROM tokens WHERE token_id=?", (token_id,)).fetchone()
            try:
                data = Store.decode_token_payload(row[0]) if row else None
            except ValueError:
                data = None
            cache[token_id] = data if data and data["token_id"] == token_id else None
        return cache[token_id]

    return token


def _sell_parts(lots, known_targets, quantity, kind, targets):
    """Use proven target ownership, then disclose any estimated lot allocation."""
    remaining = quantity
    parts = []
    used = set()
    if kind == "TARGET" and isinstance(targets, list):
        for target in targets:
            if not isinstance(target, str) or target not in known_targets:
                return parts, remaining  # Unknown earlier target may own these shares.
            if target in used or target not in lots:
                continue
            lot = lots[target]
            take = min(remaining, lot.remaining)
            if take:
                parts.append((lot, take, not lot.estimated, True))
                used.add(target)
                remaining -= take
            if not remaining:
                return parts, 0
    available = [lot for key, lot in lots.items() if key not in used and lot.remaining]
    quantity_available = sum(lot.remaining for lot in available)
    if remaining and remaining <= quantity_available:
        exact = len(available) == 1 or remaining == quantity_available
        if not exact:
            for lot in available:
                lot.estimated = True
        shares = _divide(remaining, [lot.remaining for lot in available])
        parts.extend(
            (lot, share, not lot.estimated, False) for lot, share in zip(available, shares) if share
        )
        remaining = 0
    return parts, remaining


def _apply_sell(groups, parts, unassigned, filled_at, price, amount, fee, kind):
    weights = [share for _, share, _, _ in parts] + ([unassigned] if unassigned else [])
    amounts, fees = list(_divide(amount, weights)), list(_divide(fee, weights))
    for (lot, share, exact, target_link), proceeds, commission in zip(parts, amounts, fees):
        group = groups[lot.cohort]
        group["sold"] += share
        group["proceeds"] += proceeds
        group["sell_fee"] += commission
        group["realized"] += proceeds - lot.close(share)
        if not exact:
            group["estimated_exit"] += share
        if kind == "SETTLEMENT":
            group["settled"] += share
            if price == 0 and amount == 0:
                group["zero_settlement"] += share
        elif kind in {"TARGET", "STOP"}:
            group["market_sold"] += share
            if exact and lot.bought_at > 0 and filled_at >= lot.bought_at:
                group["sell_age_quantity"] += share
                group["sell_age_weight"] += share * (filled_at - lot.bought_at)
            if kind == "TARGET":
                group["target_exit"] += share
                if exact and target_link and lot.target is not None:
                    group["target_outcome_known"] += share
                    if price >= lot.target:
                        group["target_hit"] += share
        else:
            group["unknown_exit"] += share
    if unassigned:
        return unassigned, amounts[-1], fees[-1]
    return 0, 0, 0


def _saved_bid_value(db, mode, token, quantity):
    if not token or not token["open"]:
        return 0, 0, None
    row = db.execute("SELECT payload FROM books WHERE token_id=?", (token["token_id"],)).fetchone()
    if row is None:
        return 0, 0, None
    try:
        book = json.loads(row[0])
        if book["ready"] is not True:
            return 0, 0, None
        bids = book["bid"]
        consumed = bids.get("consumed", {}) if mode == "TEST" else {}
        levels = []
        for raw_price, depth in bids["external"].items():
            price = int(raw_price)
            taken = consumed.get(raw_price, 0)
            if not (
                0 < price < SCALE
                and type(depth) is int
                and type(taken) is int
                and 0 <= taken <= depth
            ):
                return 0, 0, None
            levels.append((price, depth - taken))
        fees = Fees(**token["fees"])
        snapshot = book.get("timestamp", 0)
        stamp = _timestamp(snapshot) if type(snapshot) is int else None
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError, OSError):
        return 0, 0, None
    remaining, value = quantity, 0
    for price, depth in sorted(levels, reverse=True):
        take = min(depth, remaining)
        if take:
            value += max(0, cost(price, take) - fees.fee(take, price))
            remaining -= take
        if not remaining:
            break
    return quantity - remaining, value, stamp


def _holdings(db, mode, business, positions, groups, token_for, unreliable_tokens):
    saved = Counter()
    for cycle in business["cycles"].values():
        saved[cycle["token_id"]] += cycle["quantity"]
    settled = set(business["settled"])
    settlement_tokens = {claim.get("token_id") for claim in business["claims"].values()}
    coverage = Counter()
    stamps = []
    for token_id in sorted(saved.keys() | positions.keys()):
        lots = list(positions.get(token_id, {}).values())
        shares = [lot.remaining for lot in lots]
        total = sum(shares)
        if total != saved[token_id] or token_id in unreliable_tokens:
            coverage["unreconciled_tokens"] += 1
            coverage["unreconciled_saved"] += saved[token_id]
            for lot in lots:
                groups[lot.cohort]["unreconciled_open"] += lot.remaining
            continue
        if not total:
            continue
        token = token_for(token_id)
        pending = token_id in settlement_tokens or (token and token["condition_id"] in settled)
        priced, value, stamp = (0, 0, None) if pending else _saved_bid_value(db, mode, token, total)
        if priced and stamp:
            stamps.append(stamp)
        elif priced:
            coverage["bid_timestamp_unknown"] += priced
        priced_parts = list(_divide(priced, shares))
        values = _divide(value, priced_parts) if priced else [0] * len(lots)
        for lot, priced_share, bid_value in zip(lots, priced_parts, values):
            group = groups[lot.cohort]
            basis = lot.remaining_cost
            group["held"] += lot.remaining
            group["held_cost"] += basis
            group["bid_covered"] += priced_share
            group["bid_value"] += bid_value
            group["unpriced_cost"] += basis - basis * priced_share // lot.remaining
            if lot.estimated:
                group["estimated_held"] += lot.remaining
            if pending:
                group["pending_settlement"] += lot.remaining
    return {
        "savedHeldShares": units(sum(saved.values())),
        "unreconciledTokenCount": coverage["unreconciled_tokens"],
        "unreconciledSavedHeldShares": units(coverage["unreconciled_saved"]),
        "bidTimestampUnknownShares": units(coverage["bid_timestamp_unknown"]),
        "oldestUsedBidSnapshotUtc": min(stamps).isoformat() if stamps else None,
        "newestUsedBidSnapshotUtc": max(stamps).isoformat() if stamps else None,
    }


def _metrics(c):
    fields = {
        "boughtShares": "bought",
        "buyCostU": "buy_cost",
        "buyFeesU": "buy_fee",
        "soldShares": "sold",
        "sellProceedsU": "proceeds",
        "sellFeesU": "sell_fee",
        "realizedPnlU": "realized",
        "marketSoldShares": "market_sold",
        "sellAgeCoveredShares": "sell_age_quantity",
        "estimatedExitAllocationShares": "estimated_exit",
        "unknownExitReasonShares": "unknown_exit",
        "knownFrozenTargetBuyShares": "target_buy_known",
        "targetExitShares": "target_exit",
        "targetPriceCheckedSellShares": "target_outcome_known",
        "verifiedTargetSellShares": "target_hit",
        "settledShares": "settled",
        "zeroSettlementShares": "zero_settlement",
        "heldShares": "held",
        "heldCostU": "held_cost",
        "heldBidCoveredShares": "bid_covered",
        "heldUnpricedCostU": "unpriced_cost",
        "heldPendingSettlementShares": "pending_settlement",
        "estimatedHeldAllocationShares": "estimated_held",
        "unreconciledOpenLotShares": "unreconciled_open",
    }
    covered_cost = c["held_cost"] - c["unpriced_cost"]
    return {
        "buyFillCount": c["buy_fills"],
        **{field: units(c[key]) for field, key in fields.items()},
        "targetSellRate": _ratio(c["target_hit"], c["bought"]),
        "averageSellDays": _ratio(c["sell_age_weight"], c["sell_age_quantity"] * NS_PER_DAY),
        "zeroSettlementRate": _ratio(c["zero_settlement"], c["settled"]),
        "zeroSettlementShareOfBought": _ratio(c["zero_settlement"], c["bought"]),
        "heldBidValueU": units(c["bid_value"]) if c["bid_covered"] else None,
        "heldBidCoveredCostU": units(covered_cost),
        "heldBidValueToCoveredCost": _ratio(c["bid_value"], covered_cost)
        if covered_cost > 0
        else None,
        "heldUnpricedShares": units(c["held"] - c["bid_covered"]),
    }


def _read_report(db):
    mode, generation = _meta(db, "mode"), _meta(db, "generation")
    business = Store.read_business(db)
    if mode not in {"TEST", "LIVE"} or business is None:
        raise ValueError("not a recognized PM-NAUTILUS ledger")
    cursor = business["cursor"]
    if type(cursor) is not int or cursor < 0:
        raise ValueError("invalid ledger projection cursor")
    head = db.execute("SELECT max(seq) FROM native_events").fetchone()[0] or 0
    if cursor > head:
        raise ValueError("ledger projection cursor exceeds its journal")
    serializer = MsgSpecSerializer(encoding=msgspec.json)
    token_for = _tokens_for_report(db)
    positions, groups = defaultdict(dict), defaultdict(Counter)
    known_targets, tokens_by_instrument = defaultdict(set), {}
    unresolved_instruments = set()
    observed, unmatched = defaultdict(Counter), defaultdict(Counter)
    for payload, token_id, side, raw_intent in db.execute(
        "SELECT e.payload,i.token_id,i.side,i.payload FROM native_events e NOT INDEXED "
        "LEFT JOIN intents i ON i.order_id=json_extract(CAST(e.payload AS TEXT),'$.client_order_id') "
        "WHERE e.kind='OrderFilled' AND e.seq<=? ORDER BY e.seq",
        (cursor,),
    ):
        fill = serializer.deserialize(payload)
        if not isinstance(fill, OrderFilled) or fill.order_side not in (
            OrderSide.BUY,
            OrderSide.SELL,
        ):
            raise ValueError("invalid native fill")
        buy = fill.order_side == OrderSide.BUY
        direction = "BUY" if buy else "SELL"
        instrument = str(fill.instrument_id)
        info = fill.info or {}
        quantity, price = micros(fill.last_qty.as_decimal()), micros(fill.last_px.as_decimal())
        commission = micros(fill.commission.as_decimal())
        amount = info.get("pm_amount", cost(price, quantity) + (commission if buy else -commission))
        fee = info.get("pm_fee", commission)
        if (
            quantity <= 0
            or not 0 <= price <= SCALE
            or type(amount) is not int
            or type(fee) is not int
            or fee < 0
        ):
            raise ValueError("invalid recorded fill amounts")
        observed[direction].update(fills=1, shares=quantity, amount=amount, fees=fee)
        intent = json.loads(raw_intent) if raw_intent else None
        if (
            not isinstance(intent, dict)
            or not token_id
            or side != direction
            or intent.get("token_id") != token_id
            or intent.get("side") != direction
            or intent.get("generation") not in (None, generation)
            or info.get("pm_generation") not in (None, generation)
        ):
            unmatched[direction].update(fills=1, shares=quantity, amount=amount, fees=fee)
            unresolved_instruments.add(instrument)
            continue
        tokens_by_instrument[instrument] = token_id
        lots = positions[token_id]
        if buy:
            token = token_for(token_id)
            ids, labels = (
                ((), ())
                if not token
                else (
                    tuple(sorted(set(filter(None, token["category_ids"])))),
                    tuple(sorted(set(filter(None, token["category_labels"])))),
                )
            )
            cohort = (price // ONE_CENT, _iso_week(fill.ts_event), ids, labels)
            frozen = info.get("pm_rules")
            target = frozen.get("target") if isinstance(frozen, dict) else None
            if type(target) is not int or not 0 < target < SCALE:
                target = None
            key = f"{fill.client_order_id}:{fill.trade_id}"
            known_targets[token_id].add(key)
            lots[key] = _Lot(cohort, quantity, quantity, amount, fill.ts_event, target)
            groups[cohort].update(buy_fills=1, bought=quantity, buy_cost=amount, buy_fee=fee)
            if target is not None:
                groups[cohort]["target_buy_known"] += quantity
        else:
            kind, saved_kind = info.get("pm_kind"), intent.get("kind")
            kind = (
                (kind or saved_kind) if not kind or not saved_kind or kind == saved_kind else None
            )
            if kind not in EXIT_KINDS:
                kind = None
            if instrument in unresolved_instruments:
                parts, remaining = [], quantity
            else:
                parts, remaining = _sell_parts(
                    lots, known_targets[token_id], quantity, kind, intent.get("targets")
                )
            remaining, proceeds, commission = _apply_sell(
                groups, parts, remaining, fill.ts_event, price, amount, fee, kind
            )
            if remaining:
                unmatched[direction].update(
                    fills=1, shares=remaining, amount=proceeds, fees=commission
                )
                unresolved_instruments.add(instrument)
            positions[token_id] = {key: lot for key, lot in lots.items() if lot.remaining}

    unreliable_tokens = {tokens_by_instrument.get(i) for i in unresolved_instruments}
    holdings = _holdings(db, mode, business, positions, groups, token_for, unreliable_tokens)
    totals = Counter()
    rows = []
    for key, values in sorted(
        groups.items(), key=lambda pair: (pair[0][0], pair[0][1] or "", *pair[0][2:])
    ):
        lower, week, ids, labels = key
        totals.update(values)
        rows.append(
            {
                "buyPriceBandCents": {"lowerInclusive": lower, "upperExclusive": lower + 1},
                "buyWeekUtc": week,
                "categoryIds": list(ids),
                "categoryLabels": list(labels),
                **_metrics(values),
            }
        )

    def summary(counter):
        return {
            "fills": counter["fills"],
            "shares": units(counter["shares"]),
            "cashAmountU": units(counter["amount"]),
            "feesU": units(counter["fees"]),
        }

    return {
        "format": 1,
        "mode": mode,
        "projectedThroughSequence": cursor,
        "journalHeadSequence": head,
        "unprojectedFillCount": db.execute(
            "SELECT count(*) FROM native_events NOT INDEXED WHERE seq>? AND kind='OrderFilled'",
            (cursor,),
        ).fetchone()[0],
        "definitions": {
            "cohort": "Each BUY fill uses its actual pre-fee price in a one-cent band, UTC ISO week, and the saved token's complete category combination. Empty categories and null weeks are unknown.",
            "costAndFees": "Amounts use recorded fill cash and fees, including partial fills. Each buy lot carries its own fee-inclusive cost, so realized PnL can differ from the application's average-cost basis while lots remain open.",
            "exitAllocation": "TARGET exits follow persisted target IDs in order. A full remainder or single lot is exact only with known ownership. Other identifiable partial exits are divided by remaining shares; estimates carry into later exits and holdings. Unmatched shares are reported separately.",
            "targetSellRate": "Verified TARGET shares sold at or above their linked frozen target, divided by all attributed bought shares. This is the observed lower bound to date, including open and unknown-target buys in the denominator.",
            "averageSellDays": "Elapsed UTC days weighted by exact lot shares sold through TARGET or STOP with usable timestamps. Settlements, uncertain lot allocations and unknown reasons are excluded.",
            "zeroSettlementRate": "Recorded SETTLEMENT shares with zero price and zero proceeds, divided by recorded settled shares. The separate zeroSettlementShareOfBought divides by all attributed bought shares.",
            "heldBidValueU": "Indicative net value from the last saved bid depth, used once per token and divided among open lots. TEST shadow consumption is deducted. Only covered shares have a bid estimate. Pending settlements and unavailable or insufficient depth keep a separate cost basis.",
            "heldBidValueToCoveredCost": "Saved net bid value divided by the cost of the shares covered by those bids. It can exceed one. Cost for uncovered shares is excluded from the denominator.",
            "coverage": "Bid values use the last persisted depth and categories use saved token metadata. Historical quotes, historical categories and current bid readiness cannot be recovered. Null means no usable denominator or bid valuation.",
        },
        "coverage": {
            **holdings,
            "buySharesWithSavedCategories": units(
                sum(c["bought"] for k, c in groups.items() if k[2] or k[3])
            ),
            "buySharesWithKnownWeek": units(sum(c["bought"] for k, c in groups.items() if k[1])),
            "unattributedBuys": summary(unmatched["BUY"]),
            "unattributedSells": summary(unmatched["SELL"]),
        },
        "recordedBuyFills": summary(observed["BUY"]),
        "recordedSellFills": summary(observed["SELL"]),
        "applicationRealizedPnlU": units(business["realized"]),
        "totals": _metrics(totals),
        "cohorts": rows,
    }


def cohort_report(path):
    with _snapshot(path) as db:
        return _read_report(db)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledger", type=Path, help="Path to one existing PM-NAUTILUS state.sqlite")
    args = parser.parse_args(argv)
    try:
        report = cohort_report(args.ledger)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"cohort_report: {exc}\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
