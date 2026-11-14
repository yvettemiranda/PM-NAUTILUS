"""Export observed TEST fills and compare an historical replay with those facts."""

import argparse
import json
import sqlite3
import sys
from bisect import bisect_left, bisect_right
from collections import defaultdict, deque
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .backtest_engine import NS_PER_MS, MAX_TIME_MS, checked_backup
from .config import SCALE, SAFE_INTEGER, micros, units

COMPARISON_SCHEMA = "pm-nautilus-test-fills-v1"
MAX_DETAILS = 100


def export_observed(path, start_ms, end_ms):
    """Read an immutable TEST backup, retaining individual fill facts only."""
    if not 0 < start_ms < end_ms <= MAX_TIME_MS:
        raise ValueError("测试记录时间范围无效")
    path = checked_backup(path)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        mode = db.execute("SELECT value FROM meta WHERE key='mode'").fetchone()
        if not mode or json.loads(mode[0]) != "TEST":
            raise ValueError("对照只接受 TEST SQLite 备份")
        generation = db.execute("SELECT value FROM meta WHERE key='generation'").fetchone()
        if not generation:
            raise ValueError("TEST 备份没有账本代次")
        generation = json.loads(generation[0])
        if db.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='equity_samples'"
        ).fetchone():
            equity = [
                {"atMs": at, "total": units(total), "pnl": units(pnl)}
                for at, total, pnl in db.execute(
                    """SELECT at_ms,total,pnl FROM equity_samples
                       WHERE generation=? AND at_ms BETWEEN ? AND ? ORDER BY at_ms,id""",
                    (generation, start_ms, end_ms),
                )
            ]
        else:
            equity = []
        fills = []
        rows = db.execute(
            """SELECT payload FROM native_events
               WHERE kind='OrderFilled' AND
               CAST(json_extract(CAST(payload AS TEXT), '$.ts_event') AS INTEGER) BETWEEN ? AND ?
               ORDER BY seq""",
            (start_ms * NS_PER_MS, (end_ms + 1) * NS_PER_MS - 1),
        )
        for (payload,) in rows:
            event = json.loads(payload)
            facts = event["info"]
            if facts["pm_generation"] != generation:
                raise ValueError("成交时段跨越 TEST 账本代次，须分开导出")
            intent = db.execute(
                "SELECT token_id,payload FROM intents WHERE order_id=?", (event["client_order_id"],)
            ).fetchone()
            if not intent:
                raise ValueError("测试成交缺少原始持久订单意图")
            fills.append(
                {
                    "atMs": event["ts_event"] // NS_PER_MS,
                    "tokenId": intent[0],
                    "side": event["order_side"],
                    "kind": facts["pm_kind"],
                    "price": event["last_px"],
                    "quantity": units(facts["pm_net"]),
                    "fee": units(facts["pm_fee"]),
                    "amount": units(facts["pm_amount"]),
                    "limit": units(json.loads(intent[1])["limit"]),
                }
            )
    return {
        "schema": COMPARISON_SCHEMA,
        "startMs": start_ms,
        "endMs": end_ms,
        "fills": fills,
        "equity": equity,
        "summary": {"fillCount": len(fills), "fillsTruncated": False},
    }


@dataclass(frozen=True)
class Fill:
    at: int
    token: str
    side: str
    kind: str
    price: int
    quantity: int
    fee: int
    amount: int

    @property
    def key(self):
        return self.token, self.side, self.kind

    def as_result(self):
        return {
            "atMs": self.at,
            "tokenId": self.token,
            "side": self.side,
            "kind": self.kind,
            "price": units(self.price),
            "quantity": units(self.quantity),
            "fee": units(self.fee),
            "amount": units(self.amount),
        }


def _read_result(data, *, observed):
    if isinstance(data, dict) and isinstance(data.get("result"), dict):
        data = data["result"]
    if not isinstance(data, dict):
        raise ValueError("成交记录文件必须是 JSON 对象")
    if observed and data.get("schema") != COMPARISON_SCHEMA:
        raise ValueError("对照文件需为 TEST 原始逐笔成交导出，网页订单汇总不适用")
    if not observed and data.get("status") != "completed":
        raise ValueError("回测未完成")
    start, end, rows = data["startMs"], data["endMs"], data["fills"]
    if (
        type(start) is not int
        or type(end) is not int
        or not 0 < start < end <= MAX_TIME_MS
        or type(rows) is not list
    ):
        raise ValueError("成交记录的时间或列表无效")
    summary = data["summary"]
    count, truncated = summary["fillCount"], summary["fillsTruncated"]
    if (
        type(count) is not int
        or type(truncated) is not bool
        or count < len(rows)
        or truncated != (count > len(rows))
    ):
        raise ValueError("逐笔成交条数与声明不一致")
    fills = []
    for row in rows:
        at, token, side, kind = (row.get(k) for k in ("atMs", "tokenId", "side", "kind"))
        if (
            type(at) is not int
            or not start <= at <= end
            or type(token) is not str
            or not token
            or len(token) > 256
            or (side, kind)
            not in (
                ("BUY", "BUY"),
                ("SELL", "TARGET"),
                ("SELL", "STOP"),
                ("SELL", "SETTLEMENT"),
            )
        ):
            raise ValueError("记录缺少正确的成交时间、合约、方向或类别")
        price, qty, fee, amount = (micros(row[k]) for k in ("price", "quantity", "fee", "amount"))
        if not 0 <= price <= SCALE or not 0 < qty <= SAFE_INTEGER or min(fee, amount) < 0:
            raise ValueError("成交记录的价格、份额或资金无效")
        fills.append(Fill(at, token, side, kind, price, qty, fee, amount))
    return data, start, end, fills, count


