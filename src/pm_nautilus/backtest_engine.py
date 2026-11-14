"""Replay time-ordered public market observations through isolated TEST execution."""

import json
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from nautilus_trader.common.component import TestClock
from nautilus_trader.model.enums import OrderSide

from .config import SAFE_INTEGER, SCALE, Preferences, micros, units
from .native import instrument_id
from .order_terms import SUPPORTED_CLOB_TICKS
from .redemption import RedemptionService
from .rules import Fees, Token
from .test_runtime import create_runtime
from .views import portfolio_view

NS_PER_MS = 1_000_000
CONTROL_MS = 5_000
MINUTE_MS = 60_000
MAX_WINDOW_MS = 366 * 24 * 60 * MINUTE_MS
MAX_TIME_MS = 4_102_444_800_000  # 2100-01-01 UTC; stay within native timestamp bounds.
MAX_LINE_BYTES = 16 * 1024 * 1024
MAX_FILLS_RETURNED = 5_000
MAX_CURVE_POINTS = 1_000


def _integer(value, name, minimum=0, maximum=SAFE_INTEGER):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须为 {minimum}–{maximum} 的整数")
    return value


def _string(value, name, max_length=256):
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > max_length
        or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value)
    ):
        raise ValueError(f"{name} 无效")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("回测数据含重复字段")
        result[key] = value
    return result


def _invalid_constant(_):
    raise ValueError("回测数据含非法数字")


def _lines(source):
    if not hasattr(source, "readline"):
        yield from source
        return
    while line := source.readline(MAX_LINE_BYTES + 1):
        yield line


def _records(source):
    for number, line in enumerate(_lines(source), 1):
        try:
            if not isinstance(line, (str, bytes)) or len(line) > MAX_LINE_BYTES:
                raise ValueError("单行回测数据超长或编码无效")
            if not line.strip():
                continue
            row = json.loads(
                line, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
            )
            if not isinstance(row, dict):
                raise ValueError("回测数据行必须是 JSON 对象")
        except (ValueError, RecursionError) as exc:
            raise ValueError(f"回测数据第 {number} 行无效：{exc}") from exc
        yield number, row


def validate_header(data):
    """Read the declared observation window and scope from a versioned header."""
    if data.get("type") != "header" or type(data.get("version")) is not int or data["version"] != 1:
        raise ValueError("回测数据须以 version=1 的 header 开头")
    source = _string(data["source"], "数据来源")
    start = _integer(data["startMs"], "数据开始时间", 1, MAX_TIME_MS)
    end = _integer(data["endMs"], "数据结束时间", start + 1, MAX_TIME_MS)
    status = data.get("coverage", "unknown")
    if status not in ("unknown", "partial", "scoped"):
        raise ValueError("未知的数据覆盖标记")
    notes = data.get("notes", [])
    if type(notes) is not list or len(notes) > 50:
        raise ValueError("数据说明无效")
    if "captureScope" in data:
        validate_capture_scope(data["captureScope"])
    return source, start, end, status, [_string(n, "数据说明", 1000) for n in notes]


def validate_capture_scope(value):
    """Read the limits of the original TEST subscriptions for this capture."""
    if type(value) is not dict or value.keys() - {
        "maxBuyPriceMicros",
        "marketCoverage",
        "preferences",
        "candidateBatchNs",
    }:
        raise ValueError("录制行情范围无效")
    _integer(value.get("maxBuyPriceMicros"), "录制买价上限", 50_000, 50_000)
    if value.get("marketCoverage") != "test-subscribed":
        raise ValueError("录制行情范围无效")
    if "preferences" in value:
        Preferences.model_validate(value["preferences"])
    if "candidateBatchNs" in value:
        _integer(value["candidateBatchNs"], "候选执行间隔", 0, 10_000_000_000)
    return value


def _validate_capture_preferences(scope, requested):
    if requested.max_price > scope["maxBuyPriceMicros"]:
        raise ValueError("TEST 录制数据只支持最高 5¢ 的买入参数")
    if "preferences" not in scope:
        return
    observed = Preferences.model_validate(scope["preferences"])
    if (
        not set(requested.marketTypes) <= set(observed.marketTypes)
        or requested.minMarketDurationDays < observed.minMarketDurationDays
        or requested.maxMarketDurationDays > observed.maxMarketDurationDays
        or requested.maxMarketProgressPercent > observed.maxMarketProgressPercent
        or (
            requested.includeIndependentMultiMarkets and not observed.includeIndependentMultiMarkets
        )
        or (requested.includeAugmentedNegRisk and not observed.includeAugmentedNegRisk)
        or (
            not observed.allCategories
            and (
                requested.allCategories
                or not set(requested.selectedCategoryIds) <= set(observed.selectedCategoryIds)
            )
        )
    ):
        raise ValueError("回测市场参数超出录制时 TEST 的候选范围")


