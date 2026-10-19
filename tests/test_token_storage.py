"""Lossless token metadata compaction preserves cold identities and old bundles."""

import base64
import hashlib
import json
import sqlite3
import zlib
from dataclasses import asdict, replace

import pytest

from pm_nautilus.rules import Fees, Token
from pm_nautilus.store import Store, _ACTIVE_TOKENS_SQL, _TOKEN_JSON_MAX_BYTES, _TOKEN_ZDICT_V1


def token(token_id="1", **changes):
    original = Token(
        token_id,
        "event",
        "market",
        "0x" + "a" * 64,
        "Yes",
        2,
        False,
        0,
        864_000_000_000_000,
        Fees(True, 25_000, 2),
        5_000_000,
        1000,
        "Election — 选举 🗳️",
        "Will the designated candidate win the election before the specified deadline?",
        "election-deadline",
        ("2", "42"),
        ("Politics", "世界"),
    )
    return replace(original, **changes)


def canonical(value):
    return json.loads(json.dumps(asdict(value)))


def envelope_for_raw(raw, *, open=True):
    encoder = zlib.compressobj(6, zdict=_TOKEN_ZDICT_V1)
    packed = encoder.compress(raw) + encoder.flush()
    return {
        "_token_storage": 1,
        "open": open,
        "size": len(raw),
        "data": base64.b64encode(packed).decode(),
    }


def test_codec_v1_dictionary_and_existing_fixture_are_frozen():
    assert hashlib.sha256(_TOKEN_ZDICT_V1).hexdigest() == (
        "02f7628c504eef1abacd3c6c8f10357021af81bc7ec3bae8eb6095b3b96eb271"
    )
    # A literal produced before future writers run: it must remain decodable.
    encoded = (
        '{"_token_storage":1,"open":true,"size":349,"data":'
        '"eLtF1Gm/q0YOvxLUAExFC8FczCBMRgvDSNdgjGA0Ij4YLcxMDJAABYFqCDMBErSGYDYdQ7ekqDS1FgD67HB0"}'
    )
    expected = Token(
        "t", "e", "m", "c", "YES", 2, False, 0, 864_000_000_000_000, Fees(False), 1_000_000, 1000
    )
    assert Store.decode_token_payload(encoded) == canonical(expected)


@pytest.mark.parametrize("open", [False, True])
def test_codec_round_trip_preserves_every_field_and_legacy_json(open):
    original = token(open=open, game_start_ns=123456789012345678)
    data = canonical(original)
    encoded = Store.encode_token_payload(data)
    assert Store.decode_token_payload(encoded) == data
    assert Store.decode_token_payload(json.dumps(data)) == data
    assert Store.encode_token_payload(Store.decode_token_payload(encoded)) == encoded
    assert json.loads(encoded)["open"] is open
    assert len(encoded.encode()) < len(json.dumps(data).encode())


def test_legacy_missing_optional_fields_get_exact_dataclass_defaults():
    original = Token("t", "e", "m", "c", "YES", 2, False, 0, 10, Fees(False), 1_000_000, 1000)
    data = canonical(original)
    for field in (
        "event_title",
        "question",
        "slug",
        "category_ids",
        "category_labels",
        "game_start_ns",
        "open",
    ):
        data.pop(field)
    assert Store.decode_token_payload(json.dumps(data)) == canonical(original)


@pytest.mark.parametrize(
    "change",
    [
        {"_token_storage": 2},
        {"_token_storage": True},
        {"open": False},
        {"open": 1},
        {"size": 1},
        {"size": True},
        {"size": 0},
        {"size": -1},
        {"size": 10**100},
        {"extra": "unrecognized"},
        {"data": "bad!"},
        {"data": None},
    ],
)
def test_invalid_envelope_is_rejected(change):
    encoded = json.loads(Store.encode_token_payload(canonical(token())))
    encoded.update(change)
    with pytest.raises(ValueError, match="元数据编码无效"):
        Store.decode_token_payload(json.dumps(encoded))


