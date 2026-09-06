"""Durable native event journal and replayable PM business projection, per mode."""

import json
import msgspec
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from nautilus_trader.serialization.serializer import MsgSpecSerializer
from .books import Book, Side
from .config import Preferences
from .rules import Token, Fees


class Store:
    def __init__(self, path: str | Path, mode="TEST"):
        self.path = Path(path)
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
        CREATE TABLE IF NOT EXISTS intents(
          order_id TEXT PRIMARY KEY,event_id TEXT NOT NULL,token_id TEXT NOT NULL,
          side TEXT NOT NULL,payload TEXT NOT NULL);
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
            self.db.execute("ROLLBACK")
            raise

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return default if row is None else json.loads(row[0])

    def put(self, key, value):
        self.db.execute(
            "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False, separators=(",", ":"))),
        )

    @property
    def generation(self):
        return self.get("generation")

    def journal(self, event):
        event_id = (
            f"fill:{event.client_order_id}:{event.trade_id}"
            if type(event).__name__ == "OrderFilled"
            else str(event.id)
        )
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

    def save_token(self, token):
        self.db.execute(
            "INSERT INTO tokens VALUES(?,?) ON CONFLICT(token_id) DO UPDATE SET payload=excluded.payload",
            (token.token_id, json.dumps(asdict(token))),
        )

    def tokens(self):
        out = {}
        for row in self.db.execute("SELECT payload FROM tokens"):
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

    def intents(self):
        return {
            r[0]: json.loads(r[1]) for r in self.db.execute("SELECT order_id,payload FROM intents")
        }

    def has_history(self):
        return self.db.execute("SELECT 1 FROM intents LIMIT 1").fetchone() is not None

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
        self.db.close()
