"""Read-only projections for inactive TEST ledgers without a trading runtime."""

import asyncio
from contextlib import closing
from types import SimpleNamespace

from .config import Preferences
from .performance import PerformanceSampler
from .store import Store
from .strategy import Runtime
from .test_accounts import ReadOnlyTestStore
from .views import dashboard, records_async, records_page_async


class _UninitializedLedger:
    def __init__(self, account_id):
        self.generation = f"uninitialized-test-{account_id}"
        self._defaults = {
            "preferences": Preferences().storage(),
            "initial_capital": 100_000_000,
            "business": Store.empty_business(),
        }

    def get(self, key, default=None):
        return self._defaults.get(key, default)

    def has_history(self):
        return False


class ColdTestView:
    mode = "TEST"
    status = "PAUSED"

    def __init__(self, store, account_id):
        self.initialized = store is not None
        self.store = store if self.initialized else _UninitializedLedger(account_id)
        self.tokens = store.history_tokens if self.initialized else {}
        self.preferences = Preferences(**self.store.get("preferences"))
        self.business = self.store.get("business")
        self.events = {}
        self.monitored = set()
        self.history = None
        self._cash = None if self.initialized else self.store.get("initial_capital")

    @staticmethod
    def _read_cash(path, generation):
        # A first view may have a long journal. Give the replay its own
        # read-only snapshot and keep it off the trading event loop.
        with closing(ReadOnlyTestStore(path)) as store:
            store.db.execute("BEGIN")
            if store.generation != generation:
                raise ValueError("只读账本已更换，请重新启动程序")
            return Runtime.test_cash(SimpleNamespace(store=store))

    async def prepare_cash(self):
        if self._cash is None:
            self._cash = await asyncio.to_thread(
                self._read_cash, self.store.path, self.store.generation
            )

    def cash(self):
        return self._cash

    def held_cash(self):
        return 0

    def dashboard(self, live_enabled):
        data = dashboard(self, limit=0, live_enabled=live_enabled)
        scan = data["marketScan"]
        # Stored scan metadata belongs to a previous runtime. Keep saved
        # categories for the settings view without reporting active scanning.
        data["marketScan"] = {
            key: scan[key]
            for key in (
                "events",
                "eventCount",
                "displayEventCount",
                "pendingEventCount",
                "tokenCount",
                "diagnostics",
            )
        } | {"scanning": False}
        return data

    async def records(self, limit, paged, cursor, since, changes_cursor, changes_paged):
        if self.initialized:
            if paged:
                return await records_page_async(
                    self, limit, cursor, since, changes_cursor, changes_paged
                )
            return await records_async(self, limit)
        empty = {"records": [], "totalCount": 0}
        if not paged:
            return empty
        generation = self.store.generation
        head = f"{generation}:0"
        if any(value not in (None, head) for value in (cursor, since)) or changes_cursor:
            raise ValueError("交易历史游标已失效，请重新读取")
        return empty | {
            "changes": [],
            "generation": generation,
            "head": head,
            "changesHead": head,
            "nextCursor": None,
            "nextChangesCursor": None,
        }

    async def performance(self):
        if (
            self.initialized
            and self.history is None
            and self.store.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='equity_samples'"
            ).fetchone()
        ):
            self.history = PerformanceSampler(self, read_only=True)
        if self.history is not None:
            return await self.history.view_async()
        return {
            "generation": self.store.generation,
            "sampleSeconds": 60,
            "error": None,
            "points": [],
            "series": {"H": [], "D": []},
        }