@pytest.mark.parametrize("mutation", ["truncate", "trailing", "checksum"])
def test_corrupt_zlib_stream_is_rejected(mutation):
    encoded = json.loads(Store.encode_token_payload(canonical(token())))
    packed = base64.b64decode(encoded["data"])
    if mutation == "truncate":
        packed = packed[:-1]
    elif mutation == "trailing":
        packed += b"hidden trailing stream"
    elif mutation == "checksum":
        packed = packed[:-1] + bytes([packed[-1] ^ 1])
    encoded["data"] = base64.b64encode(packed).decode()
    with pytest.raises(ValueError, match="元数据编码无效"):
        Store.decode_token_payload(json.dumps(encoded))


def test_valid_zlib_stream_without_preset_dictionary_still_decodes_losslessly():
    data = canonical(token())
    raw = json.dumps(data).encode()
    encoded = envelope_for_raw(raw)
    encoded["data"] = base64.b64encode(zlib.compress(raw)).decode()
    assert Store.decode_token_payload(json.dumps(encoded)) == data


@pytest.mark.parametrize(
    "raw",
    [b"[]", b"{}", b'{"token_id":"one","token_id":"two"}', b'{"token_id":NaN}', b'"not an object"'],
)
def test_malformed_plain_or_compressed_token_is_rejected(raw):
    for payload in (raw, json.dumps(envelope_for_raw(raw))):
        with pytest.raises(ValueError, match="元数据编码无效"):
            Store.decode_token_payload(payload)


def test_storage_size_limit_rejects_oversize_without_truncation():
    large = canonical(token(question="X" * _TOKEN_JSON_MAX_BYTES))
    with pytest.raises(ValueError, match="JSON过大"):
        Store.encode_token_payload(large)
    raw = json.dumps(large).encode()
    encoded = envelope_for_raw(raw)
    for payload in (raw, json.dumps(encoded)):
        with pytest.raises(ValueError, match="元数据编码无效"):
            Store.decode_token_payload(payload)
    # A compressible bomb cannot use a smaller claimed size to evade the bound.
    encoded["size"] = 1000
    with pytest.raises(ValueError, match="元数据编码无效"):
        Store.decode_token_payload(json.dumps(encoded))


