"""TEST-only account ownership; each account retains an ordinary isolated runtime."""

import asyncio
import re
import sqlite3
import stat
from contextlib import asynccontextmanager, closing, contextmanager
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import msgspec
from nautilus_trader.serialization.serializer import MsgSpecSerializer

from .config import OPTIONAL_PROFILE_FIELDS, Preferences
from .store import Store
from .test_metadata import decode_test_token


class _HistoricalTokens:
    def __init__(self, lookup):
        self.lookup = lookup

    def __getitem__(self, token_id):
        return self.lookup(token_id)


class ReadOnlyTestStore:
    """The persisted read side used by cold TEST account views and history."""

    mode = "TEST"
    _get_meta = Store._get_meta
    read_business = staticmethod(Store.read_business)
    get = Store.get
    generation = Store.generation
    intent = Store.intent
    has_history = Store.has_history

    def __init__(self, path):
        self.path = path
        # Do not use immutable=1: committed WAL data can contain financial work.
        self.db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self._closed = False

    @property
    def history_tokens(self):
        return _HistoricalTokens(self.token)

    @cached_property
    def serializer(self):
        return MsgSpecSerializer(encoding=msgspec.json)

    def close(self):
        self._closed = True
        self.db.close()

    def events(self, after=0):
        for seq, payload in self.db.execute(
            "SELECT seq,payload FROM native_events WHERE seq>? ORDER BY seq", (after,)
        ):
            yield seq, self.serializer.deserialize(payload)

    def token(self, token_id):
        row = self.db.execute("SELECT payload FROM tokens WHERE token_id=?", (token_id,)).fetchone()
        if row is None:
            raise KeyError(token_id)
        return decode_test_token(token_id, row[0], Store.decode_token_payload)

    def eligible(self):
        """Prove the snapshot needs no live TEST runtime maintenance."""
        generation = self.generation
        capital = self.get("initial_capital")
        if (
            self.get("mode") != "TEST"
            or self.get("status") != "PAUSED"
            or not isinstance(generation, str)
            or not generation
            or type(capital) is not int
            or capital <= 0
        ):
            return False
        Preferences.model_validate(self.get("preferences"))
        tables = {
            row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not {"meta", "tokens", "books", "intents", "native_events"} <= tables:
            return False
        business = self._get_meta("business")
        version = Store._business_version(business)
        expected = Store.empty_business()
        if version is not None:
            expected["_storage_version"] = 2
        if (
            business.keys() != expected.keys()
            or type(business["cursor"]) is not int
            or business["cursor"] < 0
            or type(business["realized"]) is not int
            or any(
                not isinstance(business[key], list)
                or any(not isinstance(value, str) or not value for value in business[key])
                for key in ("banned", "settled")
            )
            or business["recovery_error"] is not None
        ):
            return False

        if version is None:
            if (
                "business_parts" in tables
                and self.db.execute("SELECT 1 FROM business_parts LIMIT 1").fetchone()
            ):
                return False
            if (
                not all(isinstance(business[key], dict) for key in ("cycles", "targets", "claims"))
                or business["cycles"]
                or business["targets"]
                or any(
                    not isinstance(claim, dict) or claim.get("state") != "CREDITED"
                    for claim in business["claims"].values()
                )
            ):
                return False
        elif (
            "business_parts" not in tables
            or self.db.execute(
                "SELECT 1 FROM business_parts WHERE collection IS NOT 'claims' "
                "OR json_extract(payload,'$.state') IS NOT 'CREDITED' LIMIT 1"
            ).fetchone()
        ):
            return False

        head = self.db.execute("SELECT coalesce(max(seq), 0) FROM native_events").fetchone()[0]
        if head != business["cursor"]:
            return False
        # Unknown/malformed terminal flags are not proof of a finished order.
        return (
            self.db.execute(
                "SELECT 1 FROM intents WHERE json_type(payload,'$.terminal') IS NOT 'true' LIMIT 1"
            ).fetchone()
            is None
        )


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
    cold: bool = False


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

    def check_ledger_path(self, account_id):
        """Reject ledger aliases and orphan sidecars without opening SQLite."""
        path = self.path(account_id)
        relative = path.relative_to(self.root)
        parent = self.root
        for part in relative.parts[:-1]:
            parent /= part
            try:
                directory = parent.lstat()
            except FileNotFoundError:
                return None
            if not stat.S_ISDIR(directory.st_mode):
                raise ValueError("TEST账户账本目录无效")
        try:
            ledger = path.lstat()
        except FileNotFoundError:
            ledger = None
        if ledger is not None and (not stat.S_ISREG(ledger.st_mode) or ledger.st_nlink != 1):
            raise ValueError("TEST账户账本文件无效")
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                sidecar = path.with_name(path.name + suffix).lstat()
            except FileNotFoundError:
                continue
            if ledger is None or not stat.S_ISREG(sidecar.st_mode) or sidecar.st_nlink != 1:
                raise ValueError("TEST账户账本日志文件无效")
        return path.resolve() if ledger is not None else None

    @contextmanager
    def open_cold(self, account_id):
        """Open the persisted view. An uncreated account opens no files."""
        path = self.check_ledger_path(account_id)
        if path is None:
            yield None
            return
        with closing(ReadOnlyTestStore(path)) as store:
            if store.get("mode") != "TEST":
                raise ValueError("该账本不是TEST模式")
            yield store

    def cold_eligible(self, account_id):
        number = self.account_id(account_id)
        try:
            with self.open_cold(number) as store:
                if store is None:
                    return True
                # Classify a single committed state, including WAL changes. The
                # view facade itself leaves its connection in autocommit mode.
                store.db.execute("BEGIN")
                return store.eligible()
        except (OSError, ValueError, sqlite3.Error):
            # Broken, unknown or ambiguous data always uses the normal recovery path.
            return False

    def prepare(self, account_id, source):
        """Seed only a new account's settings, atomically and before native recovery."""
        number = self.account_id(account_id)
        if number == 1:
            return self.path(number)
        store = Store(self.path(number), "TEST")
        try:
            if not store.get("test_account_initialized", False):
                if source is None:
                    raise ValueError("1号账户不可用，不能推断新增账户的初始参数")
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
        saved = store.get("preferences")
        defaults = Preferences().model_dump(mode="json")
        if (
            not isinstance(saved, dict)
            or (dict.fromkeys(OPTIONAL_PROFILE_FIELDS, False) | saved) != defaults
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
        context.cold = False
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
