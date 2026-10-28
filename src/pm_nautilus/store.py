"""Durable native event journal and replayable PM business projection, per mode."""

import asyncio
import base64
import binascii
import json
import msgspec
import sqlite3
import zlib
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
# This bounds one stored metadata payload, never the number of markets/tokens.
# Oversized input is rejected, not truncated or dropped during migration.
_TOKEN_JSON_MAX_BYTES = 1024 * 1024
# Immutable public JSON vocabulary for codec version 1. Changing these bytes
# requires a new version; old bundles must remain decodable without the network.
_TOKEN_ZDICT_V1 = (
    b'{"token_id":"","event_id":"","market_id":"","condition_id":"","direction":"",'
    b'"result_count":0,"neg_risk":false,"opened_ns":0,"ends_ns":0,'
    b'"fees":{"enabled":false,"rate":0,"exponent":1},"min_size":0,"tick":0,'
    b'"event_title":"","question":"","slug":"","category_ids":[],"category_labels":[],'
    b'"game_start_ns":null,"open":false}'
)
_ACTIVE_TOKENS_SQL = (
    "SELECT token_id,payload FROM tokens "
    "WHERE json_extract(payload,'$.open') IS NOT 0 "
    "UNION ALL SELECT token_id,payload FROM tokens "
    "WHERE token_id IN (SELECT token_id FROM intents) "
    "AND json_extract(payload,'$.open') IS 0"
)


def _token_json(raw):
    def unique(pairs):
        data = {}
        for key, value in pairs:
            if key in data:
                raise ValueError("代币元数据包含重复字段")
            data[key] = value
        return data

    def invalid_constant(_):
        raise ValueError("代币元数据包含非法数值")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)


