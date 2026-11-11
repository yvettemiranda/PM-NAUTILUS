"""Conservative TEST-only reuse of an already complete public subscription plan."""

import asyncio

from .market import ORIGINAL_CONSUMER_ID_RULES, MarketService, PublicMarketHub
from .strategy import Runtime, TokenRegistry
from .test_runtime import TestRuntime, TestTokenRegistry
from .test_quotes import PreparedTestQuote
from .test_public_queries import TestPublicQueries

# A periodic (5 s control loop) TEST request may reuse a full plan this recent.
# Every TEST account's loop asks independently, and one full plan already covers
# all consumers, so ten accounts produced about two complete plans a second.
PERIODIC_PLAN_SECONDS = 5

_ORIGINAL_MESSAGE = MarketService.message
_ORIGINAL_SUBSCRIPTIONS = MarketService.desired_subscription_ids


class TestMarketService(MarketService):
    """Identify redundant TEST requests without changing LIVE's entry point."""

    __test__ = False

    def start(self):
        if self.closed:
            raise ValueError("该TEST账户后台已停止，请重新启动程序后恢复")
        return super().start()

    async def start_async(self):
        if self.closed:
            raise ValueError("该TEST账户后台已停止，请重新启动程序后恢复")
        result = await super().start_async()
        if self.hub is not None and self._scan_finished_at is not None and not self.closed:
            # A PAUSED account held only owned markets. Subscribe the full
            # eligible set now rather than at the next 5 s control tick. Books
            # that are not ready yet keep their events INCOMPLETE (no buy).
            try:
                await self.sync_subscriptions()
            except Exception:
                pass  # The 5 s control loop plans again and owns error reporting.
        return result

    async def sync_subscriptions(self):
        if type(self.hub) is TestMarketHub:
            await self.hub.sync_test_subscriptions(self)
        else:
            await super().sync_subscriptions()

    async def get(self, url, **params):
        if (
            type(self.hub) is TestMarketHub
            and type(self.runtime) is TestRuntime
            and not self.closed
            and self.http is self.hub.http
        ):
            return await self.hub._test_public_queries.get(super().get, url, **params)
        return await super().get(url, **params)


def create_market_service(runtime, transport=None, hub=None):
    kind = TestMarketService if type(runtime) is TestRuntime else MarketService
    return kind(runtime, transport=transport, hub=hub)


