"""Bounded removal of old, unowned public discovery cache on the owner loop.

The caller owns disk-pressure policy and must provide sufficient write headroom.
This never touches financial tables, rebuilds SQLite, or evicts native objects.
Deleting rows creates reusable database pages; it does not promise a smaller file.
The time budget bounds cooperative work, not operating-system I/O or fsync latency.
"""

from collections.abc import Mapping
from dataclasses import asdict
from contextlib import closing
from math import isfinite
import sqlite3
from time import monotonic
from types import SimpleNamespace

from .native import instrument_id
from .store import Store

MAX_BATCH = 128
_REFERENCE_COLLECTIONS = ("cycles", "targets", "claims")


class _Skip(ValueError):
    pass


def _string(value):
    if not isinstance(value, str) or not value:
        raise _Skip("malformed_reference")
    return value


def _check_time(deadline):
    if monotonic() >= deadline:
        raise _Skip("time_budget")


def _protect_business(business, identities, deadline):
    """Overprotect all matching identities, including future nested JSON fields."""
    if not isinstance(business, dict) or set(business) != set(Store.empty_business()):
        raise _Skip("malformed_reference")
    for collection in _REFERENCE_COLLECTIONS:
        rows = business[collection]
        if not isinstance(rows, dict):
            raise _Skip("malformed_reference")
        for key, row in rows.items():
            _check_time(deadline)
            _string(key)
            if not isinstance(row, dict):
                raise _Skip("malformed_reference")
            _string(row.get("token_id"))
            if collection != "cycles":
                _string(row.get("event_id"))
            if collection == "claims":
                if _string(row.get("condition_id")) != key:
                    raise _Skip("malformed_reference")
                rights = row.get("rights")
                if not isinstance(rights, dict) or not rights:
                    raise _Skip("malformed_reference")
                for token_id, quantity in rights.items():
                    _check_time(deadline)
                    _string(token_id)
                    if type(quantity) is not int or quantity < 0:
                        raise _Skip("malformed_reference")
    if not isinstance(business["banned"], list) or not isinstance(business["settled"], list):
        raise _Skip("malformed_reference")
    protected = set()

    def children(mapping):
        for key, item in mapping.items():
            _check_time(deadline)
            if not isinstance(key, str):
                raise _Skip("malformed_reference")
            yield key
            yield item

    pending = [iter((business,))]
    while pending:
        _check_time(deadline)
        try:
            value = next(pending[-1])
        except StopIteration:
            pending.pop()
            continue
        if isinstance(value, str):
            protected.update(identities.get(value, ()))
        elif isinstance(value, dict):
            pending.append(children(value))
        elif isinstance(value, (list, tuple)):
            pending.append(iter(value))
        elif value is not None and type(value) not in (bool, int, float):
            raise _Skip("malformed_reference")
        elif type(value) is float and not isfinite(value):
            raise _Skip("malformed_reference")
    return protected


def _full_intent_index(db):
    indexes = db.execute("PRAGMA index_list(intents)").fetchall()
    if not any(row[1] == "intents_token" and row[4] == 0 for row in indexes):
        return False
    return [row[2] for row in db.execute("PRAGMA index_info(intents_token)")] == ["token_id"]


