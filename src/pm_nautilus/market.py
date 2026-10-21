"""Public Gamma discovery and market WS, feeding canonical Nautilus book events.

No credential import, private endpoint or signed client exists in this module.
"""

import asyncio
import json
import random
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx
from websockets.asyncio.client import connect, process_exception
from websockets.exceptions import ConnectionClosed

from .config import micros
from .books import Book
from .rules import Token, Fees, SUPPORTED_CLOB_TICKS
from .market_availability import market_state, missing_orderbook

GAMMA = "https://gamma-api.polymarket.com"
WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
TAGS = f"{GAMMA}/tags/102982/related-tags/tags"
HEARTBEAT_SECONDS = 10
RECOVERY_SECONDS = 30
AVAILABILITY_RECHECK_SECONDS = 300
CLOB_BOOK = "https://clob.polymarket.com/book"
FRESH_BASELINE_RECOVERY_SECONDS = 30
DISCOVERY_PAGE_ATTEMPTS = 3
DISCOVERY_PAGE_SECONDS = 60
DISCOVERY_RETRY_AFTER_CAP_SECONDS = 5
DISCOVERY_RETRY_STATUSES = {429, 502, 503, 504}


def timestamp(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1_000_000_000)
    except (ValueError, OverflowError):
        return None


def array(value):
    result = json.loads(value) if isinstance(value, str) else value
    return result if isinstance(result, list) else []


def normalize(event):
    if any(
        event.get(k) is not v for k, v in (("active", True), ("closed", False), ("archived", False))
    ):
        return []
    markets = event.get("markets", [])
    if event.get("negRiskAugmented") is True or not markets:
        return []
    n = 2 if len(markets) == 1 else len(markets) if event.get("negRisk") is True else 0
    if n < 2:
        return []
    out = []
    for m in markets:
        # CTF outcomes use clobTokenIds and the CTF redemption path. V2 uses
        # positionIds and a different exchange/settlement path.
        if m.get("version", event.get("version")) not in {None, "v1"} or m.get("positionIds"):
            continue
        if any(
            m.get(k) is not v
            for k, v in (
                ("active", True),
                ("closed", False),
                ("archived", False),
                ("acceptingOrders", True),
                ("enableOrderBook", True),
            )
        ):
            continue
        try:
            condition = m.get("conditionId", "")
            if len(condition) != 66 or not condition.startswith("0x"):
                continue
            int(condition[2:], 16)
            ids, outcomes = array(m.get("clobTokenIds")), array(m.get("outcomes"))
            if len(ids) != 2 or len(outcomes) != 2 or len(set(ids)) != 2:
                continue
            if any(not str(x).isdigit() for x in ids):
                continue
            enabled = m.get("feesEnabled")
            schedule = m.get("feeSchedule") or {}
            fees = Fees(
                enabled,
                micros(schedule["rate"]) if enabled else 0,
                schedule["exponent"] if enabled else 1,
            )
            opened = timestamp(m.get("startDate")) or timestamp(event.get("startDate"))
            ends = timestamp(m.get("endDate")) or timestamp(event.get("endDate"))
            if opened is None or ends is None or ends <= opened:
                continue
            tick, minimum = micros(m["orderPriceMinTickSize"]), micros(m["orderMinSize"])
            if tick not in SUPPORTED_CLOB_TICKS or minimum <= 0:
                continue
            tags = {
                str(t["id"]): t.get("label") or t.get("slug") or str(t["id"])
                for t in event.get("tags", []) + m.get("tags", [])
                if t.get("id") is not None
            }
            for tid, outcome in zip(ids, outcomes, strict=True):
                out.append(
                    Token(
                        str(tid),
                        str(event["id"]),
                        str(m["id"]),
                        condition,
                        str(outcome),
                        n,
                        event.get("negRisk") is True,
                        opened,
                        ends,
                        fees,
                        minimum,
                        tick,
                        event.get("title", ""),
                        m.get("question", ""),
                        event.get("slug", ""),
                        tuple(tags),
                        tuple(tags.values()),
                        timestamp(m.get("gameStartTime") or event.get("startTime")),
                    )
                )
        except (KeyError, ValueError, TypeError):
            continue  # Missing metadata never silently means fee-free/tradable.
    return out


def resolution(raw, token):
    if str(raw.get("id")) != token.market_id or raw.get("conditionId") != token.condition_id:
        raise ValueError("结算 Market/Condition 身份不匹配")
    status = str(raw.get("umaResolutionStatus", "")).lower()
    if raw.get("closed") is not True or status not in {"resolved", "settled"}:
        return None
    ids = list(map(str, array(raw.get("clobTokenIds"))))
    prices = [micros(p) for p in array(raw.get("outcomePrices"))]
    if len(set(ids)) != 2 or len(prices) != 2 or token.token_id not in ids:
        return None
    if sorted(prices) not in ([0, 1_000_000], [500_000, 500_000]):
        return None
    return dict(zip(ids, prices, strict=True))


