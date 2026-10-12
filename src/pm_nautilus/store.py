"""Durable native event journal and replayable PM business projection, per mode."""

import asyncio
import json
import msgspec
import sqlite3
from contextlib import closing, contextmanager
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from threading import Event
from uuid import uuid4

from nautilus_trader.serialization.serializer import MsgSpecSerializer
from .books import Book, Side
from .config import Preferences
from .rules import Token, Fees


_INTEGRITY_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pm-integrity")
_BUSINESS_COLLECTIONS = ("cycles", "targets", "claims")


class BookCache(dict):
    """Load durable shadow depth only when a token is actually needed."""

    def __init__(self, store):
        super().__init__()
        self.store = store

    def __missing__(self, token_id):
        row = self.store.db.execute(
            "SELECT payload FROM books WHERE token_id=?", (token_id,)
        ).fetchone()
        book = Book()
        if row:
            data = json.loads(row[0])
            for side in ("bid", "ask"):
                data[side] = Side(
                    **{k: {int(p): q for p, q in v.items() if q} for k, v in data[side].items()}
                )
            data["ready"] = False
            book = Book(**data)
        self[token_id] = book
        return book


class Store:
    def __init__(self, path: str | Path, mode="TEST"):
        self.path = Path(path)
        self._closed = False
        self._integrity_lock = asyncio.Lock()
        self._integrity_stops = set()
        self._business_rows = None
        self._business_target_events = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS native_events(
          seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT UNIQUE NOT NULL,
          kind TEXT NOT NULL,payload BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS tokens(token_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS books(token_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS business_parts(
          collection TEXT NOT NULL,key TEXT NOT NULL,payload TEXT NOT NULL,
          PRIMARY KEY(collection,key));
        CREATE TABLE IF NOT EXISTS intents(
          order_id TEXT PRIMARY KEY,event_id TEXT NOT NULL,token_id TEXT NOT NULL,
          side TEXT NOT NULL,payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS intents_active ON intents(event_id,json_extract(payload,'$.terminal'));
        CREATE INDEX IF NOT EXISTS intents_open ON intents(order_id)
          WHERE json_extract(payload,'$.terminal')=0;
        CREATE INDEX IF NOT EXISTS native_fill_order ON native_events(kind,json_extract(CAST(payload AS TEXT),'$.client_order_id'));
        """)
        self.serializer = MsgSpecSerializer(encoding=msgspec.json)
        if self.get("mode") not in (None, mode):
            raise ValueError("禁止复用其他模式数据库")
        if self.get("mode") is None:
            with self.transaction():
                self.put("mode", mode)
                self.put("generation", str(uuid4()))
                self.put("preferences", Preferences().model_dump(mode="json"))
                self.put("initial_capital", 100_000_000 if mode == "TEST" else None)
                self.put("business", self.empty_business())
        self.mode = mode
        raw_business = self._get_meta("business")
        try:
            if raw_business is not None and self._business_version(raw_business) is None:
                # Upgrade atomically; every legacy collection remains in the ledger.
                with self.transaction():
                    self._save_business(self.read_business(self.db), force=True)
        except BaseException:
            self.close()
            raise
        self.put("status", "PAUSED")

    @staticmethod
    def empty_business():
        return {
            "cursor": 0,
            "cycles": {},
            "targets": {},
            "banned": [],
            "settled": [],
            "claims": {},
            "realized": 0,
            "recovery_error": None,
        }

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            try:
                # SQLite can already have rolled back (RAISE(ROLLBACK), FULL,
                # IOERR). The comparison cache still contains uncommitted rows.
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
            finally:
                self.invalidate_business_cache()
            raise

    def _get_meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return default if row is None else json.loads(row[0])

    @staticmethod
    def _business_version(business):
        if not isinstance(business, dict):
            raise ValueError("业务账本结构无效")
        version = business.get("_storage_version")
        if version is None:
            return None
        if version != 2:
            raise ValueError("未知业务账本版本")
        for collection in _BUSINESS_COLLECTIONS:
            if not isinstance(business.get(collection), dict):
                raise ValueError("业务账本集合无效")
            # V2 stores every collection row in business_parts. Accepting inline
            # rows would let a later Event save discard unrelated ledger entries.
            if business[collection]:
                raise ValueError("分行业务账本不能包含内联集合记录")
        return version

    @staticmethod
    def read_business(db, schema="main"):
        """Read either ledger format without opening or mutating a Store."""
        if schema not in {"main", "live"}:
            raise ValueError("无效账本schema")
        row = db.execute(f"SELECT value FROM {schema}.meta WHERE key='business'").fetchone()
        if row is None:
            return None
        business = json.loads(row[0])
        version = Store._business_version(business)
        business.pop("_storage_version", None)
        if version is None:
            has_parts = db.execute(
                f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name='business_parts'"
            ).fetchone()
            if (
                has_parts
                and db.execute(f"SELECT 1 FROM {schema}.business_parts LIMIT 1").fetchone()
            ):
                raise ValueError("旧格式业务账本与分行记录冲突")
            return business
        for collection, key, payload in db.execute(
            f"SELECT collection,key,payload FROM {schema}.business_parts"
        ):
            if collection not in _BUSINESS_COLLECTIONS:
                raise ValueError("未知业务账本集合")
            value = json.loads(payload)
            if not isinstance(value, dict):
                raise ValueError("业务账本行无效")
            business[collection][key] = value
        return business

    def get(self, key, default=None):
        if key == "business":
            value = self.read_business(self.db)
            return default if value is None else value
        return self._get_meta(key, default)

    def put(self, key, value):
        if key == "business":
            if self.db.in_transaction:
                self._save_business(value)
            else:
                with self.transaction():
                    self._save_business(value)
            return
        self._put_meta(key, value)

    def _put_meta(self, key, value):
        self.db.execute(
            "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE meta.value != excluded.value",
            (key, json.dumps(value, ensure_ascii=False, separators=(",", ":"))),
        )

    def _load_business_rows(self):
        if self._business_rows is not None:
            return
        # A legacy backup can replace the database of an already-open cold Store.
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS business_parts("
            "collection TEXT NOT NULL,key TEXT NOT NULL,payload TEXT NOT NULL,"
            "PRIMARY KEY(collection,key))"
        )
        self._business_rows = {name: {} for name in _BUSINESS_COLLECTIONS}
        self._business_target_events = {}
        for collection, key, payload in self.db.execute(
            "SELECT collection,key,payload FROM business_parts"
        ):
            if collection not in self._business_rows:
                raise ValueError("未知业务账本集合")
            value = json.loads(payload)
            self._business_rows[collection][key] = value
            if collection == "targets":
                self._business_target_events.setdefault(value["event_id"], set()).add(key)

    def _sync_business_rows(self, collection, values, keys=None, force=False):
        saved = self._business_rows[collection]
        keys = saved.keys() | values.keys() if keys is None else keys
        for key in keys:
            previous = saved.get(key)
            if key not in values:
                if key not in saved:
                    continue
                self.db.execute(
                    "DELETE FROM business_parts WHERE collection=? AND key=?",
                    (collection, key),
                )
                saved.pop(key, None)
            else:
                value = values[key]
                if not force and previous == value:
                    continue
                self.db.execute(
                    "INSERT INTO business_parts VALUES(?,?,?) ON CONFLICT(collection,key) "
                    "DO UPDATE SET payload=excluded.payload",
                    (collection, key, json.dumps(value, ensure_ascii=False, separators=(",", ":"))),
                )
                saved[key] = deepcopy(value)
            if collection == "targets":
                if previous is not None:
                    old_keys = self._business_target_events.get(previous["event_id"])
                    if old_keys is not None:
                        old_keys.discard(key)
                        if not old_keys:
                            self._business_target_events.pop(previous["event_id"])
                if key in values:
                    self._business_target_events.setdefault(values[key]["event_id"], set()).add(key)

    def _save_business(self, business, event_id=None, force=False):
        self._load_business_rows()
        scalar = {key: value for key, value in business.items() if key not in _BUSINESS_COLLECTIONS}
        scalar.update({name: {} for name in _BUSINESS_COLLECTIONS})
        scalar["_storage_version"] = 2
        self._put_meta("business", scalar)
        if event_id is None:
            for collection in _BUSINESS_COLLECTIONS:
                self._sync_business_rows(collection, business[collection], force=force)
            return
        self._sync_business_rows("cycles", business["cycles"], {event_id})
        target_keys = self._business_target_events.get(event_id, set()) | {
            key for key, value in business["targets"].items() if value["event_id"] == event_id
        }
        self._sync_business_rows("targets", business["targets"], target_keys)
        # Claims are changed only by resolution/redemption, whose generic save
        # synchronizes them. A settlement fill supplies the changed claim below.

    def save_business_event(self, business, event_id, condition_id=None):
        """Persist only this Event's projection, atomically with its intent."""
        if not self.db.in_transaction:
            with self.transaction():
                self.save_business_event(business, event_id, condition_id)
            return
        self._save_business(business, event_id=event_id)
        if condition_id is not None:
            self._sync_business_rows("claims", business["claims"], {condition_id})

    def invalidate_business_cache(self):
        """Discard comparisons after rollback or an external snapshot replacement."""
        self._business_rows = None
        self._business_target_events = {}

    @property
    def generation(self):
        return self.get("generation")

    def journal(self, event):
        event_id = (
            f"fill:{event.client_order_id}:{event.trade_id}"
            if type(event).__name__ == "OrderFilled"
            else str(event.id)
        )
        existing = self.db.execute(
            "SELECT seq FROM native_events WHERE event_id=?", (event_id,)
        ).fetchone()
        if existing is not None:
            # INSERT OR IGNORE on AUTOINCREMENT still writes sqlite_sequence.
            # Native delivery is synchronous on the sole ledger owner; duplicates
            # must preserve the first committed fact without another FULL commit.
            return existing[0]
        self.db.execute(
            "INSERT OR IGNORE INTO native_events(event_id,kind,payload) VALUES(?,?,?)",
            (event_id, type(event).__name__, self.serializer.serialize(event)),
        )
        return self.db.execute(
            "SELECT seq FROM native_events WHERE event_id=?", (event_id,)
        ).fetchone()[0]

    def events(self, after=0):
        return [
            (r["seq"], self.serializer.deserialize(r["payload"]))
            for r in self.db.execute(
                "SELECT seq,payload FROM native_events WHERE seq>? ORDER BY seq", (after,)
            )
        ]

    def order_fills(self, order_id):
        return [
            self.serializer.deserialize(r[0])
            for r in self.db.execute(
                "SELECT payload FROM native_events WHERE kind='OrderFilled' AND json_extract(CAST(payload AS TEXT),'$.client_order_id')=? ORDER BY seq",
                (str(order_id),),
            )
        ]

    def save_token(self, token):
        self.db.execute(
            "INSERT INTO tokens VALUES(?,?) ON CONFLICT(token_id) DO UPDATE SET payload=excluded.payload",
            (token.token_id, json.dumps(asdict(token))),
        )

    def tokens(self, active_only=False):
        out = {}
        query = "SELECT payload FROM tokens"
        if active_only:
            # Retain every execution identity, including closed historical markets.
            # Inactive discovery-only metadata remains durable, not resident in RAM.
            query += " WHERE json_extract(payload,'$.open') IS NOT 0 OR token_id IN (SELECT token_id FROM intents)"
        for row in self.db.execute(query):
            data = json.loads(row[0])
            data["fees"] = Fees(**data["fees"])
            data["category_ids"] = tuple(data["category_ids"])
            data["category_labels"] = tuple(data["category_labels"])
            t = Token(**data)
            out[t.token_id] = t
        return out

    def save_book(self, token_id, book):
        self.db.execute(
            "INSERT INTO books VALUES(?,?) ON CONFLICT(token_id) DO UPDATE SET payload=excluded.payload",
            (token_id, json.dumps(asdict(book))),
        )

    def books(self):
        out = {}
        for row in self.db.execute("SELECT token_id,payload FROM books"):
            data = json.loads(row[1])
            for side in ("bid", "ask"):
                data[side] = Side(
                    **{k: {int(p): q for p, q in v.items()} for k, v in data[side].items()}
                )
            data["ready"] = False
            out[row[0]] = Book(**data)
        return out

    def intent(self, order_id):
        r = self.db.execute(
            "SELECT payload FROM intents WHERE order_id=?", (str(order_id),)
        ).fetchone()
        return json.loads(r[0]) if r else None

    def save_intent(self, order_id, payload):
        self.db.execute(
            "INSERT INTO intents VALUES(?,?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET payload=excluded.payload",
            (
                str(order_id),
                payload["event_id"],
                payload["token_id"],
                payload["side"],
                json.dumps(payload),
            ),
        )

    def intents(self, active_only=False, event_id=None):
        clauses = []
        params = []
        if active_only:
            clauses.append("json_extract(payload,'$.terminal')=0")
        if event_id is not None:
            clauses.append("event_id=?")
            params.append(event_id)
        query = "SELECT order_id,payload FROM intents"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        return {r[0]: json.loads(r[1]) for r in self.db.execute(query, params)}

    def has_history(self):
        return self.db.execute("SELECT 1 FROM intents LIMIT 1").fetchone() is not None

    @staticmethod
    def _read_integrity(path, stop):
        # Keep the writable connection and all native objects on their owning
        # event loop. A separate read-only WAL snapshot checks the entire database.
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            db.set_progress_handler(lambda: int(stop.is_set()), 1000)
            if stop.is_set():
                raise sqlite3.OperationalError("Integrity check cancelled")
            return db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    async def integrity_check_async(self):
        # Serialize full checks, without caching results or skipping any request.
        async with self._integrity_lock:
            if self._closed:
                raise ValueError("账本已关闭，请重新核对")
            stop = Event()
            self._integrity_stops.add(stop)
            worker = asyncio.get_running_loop().run_in_executor(
                _INTEGRITY_POOL, self._read_integrity, self.path.resolve(), stop
            )
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                stop.set()
                # Cancellation must finish the reader before releasing this lock.
                # The worker owns its connection and closes it even on interrupt.
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not worker.cancelled():
                    worker.exception()
                raise
            finally:
                self._integrity_stops.discard(stop)

    def reset(self):
        if self.mode != "TEST" or self.get("status") != "PAUSED":
            raise ValueError("只能在PAUSED下重置TEST")
        with self.transaction():
            for table in ("native_events", "intents", "tokens", "books"):
                self.db.execute(f"DELETE FROM {table}")
            self.put("generation", str(uuid4()))
            self.put("preferences", Preferences().model_dump(mode="json"))
            self.put("initial_capital", 100_000_000)
            self.put("business", self.empty_business())

    def close(self):
        self._closed = True
        for stop in self._integrity_stops:
            stop.set()
        self.db.close()