def prune_cold_metadata(
    runtime, cutoff_ns, *, limit=MAX_BATCH, after_token_id=None, time_budget=0.025
):
    """Delete one bounded page of unowned closed public metadata, fail closed.

    Call synchronously on the runtime's sole owner loop, only under disk pressure
    with safe write headroom. ``cutoff_ns`` is normally current time minus 90 days.
    A full ``intents_token ON intents(token_id)`` index must already exist; this
    function never builds an index or scans all historical intents. It returns
    ``removed_ids`` for the caller to evict from its TokenRegistry after commit.
    No await may separate this call and those corresponding memory evictions.
    """
    if (
        type(cutoff_ns) is not int
        or cutoff_ns <= 0
        or type(limit) is not int
        or not 1 <= limit <= MAX_BATCH
        or (after_token_id is not None and not isinstance(after_token_id, str))
        or type(time_budget) not in (int, float)
        or not isfinite(time_budget)
        or not 0 < time_budget <= 0.1
    ):
        raise ValueError("invalid bounded metadata retention request")
    result = {
        "removed": 0,
        "removed_ids": (),
        "next_token_id": after_token_id,
        "scanned": 0,
        "complete": False,
        "skipped": None,
    }
    db = runtime.store.db
    if db.in_transaction:
        return result | {"skipped": "transaction_in_progress"}
    if getattr(runtime, "faulted", None):
        return result | {"skipped": "runtime_faulted"}
    started = monotonic()
    deadline = started + time_budget
    decode_deadline = started + time_budget * 0.7
    previous_timeout = None
    try:
        previous_timeout = db.execute("PRAGMA busy_timeout").fetchone()[0]
        db.execute("PRAGMA busy_timeout=0")
        if not _full_intent_index(db):
            raise _Skip("missing_intent_index")
        db.execute("BEGIN IMMEDIATE")
        candidates, processed = {}, []
        exhausted = True
        with closing(
            db.execute(
                "SELECT token_id,payload FROM tokens WHERE token_id>? ORDER BY token_id LIMIT ?",
                (after_token_id or "", limit),
            )
        ) as rows:
            for token_id, payload in rows:
                if processed and monotonic() >= decode_deadline:
                    exhausted = False
                    break
                _check_time(deadline)
                _string(token_id)
                data = Store.decode_token_payload(payload)
                if (
                    data["token_id"] != token_id
                    or type(data["ends_ns"]) is not int
                    or not isinstance(data["event_id"], str)
                    or not data["event_id"]
                    or not isinstance(data["condition_id"], str)
                    or not data["condition_id"]
                ):
                    raise _Skip("malformed_metadata")
                processed.append(token_id)
                if data["open"] is False and 0 < data["ends_ns"] <= cutoff_ns:
                    candidates[token_id] = data
        identities = {}
        for token_id, data in candidates.items():
            for identity in (token_id, data["event_id"], data["condition_id"]):
                identities.setdefault(identity, set()).add(token_id)
        protected = _protect_business(runtime.business, identities, deadline)
        for collection in (runtime.monitored, runtime.books, runtime.instrument_tokens):
            if not isinstance(collection, (Mapping, set, frozenset)):
                raise _Skip("malformed_reference")
            for value in collection:
                _check_time(deadline)
                _string(value)
        for value in runtime.instrument_tokens.values():
            _check_time(deadline)
            protected.add(_string(value))
        protected.update(runtime.monitored)
        protected.update(runtime.books)
        for token_id in candidates:
            _check_time(deadline)
            if (
                db.execute(
                    "SELECT 1 FROM intents INDEXED BY intents_token WHERE token_id=? LIMIT 1",
                    (token_id,),
                ).fetchone()
                or db.execute(
                    "SELECT 1 FROM books WHERE token_id=? LIMIT 1", (token_id,)
                ).fetchone()
            ):
                protected.add(token_id)
        removed = []
        for token_id, data in candidates.items():
            _check_time(deadline)
            current = runtime.tokens.get(token_id)
            if current is not None:
                current_data = asdict(current)
                # Stored JSON category arrays become tuples in the Token dataclass.
                for field in ("category_ids", "category_labels"):
                    current_data[field] = list(current_data[field])
                if current_data != data:
                    protected.add(token_id)
            if runtime.native.cache.instrument(instrument_id(SimpleNamespace(**data))) is not None:
                protected.add(token_id)
            if token_id not in protected:
                db.execute("DELETE FROM tokens WHERE token_id=?", (token_id,))
                removed.append(token_id)
        _check_time(deadline)
        db.execute("COMMIT")
        complete = exhausted and len(processed) < limit
        return result | {
            "removed": len(removed),
            "removed_ids": tuple(removed),
            "next_token_id": None if complete else (processed[-1] if processed else after_token_id),
            "scanned": len(processed),
            "complete": complete,
        }
    except _Skip as exc:
        return result | {"skipped": str(exc)}
    except (sqlite3.Error, ValueError, TypeError, AttributeError, KeyError):
        return result | {"skipped": "invalid_or_unavailable_state"}
    finally:
        try:
            if db.in_transaction:
                db.execute("ROLLBACK")
        finally:
            if previous_timeout is not None:
                db.execute(f"PRAGMA busy_timeout={int(previous_timeout)}")