class MarketService:
    def __init__(self, runtime, transport=None, hub=None):
        self.runtime = runtime
        self.hub = hub
        self.http = (
            hub.http if hub is not None else httpx.AsyncClient(timeout=30, transport=transport)
        )
        self.tasks = {}
        self.streams = {}
        self.active_groups = {}
        self.sockets = {}
        self._availability_next_check = {}
        self._availability_fresh_pending = set()
        self._availability_subscribe_pending = set()
        self._availability_fresh_requested_at = {}
        self._availability_timestamp_floor = {}
        self.scan_status = runtime.store.get("scan", {})
        self.discovery_status = {
            "enabled": True,
            "monitoringSince": None,
            "lastAttemptAt": None,
            "lastSuccessAt": None,
            "error": None,
            "consecutiveFailures": 0,
            "failureSince": None,
            "affectedMarketCount": 0,
            "checkedMarketCount": 0,
        }
        self.categories = runtime.store.get("categories", [])
        self.closed = False
        self.loop_task = None
        self.scan_task = None
        self._scan_finished_at = None
        self.on_resolution = None
        self.resolution_task = None
        self.subscription_lock = asyncio.Lock()
        self.unsupported_ticks = runtime.store.get("unsupported_ticks", {})
        if hub is not None:
            hub.services.add(self)

    def stream_state(self, group, state, error=None):
        entry = self.streams.setdefault(group, {})
        entry.update(state=state, error=error)
        errors = [v["error"] for v in self.streams.values() if v.get("error")]
        current = errors[-1] if errors else None
        if self.scan_status.get("streamError") != current:
            self.scan_status["streamError"] = current
            self.runtime.store.put("scan", self.scan_status)

    def stream_diagnostics(self):
        if self.hub is not None:
            return self.hub.diagnostics_for(self)
        groups = []
        for group in sorted(self.streams):
            entry = self.streams[group]
            task = self.tasks.get(group)
            ids = self.subscription_ids(group)
            missing = [tid for tid in ids if not self.runtime.books[tid].ready]
            groups.append(
                dict(
                    entry,
                    tokenCount=len(ids),
                    readyBookCount=len(ids) - len(missing),
                    missingTokenIds=missing,
                    taskRunning=task is not None and not task.done(),
                )
            )
        return {"groups": groups, "groupCount": len(groups)}

    def remember_error(self, key, exc):
        name = type(exc).__name__
        detail = f"{name}: {str(exc)[:180]}".rstrip(": ")
        suffix = key[0].upper() + key[1:]
        self.scan_status[key] = name
        self.scan_status[f"last{suffix}"] = detail
        self.scan_status[f"last{suffix}At"] = datetime.now(timezone.utc).isoformat()
        self.runtime.store.put("scan", self.scan_status)

    def start(self):
        # An explicit START acknowledges a latched fatal background error. Historical
        # details stay available so an automatic safety pause is never unexplained.
        self.runtime.start()
        self.acknowledge_service_error()

    async def start_async(self):
        await self.runtime.start_async()
        self.acknowledge_service_error()

    def acknowledge_service_error(self):
        if self.scan_status.pop("serviceError", None) is not None:
            self.scan_status["serviceErrorClearedAt"] = datetime.now(timezone.utc).isoformat()
            self.runtime.store.put("scan", self.scan_status)

    async def get(self, url, **params):
        response = await self.http.get(url, params=params)
        response.raise_for_status()
        return response.json()

    async def discovery_page(self, **params):
        """Retry only this idempotent public page; never replay private requests."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + DISCOVERY_PAGE_SECONDS
        for attempt in range(DISCOVERY_PAGE_ATTEMPTS):
            if self.closed:
                raise asyncio.CancelledError
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise httpx.ReadTimeout("公开 Event 分页重试时间用尽")
            try:
                # The client's usual timeout still applies to each attempt. This
                # extra deadline also bounds the sum of requests and backoffs.
                async with asyncio.timeout(remaining):
                    return await self.get(f"{GAMMA}/events/keyset", **params)
            except TimeoutError as exc:
                raise httpx.ReadTimeout("公开 Event 分页重试时间用尽") from exc
            except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                response = exc.response if isinstance(exc, httpx.HTTPStatusError) else None
                if response is not None and response.status_code not in DISCOVERY_RETRY_STATUSES:
                    raise
                if attempt + 1 == DISCOVERY_PAGE_ATTEMPTS:
                    raise
                delay = 0.5 * 2**attempt
                if response is not None:
                    value = response.headers.get("Retry-After", "")
                    try:
                        retry_after = float(value)
                    except ValueError:
                        try:
                            retry_after = (
                                parsedate_to_datetime(value) - datetime.now(timezone.utc)
                            ).total_seconds()
                        except (ValueError, TypeError, OverflowError):
                            retry_after = 0
                    delay = max(
                        delay,
                        min(max(0, retry_after), DISCOVERY_RETRY_AFTER_CAP_SECONDS),
                    )
                delay += random.uniform(0, 0.25)
                if delay >= deadline - loop.time():
                    raise
                # Cancellation propagates through both the request and backoff;
                # closing a consumer must not start another page attempt.
                await asyncio.sleep(delay)

    @property
    def storage_available(self):
        return getattr(self.runtime, "storage_can_open", True)

    async def categories_refresh(self):
        if not self.storage_available:
            return
        tags = await self.get(TAGS, status="active", omit_empty="true")
        if not self.storage_available:
            return
        if not isinstance(tags, list) or not all(isinstance(tag, dict) for tag in tags):
            raise ValueError("首页栏目响应格式变化")
        tags = [t for t in tags if t.get("slug") not in {"all", "perps", "art"}]
        for slug in ("esports", "art"):
            tag = await self.get(f"{GAMMA}/tags/slug/{slug}", include_template="false")
            if not self.storage_available:
                return
            if not isinstance(tag, dict):
                raise ValueError("首页栏目标签响应格式变化")
            if not any(str(t.get("id")) == str(tag.get("id")) for t in tags):
                if slug == "esports":
                    idx = next(
                        (i + 1 for i, t in enumerate(tags) if t.get("slug") == "crypto"), len(tags)
                    )
                    tags.insert(idx, tag)
                else:
                    tags.append(tag)
        self.categories = [{"id": str(t["id"]), "label": t.get("label") or t["slug"]} for t in tags]
        if self.storage_available:
            self.runtime.store.put("categories", self.categories)

    async def scan(self):
        if self.closed:
            return
        if not self.storage_available:
            self.scan_status.update(scanning=False, storageDeferred=True)
            self.discovery_status["storageDeferred"] = True
            return
        self.scan_status.pop("storageDeferred", None)
        if self.hub is not None:
            self.scan_status.update(scanning=True, lastError=None)
            try:
                await self.hub.discover(self)
            finally:
                self.scan_status["scanning"] = False
                if not self.closed and self.storage_available:
                    self.runtime.store.put("scan", self.scan_status)
            return
        r = self.runtime
        generation = getattr(r.store, "generation", None)
        started_at = datetime.now(timezone.utc).isoformat()
        status = self.discovery_status
        status.update(lastAttemptAt=started_at, checkedMarketCount=0)
        if status["monitoringSince"] is None:
            status["monitoringSince"] = started_at
        status.pop("storageDeferred", None)
        self.scan_status.update(scanning=True, lastError=None)
        tokens = {}
        seen = set()
        checked_markets = set()
        after_cursor = None
        cursors = set()
        try:
            try:
                await self.categories_refresh()
                self.scan_status.pop("categoryError", None)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                self.scan_status["categoryError"] = type(exc).__name__
            while not self.closed:
                if getattr(r.store, "generation", None) != generation:
                    return
                if not self.storage_available:
                    self.scan_status["storageDeferred"] = True
                    status["storageDeferred"] = True
                    return
                params = {
                    "active": "true",
                    "closed": "false",
                    "limit": 100,
                    "order": "id",
                    "ascending": "true",
                }
                if after_cursor:
                    params["after_cursor"] = after_cursor
                payload = await self.discovery_page(**params)
                if self.closed or getattr(r.store, "generation", None) != generation:
                    return
                if not isinstance(payload, dict):
                    raise ValueError("公开 Event keyset 分页格式变化")
                page = payload.get("events")
                if not isinstance(page, list):
                    raise ValueError("公开 Event 分页格式变化")
                if not page:
                    break
                new = {str(e["id"]) for e in page} - seen
                if not new:
                    raise ValueError("公开 Event 分页没有前进")
                seen.update(new)
                for event in page:
                    for t in normalize(event):
                        if t.token_id not in self.unsupported_ticks:
                            tokens[t.token_id] = t
                    checked_markets.update(
                        str(m["id"])
                        for m in event.get("markets") or []
                        if isinstance(m, dict) and m.get("id") is not None
                    )
                status["checkedMarketCount"] = len(checked_markets)
                self.scan_status["eventCountScanned"] = len(seen)
                cursor = payload.get("next_cursor")
                if cursor is None:
                    break
                if not isinstance(cursor, str) or not cursor or cursor in cursors:
                    raise ValueError("公开 Event keyset 游标没有前进")
                cursors.add(cursor)
                after_cursor = cursor
            if self.closed or getattr(r.store, "generation", None) != generation:
                return
            if not self.storage_available:
                if not self.closed:
                    self.scan_status["storageDeferred"] = True
                    status["storageDeferred"] = True
                return
            # Commit discovery only after all pages succeed. Missing old tokens remain
            # available for held-position identity, but cannot open new cycles.
            merged = [replace(t, open=False) for tid, t in r.tokens.items() if tid not in tokens]
            r.add_tokens(merged + list(tokens.values()), [t["id"] for t in self.categories])
            self.scan_status["lastScanAt"] = datetime.now(timezone.utc).isoformat()
            status.update(
                lastSuccessAt=self.scan_status["lastScanAt"],
                error=None,
                consecutiveFailures=0,
                failureSince=None,
            )
            r.drain()
            await self.sync_subscriptions()
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            if not self.closed and getattr(r.store, "generation", None) == generation:
                self.scan_status["lastError"] = f"{type(exc).__name__}: {str(exc)[:180]}"
                status.update(
                    error=type(exc).__name__,
                    consecutiveFailures=status["consecutiveFailures"] + 1,
                    failureSince=status["failureSince"] or started_at,
                )
        finally:
            self.scan_status["scanning"] = False
            if (
                not self.closed
                and getattr(r.store, "generation", None) == generation
                and self.storage_available
            ):
                r.store.put("scan", self.scan_status)

    async def sync_subscriptions(self):
        if self.hub is not None:
            await self.hub.sync_subscriptions()
            return
        async with self.subscription_lock:
            await self._sync_subscriptions()

    def recovering_ids(self):
        recovering = getattr(self.runtime, "market_recovering", None)
        if recovering is None:
            recovering = self.runtime.market_recovering = set()
        return recovering

    def desired_subscription_ids(self):
        unavailable = getattr(self.runtime, "market_availability", {})
        return set(self.runtime.monitored) - unavailable.keys()

    def availability_refresh_due(self, token):
        return asyncio.get_running_loop().time() >= self._availability_next_check.get(
            token.token_id, 0
        )

    def _availability_checked(self, token):
        self._availability_next_check[token.token_id] = (
            asyncio.get_running_loop().time() + AVAILABILITY_RECHECK_SECONDS
        )
        return datetime.now(timezone.utc)

    def availability_refresh_failed(self, token, code="metadata_unavailable"):
        now = self._availability_checked(token)
        entry = getattr(self.runtime, "market_availability", {}).get(token.token_id)
        if entry is not None:
            entry.update(
                checkedAt=now.isoformat(),
                nextCheckAt=(now + timedelta(seconds=AVAILABILITY_RECHECK_SECONDS)).isoformat(),
                lastError=code,
            )

    def known_market_tokens(self, token):
        registry = self.runtime.tokens
        condition_ids = getattr(registry, "condition_token_ids", None)
        candidates = (
            (registry[tid] for tid in condition_ids(token.condition_id))
            if condition_ids is not None
            else registry.values()
        )
        return [
            known
            for known in candidates
            if known.market_id == token.market_id and known.condition_id == token.condition_id
        ]

    async def observe_market_availability(self, raw, token):
        """A metadata response is observational; no ownership or settlement write."""
        if not self.availability_refresh_due(token):
            return
        peers = self.known_market_tokens(token)
        try:
            state = market_state(raw, token, peers)
        except (ValueError, TypeError, KeyError):
            self.availability_refresh_failed(token, "metadata_invalid")
            return
        # Venue trading flags apply to both outcomes, scoped strictly to this
        # Market/Condition pair. Another market in the Event is unaffected.
        ids = {known.token_id for known in peers}
        unavailable = getattr(self.runtime, "market_availability", None)
        if unavailable is None:
            unavailable = self.runtime.market_availability = {}
        generation = self.runtime.store.generation
        now = self._availability_checked(token)
        for known in peers:
            self._availability_checked(known)
        if state == "open":
            if any(tid in unavailable for tid in ids):
                # Invalidate every known outcome before clearing any order gate.
                # A shared consumer may retain a newer continuous source book.
                self.runtime.disconnect(ids)
                self.recovering_ids().update(ids)
                requested_at = asyncio.get_running_loop().time()
                for tid in ids:
                    book = self.runtime.books.get(tid)
                    self._availability_timestamp_floor[tid] = (
                        book.timestamp if book is not None else 0
                    )
                    self._availability_fresh_pending.add(tid)
                    self._availability_subscribe_pending.add(tid)
                    self._availability_fresh_requested_at[tid] = requested_at
                    unavailable.pop(tid, None)
                self.runtime.dirty.add(token.event_id)
                await self.sync_subscriptions()
            return
        try:
            response = await self.http.get(CLOB_BOOK, params={"token_id": token.token_id})
        except httpx.HTTPError:
            for known in peers:
                self.availability_refresh_failed(known, "book_check_unavailable")
            return
        if (
            self.closed
            or self.runtime.store.generation != generation
            or self.runtime.tokens.get(token.token_id) != token
            or any(self.runtime.tokens.get(known.token_id) != known for known in peers)
        ):
            return
        if not missing_orderbook(response):
            for known in peers:
                entry = unavailable.get(known.token_id)
                if entry is not None:
                    entry.update(
                        checkedAt=now.isoformat(),
                        nextCheckAt=(
                            now + timedelta(seconds=AVAILABILITY_RECHECK_SECONDS)
                        ).isoformat(),
                        lastError="book_absence_unconfirmed",
                    )
            return
        for known in peers:
            tid = known.token_id
            previous = unavailable.get(tid)
            unavailable[tid] = {
                "status": "UNAVAILABLE",
                "reason": state,
                "marketId": token.market_id,
                "conditionId": token.condition_id,
                "tokenId": tid,
                "since": previous["since"] if previous is not None else now.isoformat(),
                "checkedAt": now.isoformat(),
                "nextCheckAt": (now + timedelta(seconds=AVAILABILITY_RECHECK_SECONDS)).isoformat(),
                "lastError": None,
            }
        self.runtime.disconnect(ids)
        self.recovering_ids().difference_update(ids)
        self._availability_fresh_pending.difference_update(ids)
        self._availability_subscribe_pending.difference_update(ids)
        for tid in ids:
            self._availability_fresh_requested_at.pop(tid, None)
            self._availability_timestamp_floor.pop(tid, None)
        self.runtime.dirty.add(token.event_id)
        await self.sync_subscriptions()

    def refresh_stream_readiness(self, group):
        entry = self.streams.get(group)
        if entry is not None and entry.get("state") in {"AWAITING_BOOKS", "READY"}:
            if all(self.runtime.books[tid].ready for tid in self.subscription_ids(group)):
                self.stream_state(group, "READY")

    async def _sync_subscriptions(self):
        if self.closed:
            return
        # Stable socket slots allow one unavailable market to unsubscribe without
        # invalidating healthy sibling streams or removing held identities.
        monitored = self.desired_subscription_ids()
        for group in list(self.tasks):
            active = self.active_groups.setdefault(group, set(group))
            removed = active - monitored
            active.intersection_update(monitored)
            # Preserve legacy filter compaction and tuple diagnostics. A proven
            # unavailable held token is the special case requiring in-place
            # removal so its healthy peers retain uninterrupted books.
            unavailable = getattr(self.runtime, "market_availability", {})
            if removed and not (removed & unavailable.keys()):
                task = self.tasks.pop(group)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.active_groups.pop(group, None)
                self.streams.pop(group, None)
                continue
            if removed:
                self.runtime.disconnect(removed)
                ws = self.sockets.get(group)
                if ws is not None:
                    try:
                        await ws.send(
                            json.dumps({"operation": "unsubscribe", "assets_ids": sorted(removed)})
                        )
                    except Exception:
                        pass  # Socket heartbeat owns transport recovery.
            if not active:
                task = self.tasks.pop(group)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.active_groups.pop(group, None)
                self.streams.pop(group, None)
            else:
                self.refresh_stream_readiness(group)
        # Reopened outcomes rejoin existing slots in place, even when an
        # unheld counteroutcome becomes eligible one control tick later.
        assigned = set().union(*self.active_groups.values()) if self.active_groups else set()
        recovering_additions = sorted((monitored - assigned) & self._availability_fresh_pending)
        for group, active in self.active_groups.items():
            added = recovering_additions[: 200 - len(active)]
            if not added:
                continue
            del recovering_additions[: len(added)]
            active.update(added)
            self.runtime.disconnect(added)
            ws = self.sockets.get(group)
            if ws is not None:
                try:
                    await ws.send(json.dumps({"operation": "subscribe", "assets_ids": added}))
                    self._availability_subscribe_pending.difference_update(added)
                except Exception:
                    pass
            self.stream_state(group, "AWAITING_BOOKS", self.streams.get(group, {}).get("error"))
        # Compact only excess fragments, keeping complete groups undisturbed.
        assigned = set().union(*self.active_groups.values()) if self.active_groups else set()
        new_groups = (len(monitored - assigned) + 199) // 200
        if len(self.tasks) + new_groups > (len(monitored) + 199) // 200 + 1:
            for group in list(self.tasks):
                if len(self.active_groups[group]) < 200:
                    self.runtime.disconnect(self.active_groups[group])
                    task = self.tasks.pop(group)
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    self.active_groups.pop(group, None)
                    self.streams.pop(group, None)
        assigned = set().union(*self.active_groups.values()) if self.active_groups else set()
        additions = sorted(monitored - assigned)
        for i in range(0, len(additions), 200):
            group = tuple(additions[i : i + 200])
            self.active_groups[group] = set(group)
            self.tasks[group] = asyncio.create_task(self.socket(group))
        self.runtime.release_unmonitored()
        errors = [v["error"] for v in self.streams.values() if v.get("error")]
        current = errors[-1] if errors else None
        if self.scan_status.get("streamError") != current:
            self.scan_status["streamError"] = current
            self.runtime.store.put("scan", self.scan_status)

    def message(self, msg, allowed):
        if self.closed:
            return
        r = self.runtime
        allowed = set(allowed) - getattr(r, "market_availability", {}).keys()
        kind = msg.get("event_type")
        ts = int(msg.get("timestamp", 0)) * 1_000_000
        if kind == "price_change":
            for change in msg.get("price_changes", []):
                tid = change.get("asset_id")
                if tid not in allowed or msg.get("market") != r.tokens[tid].condition_id:
                    continue
                b = r.books[tid]
                shadow = r.shadow_depth(b)
                side = (
                    "BID"
                    if change.get("side") == "BUY"
                    else "ASK"
                    if change.get("side") == "SELL"
                    else None
                )
                levels = [(micros(change["price"]), micros(change["size"]))]
                if side and b.delta(side, levels, ts):
                    r.checkpoint_shadow(tid, shadow)
                    r.native.feed_delta(r.tokens[tid], b, side, levels)
                    r.dirty.add(r.tokens[tid].event_id)
            r.drain()
        elif kind == "book":
            tid = msg.get("asset_id")
            if tid in allowed and msg.get("market") == r.tokens[tid].condition_id:
                if ts < self._availability_timestamp_floor.get(tid, 0):
                    return

                def levels(side):
                    return [(micros(x["price"]), micros(x["size"])) for x in msg[side]]

                before_revision = r.books[tid].revision
                r.book(tid, levels("bids"), levels("asks"), ts)
                if r.books[tid].ready and r.books[tid].revision != before_revision:
                    was_recovering = tid in self._availability_fresh_pending
                    self._availability_fresh_pending.discard(tid)
                    self._availability_subscribe_pending.discard(tid)
                    self._availability_fresh_requested_at.pop(tid, None)
                    self._availability_timestamp_floor.pop(tid, None)
                    self.recovering_ids().discard(tid)
                    if was_recovering:
                        r.dirty.add(r.tokens[tid].event_id)
                        r.drain()
        elif kind == "tick_size_change":
            tid = msg.get("asset_id")
            if tid in allowed and msg.get("market") == r.tokens[tid].condition_id:
                t = replace(r.tokens[tid], tick=micros(msg["new_tick_size"]))
                if t.tick <= 0:
                    raise ValueError("无效 tick 变更")
                if t.tick not in SUPPORTED_CLOB_TICKS:
                    self.unsupported_ticks[tid] = t.tick
                    r.store.put("unsupported_ticks", self.unsupported_ticks)
                    t = replace(t, open=False)
                    self.remember_error("serviceError", ValueError("市场 tick 不受当前 SDK 支持"))
                    r.pause()
                elif tid in self.unsupported_ticks:
                    self.unsupported_ticks.pop(tid)
                    r.store.put("unsupported_ticks", self.unsupported_ticks)
                r.add_tokens([t])
                r.disconnect([tid])
                raise ConnectionError("tick 变化，需要新完整盘口")

    def subscription_ids(self, group):
        return tuple(sorted(self.active_groups.get(group, group)))

    def socket_opened(self, group, ws):
        self.sockets[group] = ws

    def socket_closed(self, group):
        self.sockets.pop(group, None)

    def baseline_consumers(self, group):
        return [self]

    async def recover_consumer_baselines(self, group, ws):
        """Bounded escalation when an idempotent subscribe returned no snapshot.

        Only the affected token's baselines are invalidated. Other outcomes on
        this connection keep their continuous books, heartbeat and shadow depth.
        """
        async with self.subscription_lock:
            if self.closed or group not in self.streams:
                return
            now = asyncio.get_running_loop().time()
            consumers = self.baseline_consumers(group)
            active = set(self.subscription_ids(group))
            due = (
                set().union(
                    *(
                        {
                            tid
                            for tid, requested_at in service._availability_fresh_requested_at.items()
                            if tid in service._availability_fresh_pending
                            and tid in service.desired_subscription_ids()
                            and now - requested_at >= FRESH_BASELINE_RECOVERY_SECONDS
                        }
                        for service in consumers
                        if not service.closed
                    )
                )
                & active
            )
            if not due:
                return
            # Invalidate before the first await, retaining consumed TEST depth.
            for tid in due:
                self._availability_timestamp_floor[tid] = max(
                    self._availability_timestamp_floor.get(tid, 0),
                    self.runtime.books[tid].timestamp,
                )
            self.runtime.disconnect(due)
            for service in consumers:
                ids = due & service.desired_subscription_ids()
                for tid in ids:
                    service._availability_timestamp_floor[tid] = max(
                        service._availability_timestamp_floor.get(tid, 0),
                        service.runtime.books[tid].timestamp,
                        self._availability_timestamp_floor[tid],
                    )
                service._availability_fresh_pending.update(ids)
                service.recovering_ids().update(ids)
                for tid in ids:
                    service._availability_fresh_requested_at[tid] = now
                    service._availability_subscribe_pending.discard(tid)
            await ws.send(json.dumps({"operation": "unsubscribe", "assets_ids": sorted(due)}))
            if self.closed or not due.issubset(set(self.subscription_ids(group))):
                return
            await ws.send(json.dumps({"operation": "subscribe", "assets_ids": sorted(due)}))
            entry = self.streams.get(group)
            if entry is not None:
                entry["snapshotRetries"] = entry.get("snapshotRetries", 0) + 1
                self.stream_state(group, "AWAITING_BOOKS", entry.get("error"))

    def socket_error(self, group, key, exc):
        self.remember_error(key, exc)

    def pause_socket(self, group, exc):
        self.runtime.pause()

    async def receive(self, ws, group):
        """Text heartbeat deadlines are independent of incoming market traffic."""
        loop = asyncio.get_running_loop()
        next_ping = loop.time()
        next_recovery = loop.time() + RECOVERY_SECONDS
        last_pong = loop.time()
        while not self.closed:
            now = loop.time()
            await self.recover_consumer_baselines(group, ws)
            self.refresh_stream_readiness(group)
            if now >= next_ping:
                if now - last_pong > RECOVERY_SECONDS:
                    raise ConnectionError("市场文本心跳PONG超时")
                await ws.send("PING")
                self.streams[group]["lastPingAt"] = datetime.now(timezone.utc).isoformat()
                next_ping = loop.time() + HEARTBEAT_SECONDS
            if now >= next_recovery:
                missing = [
                    tid for tid in self.subscription_ids(group) if not self.runtime.books[tid].ready
                ]
                if missing:
                    # Only retry missing snapshots; quiet but complete books stay valid.
                    await ws.send(json.dumps({"operation": "unsubscribe", "assets_ids": missing}))
                    await ws.send(json.dumps({"operation": "subscribe", "assets_ids": missing}))
                    entry = self.streams[group]
                    entry["snapshotRetries"] = entry.get("snapshotRetries", 0) + 1
                next_recovery = loop.time() + RECOVERY_SECONDS
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.001, next_ping - loop.time()))
            except TimeoutError:
                continue
            self.streams[group]["lastReceivedAt"] = datetime.now(timezone.utc).isoformat()
            if raw == "PONG":
                last_pong = loop.time()
                self.streams[group]["lastPongAt"] = datetime.now(timezone.utc).isoformat()
                continue
            if raw == "PING":
                await ws.send("PONG")
                continue
            messages = json.loads(raw)
            for msg in messages if isinstance(messages, list) else [messages]:
                self.message(msg, self.subscription_ids(group))
                # Large snapshot bursts must allow other sockets' timers to run.
                await asyncio.sleep(0)
            if self.streams[group]["state"] != "READY" and all(
                self.runtime.books[tid].ready for tid in self.subscription_ids(group)
            ):
                self.stream_state(group, "READY")

    async def socket(self, group):
        r = self.runtime
        backoff = 1
        try:
            while not self.closed:
                r.disconnect(self.subscription_ids(group))
                previous_error = self.streams.get(group, {}).get("error")
                self.stream_state(group, "CONNECTING", previous_error)
                try:
                    async with connect(
                        WS, ping_interval=10, ping_timeout=10, max_size=16 * 1024 * 1024
                    ) as ws:
                        self.socket_opened(group, ws)
                        await ws.send(
                            json.dumps(
                                {
                                    "assets_ids": list(self.subscription_ids(group)),
                                    "type": "market",
                                    "custom_feature_enabled": True,
                                }
                            )
                        )
                        backoff = 1
                        self.stream_state(group, "AWAITING_BOOKS", previous_error)
                        await self.receive(ws, group)
                except Exception as exc:
                    # Use the pinned client's handshake classification (including
                    # HTTP 500/502/503/504). Auth errors and unknown bugs stay fatal.
                    retryable = isinstance(exc, (ConnectionClosed, ConnectionError, ValueError))
                    retryable = retryable or process_exception(exc) is None
                    self.socket_error(group, "streamError", exc)
                    self.stream_state(group, "RECONNECTING", type(exc).__name__)
                    if not retryable:
                        self.socket_error(group, "serviceError", exc)
                        self.pause_socket(group, exc)
                finally:
                    self.socket_closed(group)
                    r.disconnect(self.subscription_ids(group))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
        finally:
            self.socket_closed(group)
            r.disconnect(self.subscription_ids(group))
            if group in self.streams:
                self.stream_state(group, "STOPPED", self.streams[group].get("error"))

    async def run(self):
        while not self.closed:
            try:
                if self.scan_task is not None and self.scan_task.done():
                    completed, self.scan_task = self.scan_task, None
                    completed.result()
                if self.scan_task is None and (
                    self._scan_finished_at is None
                    or asyncio.get_running_loop().time() - self._scan_finished_at >= 300
                ):
                    # Public discovery may await hundreds of cursor pages. Keep
                    # control checks independent without overlapping scans or
                    # shortening the interval measured from scan completion.
                    self.scan_task = asyncio.create_task(self.run_scan())
                self.runtime.refresh_eligibility()
                self.runtime.drain()
                if self._scan_finished_at is not None:
                    # Preserve startup ordering: do not subscribe persisted open
                    # discovery before the first full scan attempt has finished.
                    await self.sync_subscriptions()
                if self.on_resolution and (
                    self.resolution_task is None or self.resolution_task.done()
                ):
                    self.resolution_task = asyncio.create_task(self.run_resolution())
            except Exception as exc:
                # Keep the service observable and retryable, without silently
                # leaving a dead task behind a RUNNING UI.
                self.runtime.pause()
                self.remember_error("serviceError", exc)
            await asyncio.sleep(5)

    async def run_scan(self):
        try:
            await self.scan()
        except Exception as exc:
            if not self.closed:
                self.runtime.pause()
                self.remember_error("serviceError", exc)
        finally:
            self._scan_finished_at = asyncio.get_running_loop().time()

    async def run_resolution(self):
        try:
            await self.on_resolution()
        except Exception as exc:
            if not self.closed:
                self.runtime.pause()
                self.remember_error("serviceError", exc)

    async def close(self):
        self.closed = True
        self.discovery_status["enabled"] = False
        tasks = (
            list(self.tasks.values())
            + ([self.loop_task] if self.loop_task else [])
            + ([self.scan_task] if self.scan_task else [])
            + ([self.resolution_task] if self.resolution_task else [])
        )
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        if self.hub is None:
            await self.http.aclose()
        else:
            self.hub.services.discard(self)
            await self.hub.sync_subscriptions()


class _PublicStore:
    """Transient public diagnostics; never a trading ledger or wallet."""

    def __init__(self):
        self.values = {}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def put(self, key, value):
        self.values[key] = value


class _PublicRuntime:
    def __init__(self, hub):
        self.hub = hub
        self.store = _PublicStore()
        self.tokens = {}
        self.books = defaultdict(Book)
        self.monitored = set()

    def add_tokens(self, tokens, official_tags=None):
        changed_ticks = set()
        for token in tokens:
            tid = token.token_id
            previous = self.tokens.get(tid)
            scan_versions = self.hub._scan_tick_versions
            if (
                previous is not None
                and scan_versions is not None
                and self.hub._tick_versions.get(tid, 0) != scan_versions.get(tid, 0)
            ):
                # A page collected before a WS control event cannot roll that
                # event back when the complete discovery is finally committed.
                token = replace(token, tick=previous.tick)
            if previous is not None and previous.tick != token.tick:
                changed_ticks.add(tid)
            self.tokens[tid] = token
        if changed_ticks:
            # Metadata can reach Gamma before the socket's tick notification.
            # Existing prices are not a baseline for the newly discovered tick.
            self.disconnect(changed_ticks)

    def drain(self):
        pass

    def disconnect(self, token_ids):
        ids = set(token_ids)
        for tid in ids:
            if tid in self.books:
                self.books[tid].disconnect()
        for service in tuple(self.hub.services):
            if not service.closed:
                try:
                    service.runtime.disconnect(ids & service.runtime.monitored)
                except Exception as exc:
                    self.hub.consumer_error(service, exc)

    def release_unmonitored(self):
        for tid in self.books.keys() - self.monitored:
            del self.books[tid]
        for tid, token in list(self.tokens.items()):
            if not token.open and tid not in self.monitored:
                del self.tokens[tid]

    def pause(self):
        for service in tuple(self.hub.services):
            self.hub.consumer_error(service, RuntimeError("公开行情源发生不可恢复错误"))


class PublicMarketHub(MarketService):
    """One public discovery/WS source, with completely separate consumers."""

    def __init__(self, transport=None):
        self.services = set()
        self.active_groups = {}
        self.sockets = {}
        self.discovery_lock = asyncio.Lock()
        self.discovered_at = None
        self.discovery_version = 0
        self.consumer_versions = {}
        self._tick_versions = {}
        self._scan_tick_versions = None
        self._next_group = 0
        self._subscription_monitored = {}
        super().__init__(_PublicRuntime(self), transport=transport)

    def consumer_error(self, service, exc):
        if service.closed:
            return
        # A consumer's native/SQLite failure must never stop another consumer's
        # public feed. Each consumer still records its own fatal safety pause.
        try:
            service.runtime.pause()
        except Exception:
            pass
        try:
            service.remember_error("serviceError", exc)
        except Exception:
            pass

    @property
    def storage_available(self):
        # Discovery is shared, but each consumer commits only to its own ledger.
        return any(not s.closed and s.storage_available for s in self.services)

    async def discover(self, requester):
        async with self.discovery_lock:
            if self.closed or requester.closed or not requester.storage_available:
                return
            now = asyncio.get_running_loop().time()
            if self.discovered_at is None or now - self.discovered_at >= 300:
                self._scan_tick_versions = dict(self._tick_versions)
                try:
                    await super().scan()
                finally:
                    self._scan_tick_versions = None
                if self.scan_status.get("lastError") is None and not self.scan_status.get(
                    "storageDeferred"
                ):
                    self.discovery_version += 1
                    self.discovered_at = asyncio.get_running_loop().time()
                for service in tuple(self.services):
                    self.apply_discovery(service)
            else:
                self.apply_discovery(requester)
            await self.sync_subscriptions()

    def apply_discovery(self, service):
        if service.closed:
            return
        service.discovery_status = dict(self.discovery_status)
        if not service.storage_available:
            service.scan_status["storageDeferred"] = True
            return
        service.scan_status.pop("storageDeferred", None)
        try:
            # Discovery belongs to the shared source. Fatal acknowledgements and
            # stream health belong to each mode and must not be replayed by a scan.
            for key in (
                "scanning",
                "lastError",
                "eventCountScanned",
                "lastScanAt",
                "categoryError",
            ):
                if key in self.scan_status:
                    service.scan_status[key] = self.scan_status[key]
            service.categories = [dict(category) for category in self.categories]
            if self.scan_status.get("categoryError") is None:
                service.scan_status.pop("categoryError", None)
            version = self.consumer_versions.get(service)
            if (
                self.discovery_version
                and version != self.discovery_version
                and self.scan_status.get("lastError") is None
            ):
                tokens = {
                    tid: token
                    for tid, token in self.runtime.tokens.items()
                    if token.open and tid not in service.unsupported_ticks
                }
                closed = [
                    replace(t, open=False)
                    for tid, t in service.runtime.tokens.items()
                    if tid not in tokens
                ]
                service.runtime.add_tokens(
                    closed + list(tokens.values()), [t["id"] for t in self.categories]
                )
                service.runtime.store.put("categories", service.categories)
                service.runtime.drain()
                self.consumer_versions[service] = self.discovery_version
            service.runtime.store.put("scan", service.scan_status)
        except Exception as exc:
            self.consumer_error(service, exc)

    def subscription_ids(self, group):
        return tuple(sorted(self.active_groups.get(group, ())))

    def socket_opened(self, group, ws):
        self.sockets[group] = ws

    def socket_closed(self, group):
        self.sockets.pop(group, None)

    def socket_consumers(self, group):
        ids = self.active_groups.get(group, set())
        return [
            service
            for service in tuple(self.services)
            if not service.closed and ids & service.desired_subscription_ids()
        ]

    def baseline_consumers(self, group):
        return self.socket_consumers(group)

    def socket_error(self, group, key, exc):
        # Keep source history, but a socket fault only affects its subscribers.
        MarketService.remember_error(self, key, exc)
        for service in self.socket_consumers(group):
            try:
                service.remember_error(key, exc)
            except Exception as consumer_exc:
                self.consumer_error(service, consumer_exc)

    def pause_socket(self, group, exc):
        for service in self.socket_consumers(group):
            try:
                service.runtime.pause()
            except Exception as consumer_exc:
                self.consumer_error(service, consumer_exc)

    def stream_state(self, group, state, error=None):
        super().stream_state(group, state, error)
        self.update_consumer_stream_errors()

    def update_consumer_stream_errors(self):
        for service in tuple(self.services):
            if service.closed:
                continue
            errors = [
                entry["error"]
                for group, entry in self.streams.items()
                if entry.get("error")
                and self.active_groups.get(group, set()) & service.desired_subscription_ids()
            ]
            current = errors[-1] if errors else None
            if service.scan_status.get("streamError") != current:
                service.scan_status["streamError"] = current
                try:
                    service.runtime.store.put("scan", service.scan_status)
                except Exception as exc:
                    self.consumer_error(service, exc)

    def remember_error(self, key, exc):
        super().remember_error(key, exc)
        for service in tuple(self.services):
            if not service.closed:
                try:
                    service.remember_error(key, exc)
                except Exception as consumer_exc:
                    self.consumer_error(service, consumer_exc)

    async def _sync_subscriptions(self):
        if self.closed:
            return
        services = [s for s in self.services if not s.closed]
        consumer_ids = {service: service.desired_subscription_ids() for service in services}
        monitored = set().union(*consumer_ids.values())
        # Keep connections still used by an unchanged consumer. A TEST reset or
        # filter reduction must not invalidate another mode's continuous books.
        protected_ids = set().union(
            *(
                ids
                for service, ids in consumer_ids.items()
                if self._subscription_monitored.get(service) == ids
            )
        )
        self.runtime.monitored = monitored
        for service in services:
            for tid in tuple(consumer_ids[service]):
                consumer = service.runtime.tokens[tid]
                source = self.runtime.tokens.setdefault(tid, consumer)
                if source.tick != consumer.tick:
                    try:
                        # A mode which was not subscribed during a tick change
                        # must receive that control event before its new baseline.
                        service.message(
                            dict(
                                event_type="tick_size_change",
                                asset_id=tid,
                                market=consumer.condition_id,
                                new_tick_size=f"{source.tick // 1_000_000}.{source.tick % 1_000_000:06d}",
                            ),
                            {tid},
                        )
                    except ConnectionError:
                        pass  # The existing shared source already owns recovery.
                    except Exception as exc:
                        self.consumer_error(service, exc)
            # A consumer can arrive with a previously connected book. Until the
            # shared source has a complete baseline, that old book cannot accept
            # deltas from the new connection as if it were still continuous.
            missing = {
                tid
                for tid in consumer_ids[service]
                if (tid not in self.runtime.books or not self.runtime.books[tid].ready)
                and service.runtime.books[tid].ready
            }
            if missing:
                try:
                    service.runtime.disconnect(missing)
                except Exception as exc:
                    self.consumer_error(service, exc)
        # Existing sockets are subscription slots. Removing one consumer's IDs
        # changes subscriptions in place, so another consumer never reconnects
        # merely because TEST was cleared or closed.
        removed_any = False
        for group in list(self.tasks):
            old = self.active_groups[group]
            current = old & monitored
            removed = old - current
            removed_any = removed_any or bool(removed)
            self.active_groups[group] = current
            ws = self.sockets.get(group)
            if removed and ws is not None:
                try:
                    await ws.send(
                        json.dumps({"operation": "unsubscribe", "assets_ids": sorted(removed)})
                    )
                except Exception:
                    # The socket's own heartbeat/retry loop owns reconnection.
                    pass
            if not current:
                task = self.tasks.pop(group)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.active_groups.pop(group, None)
                self.streams.pop(group, None)
        # A large filter reduction can leave one token on every old socket.
        # Retain the fullest sockets and move only excess fragments; unaffected
        # subscriptions keep their connection and complete book baseline.
        minimum_groups = (len(monitored) + 199) // 200
        if removed_any and len(self.active_groups) > minimum_groups:
            ordered = sorted(self.active_groups, key=lambda g: (-len(self.active_groups[g]), g))
            protected_groups = {g for g in ordered if self.active_groups[g] & protected_ids}
            retained = [g for g in ordered if g in protected_groups]
            retained.extend(
                [g for g in ordered if g not in protected_groups][
                    : max(0, minimum_groups - len(retained))
                ]
            )
            retained_groups = set(retained)
            for group in ordered:
                if group in retained_groups:
                    continue
                moving = sorted(self.active_groups[group])
                # Stop the old owner before any await can deliver another old
                # snapshot. Closing the socket also removes its subscriptions.
                self.active_groups.pop(group, None)
                self.runtime.disconnect(moving)
                task = self.tasks.pop(group)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.streams.pop(group, None)
                for destination in retained:
                    active = self.active_groups[destination]
                    added = moving[: 200 - len(active)]
                    if not added:
                        continue
                    del moving[: len(added)]
                    active.update(added)
                    destination_ws = self.sockets.get(destination)
                    if destination_ws is not None:
                        try:
                            await destination_ws.send(
                                json.dumps({"operation": "subscribe", "assets_ids": added})
                            )
                        except Exception:
                            pass
                    self.stream_state(
                        destination,
                        "AWAITING_BOOKS",
                        self.streams.get(destination, {}).get("error"),
                    )
        assigned = set().union(*self.active_groups.values()) if self.active_groups else set()
        additions = sorted(monitored - assigned)
        for group, active in self.active_groups.items():
            added = additions[: 200 - len(active)]
            if not added:
                continue
            del additions[: len(added)]
            active.update(added)
            ws = self.sockets.get(group)
            if ws is not None:
                try:
                    await ws.send(json.dumps({"operation": "subscribe", "assets_ids": added}))
                except Exception:
                    pass
            self.stream_state(group, "AWAITING_BOOKS", self.streams.get(group, {}).get("error"))
        for i in range(0, len(additions), 200):
            self._next_group += 1
            group = (f"public-{self._next_group}",)
            self.active_groups[group] = set(additions[i : i + 200])
            self.tasks[group] = asyncio.create_task(self.socket(group))
        for service in services:
            pending = service._availability_subscribe_pending & consumer_ids[service]
            for group, active in self.active_groups.items():
                requested = pending & active
                ws = self.sockets.get(group)
                if requested and ws is not None:
                    try:
                        await ws.send(
                            json.dumps({"operation": "subscribe", "assets_ids": sorted(requested)})
                        )
                        service._availability_subscribe_pending.difference_update(requested)
                    except Exception:
                        pass
        self.runtime.release_unmonitored()
        for group in tuple(self.streams):
            self.refresh_stream_readiness(group)
        self.update_consumer_stream_errors()
        for service in services:
            try:
                for tid in consumer_ids[service]:
                    source = self.runtime.books.get(tid)
                    if (
                        source is not None
                        and source.ready
                        and not service.runtime.books[tid].ready
                        and tid not in service._availability_fresh_pending
                    ):
                        # Copy values, including the original venue timestamp;
                        # no Book or TEST consumption object is shared.
                        service.runtime.book(
                            tid,
                            list(source.bid.external.items()),
                            list(source.ask.external.items()),
                            source.timestamp,
                        )
                service.runtime.release_unmonitored()
            except Exception as exc:
                self.consumer_error(service, exc)
        for service in self.consumer_versions.keys() - self.services:
            del self.consumer_versions[service]
        self._subscription_monitored = consumer_ids

    def diagnostics_for(self, service):
        groups = []
        for group in sorted(self.streams):
            ids = self.active_groups.get(group, set()) & service.desired_subscription_ids()
            if not ids:
                continue
            missing = [tid for tid in sorted(ids) if not service.runtime.books[tid].ready]
            task = self.tasks.get(group)
            entry = dict(self.streams[group])
            if not missing and entry.get("state") == "AWAITING_BOOKS":
                entry["state"] = "READY"
            elif missing and entry.get("state") == "READY":
                entry["state"] = "AWAITING_BOOKS"
            groups.append(
                dict(
                    entry,
                    tokenCount=len(ids),
                    readyBookCount=len(ids) - len(missing),
                    missingTokenIds=missing,
                    taskRunning=task is not None and not task.done(),
                )
            )
        return {"groups": groups, "groupCount": len(groups)}

    def message(self, msg, allowed):
        if self.closed:
            return
        allowed = set(allowed)
        kind = msg.get("event_type")
        ts = int(msg.get("timestamp", 0)) * 1_000_000
        if kind == "book":
            tid = msg.get("asset_id")
            if tid not in allowed or msg.get("market") != self.runtime.tokens[tid].condition_id:
                return
            if ts < self._availability_timestamp_floor.get(tid, 0):
                return
            bids = [(micros(x["price"]), micros(x["size"])) for x in msg["bids"]]
            asks = [(micros(x["price"]), micros(x["size"])) for x in msg["asks"]]
            if not self.runtime.books[tid].snapshot(bids, asks, ts):
                return
            self._availability_timestamp_floor.pop(tid, None)
        elif kind == "price_change":
            for change in msg.get("price_changes", []):
                tid = change.get("asset_id")
                if tid not in allowed or msg.get("market") != self.runtime.tokens[tid].condition_id:
                    continue
                side = (
                    "BID"
                    if change.get("side") == "BUY"
                    else "ASK"
                    if change.get("side") == "SELL"
                    else None
                )
                if side:
                    self.runtime.books[tid].delta(
                        side, [(micros(change["price"]), micros(change["size"]))], ts
                    )
        elif kind == "tick_size_change":
            tid = msg.get("asset_id")
            if tid not in allowed or msg.get("market") != self.runtime.tokens[tid].condition_id:
                return
            tick = micros(msg["new_tick_size"])
            if tick <= 0:
                raise ValueError("无效 tick 变更")
            self._tick_versions[tid] = self._tick_versions.get(tid, 0) + 1
            self.runtime.tokens[tid] = replace(self.runtime.tokens[tid], tick=tick)
            self.runtime.disconnect([tid])
        for service in tuple(self.services):
            if service.closed:
                continue
            try:
                service.message(msg, allowed & service.desired_subscription_ids())
            except ConnectionError:
                if kind != "tick_size_change":
                    self.consumer_error(service, ConnectionError("消费者行情处理失败"))
            except Exception as exc:
                self.consumer_error(service, exc)
        if kind == "tick_size_change":
            raise ConnectionError("tick 变化，需要新完整盘口")