def test_indexed_active_reads_keep_closed_history_without_decoding_cold_rows(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.sqlite")
    originals = [token("open"), token("historical", open=False), token("cold", open=False)]
    try:
        for value in originals:
            store.save_token(value)
        store.save_intent("order", {"event_id": "event", "token_id": "historical", "side": "BUY"})
        decode = Store.decode_token_payload
        decoded = []

        def tracked(payload):
            data = decode(payload)
            decoded.append(data["token_id"])
            return data

        monkeypatch.setattr(Store, "decode_token_payload", staticmethod(tracked))
        assert store.tokens(active_only=True) == {value.token_id: value for value in originals[:2]}
        assert set(decoded) == {"open", "historical"} and len(decoded) == 2
        plan = "\n".join(
            row[3] for row in store.db.execute("EXPLAIN QUERY PLAN " + _ACTIVE_TOKENS_SQL)
        )
        assert "USING INDEX tokens_open" in plan
        assert "sqlite_autoindex_tokens_1" in plan
        # Restoring an old SQLite backup into an existing cold Store removes new
        # indexes. The query must remain correct without any cached schema flag.
        store.db.execute("DROP INDEX tokens_open")
        assert set(store.tokens(active_only=True)) == {"open", "historical"}
        Store.ensure_token_index(store.db)
        assert set(store.tokens(active_only=True)) == {"open", "historical"}
        assert store.tokens() == {value.token_id: value for value in originals}
    finally:
        store.close()


def test_duplicate_save_does_not_rewrite_or_change_metadata(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    try:
        value = token()
        store.save_token(value)
        before = store.db.total_changes
        store.save_token(value)
        assert store.db.total_changes == before
        store.save_token(replace(value, tick=10_000, open=False))
        assert store.tokens()[value.token_id] == replace(value, tick=10_000, open=False)
        assert store.tokens(active_only=True) == {}
    finally:
        store.close()


def test_compaction_keeps_all_rows_and_other_tables_in_callers_transaction(tmp_path):
    path = tmp_path / "state.sqlite"
    store = Store(path)
    originals = [token(str(n), open=n == 2) for n in range(4)]
    for row_id, value in zip((-2, 0, 1, 20), originals, strict=True):
        store.db.execute(
            "INSERT INTO tokens(rowid,token_id,payload) VALUES(?,?,?)",
            (row_id, value.token_id, json.dumps(asdict(value))),
        )
    store.put("status", "RUNNING")
    store.close()
    with sqlite3.connect(path, isolation_level=None) as db:
        db.execute("DROP INDEX tokens_open")
        before_meta = db.execute("SELECT * FROM meta ORDER BY key").fetchall()
        before_rows = db.execute(
            "SELECT rowid,token_id,payload FROM tokens ORDER BY rowid"
        ).fetchall()
        with pytest.raises(ValueError, match="调用方事务"):
            Store.compact_tokens(db)
        db.execute("BEGIN IMMEDIATE")
        progress = []
        stats = Store.compact_tokens(db, batch_size=1, progress=progress.append)
        assert db.in_transaction and stats["rows"] == stats["changed"] == len(originals)
        assert stats["after_bytes"] < stats["before_bytes"]
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='tokens_open'"
        ).fetchone() == ("tokens_open",)
        assert len(progress) == len(originals) and progress[-1] == stats
        assert db.execute("SELECT * FROM meta ORDER BY key").fetchall() == before_meta
        after = db.execute("SELECT rowid,token_id,payload FROM tokens ORDER BY rowid").fetchall()
        assert [(r, t) for r, t, _ in after] == [(r, t) for r, t, _ in before_rows]
        assert [Store.decode_token_payload(raw) for _, _, raw in after] == [
            canonical(t) for t in originals
        ]
        assert Store.compact_tokens(db)["changed"] == 0
        db.execute("ROLLBACK")
        assert (
            db.execute("SELECT rowid,token_id,payload FROM tokens ORDER BY rowid").fetchall()
            == before_rows
        )
        db.execute("BEGIN IMMEDIATE")
        Store.compact_tokens(db)
        db.execute("COMMIT")
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)


@pytest.mark.parametrize("fault", ["invalid_payload", "identity", "cancel"])
def test_failed_compaction_rolls_back_prior_rows_without_touching_status(tmp_path, fault):
    store = Store(tmp_path / "state.sqlite")
    try:
        first, second = token("1"), token("2")
        store.db.execute("INSERT INTO tokens VALUES(?,?)", ("1", json.dumps(asdict(first))))
        payload = json.dumps(asdict(second)) if fault == "cancel" else "{}"
        store.db.execute("INSERT INTO tokens VALUES(?,?)", ("2", payload))
        if fault == "identity":
            store.db.execute(
                "UPDATE tokens SET payload=? WHERE token_id='2'", (json.dumps(asdict(first)),)
            )
        before = list(store.db.iterdump())

        def progress(_):
            if fault == "cancel":
                raise RuntimeError("cancel offline migration")

        with pytest.raises((ValueError, RuntimeError)):
            with store.transaction():
                Store.compact_tokens(store.db, batch_size=1, progress=progress)
        assert list(store.db.iterdump()) == before
    finally:
        store.close()


def test_payload_key_mismatch_cannot_replace_another_token(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    try:
        store.db.execute(
            "INSERT INTO tokens VALUES(?,?)",
            ("wrong-key", Store.encode_token_payload(canonical(token()))),
        )
        with pytest.raises(ValueError, match="主键"):
            store.tokens()
    finally:
        store.close()
