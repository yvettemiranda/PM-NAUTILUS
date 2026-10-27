"""TEST-only account ownership; each account retains an ordinary isolated runtime."""

import asyncio
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .config import Preferences
from .store import Store


@dataclass
class TestAccountContext:
    __test__ = False

    account_id: int
    path: Path
    runtime: object = None
    service: object = None
    sampler: object = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    error: str | None = None
    resetting: bool = False


class TestAccounts:
    """No selected-account global and no LIVE state or wallet ownership."""

    __test__ = False
    count = 10

    def __init__(self, root):
        self.root = Path(root)
        self.contexts = {
            number: TestAccountContext(number, self.path(number))
            for number in range(1, self.count + 1)
        }
        self.runtimes = {}
        self.services = {}
        self.samplers = {}

    @staticmethod
    def account_id(value=None):
        if value is None:
            return 1
        if isinstance(value, bool) or not re.fullmatch(r"(?:[1-9]|10)", str(value)):
            raise ValueError("TEST账户须为1–10")
        return int(value)

    def path(self, account_id):
        number = self.account_id(account_id)
        if number == 1:
            return self.root / "TEST" / "state.sqlite"
        return self.root / "TEST" / "accounts" / str(number) / "state.sqlite"

    def prepare(self, account_id, source):
        """Seed only a new account's settings, atomically and before native recovery."""
        number = self.account_id(account_id)
        if number == 1:
            return self.path(number)
        store = Store(self.path(number), "TEST")
        try:
            if not store.get("test_account_initialized", False):
                # This marker also completes initialization after an interrupted
                # first boot. Never replace an unexpectedly populated ledger.
                if self.has_uninitialized_state(store):
                    raise ValueError(f"TEST账户{number}已有未识别历史，不能覆盖初始化")
                with store.transaction():
                    store.put("preferences", source.get("preferences"))
                    store.put("initial_capital", source.get("initial_capital"))
                    store.put("test_account_initialized", True)
        finally:
            store.close()
        return self.path(number)

    @staticmethod
    def has_uninitialized_state(store):
        # An interrupted first creation contains only defaults. Any financial
        # evidence or customized configuration is existing account data, even if
        # an external copy omitted intents or our initialization marker.
        if (
            store.get("preferences") != Preferences().model_dump(mode="json")
            or store.get("initial_capital") != 100_000_000
            or store.get("business") != Store.empty_business()
        ):
            return True
        for table in ("intents", "native_events", "books"):
            if store.db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                return True
        equity_exists = store.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='equity_samples'"
        ).fetchone()
        return bool(
            equity_exists and store.db.execute("SELECT 1 FROM equity_samples LIMIT 1").fetchone()
        )

    def attach_runtime(self, account_id, runtime):
        number = self.account_id(account_id)
        context = self.contexts[number]
        context.runtime = runtime
        context.service = None
        context.sampler = None
        context.error = None
        self.runtimes[number] = runtime
        self.services.pop(number, None)
        self.samplers.pop(number, None)
        return context

    @staticmethod
    def key(account_id):
        return "TEST" if account_id == 1 else f"TEST:{account_id}"

    @asynccontextmanager
    async def quiesce(self):
        """Shutdown waits for in-flight per-account readers and lifecycle changes."""
        acquired = []
        try:
            for context in self.contexts.values():
                await context.lock.acquire()
                acquired.append(context.lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()
