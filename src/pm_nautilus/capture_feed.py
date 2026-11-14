"""TEST market observation projection for a bounded low-price replay."""

from collections import defaultdict
from dataclasses import replace
from logging import getLogger
from time import time_ns

import msgspec

from .order_terms import SUPPORTED_CLOB_TICKS

MAX_BUY_PRICE_MICROS = 50_000
BASELINE_INTERVAL_MS = 60 * 60 * 1000
METADATA_BATCH_SIZE = 128
BOOK_BATCH_SIZE = 64
PACK_DEPTH_AT = 64
LOGGER = getLogger(__name__)


class MarketCapture:
    """Record one TEST consumer's observed books without owning subscriptions."""

    def __init__(self, runtime, recorder):
        self.runtime = runtime
        self.recorder = recorder
        self.active = False
        self.needs_baseline = True
        self.last_baseline_at = 0
        self.last_time = 0
        self.metadata = {}
        self.token_refs = {}
        self.next_token_ref = 1
        self.known_states = {}
        self.tracked_bids = set()
        self.ready = set()
        self.off_grid = {}
        self.tracked_events = set()
        self.settled = set()
        self.ids = set()
        self._preferences = None
        self._official_tags = None
        self._recorder_epoch = recorder.epoch
        self._error_reported = False

    def observe(self, name, *args, **kwargs):
        try:
            getattr(self, name)(*args, **kwargs)
        except Exception:
            # The recorder does not own the strategy, ledger or public stream.
            self.needs_baseline = True
            if not self._error_reported:
                LOGGER.exception("TEST 行情录制观察失败")
                self._error_reported = True

    def at_ms(self):
        # Match observation order through small wall clock corrections.
        self.last_time = max(self.last_time, time_ns() // 1_000_000)
        return self.last_time

    def _record(self, rows):
        if not rows:
            return True
        if not self.recorder.submit_many(rows):
            self.needs_baseline = True
            return False
        return True

    def _observe(self, row_type, at, **fields):
        return self._record([dict(type=row_type, atMs=at, **fields)])

    @staticmethod
    def _batches(rows, size):
        for offset in range(0, len(rows), size):
            yield rows[offset : offset + size]

    def recording(self):
        if self.recorder.epoch != self._recorder_epoch:
            self.needs_baseline = True
            return False
        return self.active and not self.needs_baseline and not self.runtime.faulted

    def stop(self):
        if self.active:
            at = self.at_ms()
            self._record(
                [dict(type="control", atMs=at, status="PAUSED"), dict(type="gap", atMs=at)]
            )
        self.active = False
        self.needs_baseline = True

    def _subscription_state(self, tid):
        if tid in self.runtime.market_availability:
            return "unavailable"
        if tid in self.runtime.market_recovering:
            return "recovering"
        return "ready"

    def sync(self, refresh=False, metadata_changed=False, *, drain=True):
        runtime = self.runtime
        if runtime.status != "RUNNING" or runtime.faulted:
            self.stop()
            return
        at = self.at_ms()
        starting = not self.active
        self.active = True
        if self.recorder.epoch != self._recorder_epoch:
            self.needs_baseline = True
        if not self.recorder.accepting:
            return
        complete = self.needs_baseline or at - self.last_baseline_at >= BASELINE_INTERVAL_MS
        if complete:
            self._baseline(at, invalidate=self.needs_baseline and self.last_baseline_at > 0)
        else:
            self._update_scope(at, metadata_changed=metadata_changed)
        if drain and self.recording() and not (complete and starting):
            self.pulse(at, refresh=refresh)

    def _scope(self, at, *, baseline=False):
        preferences = self.runtime.preferences
        return dict(
            type="scope",
            atMs=at,
            baseline=baseline,
            captureScope=dict(
                maxBuyPriceMicros=MAX_BUY_PRICE_MICROS,
                marketCoverage="test-subscribed",
                candidateBatchNs=self.runtime.CANDIDATE_BATCH_NS,
                preferences=preferences.model_dump(mode="json"),
            ),
        )

    def _observed_ids(self):
        runtime = self.runtime
        # Temporarily unavailable or recovering candidates can drop out of
        # subscriptions. Their gate must remain visible in the recording.
        relevant = (
            runtime.monitored
            | (self.ids & runtime.market_availability.keys())
            | (self.ids & runtime.market_recovering)
        )
        if not self.settled:
            return relevant
        return {tid for tid in relevant if runtime.tokens[tid].condition_id not in self.settled}

    def _metadata_rows(self, at, updated):
        tags = sorted(self.runtime.official_tags)
        rows = []
        for batch in self._batches(updated, METADATA_BATCH_SIZE):
            refs = []
            for token in batch:
                tid = token.token_id
                if tid not in self.token_refs:
                    self.token_refs[tid] = self.next_token_ref
                    self.next_token_ref += 1
                refs.append(self.token_refs[tid])
            rows.append(dict(type="market", atMs=at, tokens=batch, tokenRefs=refs, drain=False))
        if rows:
            rows[-1]["officialTags"] = tags
        elif tags != self._official_tags:
            rows.append(dict(type="market", atMs=at, tokens=[], officialTags=tags, drain=False))
        return rows

    def _availability_rows(self, at, ids, *, include_ready):
        states = defaultdict(list)
        for tid in ids:
            state = self._subscription_state(tid)
            if self.known_states.get(tid) != state:
                self.known_states[tid] = state
                if state != "ready" or include_ready:
                    states[state].append(tid)
        return [
            dict(type="availability", atMs=at, tokenIds=batch, status=state, drain=False)
            for state, members in states.items()
            for batch in self._batches(members, METADATA_BATCH_SIZE)
        ]

    def _detached_rows(self, at, ids):
        return [
            dict(type="gap", atMs=at, tokenIds=batch, detached=True)
            for batch in self._batches(sorted(ids), METADATA_BATCH_SIZE)
        ]

    def _baseline(self, at, *, invalidate):
        runtime = self.runtime
        ids = self._observed_ids()
        self.tracked_bids.update(cycle["token_id"] for cycle in runtime.business["cycles"].values())
        self.tracked_events.update(runtime.tokens[tid].event_id for tid in ids & self.tracked_bids)
        dropped = self.ids - ids
        self.ids = ids
        self.metadata = {tid: runtime.tokens[tid] for tid in ids}
        self.token_refs.clear()
        self.next_token_ref = 1
        self.ready.clear()
        self.off_grid.clear()
        self.known_states.clear()
        rows = []
        if invalidate:
            rows.append(dict(type="gap", atMs=at))
        rows.append(self._scope(at, baseline=True))
        rows.extend(self._metadata_rows(at, list(self.metadata.values())))
        rows.extend(self._detached_rows(at, dropped))
        rows.extend(self._availability_rows(at, ids, include_ready=invalidate))
        books = []
        for tid, token in self.metadata.items():
            book = runtime.books.get(tid)
            if (
                book is not None
                and book.ready
                and token.open
                and token.tick in SUPPORTED_CLOB_TICKS
                and self.known_states[tid] != "unavailable"
            ):
                books.append(self._book(tid, book))
        rows.extend(
            dict(type="book_batch", atMs=at, books=batch, drain=False)
            for batch in self._batches(books, BOOK_BATCH_SIZE)
        )
        rows.append(dict(type="control", atMs=at, status="RUNNING", baselineEnd=True))
        epoch = self.recorder.epoch
        if self._record(rows):
            self.needs_baseline = False
            self.last_baseline_at = at
            self._recorder_epoch = epoch
            self._error_reported = False
            self._preferences = runtime.preferences
            self._official_tags = sorted(runtime.official_tags)

    def _update_scope(self, at, *, metadata_changed):
        runtime = self.runtime
        if not (
            metadata_changed
            or self._preferences != runtime.preferences
            or runtime.monitored != self.ids
        ):
            return
        ids = self._observed_ids()
        if not metadata_changed and self._preferences == runtime.preferences and self.ids == ids:
            return
        removed = self.ids - ids
        additions = ids - self.ids
        changed = [
            runtime.tokens[tid]
            for tid in (ids if metadata_changed else additions)
            if self.metadata.get(tid) != runtime.tokens[tid]
        ]
        # Closure observed at discovery can coincide with leaving monitored.
        # Preserve that public state for a hypothetical position too.
        for tid in removed:
            token = runtime.tokens.get(tid)
            if token is not None and not token.open and self.metadata.get(tid) != token:
                changed.append(token)
        self.ids = ids
        self.metadata.update((token.token_id, token) for token in changed)
        rows = []
        if self._preferences != runtime.preferences:
            rows.append(self._scope(at))
        rows.extend(self._metadata_rows(at, changed))
        if removed:
            rows.extend(self._detached_rows(at, removed))
            self.ready.difference_update(removed)
            for tid in removed:
                self.off_grid.pop(tid, None)
                self.known_states.pop(tid, None)
        rows.extend(self._availability_rows(at, ids, include_ready=True))
        # Re-entered subscriptions can carry a valid existing consumer book.
        books = []
        for tid in additions:
            book = runtime.books.get(tid)
            token = self.metadata[tid]
            if (
                book is not None
                and book.ready
                and token.open
                and token.tick in SUPPORTED_CLOB_TICKS
                and self.known_states[tid] != "unavailable"
            ):
                books.append(self._book(tid, book))
        rows.extend(
            dict(type="book_batch", atMs=at, books=batch, drain=False)
            for batch in self._batches(books, BOOK_BATCH_SIZE)
        )
        if self._record(rows):
            for tid in removed:
                self.metadata.pop(tid, None)
                self.token_refs.pop(tid, None)
            self._preferences = runtime.preferences
            self._official_tags = sorted(runtime.official_tags)

    @staticmethod
    def _compact_levels(levels):
        # Raw contains JSON we just encoded. It is embedded as the same
        # array by the archive encoder, while freeing the level tuples.
        if len(levels) >= PACK_DEPTH_AT:
            return msgspec.Raw(msgspec.json.encode(levels))
        return levels

    def _book(self, tid, book):
        token = self.metadata[tid]
        asks = [
            (price, qty)
            for price, qty in book.ask.external.items()
            if price <= MAX_BUY_PRICE_MICROS
        ]
        if asks:
            self.tracked_bids.add(tid)
            self.tracked_events.add(token.event_id)
        tracked = tid in self.tracked_bids
        invalid = {
            (side, price)
            for side, depth in (("BUY", book.bid.external), ("SELL", book.ask.external))
            for price in depth
            if price % token.tick
        }
        if invalid:
            self.off_grid[tid] = invalid
        else:
            self.off_grid.pop(tid, None)
        self.ready.add(tid)
        return dict(
            tokenId=tid,
            venueMs=book.timestamp // 1_000_000,
            bids=self._compact_levels(list(book.bid.external.items())) if tracked else [],
            asks=self._compact_levels(asks),
            bidCovered=tracked,
            invalidGrid=bool(invalid),
        )

    def book(self, tid, book):
        if self.recording() and tid in self.ids:
            self._observe("book", self.at_ms(), **self._book(tid, book))

    def deltas(self, affected, venue):
        if not self.recording():
            return
        at = self.at_ms()
        promotions = set()
        books, changes = [], []
        touches = set()
        updated = set()
        for tid, side, levels in affected:
            if tid not in self.ready or tid in self.tracked_bids or side != "SELL":
                continue
            current = self.runtime.books[tid]
            if any(
                price <= MAX_BUY_PRICE_MICROS and current.ask.external.get(price)
                for price, _ in levels
            ):
                promotions.add(tid)
        for tid in sorted(promotions):
            books.append(self._book(tid, self.runtime.books[tid]))
        for tid, side, levels in affected:
            if tid not in self.ready or tid in promotions:
                continue
            token = self.metadata[tid]
            invalid = self.off_grid.get(tid)
            was_invalid = bool(invalid)
            for price, qty in levels:
                if price % token.tick:
                    if qty:
                        invalid = self.off_grid.setdefault(tid, set())
                        invalid.add((side, price))
                    elif invalid is not None:
                        invalid.discard((side, price))
            if invalid is not None and not invalid:
                self.off_grid.pop(tid, None)
            selected = (
                levels
                if side == "BUY" and tid in self.tracked_bids
                else [
                    (price, qty)
                    for price, qty in levels
                    if side == "SELL" and price <= MAX_BUY_PRICE_MICROS
                ]
            )
            if selected or was_invalid != bool(invalid):
                changes.append(
                    dict(tokenId=tid, side=side, levels=selected, invalidGrid=bool(invalid))
                )
                updated.add(tid)
            elif token.event_id in self.tracked_events:
                # Filtered depth has no execution effect, but this accepted
                # update dirtied its event in the original strategy drain.
                touches.add(tid)
        refs = sorted(self.token_refs[tid] for tid in touches - updated)
        if books or changes:
            row = dict(venueMs=venue, books=books, changes=changes)
            if refs:
                row["touches"] = refs
            self._observe("delta_batch", at, **row)
        elif refs:
            self._observe("touch", at, tokenRefs=refs)
        elif self._candidate_batch_due():
            self.pulse(at)

    def _candidate_batch_due(self):
        runtime = self.runtime
        interval = runtime.CANDIDATE_BATCH_NS
        if not interval:
            return False
        last = runtime._candidate_flushed_at
        now = runtime.now
        # Even an empty actual drain advances the batch clock. Preserve that
        # boundary without emitting a row for every unrelated high-price frame.
        return last is None or not 0 <= now - last < interval

    def pulse(self, at, *, refresh=False):
        self._observe("pulse", at, refresh=refresh)

    def gap(self, ids):
        if self.recording():
            affected = self.ids.intersection(ids) & self.ready
            if affected:
                self.ready.difference_update(affected)
                self._observe("gap", self.at_ms(), tokenIds=sorted(affected))

    def tick(self, tid, tick):
        if self.recording() and tid in self.ids:
            self.ready.discard(tid)
            self.off_grid.pop(tid, None)
            self.metadata[tid] = replace(self.metadata[tid], tick=tick)
            self._observe("tick", self.at_ms(), tokenId=tid, tick=tick)

    def availability(self, ids, state):
        if self.recording():
            affected = sorted(self.ids.intersection(ids))
            if state != "ready":
                self.ready.difference_update(affected)
            if affected:
                self.known_states.update((tid, state) for tid in affected)
                self._observe(
                    "availability",
                    self.at_ms(),
                    tokenIds=affected,
                    status=state,
                    drain=state == "ready",
                )

    def settlement(self, token, payouts):
        if not self.recording() or token.condition_id in self.settled:
            return
        runtime = self.runtime
        at = self.at_ms()
        if len(payouts) != 2 or any(tid not in runtime.tokens for tid in payouts):
            self.gap(payouts)
            return
        unseen = [runtime.tokens[tid] for tid in payouts if tid not in self.metadata]
        rows = self._metadata_rows(at, unseen) if unseen else []
        rows.append(
            dict(type="settlement", atMs=at, conditionId=token.condition_id, payouts=dict(payouts))
        )
        if self._record(rows):
            self.settled.add(token.condition_id)
            self.ids.difference_update(payouts)
            self.ready.difference_update(payouts)
            self.tracked_bids.difference_update(payouts)
            for tid in payouts:
                self.metadata.pop(tid, None)
                self.token_refs.pop(tid, None)
                self.off_grid.pop(tid, None)
                self.known_states.pop(tid, None)