class TestMarketHub(PublicMarketHub):
    """Skip redundant planning only when it cannot repair or change anything.

    Sockets, heartbeat and recovery remain the original hub's implementation.
    TEST quotes reuse immutable integer decoding; LIVE consumes original raw
    messages and always executes its original subscription planning path.
    Only redundant TEST requests may reuse a completely ready mixed plan.
    """

    __test__ = False

    def __init__(self, transport=None):
        super().__init__(transport=transport)
        self._complete_test_plan = None
        self.plan_runs = 0
        self.plan_skips = 0
        self._test_sync_requester = None
        self._test_public_queries = TestPublicQueries()
        self._last_full_plan_at = None
        self.plan_debounces = 0

    async def close(self):
        try:
            await self._test_public_queries.close()
        finally:
            await super().close()

    def consumer_subscription_ids(self, service):
        if type(service) is not TestMarketService or type(service.runtime) is not TestRuntime:
            return super().consumer_subscription_ids(service)
        try:
            ids = super().consumer_subscription_ids(service)
            if type(ids) is not set:
                raise ValueError("TEST订阅集合无效")
            return ids
        except Exception as exc:
            self.consumer_error(service, exc)
            service.closed = True
            self.services.discard(service)
            return set()

    def _prepare_subscription_consumer(self, service, ids):
        if type(service) is not TestMarketService or type(service.runtime) is not TestRuntime:
            return super()._prepare_subscription_consumer(service, ids)
        try:
            return super()._prepare_subscription_consumer(service, ids)
        except Exception as exc:
            self.consumer_error(service, exc)
            service.closed = True
            self.services.discard(service)
            return False

    def _known_quote_consumers(self):
        has_test = False
        for service in self.services:
            if type(service) not in (MarketService, TestMarketService) or service.closed:
                return False
            runtime = service.runtime
            has_test |= type(runtime) is TestRuntime
            if not (
                (
                    type(service) is TestMarketService
                    and type(runtime) is TestRuntime
                    and runtime.mode == "TEST"
                )
                or (
                    type(service) is MarketService
                    and type(runtime) is Runtime
                    and runtime.mode == "LIVE"
                )
            ):
                return False
            if (
                getattr(service.message, "__func__", None) is not _ORIGINAL_MESSAGE
                or getattr(service.desired_subscription_ids, "__func__", None)
                is not _ORIGINAL_SUBSCRIPTIONS
            ):
                return False
        return has_test

    def message(self, msg, allowed):
        if self.closed:
            return
        if not self._known_quote_consumers() or not PreparedTestQuote.supports(msg):
            return super().message(msg, allowed)
        return super().message(msg, allowed, _prepared=PreparedTestQuote(msg))

    def _deliver_message(self, service, msg, allowed, _prepared):
        if _prepared is not None and type(service) is TestMarketService:
            touched = {
                token_id
                for token_id in _prepared.touched
                if token_id in allowed and token_id in service.runtime.monitored
            }
            if not touched and self._idle_delivery(service):
                return
            service.message(msg, touched, _prepared=_prepared)
        elif type(service) is TestMarketService and type(service.runtime) is TestRuntime:
            ids = self._consumer_allowed(service, allowed)
            if not ids and self._idle_delivery(service):
                return
            service.message(msg, ids)
        else:
            super()._deliver_message(service, msg, allowed, _prepared)

    @staticmethod
    def _idle_delivery(service):
        """A frame with none of this TEST account's tokens and no pending work.

        The original call would update no book and drain an empty work set
        (no evaluation, no order); skipping it changes nothing. Any pending
        dirty Event or waiting candidate batch keeps the original call.
        """
        runtime = service.runtime
        return (
            type(runtime) is TestRuntime
            and getattr(service.message, "__func__", None) is MarketService.message
            and not runtime.dirty
            and not runtime._candidate_backlog
        )

    async def sync_test_subscriptions(self, requester):
        # Same lock as the original entry point. Never leak a TEST request's
        # permission to skip into a concurrent LIVE/control/recovery request.
        async with self.subscription_lock:
            eligible = (
                type(requester) is TestMarketService
                and requester.hub is self
                and requester in self.services
                and not requester.closed
            )
            if (
                eligible
                and getattr(requester, "_periodic_sync", False)
                and self._recent_plan_covers(requester)
            ):
                self.plan_debounces += 1
                return
            self._test_sync_requester = requester if eligible else None
            try:
                await self._sync_subscriptions()
            finally:
                self._test_sync_requester = None

    def _recent_plan_covers(self, requester):
        """A periodic TEST request has nothing a recent full plan did not cover.

        Anything the requester itself changed, a pending recovery, new discovery
        metadata, a ready shared book it has not received yet, or any LIVE
        consumer always runs the original planning path. Otherwise the next full
        plan (at most PERIODIC_PLAN_SECONDS away) serves every consumer.
        """
        try:
            if self._last_full_plan_at is None or self.closed:
                return False
            if self._loop_time() - self._last_full_plan_at >= PERIODIC_PLAN_SECONDS:
                return False
            runtime = requester.runtime
            if (
                type(requester) is not TestMarketService
                or type(runtime) is not TestRuntime
                or runtime.mode != "TEST"
                or runtime.faulted
                or runtime.market_recovering
                or self._has_recovery(self)
                or self._has_recovery(requester)
                or any(
                    type(service) not in (MarketService, TestMarketService)
                    or service.runtime.mode != "TEST"
                    for service in self.services
                )
                or (
                    self.discovery_version
                    and self.consumer_versions.get(requester) != self.discovery_version
                )
            ):
                return False
            ids = requester.desired_subscription_ids()
            if self._subscription_monitored.get(requester) != ids:
                return False
            assigned = set().union(*self.active_groups.values()) if self.active_groups else set()
            if not ids <= assigned:
                return False
            source_books, books = self.runtime.books, runtime.books
            for tid in ids:
                source = source_books.get(tid)
                if source is not None and source.ready:
                    own = books.get(tid)
                    if own is None or not own.ready:
                        return False
            return True
        except Exception:
            return False

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
            # Check every consumer, including a closing LIVE owner. Unknown
            # runtimes/services must use the complete original planning path.
            if any(
                type(service) not in (MarketService, TestMarketService)
                or service.closed
                or not (
                    (type(service.runtime) is TestRuntime and service.runtime.mode == "TEST")
                    or (type(service.runtime) is Runtime and service.runtime.mode == "LIVE")
                )
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
                if type(runtime) is TestRuntime:
                    if (
                        type(runtime.tokens) is not TestTokenRegistry
                        or type(runtime.test_market_epoch) is not int
                        or type(runtime.tokens.test_version) is not int
                    ):
                        return None
                    metadata = (runtime.test_market_epoch, runtime.tokens.test_version)
                else:
                    if type(runtime.tokens) is not TokenRegistry:
                        return None
                    # Original LIVE has no TEST version counters. Tokens are
                    # immutable; retain their complete contents so same-ID
                    # tick/metadata replacements cannot hit an old signature.
                    metadata = tuple(runtime.tokens.items())
                if (
                    runtime.store._closed
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
                        metadata,
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
        if not any(
            type(service.runtime) is TestRuntime and not service.closed for service in self.services
        ):
            # Once the application transfers ownership to LIVE, no TEST cache
            # or whole-TEST signature needs to be consulted at all.
            self._complete_test_plan = None
            self.plan_runs += 1
            await super()._sync_subscriptions()
            self._last_full_plan_at = self._loop_time()
            return
        signature = self._complete_signature()
        test_request = self._test_sync_requester is not None
        mixed = any(service.runtime.mode == "LIVE" for service in self.services)
        if (
            (test_request or not mixed)
            and signature is not None
            and signature == self._complete_test_plan
        ):
            self.plan_skips += 1
            # The previous complete plan is proven current: as good as a new one.
            self._last_full_plan_at = self._loop_time()
            return
        self._complete_test_plan = None
        self.plan_runs += 1
        await super()._sync_subscriptions()
        self._last_full_plan_at = self._loop_time()
        self._complete_test_plan = self._complete_signature()

    @staticmethod
    def _loop_time():
        return asyncio.get_running_loop().time()


# Only adds error isolation around the original rule.
ORIGINAL_CONSUMER_ID_RULES.add(TestMarketHub.consumer_subscription_ids)