def _compare_rows(actual, simulated, tolerance_ms):
    # Pair fully equal executions first, then pair the nearest remaining
    # observation of the same token, order direction and kind within tolerance.
    by_exact = defaultdict(deque)
    for i, row in enumerate(simulated):
        by_exact[row].append(i)
    used = set()
    exact = 0
    pending = []
    for row in actual:
        matches = by_exact.get(row)
        if matches:
            used.add(matches.popleft())
            exact += 1
        else:
            pending.append(row)
    groups = defaultdict(list)
    for i, row in sorted(enumerate(simulated), key=lambda pair: pair[1].at):
        if i not in used:
            groups[row.key].append((row.at, i))
    approximate = []
    misses = []
    for row in sorted(pending, key=lambda fill: fill.at):
        known = groups[row.key]
        first = bisect_left(known, (row.at - tolerance_ms, -1))
        last = bisect_right(known, (row.at + tolerance_ms, sys.maxsize))
        candidates = (i for _, i in known[first:last] if i not in used)

        def proximity(i):
            other = simulated[i]
            return (
                abs(other.at - row.at),
                abs(other.price - row.price),
                abs(other.quantity - row.quantity),
                i,
            )

        closest = min(candidates, key=proximity, default=None)
        if closest is None:
            misses.append(row)
            continue
        used.add(closest)
        approximate.append((row, simulated[closest]))
    return exact, approximate, misses, [row for i, row in enumerate(simulated) if i not in used]


def _equity_point(row):
    at = row["atMs"]
    if type(at) is not int:
        raise ValueError("权益样本时间无效")
    total = None if row["total"] is None else micros(row["total"])
    pnl = None if row["pnl"] is None else micros(row["pnl"])
    return at, total, pnl


