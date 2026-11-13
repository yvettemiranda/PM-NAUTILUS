"""Unchanged receive coroutine frozen from main 4e5fabf for decoder differential tests."""

import asyncio
import json
from datetime import datetime, timezone

from pm_nautilus.market import HEARTBEAT_SECONDS, RECOVERY_SECONDS


async def reference_receive(self, ws, group):
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
            self.message(msg, self.subscription_members(group))
            await asyncio.sleep(0)
        if self.streams[group]["state"] != "READY" and all(
            (self.runtime.books[tid].ready for tid in self.subscription_members(group))
        ):
            self.stream_state(group, "READY")