def _optional_flag(row, name, default):
    result = row.get(name, default)
    if type(result) is not bool:
        raise ValueError(f"{name} 必须是布尔值")
    return result


def _levels(rows, name):
    if type(rows) is not list or len(rows) > 10_000:
        raise ValueError(f"{name} 必须是盘口档位数组")
    result = []
    seen = set()
    for pair in rows:
        if type(pair) is not list or len(pair) != 2:
            raise ValueError(f"{name} 档位必须是 [价格, 数量]")
        price = _integer(pair[0], "档位价格", 1, SCALE - 1)
        size = _integer(pair[1], "档位数量")
        if price in seen:
            raise ValueError(f"{name} 含重复档位")
        seen.add(price)
        result.append((price, size))
    return result


def _token(data):
    if type(data) is not dict:
        raise ValueError("市场合约资料必须是对象")
    data = dict(data)
    for key in ("token_id", "event_id", "market_id", "condition_id", "direction"):
        _string(data[key], key)
    for key in ("opened_ns", "ends_ns", "game_start_ns"):
        if key != "game_start_ns" or data.get(key) is not None:
            _integer(data[key], key, maximum=MAX_TIME_MS * NS_PER_MS)
    if data["ends_ns"] <= data["opened_ns"]:
        raise ValueError("市场结束时间必须晚于开始时间")
    _integer(data["result_count"], "市场结果数", 2, 100_000)
    _integer(data["min_size"], "最小下单量", 1)
    _integer(data["tick"], "价格档位", 1, SCALE - 1)
    for key in ("neg_risk", "open", "independent_market", "augmented_neg_risk"):
        if key in data and type(data[key]) is not bool:
            raise ValueError(f"{key} 必须为布尔值")
    for key in ("category_ids", "category_labels"):
        items = data.get(key, [])
        if type(items) is not list or len(items) > 1024:
            raise ValueError("市场栏目必须是字符串数组")
        data[key] = tuple(_string(item, key, 256) for item in items)
    for key in ("event_title", "question", "slug"):
        if data.get(key, "") != "":
            _string(data[key], key, 4096)
    fees = data["fees"]
    if type(fees) is not dict:
        raise ValueError("市场费率必须为对象")
    _integer(fees.get("rate", 0), "市场费率", maximum=SCALE)
    data["fees"] = Fees(**fees)
    return Token(**data)


