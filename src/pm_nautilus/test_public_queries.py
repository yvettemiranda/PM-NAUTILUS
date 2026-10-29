"""Merge simultaneous TEST public observations, never cache completed replies."""

import asyncio
import re
from copy import deepcopy
from dataclasses import dataclass
from urllib.parse import urlsplit

from .market import CLOB_BOOK, GAMMA


def _public_key(url, params):
    if type(url) is not str:
        return None
    try:
        address = urlsplit(url)
    except ValueError:
        return None
    if address.query or address.fragment or address.username or address.password:
        return None
    gamma = urlsplit(GAMMA)
    clob = urlsplit(CLOB_BOOK)
    permitted = (
        address.scheme == gamma.scheme
        and address.netloc == gamma.netloc
        and re.fullmatch(r"/markets/[A-Za-z0-9_-]+", address.path)
    ) or (
        address.scheme == clob.scheme and address.netloc == clob.netloc and address.path == "/book"
    )
    if not permitted or any(
        type(key) is not str or type(value) not in (str, int, bool, type(None))
        for key, value in params.items()
    ):
        return None
    # bool/int and absent/empty values need distinct keys even though some
    # Python values compare equal; their actual HTTP representations differ.
    return url, tuple(sorted((key, type(value), value) for key, value in params.items()))


@dataclass
class _Pending:
    task: asyncio.Task
    waiters: int = 0


async def _drain(tasks):
    async def finish():
        await asyncio.gather(*tasks, return_exceptions=True)

    cleanup = asyncio.create_task(finish())
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    cleanup.result()
    if cancelled:
        raise asyncio.CancelledError


class TestPublicQueries:
    """Owner-loop, bounded in-flight sharing of whitelisted public GETs.

    Every account still observes and processes its own independent JSON reply.
    A new query after completion always performs a new network read. LIVE calls
    never enter this broker, so its timing, retry policy and wallet are unchanged.
    """

    __test__ = False

    def __init__(self, max_pending=64):
        self._pending = {}
        self._max_pending = max_pending
        self._closed = False
        self.requests = 0
        self.joins = 0

    async def get(self, fetch, url, **params):
        if self._closed:
            raise RuntimeError("TEST公开查询已关闭")
        key = _public_key(url, params)
        if key is None:
            return await fetch(url, **params)
        entry = self._pending.get(key)
        if entry is None or entry.task.done():
            if entry is None and len(self._pending) >= self._max_pending:
                # Capacity pressure preserves the original request, rather than
                # delaying an observation or serving a stale result.
                return await fetch(url, **params)
            entry = _Pending(asyncio.create_task(fetch(url, **params)))
            self._pending[key] = entry
            self.requests += 1
        else:
            self.joins += 1
        entry.waiters += 1
        try:
            return deepcopy(await asyncio.shield(entry.task))
        finally:
            entry.waiters -= 1
            if entry.waiters == 0:
                if self._pending.get(key) is entry:
                    self._pending.pop(key)
                if not entry.task.done():
                    entry.task.cancel()
                    await _drain([entry.task])

    async def close(self):
        self._closed = True
        tasks = [entry.task for entry in self._pending.values()]
        self._pending.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        await _drain(tasks)