def _canonical_token_data(data):
    if not isinstance(data, dict):
        raise ValueError("代币元数据结构无效")
    values = dict(data)
    values["fees"] = Fees(**values["fees"])
    for key in ("category_ids", "category_labels"):
        categories = values.get(key, ())
        if not isinstance(categories, (list, tuple)) or not all(
            isinstance(value, str) for value in categories
        ):
            raise ValueError("代币分类元数据无效")
        values[key] = tuple(categories)
    token = Token(**values)
    if type(token.open) is not bool:
        raise ValueError("代币开放状态无效")
    result = asdict(token)
    for key in ("category_ids", "category_labels"):
        result[key] = list(result[key])
    return result


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
        try:
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
        CREATE INDEX IF NOT EXISTS intents_token ON intents(token_id);
        CREATE INDEX IF NOT EXISTS native_fill_order ON native_events(kind,json_extract(CAST(payload AS TEXT),'$.client_order_id'));
            """)
            self.ensure_token_index(self.db)
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
            if raw_business is not None and self._business_version(raw_business) is None:
                # Upgrade atomically; every legacy collection remains in the ledger.
                with self.transaction():
                    self._save_business(self.read_business(self.db), force=True)
            self.put("status", "PAUSED")
        except BaseException:
            # The constructor can fail before a caller receives its Store. The
            # opened connection must not wait for a traceback/GC cycle to close.
            try:
                self.close()
            except Exception:
                pass  # Preserve the original initialization failure.
            raise

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

    def events(self, after=0, until=None):
        query = "SELECT seq,payload FROM native_events WHERE seq>?"
        parameters = (after,)
        if until is not None:
            query += " AND seq<=?"
            parameters += (until,)
        query += " ORDER BY seq"
        return [
            (r["seq"], self.serializer.deserialize(r["payload"]))
            for r in self.db.execute(query, parameters)
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
            "INSERT INTO tokens VALUES(?,?) ON CONFLICT(token_id) DO UPDATE SET payload=excluded.payload "
            "WHERE tokens.payload != excluded.payload",
            (token.token_id, self.encode_token_payload(asdict(token))),
        )

    @staticmethod
    def ensure_token_index(db):
        """Build the optional hot-row index, including after an old bundle restore."""
        db.execute(
            "CREATE INDEX IF NOT EXISTS tokens_open ON tokens(token_id) "
            "WHERE json_extract(payload,'$.open') IS NOT 0"
        )

    @staticmethod
    def decode_token_payload(raw):
        """Read legacy flat JSON or a lossless versioned token envelope."""
        try:
            if not isinstance(raw, (str, bytes, bytearray)):
                raise ValueError("代币元数据类型无效")
            raw_size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
            # Even incompressible 1MiB JSON fits this base64-envelope bound.
            if raw_size > 2 * _TOKEN_JSON_MAX_BYTES:
                raise ValueError("代币元数据编码过大")
            data = _token_json(raw)
            if isinstance(data, dict) and "_token_storage" in data:
                if (
                    set(data) != {"_token_storage", "open", "size", "data"}
                    or type(data["_token_storage"]) is not int
                    or data["_token_storage"] != 1
                    or type(data["open"]) is not bool
                    or type(data["size"]) is not int
                    or not 0 < data["size"] <= _TOKEN_JSON_MAX_BYTES
                    or not isinstance(data["data"], str)
                ):
                    raise ValueError("代币元数据编码版本或结构无效")
                packed = base64.b64decode(data["data"], validate=True)
                decoder = zlib.decompressobj(zdict=_TOKEN_ZDICT_V1)
                decoded = decoder.decompress(packed, data["size"] + 1)
                if (
                    len(decoded) != data["size"]
                    or not decoder.eof
                    or decoder.unused_data
                    or decoder.unconsumed_tail
                ):
                    raise ValueError("代币元数据压缩内容无效")
                expanded = _canonical_token_data(_token_json(decoded))
                if expanded["open"] is not data["open"]:
                    raise ValueError("代币元数据开放状态不一致")
                return expanded
            if raw_size > _TOKEN_JSON_MAX_BYTES:
                raise ValueError("代币元数据JSON过大")
            return _canonical_token_data(data)
        except (KeyError, TypeError, ValueError, zlib.error, binascii.Error, OverflowError):
            raise ValueError("代币元数据编码无效") from None

    @staticmethod
    def encode_token_payload(data):
        """Preserve every Token field, with a queryable open flag outside zlib."""
        data = _canonical_token_data(data)
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > _TOKEN_JSON_MAX_BYTES:
            raise ValueError("代币元数据JSON过大")
        compressor = zlib.compressobj(6, zdict=_TOKEN_ZDICT_V1)
        packed = compressor.compress(raw) + compressor.flush()
        return json.dumps(
            {
                "_token_storage": 1,
                "open": data["open"],
                "size": len(raw),
                "data": base64.b64encode(packed).decode("ascii"),
            },
            separators=(",", ":"),
        )

    @staticmethod
    def compact_tokens(db, batch_size=256, progress=None):
        """Transcode existing rows inside the caller's offline transaction.

        The caller owns process locking, backup, logical comparisons, COMMIT and
        any later optional VACUUM. This method never opens Store or changes meta.
        Every original row remains, including inactive discovery-only metadata.
        """
        if not db.in_transaction:
            raise ValueError("代币元数据压缩需要调用方事务")
        if type(batch_size) is not int or not 1 <= batch_size <= 10_000:
            raise ValueError("代币元数据批次大小无效")
        result = {"rows": 0, "changed": 0, "before_bytes": 0, "after_bytes": 0}
        cursor = None
        while True:
            query = "SELECT rowid,token_id,payload FROM tokens"
            query += " WHERE rowid>?" if cursor is not None else ""
            query += " ORDER BY rowid LIMIT ?"
            rows = db.execute(
                query, (cursor, batch_size) if cursor is not None else (batch_size,)
            ).fetchall()
            if not rows:
                break
            for row_id, token_id, payload in rows:
                data = Store.decode_token_payload(payload)
                if data["token_id"] != token_id:
                    raise ValueError("代币元数据身份与主键不一致")
                encoded = Store.encode_token_payload(data)
                result["rows"] += 1
                result["before_bytes"] += len(payload.encode("utf-8"))
                result["after_bytes"] += len(encoded.encode("utf-8"))
                if payload != encoded:
                    db.execute("UPDATE tokens SET payload=? WHERE rowid=?", (encoded, row_id))
                    result["changed"] += 1
            cursor = rows[-1][0]
            if progress is not None:
                progress(dict(result))
        Store.ensure_token_index(db)
        return result

    def tokens(self, active_only=False):
        out = {}
        # The partial index reads only open metadata; separate historical lookups
        # retain all execution identities without scanning cold discovery JSON.
        query = _ACTIVE_TOKENS_SQL if active_only else "SELECT token_id,payload FROM tokens"
        for row in self.db.execute(query):
            data = self.decode_token_payload(row[1])
            if data["token_id"] != row[0]:
                raise ValueError("代币元数据身份与主键不一致")
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

    def historical_token_ids(self):
        """Read committed order ownership without decoding historical orders."""
        return {row[0] for row in self.db.execute("SELECT DISTINCT token_id FROM intents")}

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
