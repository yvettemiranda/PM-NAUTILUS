"""Public Gamma discovery and market WS, feeding canonical Nautilus book events.

No credential import, private endpoint or signed client exists in this module.
"""

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .config import micros
from .rules import Token, Fees

GAMMA = "https://gamma-api.polymarket.com"
WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
TAGS = f"{GAMMA}/tags/102982/related-tags/tags"


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
            if tick <= 0 or minimum <= 0:
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
    def __init__(self, runtime, transport=None):
        self.runtime = runtime
        self.http = httpx.AsyncClient(timeout=30, transport=transport)
        self.tasks = {}
        self.scan_status = runtime.store.get("scan", {})
        self.categories = runtime.store.get("categories", [])
        self.closed = False
        self.loop_task = None
        self.on_resolution = None
        self.subscription_lock = asyncio.Lock()

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
        if self.scan_status.pop("serviceError", None) is not None:
            self.scan_status["serviceErrorClearedAt"] = datetime.now(timezone.utc).isoformat()
            self.runtime.store.put("scan", self.scan_status)

    async def get(self, url, **params):
        response = await self.http.get(url, params=params)
        response.raise_for_status()
        return response.json()

    async def categories_refresh(self):
        tags = await self.get(TAGS, status="active", omit_empty="true")
        if not isinstance(tags, list) or not all(isinstance(tag, dict) for tag in tags):
            raise ValueError("首页栏目响应格式变化")
        tags = [t for t in tags if t.get("slug") not in {"all", "perps", "art"}]
        for slug in ("esports", "art"):
            tag = await self.get(f"{GAMMA}/tags/slug/{slug}", include_template="false")
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
        self.runtime.store.put("categories", self.categories)

    async def scan(self):
        r = self.runtime
        self.scan_status.update(scanning=True, lastError=None)
        tokens = {}
        seen = set()
        after_cursor = None
        cursors = set()
        try:
            try:
                await self.categories_refresh()
                self.scan_status.pop("categoryError", None)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                self.scan_status["categoryError"] = type(exc).__name__
            while not self.closed:
                params = {
                    "active": "true",
                    "closed": "false",
                    "limit": 100,
                    "order": "id",
                    "ascending": "true",
                }
                if after_cursor:
                    params["after_cursor"] = after_cursor
                payload = await self.get(f"{GAMMA}/events/keyset", **params)
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
                        tokens[t.token_id] = t
                self.scan_status["eventCountScanned"] = len(seen)
                cursor = payload.get("next_cursor")
                if cursor is None:
                    break
                if not isinstance(cursor, str) or not cursor or cursor in cursors:
                    raise ValueError("公开 Event keyset 游标没有前进")
                cursors.add(cursor)
                after_cursor = cursor
            if self.closed:
                return
            # Commit discovery only after all pages succeed. Missing old tokens remain
            # available for held-position identity, but cannot open new cycles.
            merged = [replace(t, open=False) for tid, t in r.tokens.items() if tid not in tokens]
            r.add_tokens(merged + list(tokens.values()), [t["id"] for t in self.categories])
            self.scan_status["lastScanAt"] = datetime.now(timezone.utc).isoformat()
            r.drain()
            await self.sync_subscriptions()
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            self.scan_status["lastError"] = f"{type(exc).__name__}: {str(exc)[:180]}"
        finally:
            self.scan_status["scanning"] = False
            r.store.put("scan", self.scan_status)

    async def sync_subscriptions(self):
        async with self.subscription_lock:
            await self._sync_subscriptions()

    async def _sync_subscriptions(self):
        if self.closed:
            return
        # Batch size limits one socket, never the monitored universe. Stable sorted
        # chunks bound connections; changed chunks are invalid until fresh snapshots.
        ids = sorted(self.runtime.monitored)
        groups = {tuple(ids[i : i + 200]) for i in range(0, len(ids), 200)}
        for group in self.tasks.keys() - groups:
            task = self.tasks.pop(group)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for group in groups - self.tasks.keys():
            if self.closed:
                return
            self.tasks[group] = asyncio.create_task(self.socket(group))

    def message(self, msg, allowed):
        if self.closed:
            return
        r = self.runtime
        kind = msg.get("event_type")
        ts = int(msg.get("timestamp", 0)) * 1_000_000
        if kind == "price_change":
            for change in msg.get("price_changes", []):
                tid = change.get("asset_id")
                if tid not in allowed or msg.get("market") != r.tokens[tid].condition_id:
                    continue
                b = r.books[tid]
                side = (
                    "BID"
                    if change.get("side") == "BUY"
                    else "ASK"
                    if change.get("side") == "SELL"
                    else None
                )
                if side and b.delta(side, [(micros(change["price"]), micros(change["size"]))], ts):
                    r.store.save_book(tid, b)
                    r.native.feed(r.tokens[tid], b)
                    r.dirty.add(r.tokens[tid].event_id)
            r.drain()
        elif kind == "book":
            tid = msg.get("asset_id")
            if tid in allowed and msg.get("market") == r.tokens[tid].condition_id:

                def levels(side):
                    return [(micros(x["price"]), micros(x["size"])) for x in msg[side]]

                r.book(tid, levels("bids"), levels("asks"), ts)
        elif kind == "tick_size_change":
            tid = msg.get("asset_id")
            if tid in allowed and msg.get("market") == r.tokens[tid].condition_id:
                t = replace(r.tokens[tid], tick=micros(msg["new_tick_size"]))
                if t.tick <= 0:
                    raise ValueError("无效 tick 变更")
                r.add_tokens([t])
                r.disconnect([tid])
                raise ConnectionError("tick 变化，需要新完整盘口")

    async def socket(self, group):
        r = self.runtime
        backoff = 1
        try:
            while not self.closed:
                r.disconnect(group)
                try:
                    async with connect(
                        WS, ping_interval=10, ping_timeout=10, max_size=16 * 1024 * 1024
                    ) as ws:
                        await ws.send(
                            json.dumps(
                                {
                                    "assets_ids": list(group),
                                    "type": "market",
                                    "custom_feature_enabled": True,
                                }
                            )
                        )
                        backoff = 1
                        while not self.closed:
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=15)
                            except TimeoutError:
                                await ws.send("PING")
                                raw = await asyncio.wait_for(ws.recv(), timeout=15)
                            if raw in ("PONG", "PING"):
                                if raw == "PING":
                                    await ws.send("PONG")
                                continue
                            messages = json.loads(raw)
                            for msg in messages if isinstance(messages, list) else [messages]:
                                self.message(msg, group)
                except (
                    ConnectionClosed,
                    OSError,
                    ConnectionError,
                    ValueError,
                    TimeoutError,
                ) as exc:
                    # Normal public-feed disconnects are recoverable. The socket is
                    # invalidated in finally and reconnected with bounded backoff.
                    self.remember_error("streamError", exc)
                except Exception as exc:
                    # A broken feed cannot retain an executable last-known book.
                    self.remember_error("streamError", exc)
                    self.remember_error("serviceError", exc)
                    r.pause()
                finally:
                    r.disconnect(group)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
        finally:
            r.disconnect(group)

    async def run(self):
        last_scan = 0
        while not self.closed:
            try:
                if asyncio.get_running_loop().time() - last_scan >= 300:
                    await self.scan()
                    last_scan = asyncio.get_running_loop().time()
                self.runtime.refresh_eligibility()
                self.runtime.drain()
                await self.sync_subscriptions()
                if self.on_resolution:
                    await self.on_resolution()
            except Exception as exc:
                # Keep the service observable and retryable, without silently
                # leaving a dead task behind a RUNNING UI.
                self.runtime.pause()
                self.remember_error("serviceError", exc)
            await asyncio.sleep(5)

    async def close(self):
        self.closed = True
        tasks = list(self.tasks.values()) + ([self.loop_task] if self.loop_task else [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        await self.http.aclose()
