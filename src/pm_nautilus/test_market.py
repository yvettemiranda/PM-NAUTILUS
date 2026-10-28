"""Conservative TEST-only reuse of an already complete public subscription plan."""

from .market import MarketService, PublicMarketHub
from .test_runtime import TestRuntime, TestTokenRegistry


class TestMarketHub(PublicMarketHub):
    """Skip redundant planning only when it cannot repair or change anything.

    Sockets, heartbeat, quote delivery and recovery remain the original hub's
    implementation. A LIVE/legacy consumer, an unknown state or any incomplete
    baseline always executes the original subscription path without waiting.
    """

    __test__ = False

    def __init__(self, transport=None):
        super().__init__(transport=transport)
        self._complete_test_plan = None
        self.plan_runs = 0
        self.plan_skips = 0

    @staticmethod
    def _has_recovery(owner):
        return any(
            getattr(owner, name)
            for name in (
                "_availability_fresh_pending",
                "_availability_subscribe_pending",
                "_availability_fresh_requested_at",
                "_availability_timestamp_floor",
            )
        )

    def _complete_signature(self):
        """Owner-loop snapshot, never a substitute for quote/book validation."""
        if self.closed or not self.services or not self.active_groups:
            return None
        try:
            services = tuple(sorted(self.services, key=id))
            # Check every registered consumer, including a closing LIVE owner.
            # No optimization of a mixed source may change its old behavior.
            if any(
                type(service) is not MarketService
                or service.closed
                or type(service.runtime) is not TestRuntime
                or service.runtime.mode != "TEST"
                for service in services
            ):
                return None
            if (
                self._scan_tick_versions is not None
                or self.scan_status.get("scanning")
                or self.scan_status.get("streamError")
                or self.scan_status.get("serviceError")
                or self.scan_status.get("lastError")
                or self.scan_status.get("storageDeferred")
                or self._has_recovery(self)
                or set(self.active_groups) != set(self.tasks)
                or set(self.active_groups) != set(self.streams)
                or set(self.active_groups) != set(self.sockets)
            ):
                return None
            assigned = set()
            groups = []
            for group, ids in self.active_groups.items():
                entry, task, socket = self.streams[group], self.tasks[group], self.sockets[group]
                if (
                    not ids
                    or entry.get("state") != "READY"
                    or entry.get("error")
                    or task.done()
                    or any(
                        (book := self.runtime.books.get(tid)) is None or not book.ready
                        for tid in ids
                    )
                ):
                    return None
                assigned.update(ids)
                groups.append((group, frozenset(ids), task, socket))
            consumers = []
            desired = set()
            for service in services:
                runtime = service.runtime
                if (
                    type(runtime.tokens) is not TestTokenRegistry
                    or type(runtime.test_market_epoch) is not int
                    or type(runtime.tokens.test_version) is not int
                    or runtime.store._closed
                    or runtime.faulted
                    or not runtime.storage_can_open
                    or service.scan_status.get("scanning")
                    or service.scan_status.get("streamError")
                    or service.scan_status.get("serviceError")
                    or service.scan_status.get("lastError")
                    or service.scan_status.get("storageDeferred")
                    or runtime.market_recovering
                    or self._has_recovery(service)
                    or (
                        self.discovery_version
                        and self.consumer_versions.get(service) != self.discovery_version
                    )
                ):
                    return None
                ids = frozenset(service.desired_subscription_ids())
                if self._subscription_monitored.get(service) != ids:
                    return None
                if any((book := runtime.books.get(tid)) is None or not book.ready for tid in ids):
                    return None
                desired.update(ids)
                consumers.append(
                    (
                        service,
                        runtime,
                        runtime.tokens,
                        runtime.store.generation,
                        runtime.test_market_epoch,
                        runtime.tokens.test_version,
                        runtime._start_epoch,
                        runtime.status,
                        runtime.preferences.model_dump_json(),
                        frozenset(runtime.monitored),
                        frozenset(runtime.market_availability),
                        frozenset(service.unsupported_ticks.items()),
                        ids,
                    )
                )
            if assigned != desired or set(self._subscription_monitored) != set(services):
                return None
            return (
                self.discovery_version,
                tuple(sorted(self._tick_versions.items())),
                len(self.runtime.tokens),
                tuple(groups),
                tuple(consumers),
            )
        except Exception:
            # A new/mocked runtime or an unfamiliar transitional state must use
            # the old path, where its ordinary validation/error handling applies.
            return None

    async def _sync_subscriptions(self):
        signature = self._complete_signature()
        if signature is not None and signature == self._complete_test_plan:
            self.plan_skips += 1
            return
        self._complete_test_plan = None
        self.plan_runs += 1
        await super()._sync_subscriptions()
        self._complete_test_plan = self._complete_signature()
