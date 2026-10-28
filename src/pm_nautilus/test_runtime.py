"""TEST-only scheduling: defer unowned previews while preserving every quote."""

from .strategy import Runtime, TokenRegistry


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
        super().__init__(path, mode, clock, live_factory, storage_guard)
        self.tokens = TestTokenRegistry(self.tokens)

    def add_tokens(self, tokens, official_tags=None):
        # Shared-feed planning may cache unchanged consumers. Metadata/tick
        # delivery invalidates that plan even when eligibility stays unchanged.
        self.test_market_epoch += 1
        return super().add_tokens(tokens, official_tags)

    def refresh_eligibility(self):
        previous = self.monitored
        result = super().refresh_eligibility()
        self._test_can_defer = self.status == "PAUSED"
        if previous != self.monitored:
            self.test_market_epoch += 1
        return result

    def evaluate(self, event_id, dispatch=True):
        try:
            return super().evaluate(event_id, dispatch)
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

    def drain(self):
        if self.faulted or not self._test_can_defer:
            return super().drain()
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
            self.dirty.update(deferred)

    def pause(self):
        result = super().pause()
        self._test_can_defer = True
        return result

    def refresh_test_market_preview(self):
        """Owner-loop, read-only preview requested by the TEST market detail view."""
        if self.faulted or self.status != "PAUSED":
            return
        maintenance = self._maintenance_events()
        pending = self.dirty - maintenance
        pending.update(self._time_sensitive_events & self.events.keys() - maintenance)
        for event_id in pending:
            self.evaluate(event_id, dispatch=False)
        # Never consume held-position maintenance work from a page read. START
        # changes control status and the original refresh_eligibility marks all
        # current events dirty again before the unchanged cross-event drain.
        self.dirty.difference_update(pending)

    def test_market_pending_events(self):
        # Summary responses must not present an old READY preview as current.
        return self.dirty if self.status == "PAUSED" else ()


def create_runtime(path, mode="TEST", clock=None, live_factory=None, storage_guard=None):
    """Retain the app's Runtime factory seam and the original LIVE construction."""
    runtime_type = TestRuntime if mode == "TEST" else Runtime
    return runtime_type(path, mode, clock, live_factory, storage_guard)
