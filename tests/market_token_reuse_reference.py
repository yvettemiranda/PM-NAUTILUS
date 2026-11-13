"""Frozen 4e5fabf scan/public publication, independent metadata reuse oracle."""

from dataclasses import replace
from datetime import datetime, timezone

import httpx

from pm_nautilus.market import normalize


async def reference_scan(self):
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
    include_independent, include_augmented = self.market_scope()
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
                for t in normalize(
                    event,
                    include_independent=include_independent,
                    include_augmented=include_augmented,
                ):
                    if t.token_id not in self.unsupported_ticks:
                        tokens[t.token_id] = t
                checked_markets.update(
                    (
                        str(m["id"])
                        for m in event.get("markets") or []
                        if isinstance(m, dict) and m.get("id") is not None
                    )
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


def reference_public_add_tokens(self, tokens, official_tags=None):
    changed_ticks = set()
    for token in tokens:
        tid = token.token_id
        previous = self.tokens.get(tid)
        scan_versions = self.hub._scan_tick_versions
        if (
            previous is not None
            and scan_versions is not None
            and (self.hub._tick_versions.get(tid, 0) != scan_versions.get(tid, 0))
        ):
            token = replace(token, tick=previous.tick)
        if previous is not None and previous.tick != token.tick:
            changed_ticks.add(tid)
        self.tokens[tid] = token
    if changed_ticks:
        self.disconnect(changed_ticks)