def _compare_equity(actual_rows, simulated_rows, max_time_ms):
    actual = sorted((_equity_point(row) for row in actual_rows), key=lambda p: p[0])
    simulated = sorted((_equity_point(row) for row in simulated_rows), key=lambda p: p[0])
    actual_times = [row[0] for row in actual]
    taken = set()
    no_nearby = unpriced_pair = sum_abs_error = max_abs_error = max_skew = count = 0
    first_delta = last_delta = None
    for at, total, _ in simulated:
        before = bisect_left(actual_times, at - max_time_ms)
        after = bisect_right(actual_times, at + max_time_ms)
        closest = min(
            (i for i in range(before, after) if i not in taken),
            key=lambda i: (abs(actual_times[i] - at), actual_times[i], i),
            default=None,
        )
        if closest is None:
            no_nearby += 1
            continue
        taken.add(closest)
        other_at, other_total, _ = actual[closest]
        if total is None or other_total is None:
            unpriced_pair += 1
            continue
        delta = total - other_total
        if first_delta is None:
            first_delta = delta
        last_delta = delta
        count += 1
        sum_abs_error += abs(delta)
        max_abs_error = max(max_abs_error, abs(delta))
        max_skew = max(max_skew, abs(at - other_at))
    return {
        "observedSamples": len(actual),
        "replaySamples": len(simulated),
        "pairedValuedSamples": count,
        "pairedUnpricedSamples": unpriced_pair,
        "replaySamplesWithoutNearbyObservation": no_nearby,
        "maxMatchSkewMs": max_time_ms,
        "maxObservedMatchSkewMs": max_skew if count else None,
        "meanAbsoluteTotalDelta": units(sum_abs_error // count) if count else None,
        "maxAbsoluteTotalDelta": units(max_abs_error) if count else None,
        "firstTotalDelta": units(first_delta),
        "lastTotalDelta": units(last_delta),
    }


def compare(replay, observed, *, tolerance_ms=5_000, tolerance_price="0", tolerance_quantity="0"):
    if type(tolerance_ms) is not int or not 0 <= tolerance_ms <= 3_600_000:
        raise ValueError("匹配时间容差须为 0 到 3,600,000 毫秒")
    price_bound, qty_bound = micros(tolerance_price), micros(tolerance_quantity)
    if not 0 <= price_bound <= SCALE or qty_bound < 0:
        raise ValueError("价格和数量容差无效")
    run, start, end, simulated, sim_count = _read_result(replay, observed=False)
    observed_document, actual_start, actual_end, actual, actual_count = _read_result(
        observed, observed=True
    )
    if (start, end) != (actual_start, actual_end):
        raise ValueError("两份成交记录必须使用相同的起止时间")
    exact, near, missing, extra = _compare_rows(actual, simulated, tolerance_ms)
    delta_details = []
    within = exact
    max_time_diff = max_price_diff = max_qty_diff = max_fee_diff = 0
    for reference, candidate in near:
        time_diff = candidate.at - reference.at
        price_diff = candidate.price - reference.price
        qty_diff = candidate.quantity - reference.quantity
        fee_diff = candidate.fee - reference.fee
        amount_diff = candidate.amount - reference.amount
        max_time_diff = max(max_time_diff, abs(time_diff))
        max_price_diff = max(max_price_diff, abs(price_diff))
        max_qty_diff = max(max_qty_diff, abs(qty_diff))
        max_fee_diff = max(max_fee_diff, abs(fee_diff))
        passes = abs(price_diff) <= price_bound and abs(qty_diff) <= qty_bound
        within += passes
        if len(delta_details) < MAX_DETAILS:
            delta_details.append(
                {
                    "observed": reference.as_result(),
                    "replayed": candidate.as_result(),
                    "atDeltaMs": time_diff,
                    "priceDelta": units(price_diff),
                    "quantityDelta": units(qty_diff),
                    "feeDelta": units(fee_diff),
                    "amountDelta": units(amount_diff),
                    "priceAndQuantityWithinTolerance": passes,
                }
            )
    matched = exact + len(near)
    complete = sim_count == len(simulated) and actual_count == len(actual)
    return {
        "startMs": start,
        "endMs": end,
        "replayCoverage": run.get("coverage"),
        "equityComparison": _compare_equity(
            observed_document.get("equity", []), run.get("equity", []), 30_000
        ),
        "fullFillLists": complete,
        "observedFillCount": actual_count,
        "replayFillCount": sim_count,
        "observedOmittedCount": actual_count - len(actual),
        "replayOmittedCount": sim_count - len(simulated),
        "exactMatches": exact,
        "pairedMatches": matched,
        "pairedWithinPriceAndQuantityTolerance": within,
        "pairedOutsidePriceOrQuantityTolerance": matched - within,
        "unmatchedObserved": len(missing),
        "unmatchedReplayed": len(extra),
        "maxPairedAbsoluteTimeDeltaMs": max_time_diff if matched else None,
        "maxPairedAbsolutePriceDelta": units(max_price_diff) if matched else None,
        "maxPairedAbsoluteQuantityDelta": units(max_qty_diff) if matched else None,
        "maxPairedAbsoluteFeeDelta": units(max_fee_diff) if matched else None,
        "tolerance": {
            "timeMs": tolerance_ms,
            "price": units(price_bound),
            "quantity": units(qty_bound),
        },
        "matchedDetails": delta_details,
        "unmatchedObservedExamples": [row.as_result() for row in missing[:MAX_DETAILS]],
        "unmatchedReplayedExamples": [row.as_result() for row in extra[:MAX_DETAILS]],
        "notes": [
            "精确匹配要求时间、合约、方向、类别、价格、数量、手续费及金额全部相同。",
            "同合约、方向、类别按最近时间逐笔配对，同一成交最多使用一次；未配对也可能是行情或起点差异。",
            "价格和份额容差不包含手续费或金额差异，报告分别列出这些差异。",
            "权益只对照 30 秒内最近的独立样本；缺失估值不补零，也不插值。",
            "报告仅核对给定成交列表，起始状态、参数与行情覆盖需独立核对。"
            if complete
            else "至少一方的成交列表截断，未匹配项和比例不能代表完整时段。",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="导出历史 TEST 原始成交并核对回测结果")
    commands = parser.add_subparsers(dest="operation", required=True)
    extract = commands.add_parser("export", help="从已一致导出的离线 TEST 备份读取成交")
    extract.add_argument("checkpoint", type=Path)
    extract.add_argument("--start-ms", required=True, type=int)
    extract.add_argument("--end-ms", required=True, type=int)
    check = commands.add_parser("compare", help="比较回测结果与导出的 TEST 成交")
    check.add_argument("replay", type=Path)
    check.add_argument("observed", type=Path)
    check.add_argument("--tolerance-ms", type=int, default=5_000)
    check.add_argument("--tolerance-price", default="0")
    check.add_argument("--tolerance-quantity", default="0")
    args = parser.parse_args(argv)
    try:
        if args.operation == "export":
            result = export_observed(args.checkpoint, args.start_ms, args.end_ms)
        else:
            result = compare(
                json.loads(args.replay.read_bytes()),
                json.loads(args.observed.read_bytes()),
                tolerance_ms=args.tolerance_ms,
                tolerance_price=args.tolerance_price,
                tolerance_quantity=args.tolerance_quantity,
            )
    except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
