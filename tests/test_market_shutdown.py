"""Large public-feed shutdown with controlled close handshakes, without network."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pm_nautilus.market import MarketService, PublicMarketHub
from pm_nautilus.test_market import TestMarketHub

from test_market_efficiency import IdleSocket, public_book, settle_tasks
from test_runtime import setup


async def gated_sockets(hub, release, all_closing, finished, failing_group=None):
    closing = set()
    sockets, tasks = [], []

    async def socket(group):
        try:
            await asyncio.Event().wait()
        finally:
            closing.add(group)
            if len(closing) == 64:
                all_closing.set()
            try:
                await release.wait()
                if group == failing_group:
                    raise ConnectionError("controlled close failure")
            finally:
                hub.socket_closed(group)
                finished.add(group)

    for number in range(64):
        group = (f"public-{number}",)
        ws = SimpleNamespace(send=AsyncMock())
        hub.active_groups[group] = {str(number)}
        hub.sockets[group] = ws
        hub.streams[group] = {"state": "READY"}
        task = asyncio.create_task(socket(group))
        hub.tasks[group] = task
        sockets.append(ws)
        tasks.append(task)
    await asyncio.sleep(0)
    return sockets, tasks


@pytest.mark.parametrize("hub_type", [PublicMarketHub, TestMarketHub])
@pytest.mark.parametrize("failing_group", [None, ("public-0",)])
def test_empty_subscription_groups_close_concurrently_and_drain_finalizers(hub_type, failing_group):
    async def run():
        hub = hub_type()
        release, all_closing = asyncio.Event(), asyncio.Event()
        finished = set()
        sockets, tasks = await gated_sockets(hub, release, all_closing, finished, failing_group)
        draining = asyncio.create_task(hub.sync_subscriptions())
        try:
            await asyncio.wait_for(all_closing.wait(), 1)
            assert not draining.done() and not finished
            assert not hub.tasks
            assert all(not ids for ids in hub.active_groups.values())
            for ws in sockets:
                ws.send.assert_not_awaited()
            release.set()
            await asyncio.wait_for(draining, 1)
            assert len(finished) == 64 and all(task.done() for task in tasks)
            assert not hub.tasks and not hub.active_groups and not hub.sockets and not hub.streams
        finally:
            release.set()
            await asyncio.gather(draining, *tasks, return_exceptions=True)
            await hub.close()

    asyncio.run(run())


def test_retained_socket_delivers_quotes_while_empty_socket_close_waits(tmp_path, monkeypatch):
    async def run():
        closing, release, received = asyncio.Event(), asyncio.Event(), asyncio.Event()
        sockets = []

        class GatedSocket(IdleSocket):
            async def __aexit__(self, *args):
                if self is sockets[1]:
                    closing.set()
                    await release.wait()
                return False

        def connect(*args, **kwargs):
            ws = GatedSocket()
            sockets.append(ws)
            return ws

        monkeypatch.setattr("pm_nautilus.market.connect", connect)
        runtime, _, token = setup(tmp_path)
        hub = PublicMarketHub()
        service = MarketService(runtime, hub=hub)
        syncing = None
        try:
            await service.sync_subscriptions()
            await settle_tasks()
            retained = next(iter(hub.tasks))
            retained_task = hub.tasks[retained]
            hub.active_groups[retained].add("obsolete")
            retiring = ("retiring",)
            hub.active_groups[retiring] = {"removed"}
            retiring_task = asyncio.create_task(hub.socket(retiring))
            hub.tasks[retiring] = retiring_task
            await settle_tasks()
            original_message = hub.message

            def message(payload, ids):
                original_message(payload, ids)
                if payload.get("asset_id") == token.token_id:
                    received.set()

            monkeypatch.setattr(hub, "message", message)
            syncing = asyncio.create_task(service.sync_subscriptions())
            await asyncio.wait_for(closing.wait(), 5)
            assert not syncing.done() and not retiring_task.done()
            await sockets[0].queue.put(json.dumps(public_book(token)))
            await asyncio.wait_for(received.wait(), 5)
            assert runtime.books[token.token_id].ready
            assert hub.tasks[retained] is retained_task and not retained_task.done()
            release.set()
            await asyncio.wait_for(syncing, 5)
            assert retiring_task.done() and retiring not in hub.sockets
            assert hub.tasks[retained] is retained_task and runtime.books[token.token_id].ready
            sent = [json.loads(value) for value in sockets[0].sent if value.startswith("{")]
            assert {"operation": "unsubscribe", "assets_ids": ["obsolete"]} in sent
            assert all("unsubscribe" not in value for value in sockets[1].sent)
        finally:
            release.set()
            if syncing is not None:
                await asyncio.gather(syncing, return_exceptions=True)
            await service.close()
            await hub.close()
            runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("hub_type", [PublicMarketHub, TestMarketHub])
def test_empty_subscription_group_drain_propagates_cancel_without_orphan_tasks(hub_type):
    async def run():
        hub = hub_type()
        release, all_closing = asyncio.Event(), asyncio.Event()
        finished = set()
        _, tasks = await gated_sockets(hub, release, all_closing, finished)
        draining = asyncio.create_task(hub.sync_subscriptions())
        try:
            await asyncio.wait_for(all_closing.wait(), 1)
            draining.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(draining, 1)
            assert len(finished) == 64 and all(task.done() for task in tasks)
            assert not hub.tasks and not hub.active_groups and not hub.sockets and not hub.streams
        finally:
            release.set()
            await asyncio.gather(draining, *tasks, return_exceptions=True)
            await hub.close()

    asyncio.run(run())
