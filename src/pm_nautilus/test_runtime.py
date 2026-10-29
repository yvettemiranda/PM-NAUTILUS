"""TEST-only scheduling: defer unowned previews while preserving every quote."""

from sqlite3 import Connection

from .store import Store
from .strategy import Runtime, TokenRegistry
from .test_construction import close_test_runtime, construct_test_runtime


class TestTokenRegistry(TokenRegistry):
    """The original metadata index plus a TEST-only mutation version for caches."""

    __test__ = False

    def __init__(self, *args, **kwargs):
        self.test_version = 0
        super().__init__(*args, **kwargs)

    def __setitem__(self, key, token):
        changed = key not in self or self[key] != token
        super().__setitem__(key, token)
        if changed:
            self.test_version += 1

    def __delitem__(self, key):
        super().__delitem__(key)
        self.test_version += 1

    def clear(self):
        changed = bool(self)
        super().clear()
        if changed:
            self.test_version += 1


class TestRuntime(Runtime):
    __test__ = False

    def __init__(self, path, mode="TEST", clock=None, live_factory=None, storage_guard=None):
        if mode != "TEST":
            raise ValueError("TestRuntime仅适用于TEST模式")
        self.test_market_epoch = 0
        self._paused_dirty_capture = None
        self._test_can_defer = True
        self._test_preview_pending = set()
        self._test_empty_paused_proof = None
        super().__init__(path, mode, clock, live_factory, storage_guard)
        self.tokens = TestTokenRegistry(self.tokens)
        self.native.bus.subscribe("data.book.deltas.*", self._mark_test_preview_quote)

    def _mark_test_preview_quote(self, deltas):
        token_id = self.instrument_tokens.get(str(deltas.instrument_id))
        token = self.tokens.get(token_id)
        if token is not None:
            self._test_preview_pending.add(token.event_id)

    def add_tokens(self, tokens, official_tags=None):
        # Shared-feed planning may cache unchanged consumers. Metadata/tick
        # delivery invalidates that plan even when eligibility stays unchanged.
        self.test_market_epoch += 1
        return super().add_tokens(tokens, official_tags)

    def refresh_eligibility(self):
        previous = self.monitored
        result = super().refresh_eligibility()
        self._test_preview_pending.update(self.dirty)
        self._test_can_defer = self.status == "PAUSED"
        if previous != self.monitored:
            self.test_market_epoch += 1
        return result

    def evaluate(self, event_id, dispatch=True):
        try:
            result = super().evaluate(event_id, dispatch)
            self._test_preview_pending.discard(event_id)
            return result
        finally:
            if self._paused_dirty_capture is not None:
                # Original drain intentionally clears callback dirtiness at its
                # pass boundary. Keep new unowned previews pending for the UI,
                # including an event whose position was just fully sold.
                self._paused_dirty_capture.update(self.dirty)

    def _maintenance_events(self):
        # PAUSED keeps the original target sells, stop observations, pending
        # orders and settlement rights. Only events with no owned work may wait.
        events = set(self.business["cycles"])
        events.update(target["event_id"] for target in self.business["targets"].values())
        events.update(
            intent["event_id"] for intent in self.store.intents(active_only=True).values()
        )
        events.update(
            claim["event_id"]
            for claim in self.business["claims"].values()
            if claim["state"] != "CREDITED"
        )
        return events

    def _empty_paused_without_work(self):
        """Skip only a previously proven empty PAUSED owner with no DB changes.

        Each quote still checks foreign SQLite commits. Transaction snapshots
        cannot establish or reuse a proof: a later rollback may restore rights.
        Financial objects, cash, holdings and orders are never shared or cached.
        """
        business = self.business
        store = self.store
        if (
            self.mode != "TEST"
            or type(business) is not dict
            or any(
                type(business.get(key)) is not dict or business[key]
                for key in ("cycles", "targets", "claims")
            )
            or "recovery_error" not in business
            or business["recovery_error"] is not None
            or self._paused_dirty_capture is not None
            or type(store) is not Store
            or store._closed
            or type(store.db) is not Connection
        ):
            self._test_empty_paused_proof = None
            return False
        db = store.db
        try:
            if db.in_transaction:
                self._test_empty_paused_proof = None
                return False
            version = db.execute("PRAGMA data_version").fetchone()[0]
            stamp = (store, db, db.total_changes, version, self._start_epoch)
            proof = self._test_empty_paused_proof
            if proof is not None and proof[0] == stamp:
                return True
            self._test_empty_paused_proof = None
            if self.status != "PAUSED" or store.intents(active_only=True):
                return False
            generation = store.generation
            # A writer can commit between the separate reads above. Such an
            # uncertain observation must retain the original complete path.
            after = db.execute("PRAGMA data_version").fetchone()[0]
            if stamp != (store, db, db.total_changes, after, self._start_epoch):
                return False
            self._test_empty_paused_proof = (stamp, generation)
            return True
        except Exception:
            self._test_empty_paused_proof = None
            return False

    def drain(self):
        if self.faulted or not self._test_can_defer:
            return super().drain()
        if self._empty_paused_without_work():
            return
        if self.status != "PAUSED":
            self._test_can_defer = False
            return super().drain()
        maintenance = self.dirty & self._maintenance_events()
        if not maintenance:
            # Keep these exact events dirty. Books/native quotes and durable
            # shadow depth have already been updated by the ordinary feed path.
            return
        deferred = self.dirty - maintenance
        self.dirty = maintenance
        previous_capture = self._paused_dirty_capture
        captured = set()
        self._paused_dirty_capture = captured
        try:
            return super().drain()
        finally:
            self._paused_dirty_capture = previous_capture
            try:
                deferred.update(captured - self._maintenance_events())
            except Exception:
                # A failing ledger must not erase pending work or replace the
                # original exception merely because rights cannot be reread.
                deferred.update(captured)
            self._test_preview_pending.update(captured & deferred)
            self.dirty.update(deferred)

    def disconnect(self, token_ids):
        token_ids = tuple(token_ids)
        result = super().disconnect(token_ids)
        self._test_preview_pending.update(
            self.tokens[token_id].event_id for token_id in token_ids if token_id in self.tokens
        )
        return result

    def pause(self):
        self._test_empty_paused_proof = None
        result = super().pause()
        self._test_can_defer = True
        return result

    def refresh_test_market_preview(self):
        """Owner-loop, read-only preview requested by the TEST market detail view."""
        if self.faulted or self.status != "PAUSED":
            return
        maintenance = self._maintenance_events()
        pending = self._test_preview_pending - maintenance
        pending.update(self._time_sensitive_events & self.events.keys() - maintenance)
        for event_id in pending:
            self.evaluate(event_id, dispatch=False)
        # Preview completion belongs only to the page. Trading dirtiness must
        # survive reads so a quote seen while PAUSED is still pending at START.

    def _finish_start(self, result):
        self._test_empty_paused_proof = None
        if result["ok"]:
            # PAUSE followed by START can happen before the next control tick,
            # leaving the old control cache at RUNNING. Every explicit START
            # must rebuild the complete current candidate ordering regardless
            # of that cache, prior page reads or deferred quotation work.
            self._refresh_control = None
        return super()._finish_start(result)

    async def start_async(self):
        # A PAUSE during even the first storage await invalidates this START.
        # The original LIVE implementation remains untouched by this TEST guard.
        epoch = self._start_epoch
        if self.storage_guard is not None:
            await self.storage_guard.refresh()
        if epoch != self._start_epoch:
            raise ValueError("启动核对期间状态已更改，请重新点击 START")
        self._check_start_readiness()
        result = await self.validate_async()
        if epoch != self._start_epoch:
            raise ValueError("启动核对期间状态已更改，请重新点击 START")
        self._check_start_readiness()
        self._finish_start(result)

    def test_market_pending_events(self):
        # Summary responses must not present an old READY preview as current.
        return self._test_preview_pending if self.status == "PAUSED" else ()

    def close(self):
        return close_test_runtime(self)


def create_runtime(path, mode="TEST", clock=None, live_factory=None, storage_guard=None):
    """Retain the app's Runtime factory seam and the original LIVE construction."""
    if mode == "TEST":
        return construct_test_runtime(TestRuntime, path, mode, clock, live_factory, storage_guard)
    return Runtime(path, mode, clock, live_factory, storage_guard)