class Replay:
    def __init__(
        self, runtime, clock, start, end, coverage, notes, *, checkpoint, capture_scope=None
    ):
        self.runtime = runtime
        self.clock = clock
        self.start = start
        self.end = end
        self.initial_capital = runtime.store.get("initial_capital")
        self.starting_cash = runtime.cash()
        self.starting_positions = sum(
            cycle["quantity"] > 0 for cycle in runtime.business["cycles"].values()
        )
        self.baseline_realized = runtime.business["realized"]
        self.checkpoint = checkpoint
        self.fill_after_seq = runtime.store.db.execute(
            "SELECT coalesce(max(seq), 0) FROM native_events"
        ).fetchone()[0]
        self.redemption = RedemptionService(runtime, None)
        self.claims_to_progress = {
            condition
            for condition, claim in runtime.business["claims"].items()
            if claim["state"] in {"IDENTIFIED", "CONFIRMED"}
        }
        self.resolved = {
            condition: claim["payouts"] for condition, claim in runtime.business["claims"].items()
        }
        self.started = False
        self.next_tick = start + CONTROL_MS if capture_scope is None else MAX_TIME_MS + 1
        sample_minutes = max(1, -(-(end - start) // ((MAX_CURVE_POINTS - 2) * MINUTE_MS)))
        self.sample_every = sample_minutes * MINUTE_MS
        self.next_sample = start
        self.equity = []
        self.coverage = coverage
        self.notes = notes
        self.market_ids = {token.market_id for token in runtime.tokens.values()}
        self.book_ids = set()
        self.waiting_snapshot = set()
        self.finalized_tokens = {
            token.token_id for token in runtime.tokens.values() if not token.open
        }
        self.first_book = self.last_book = None
        self.gap_count = self.ignored_deltas = self.ignored_snapshots = 0
        self.unsupported_ticks = set()
        self.gap_since_sample = False
        self.events = Counter()
        self.capture_scope = capture_scope
        self.observed_running = False
        self.capture_gap = False
        self.in_capture_baseline = False
        self.capture_baseline_ids = set()
        self.capture_bid_missing = set()
        self.capture_token_refs = {}

    def _set_time(self, at):
        self.clock.set_time(at * NS_PER_MS)

    def _start(self):
        self._set_time(self.start)
        if self.capture_scope is None or self.observed_running:
            if self.runtime.status != "RUNNING":
                self.runtime.start()
        elif self.runtime.status != "PAUSED":
            self.runtime.pause()
        self.started = True

    def _protect_market(self, token):
        self.runtime.market_availability[token.token_id] = {
            "status": "UNAVAILABLE",
            "marketId": token.market_id,
            "conditionId": token.condition_id,
        }
        self.runtime.disconnect([token.token_id])
        self.waiting_snapshot.add(token.token_id)

    def _market(self, row):
        runtime = self.runtime
        entries = row["tokens"]
        if type(entries) is not list:
            raise ValueError("tokens 必须是数组")
        tokens = [_token(raw) for raw in entries]
        if self.capture_scope is not None and "tokenRefs" in row:
            refs = row["tokenRefs"]
            if type(refs) is not list or len(refs) != len(tokens):
                raise ValueError("录制合约编号与市场资料数量不一致")
            for ref, token in zip(refs, tokens, strict=True):
                ref = _integer(ref, "录制合约编号", 1)
                previous = self.capture_token_refs.setdefault(ref, token.token_id)
                if previous != token.token_id:
                    raise ValueError("录制合约编号发生改变")
        seen = set()
        for token in tokens:
            old = runtime.tokens.get(token.token_id)
            if token.token_id in seen:
                raise ValueError("同一批市场资料含重复合约")
            seen.add(token.token_id)
            if old and (token.market_id, token.condition_id, token.event_id, token.direction) != (
                old.market_id,
                old.condition_id,
                old.event_id,
                old.direction,
            ):
                raise ValueError("历史合约身份发生改变")
            self.market_ids.add(token.market_id)
            if self.in_capture_baseline:
                self.capture_baseline_ids.add(token.token_id)
            if token.condition_id in self.resolved and token.open:
                raise ValueError("已确认结算的合约不能重新开放")
            if not token.open:
                self._protect_market(token)
                self.finalized_tokens.add(token.token_id)
            elif token.tick not in SUPPORTED_CLOB_TICKS:
                self.unsupported_ticks.add(token.token_id)
                self._protect_market(token)
            else:
                if old and (
                    not old.open
                    or old.tick != token.tick
                    or (
                        self.capture_scope is None and token.token_id in runtime.market_availability
                    )
                ):
                    runtime.disconnect([token.token_id])
                    self.waiting_snapshot.add(token.token_id)
                if self.capture_scope is None or (old and not old.open):
                    runtime.market_availability.pop(token.token_id, None)
                self.finalized_tokens.discard(token.token_id)
        tags = row.get("officialTags")
        if tags is not None:
            if type(tags) is not list or len(tags) > 100_000:
                raise ValueError("officialTags 必须是字符串数组")
            tags = [_string(tag, "官方栏目ID") for tag in tags]
        runtime.add_tokens(tokens, tags)
        if self.capture_scope is None or _optional_flag(row, "drain", True):
            runtime.drain()

    def _named_token(self, row):
        token_id = _string(row["tokenId"], "tokenId")
        if token_id not in self.runtime.tokens:
            raise ValueError("盘口或 tick 在市场资料出现前引用了合约")
        return self.runtime.tokens[token_id]

    def _tick_size(self, row):
        token = self._named_token(row)
        tick = _integer(row["tick"], "价格档位", 1, SCALE - 1)
        self.runtime.disconnect([token.token_id])
        self.waiting_snapshot.add(token.token_id)
        updated = replace(token, tick=tick)
        if tick not in SUPPORTED_CLOB_TICKS:
            self.unsupported_ticks.add(token.token_id)
            self._protect_market(updated)
        elif token.open and token.condition_id not in self.resolved:
            if self.capture_scope is None or token.tick not in SUPPORTED_CLOB_TICKS:
                self.runtime.market_availability.pop(token.token_id, None)
        self.runtime.add_tokens([updated])

    def _capture_book_state(self, token_id, row, snapshot=False):
        if self.capture_scope is None:
            return
        if "bidCovered" in row or snapshot:
            if _optional_flag(row, "bidCovered", True):
                self.capture_bid_missing.discard(token_id)
            else:
                self.capture_bid_missing.add(token_id)
        if "invalidGrid" in row or snapshot:
            self.runtime.books[token_id].replay_invalid_grid = _optional_flag(
                row, "invalidGrid", False
            )

    def _validate_capture_levels(self, row, bids, asks):
        if any(price > self.capture_scope["maxBuyPriceMicros"] for price, _ in asks):
            raise ValueError("录制盘口含 5¢ 以上卖盘")
        if not _optional_flag(row, "bidCovered", True) and (bids or asks):
            raise ValueError("未覆盖买盘的合约含可成交深度")
        _optional_flag(row, "invalidGrid", False)

    def _book(self, row, at, venue, *, drain=True):
        token = self._named_token(row)
        tid = token.token_id
        bids, asks = _levels(row["bids"], "买盘"), _levels(row["asks"], "卖盘")
        if self.capture_scope is not None:
            self._validate_capture_levels(row, bids, asks)
        if self.runtime.market_is_unavailable(tid):
            self.ignored_snapshots += 1
            return
        revision = self.runtime.books[tid].revision
        if self.capture_scope is None:
            self.runtime.book(tid, bids, asks, venue)
        else:
            self.runtime.book(tid, bids, asks, venue, drain=False)
        if self.runtime.books[tid].revision == revision:
            self.ignored_snapshots += 1
            return
        self._capture_book_state(tid, row, snapshot=True)
        if self.capture_scope is not None and drain:
            self.runtime.drain()
        self.book_ids.add(tid)
        self.waiting_snapshot.discard(tid)
        self.first_book = at if self.first_book is None else self.first_book
        self.last_book = at

    def _book_batch(self, books, at, venue):
        if type(books) is not list:
            raise ValueError("books 必须是数组")
        for row in books:
            if type(row) is not dict:
                raise ValueError("盘口快照必须是对象")
            book_venue = _integer(row.get("venueMs", venue), "盘口时间", maximum=MAX_TIME_MS)
            self._book(row, at, book_venue * NS_PER_MS, drain=False)

    def _deltas(self, changes, venue, at, allow_empty=False):
        runtime = self.runtime
        if type(changes) is not list or (not changes and not allow_empty):
            raise ValueError("changes 必须是非空数组")
        for change in changes:
            if type(change) is not dict:
                raise ValueError("盘口增量必须是对象")
            token = self._named_token(change)
            tid = token.token_id
            side = change["side"]
            if side not in ("BUY", "SELL"):
                raise ValueError("盘口增量方向必须为 BUY 或 SELL")
            side = "BID" if side == "BUY" else "ASK"
            levels = _levels(change["levels"], "盘口增量")
            if self.capture_scope is not None:
                bids, asks = (levels, ()) if side == "BID" else ((), levels)
                self._validate_capture_levels(change, bids, asks)
                if tid in self.capture_bid_missing and levels:
                    raise ValueError("录制增量未提供可成交的完整盘口")
                if "bidCovered" in change:
                    raise ValueError("录制买盘覆盖状态只可用盘口快照更新")
            if runtime.market_is_unavailable(tid):
                self.ignored_deltas += 1
                continue
            book = runtime.books[tid]
            previous = runtime.shadow_depth(book)
            if not book.delta(side, levels, venue):
                self.ignored_deltas += 1
                continue
            self._capture_book_state(tid, change)
            runtime.checkpoint_shadow(tid, previous)
            runtime.native.feed_delta(token, book, side, levels)
            runtime.dirty.add(runtime.cycle_id(token))
            self.last_book = at

    def _capture_availability(self, row):
        ids = row["tokenIds"]
        status = row["status"]
        if type(ids) is not list or not ids or status not in ("recovering", "unavailable", "ready"):
            raise ValueError("录制市场可用状态无效")
        runtime = self.runtime
        for tid in set(_string(tid, "tokenId") for tid in ids):
            token = self._named_token({"tokenId": tid})
            if status == "unavailable":
                runtime.market_recovering.discard(tid)
                self._protect_market(token)
            else:
                runtime.market_availability.pop(tid, None)
                if status == "recovering":
                    runtime.market_recovering.add(tid)
                    runtime.disconnect([tid])
                    self.waiting_snapshot.add(tid)
                else:
                    runtime.market_recovering.discard(tid)
                    runtime.dirty.add(runtime.cycle_id(token))
        if _optional_flag(row, "refresh", False):
            runtime.refresh_eligibility()
        if _optional_flag(row, "drain", True):
            runtime.drain()

    def _capture_touch(self, refs, at):
        if type(refs) is not list or len(refs) > 10_000:
            raise ValueError("录制盘口更新时间的合约编号必须为数组")
        runtime = self.runtime
        for ref in refs:
            tid = self.capture_token_refs.get(_integer(ref, "录制合约编号", 1))
            if tid is None or tid not in runtime.tokens:
                raise ValueError("录制盘口更新时间引用了未知合约")
            book = runtime.books.get(tid)
            if book is None or not book.ready or runtime.market_is_unavailable(tid):
                self.ignored_deltas += 1
                continue
            runtime.dirty.add(runtime.cycle_id(runtime.tokens[tid]))
            self.last_book = at

    def _capture_control(self, row):
        status = row["status"]
        if status not in ("RUNNING", "PAUSED"):
            raise ValueError("录制策略运行状态无效")
        self.observed_running = status == "RUNNING"
        if _optional_flag(row, "baselineEnd", False):
            if not self.in_capture_baseline:
                raise ValueError("录制盘口基线缺少起始标记")
            left_scope = self.runtime.tokens.keys() - self.capture_baseline_ids
            if left_scope:
                self._capture_detach(left_scope)
            self.capture_baseline_ids.clear()
            self.in_capture_baseline = False
            self.capture_gap = False
        if not self.started:
            return
        if not self.observed_running and self.runtime.status != "PAUSED":
            self.runtime.pause()
            self.runtime.refresh_eligibility()
        elif self.observed_running and self.runtime.status != "RUNNING":
            self.runtime.start()

    def _capture_event(self, kind, row, at):
        if kind == "scope":
            scope = validate_capture_scope(row["captureScope"])
            if "preferences" in row:
                if "preferences" in scope and scope["preferences"] != row["preferences"]:
                    raise ValueError("录制的策略参数重复且不一致")
                scope = validate_capture_scope(scope | {"preferences": row["preferences"]})
            _validate_capture_preferences(scope, self.runtime.preferences)
            self.capture_scope = scope
            if "candidateBatchNs" in scope:
                self.runtime.CANDIDATE_BATCH_NS = scope["candidateBatchNs"]
            self.in_capture_baseline = _optional_flag(row, "baseline", False)
            self.capture_baseline_ids.clear()
            if self.in_capture_baseline:
                self.capture_token_refs.clear()
        elif kind == "availability":
            self._capture_availability(row)
        elif kind == "control":
            self._capture_control(row)
        elif kind == "pulse":
            refresh = _optional_flag(row, "refresh", False)
            if refresh:
                self.runtime.refresh_eligibility()
            self.runtime.drain()
            if refresh:
                self._progress_claims()
        elif kind == "touch":
            refs = row["tokenRefs"]
            if not refs:
                raise ValueError("录制盘口更新时间缺少合约编号")
            self._capture_touch(refs, at)
            self.runtime.drain()
        elif kind == "book_batch":
            venue = _integer(row.get("venueMs", at), "盘口时间", maximum=MAX_TIME_MS)
            self._book_batch(row["books"], at, venue)
            if _optional_flag(row, "drain", True):
                self.runtime.drain()
        else:
            raise ValueError("不支持的回测事件类型")

    def _capture_detach(self, ids):
        runtime = self.runtime
        present = ids & runtime.tokens.keys()
        if not present:
            return
        owned = {cycle["token_id"] for cycle in runtime.business["cycles"].values()}
        if present & owned - self.waiting_snapshot:
            self.gap_since_sample = True
            self.gap_count += 1
        runtime.disconnect(present)
        for tid in present:
            token = runtime.tokens[tid]
            runtime.market_availability.pop(tid, None)
            runtime.market_recovering.discard(tid)
            runtime.books.pop(tid, None)
            runtime._preview_cache.pop(tid, None)
            self.capture_bid_missing.discard(tid)
            self.capture_baseline_ids.discard(tid)
            if tid in owned:
                self.waiting_snapshot.add(tid)
            else:
                self.waiting_snapshot.discard(tid)
            historical = runtime.store.db.execute(
                "SELECT 1 FROM intents WHERE token_id=? LIMIT 1", (tid,)
            ).fetchone()
            if tid in owned or historical:
                # Preserve native order and settlement identity while keeping
                # unsubscribed contracts out of candidate arbitration.
                runtime.tokens[tid] = replace(token, open=False)
            else:
                iid = instrument_id(token)
                if runtime.instrument_tokens.pop(str(iid), None) is not None:
                    runtime.native.cache.purge_instrument(iid)
                runtime._native_deferred.discard(tid)
                del runtime.tokens[tid]
        runtime.refresh_eligibility()

    def _gap(self, row):
        ids = row.get("tokenIds", [])
        if type(ids) is not list:
            raise ValueError("tokenIds 必须是数组，空数组表示全体合约")
        detached = self.capture_scope is not None and _optional_flag(row, "detached", False)
        if detached:
            if not ids:
                raise ValueError("退出采集范围需指定合约")
            self._capture_detach({_string(tid, "tokenId") for tid in ids})
            return
        if not ids and self.capture_scope is not None:
            self.capture_gap = True
            self.in_capture_baseline = False
            self.capture_baseline_ids.clear()
            self.capture_token_refs.clear()
            self.observed_running = False
            self.runtime.pause()
            self.runtime.market_availability.clear()
            self.runtime.market_recovering.clear()
        ids = {_string(tid, "tokenId") for tid in ids} or set(self.runtime.tokens)
        if ids - self.runtime.tokens.keys():
            raise ValueError("断流记录引用了未知合约")
        self.runtime.disconnect(ids)
        self.waiting_snapshot.update(ids - self.finalized_tokens)
        self.gap_count += 1
        self.gap_since_sample = True

    def _settle(self, row):
        runtime = self.runtime
        condition = _string(row["conditionId"], "conditionId")
        tokens = runtime.condition_token_ids(condition)
        payout = row["payouts"]
        if (
            type(payout) is not dict
            or len(tokens) != 2
            or set(payout) != set(tokens)
            or any(type(p) is not int for p in payout.values())
            or sorted(payout.values()) not in ([0, SCALE], [SCALE // 2, SCALE // 2])
        ):
            raise ValueError("结算需含该条件的两份正确合约、以及 1/0 或 0.5/0.5 兑付")
        if condition in self.resolved:
            if self.resolved[condition] != payout:
                raise ValueError("同一市场的确认结算结果冲突")
            return
        self.resolved[condition] = payout
        updated = [replace(runtime.tokens[tid], open=False) for tid in tokens]
        for token in updated:
            self._protect_market(token)
        self.finalized_tokens.update(tokens)
        self.waiting_snapshot.difference_update(tokens)
        runtime.add_tokens(updated)
        for token in updated:
            self.redemption.identify(token, payout)
        claim = runtime.business["claims"].get(condition)
        if claim is not None:
            # Identify and confirm at the recorded observation; the existing
            # TEST scheme only credits cash on the next maintenance pass.
            self.redemption._save_claim(claim, dict(claim, state="CONFIRMED"))
            self.claims_to_progress.add(condition)
        runtime.drain()

    def event(self, row, at):
        kind = row["type"]
        self._set_time(at)
        if kind == "market":
            self._market(row)
        elif kind == "book":
            venue = _integer(row.get("venueMs", at), "盘口时间", maximum=MAX_TIME_MS)
            self._book(row, at, venue * NS_PER_MS)
        elif kind in ("delta", "delta_batch"):
            venue = _integer(row.get("venueMs", at), "盘口时间", maximum=MAX_TIME_MS)
            books = row.get("books", []) if kind == "delta_batch" and self.capture_scope else []
            touches = row.get("touches", []) if kind == "delta_batch" and self.capture_scope else []
            if books:
                self._book_batch(books, at, venue)
            self._deltas(
                row["changes"] if kind == "delta_batch" else [row],
                venue * NS_PER_MS,
                at,
                allow_empty=bool(books or touches),
            )
            if kind == "delta_batch" and self.capture_scope and "touches" in row:
                self._capture_touch(touches, at)
            self.runtime.drain()
        elif kind == "gap":
            self._gap(row)
        elif kind == "tick":
            self._tick_size(row)
        elif kind == "settlement":
            self._settle(row)
        elif self.capture_scope is not None:
            self._capture_event(kind, row, at)
        else:
            raise ValueError("不支持的回测事件类型")
        self.events[kind] += 1
        if self.runtime.faulted:
            raise ValueError("回测账本发生错误")

    def _sample(self, at):
        _, portfolio = portfolio_view(self.runtime, position_limit=0)
        total = portfolio["totalFunds"]
        missing = portfolio["unvaluedPositionCount"]
        if self.capture_scope is not None:
            runtime = self.runtime
            for cycle in runtime.business["cycles"].values():
                tid = cycle["token_id"]
                if (
                    tid in self.capture_bid_missing
                    and cycle["quantity"] > 0
                    and runtime.tokens[tid].condition_id not in runtime.business["settled"]
                    and runtime.books[tid].ready
                    and runtime.market_can_trade(tid)
                ):
                    missing += 1
            if missing or self.capture_gap:
                total = None
        last = self.equity[-1] if self.equity else None
        self.equity.append(
            {
                "atMs": at,
                "cash": units(self.runtime.cash()),
                "total": total,
                "pnl": None if total is None else units(micros(total) - self.initial_capital),
                "missingMarkCount": missing,
                "breakBefore": self.gap_since_sample
                or total is None
                or bool(last and last["total"] is None),
            }
        )
        self.gap_since_sample = False

    def _progress_claims(self):
        for condition in sorted(self.claims_to_progress):
            claim = self.runtime.business["claims"][condition]
            if claim["state"] == "IDENTIFIED":
                self.redemption._save_claim(claim, dict(claim, state="CONFIRMED"))
            else:
                self.runtime.settlement_fill(claim)
            if self.runtime.business["claims"][condition]["state"] == "CREDITED":
                self.claims_to_progress.remove(condition)

    def advance_before(self, at):
        """Run scheduled work strictly before the next observation timestamp."""
        if not self.started and at >= self.start:
            self._start()
        if not self.started:
            return
        while (boundary := min(self.next_sample, self.next_tick)) < at:
            self._set_time(boundary)
            if boundary == self.next_tick:
                if not self.runtime.tokens:
                    # Before metadata arrives there is no eligibility, claim or
                    # order state to evaluate. Preserve the next control phase.
                    self.next_tick += CONTROL_MS * (-(-(at - boundary) // CONTROL_MS))
                else:
                    self.runtime.refresh_eligibility()
                    self.runtime.drain()
                    self._progress_claims()
                    self.next_tick += CONTROL_MS
            if boundary == self.next_sample:
                self._sample(boundary)
                self.next_sample += self.sample_every

    def _fills(self):
        runtime = self.runtime
        rows = runtime.store.db.execute(
            "SELECT payload FROM native_events WHERE kind='OrderFilled' AND seq>? ORDER BY seq",
            (self.fill_after_seq,),
        )
        fills = []
        counts = Counter()
        for (payload,) in rows:
            event = runtime.store.serializer.deserialize(payload)
            side = "BUY" if event.order_side == OrderSide.BUY else "SELL"
            counts[side] += 1
            # Count the whole run but bound the result sent to the browser.
            if len(fills) >= MAX_FILLS_RETURNED:
                continue
            intent = runtime.store.intent(event.client_order_id)
            token = runtime.tokens[intent["token_id"]]
            fills.append(
                {
                    "atMs": event.ts_event // NS_PER_MS,
                    "tokenId": token.token_id,
                    "eventId": token.event_id,
                    "conditionId": token.condition_id,
                    "marketId": token.market_id,
                    "question": token.question[:256],
                    "outcome": token.direction,
                    "side": side,
                    "kind": intent["kind"],
                    "price": units(micros(event.last_px.as_decimal())),
                    "quantity": units(event.info["pm_net"]),
                    "fee": units(event.info["pm_fee"]),
                    "amount": units(event.info["pm_amount"]),
                    "limit": units(intent["limit"]),
                }
            )
        return fills, counts

    def result(self, source):
        self.advance_before(self.end + 1)
        if not self.book_ids or (
            self.last_book < self.start
            and not any(book.ready for book in self.runtime.books.values())
        ):
            raise ValueError("所选时段没有可重建的历史盘口")
        if self.equity[-1]["atMs"] < self.end:
            self._set_time(self.end)
            self._sample(self.end)
        if self.in_capture_baseline:
            raise ValueError("所选时段包含未完成的录制盘口基线")
        validation = self.runtime.validate()
        if not validation["ok"]:
            raise ValueError("回测账本核对失败：" + "、".join(validation["errors"]))
        fills, counts = self._fills()
        ending = self.equity[-1]
        unpriced = sum(p["total"] is None for p in self.equity)
        missing = len(set(self.runtime.tokens) - self.book_ids - self.finalized_tokens)
        incomplete = (
            self.gap_count
            or self.waiting_snapshot - self.finalized_tokens
            or unpriced
            or missing
            or self.unsupported_ticks
            or self.ignored_deltas
            or self.first_book > self.start
            or self.capture_gap
        )
        status = "partial" if incomplete else self.coverage
        fill_count = sum(counts.values())
        return {
            "status": "completed",
            "source": source,
            "startMs": self.start,
            "endMs": self.end,
            "startingCash": units(self.starting_cash),
            "initialCapital": units(self.initial_capital),
            "preferences": self.runtime.preferences.model_dump(mode="json"),
            "coverage": {
                "status": status,
                "notes": self.notes,
                "marketCount": len(self.market_ids),
                "tokenCount": len(self.runtime.tokens),
                "bookTokenCount": len(self.book_ids),
                "missingBookTokenCount": missing,
                "waitingSnapshotTokenCount": len(self.waiting_snapshot - self.finalized_tokens),
                "firstBookAtMs": self.first_book,
                "lastBookAtMs": self.last_book,
                "explicitGapCount": self.gap_count,
                "ignoredDeltaCount": self.ignored_deltas,
                "ignoredSnapshotCount": self.ignored_snapshots,
                "unsupportedTickTokenCount": len(self.unsupported_ticks),
                "unpricedPointCount": unpriced,
                "eventCounts": dict(self.events),
                "emptyStartingPositions": self.starting_positions == 0,
                "checkpointAtMs": self.start if self.checkpoint else None,
                "sampleIntervalMs": self.sample_every,
                **({"captureScope": self.capture_scope} if self.capture_scope is not None else {}),
            },
            "equity": self.equity,
            "fills": fills,
            "summary": {
                "fillCount": fill_count,
                "buyFillCount": counts["BUY"],
                "sellFillCount": counts["SELL"],
                "returnedFillCount": len(fills),
                "fillsTruncated": fill_count > len(fills),
                "realizedPnl": units(self.runtime.business["realized"] - self.baseline_realized),
                "startingPositionCount": self.starting_positions,
                "endingCash": ending["cash"],
                "endingEquity": ending["total"],
                "endingPnl": ending["pnl"],
                "openPositionCount": sum(
                    cycle["quantity"] > 0 for cycle in self.runtime.business["cycles"].values()
                ),
            },
        }


def checked_backup(path):
    """Accept an offline exported database, never a copy with committed WAL data."""
    path = Path(path).resolve(strict=True)
    sidecars = [path.with_name(path.name + suffix) for suffix in ("-wal", "-journal")]
    enabled_journal = any(sidecar.exists() and sidecar.stat().st_size for sidecar in sidecars)
    if not path.is_file() or enabled_journal:
        raise ValueError("仅接受已一致导出的 TEST 1 SQLite 备份，不含活动 WAL／回滚日志")
    return path


def _copy_checkpoint(path, target, as_of_ms, requested_preferences, starting_cash):
    path = checked_backup(path)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as source:
        meta = {
            key: json.loads(raw)
            for key, raw in source.execute(
                "SELECT key, value FROM meta WHERE key IN ('mode', 'preferences', 'initial_capital')"
            )
        }
        if meta.get("mode") != "TEST":
            raise ValueError("回测起点备份必须是 TEST 账本")
        if Preferences.model_validate(requested_preferences) != Preferences.model_validate(
            meta["preferences"]
        ):
            raise ValueError("对照 TEST 1 时必须使用备份中的原始参数")
        if micros(starting_cash) != meta["initial_capital"]:
            raise ValueError("对照 TEST 1 时初始本金必须与备份一致")
        if source.execute(
            "SELECT 1 FROM intents WHERE json_extract(payload,'$.terminal') IS NOT 1 LIMIT 1"
        ).fetchone():
            raise ValueError("回测起点备份含有在途订单")
        if source.execute(
            """SELECT 1 FROM native_events
               WHERE max(COALESCE(json_extract(CAST(payload AS TEXT),'$.ts_event'), 0),
                         COALESCE(json_extract(CAST(payload AS TEXT),'$.ts_init'), 0)) > ?
               LIMIT 1""",
            (as_of_ms * NS_PER_MS,),
        ).fetchone():
            raise ValueError("备份包含晚于回测起点的订单事件")
        if (
            source.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='equity_samples'"
            ).fetchone()
            and source.execute(
                "SELECT 1 FROM equity_samples WHERE at_ms>? LIMIT 1", (as_of_ms,)
            ).fetchone()
        ):
            raise ValueError("备份包含晚于回测起点的收益采样")
        with closing(sqlite3.connect(target)) as isolated:
            source.backup(isolated)


def run_backtest(
    source,
    *,
    preferences,
    starting_cash,
    start_ms=None,
    end_ms=None,
    workspace=None,
    checkpoint=None,
    checkpoint_at_ms=None,
    ledger_max_bytes=None,
):
    """Stream observed NDJSON through a scratch ledger, optionally from a TEST backup."""
    records = _records(source)
    _, row = next(records, (0, {}))
    try:
        label, data_start, data_end, coverage, notes = validate_header(row)
        capture_scope = row.get("captureScope")
        if capture_scope is not None:
            _validate_capture_preferences(capture_scope, Preferences.model_validate(preferences))
    except (KeyError, ValueError) as exc:
        raise ValueError(f"回测数据头无效：{exc}") from exc
    start = data_start if start_ms is None else _integer(start_ms, "开始时间", data_start, data_end)
    end = data_end if end_ms is None else _integer(end_ms, "结束时间", start + 1, data_end)
    if not start < end <= start + MAX_WINDOW_MS:
        raise ValueError("回测时间范围须大于零且不超过366天")
    if (checkpoint is None) != (checkpoint_at_ms is None) or (
        checkpoint is not None and (type(checkpoint_at_ms) is not int or checkpoint_at_ms != start)
    ):
        raise ValueError("账本备份的已知取得时间必须等于回测开始时间")
    clock = TestClock()
    clock.set_time((data_start if checkpoint is None else start) * NS_PER_MS)
    with TemporaryDirectory(prefix="pm-backtest-", dir=workspace) as scratch:
        ledger = Path(scratch) / "state.sqlite"
        if checkpoint is not None:
            _copy_checkpoint(checkpoint, ledger, start, preferences, starting_cash)
        runtime = create_runtime(ledger, clock=clock)
        try:
            if ledger_max_bytes is not None:
                page_size = runtime.store.db.execute("PRAGMA page_size").fetchone()[0]
                max_bytes = _integer(ledger_max_bytes, "回测账本字节限制", page_size)
                max_pages = max_bytes // page_size
                page_count = runtime.store.db.execute("PRAGMA page_count").fetchone()[0]
                if page_count > max_pages:
                    raise ValueError("初始回测账本超过容量限制")
                runtime.store.db.execute(f"PRAGMA max_page_count={max_pages}")
            if checkpoint is None:
                runtime.update_preferences(preferences, capital=starting_cash)
            replay = Replay(
                runtime,
                clock,
                start,
                end,
                coverage,
                notes,
                checkpoint=checkpoint,
                **({"capture_scope": capture_scope} if capture_scope else {}),
            )
            previous = data_start
            for number, record in records:
                try:
                    at = _integer(record["atMs"], "观测时间", previous, data_end)
                    if at > end:
                        break
                    previous = at
                    if checkpoint is not None and at < start:
                        continue
                    replay.advance_before(at)
                    replay.event(record, at)
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(f"回测数据第 {number} 行无效：{exc}") from exc
            return replay.result(label)
        finally:
            runtime.close()
