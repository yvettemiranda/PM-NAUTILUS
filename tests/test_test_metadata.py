"""TEST public metadata sharing never shares mutable or financial account state."""

import json
import gc
import weakref
import subprocess
import sys
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, asdict, replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pm_nautilus.rules import Fees, Token
from pm_nautilus.store import Store
from pm_nautilus.test_metadata import MAX_CACHE_BYTES, MAX_CACHE_ENTRIES, TestMetadataMemo

from test_test_optimization_equivalence import token_set


def encoded(token):
    return Store.encode_token_payload(asdict(token))


def test_memo_reuses_complete_immutable_metadata_and_full_payload_is_the_key():
    memo = TestMetadataMemo()
    token = token_set(1)[0]
    first = memo.decode(token.token_id, encoded(token), Store.decode_token_payload)
    assert first == token
    assert memo.decode(token.token_id, encoded(token), Store.decode_token_payload) is first
    changed = replace(token, tick=10_000, question="new public metadata")
    second = memo.decode(token.token_id, encoded(changed), Store.decode_token_payload)
    assert second == changed and second is not first
    with pytest.raises(FrozenInstanceError):
        first.tick = 1
    with pytest.raises(FrozenInstanceError):
        first.fees.rate = 1
    assert type(first.category_ids) is tuple and type(first.category_labels) is tuple
    assert memo.info().hits == 1 and memo.info().entries == 2


def test_decoder_identity_and_cache_schema_are_part_of_the_key(monkeypatch):
    memo = TestMetadataMemo()
    token = token_set(1)[0]
    payload = encoded(token)
    decoder = Mock(side_effect=Store.decode_token_payload)
    first = memo.decode(token.token_id, payload, decoder)
    other = Mock(side_effect=Store.decode_token_payload)
    assert memo.decode(token.token_id, payload, other) == first
    assert other.call_count == 1
    monkeypatch.setattr("pm_nautilus.test_metadata.CACHE_SCHEMA_VERSION", 2)
    assert memo.decode(token.token_id, payload, decoder) == first
    assert decoder.call_count == 2


@pytest.mark.parametrize("payload", ['{"token_id":', "bad-codec:xx", "{}"])
def test_invalid_rows_are_revalidated_and_never_memoized(payload):
    memo = TestMetadataMemo()
    decoder = Mock(side_effect=Store.decode_token_payload)
    for _ in range(2):
        with pytest.raises((ValueError, KeyError, TypeError)):
            memo.decode("1", payload, decoder)
    assert decoder.call_count == 2 and memo.info().entries == 0


def test_identity_mismatch_is_rejected_each_time_even_after_a_valid_row_is_cached():
    memo = TestMetadataMemo()
    token = token_set(1)[0]
    decoder = Mock(side_effect=Store.decode_token_payload)
    payload = encoded(token)
    keep = memo.decode(token.token_id, payload, decoder)
    for _ in range(2):
        with pytest.raises(ValueError, match="身份与主键"):
            memo.decode("another-id", payload, decoder)
    assert decoder.call_count == 3 and memo.info().entries == 1 and keep == token


def test_mutable_legacy_descriptive_fields_keep_independent_original_decoding():
    memo = TestMetadataMemo()
    data = asdict(token_set(1)[0])
    data["question"] = {"legacy": ["mutable"]}
    payload = json.dumps(data)
    decoder = Mock(side_effect=Store.decode_token_payload)
    first = memo.decode("1", payload, decoder)
    second = memo.decode("1", payload, decoder)
    assert first == second and first is not second
    first.question["legacy"].append("only first")
    assert second.question == {"legacy": ["mutable"]}
    assert decoder.call_count == 2 and memo.info().entries == 0


def test_a_future_non_frozen_token_type_automatically_uses_original_decoding(monkeypatch):
    memo = TestMetadataMemo()
    token = token_set(1)[0]
    monkeypatch.setattr(Token, "__dataclass_params__", SimpleNamespace(frozen=False))
    first = memo.decode("1", encoded(token), Store.decode_token_payload)
    second = memo.decode("1", encoded(token), Store.decode_token_payload)
    assert first == second and first is not second
    assert memo.info().entries == 0


def test_entry_and_byte_budgets_keep_live_subset_instead_of_scan_thrashing():
    tokens = token_set(3)
    memo = TestMetadataMemo(max_entries=2)
    values = [memo.decode(t.token_id, encoded(t), Store.decode_token_payload) for t in tokens]
    assert memo.info().entries == 2 and memo.info().admission_rejects == 1
    for token, value in zip(tokens[:2], values):
        assert memo.decode(token.token_id, encoded(token), Store.decode_token_payload) is value
    assert (
        memo.decode(tokens[2].token_id, encoded(tokens[2]), Store.decode_token_payload)
        is not values[2]
    )
    assert memo.info().hits == 2
    assert memo.info().bytes <= memo.max_bytes
    small = TestMetadataMemo(max_bytes=1)
    assert small.decode("1", encoded(tokens[0]), Store.decode_token_payload) == tokens[0]
    assert small.info().entries == 0 and small.info().bytes == 0
    assert MAX_CACHE_BYTES == 32 * 1024**2 and MAX_CACHE_ENTRIES == 32_768


def test_byte_budget_rejects_admission_without_evicting_a_live_value():
    first, second = token_set(2)
    reference = TestMetadataMemo()
    keep = reference.decode("1", encoded(first), Store.decode_token_payload)
    memo = TestMetadataMemo(max_bytes=reference.info().bytes + 1)
    retained = memo.decode("1", encoded(first), Store.decode_token_payload)
    rejected = memo.decode("2", encoded(second), Store.decode_token_payload)
    assert memo.info().entries == 1 and memo.info().admission_rejects == 1
    assert memo.info().bytes <= memo.max_bytes
    assert retained == keep and rejected == second


def test_memo_does_not_keep_tokens_alive_and_dead_values_release_keys_and_budget():
    memo = TestMetadataMemo(max_entries=1)
    token = token_set(1)[0]
    decoded = memo.decode("1", encoded(token), Store.decode_token_payload)
    observed = weakref.ref(decoded)
    assert memo.info().entries == 1 and memo.info().bytes > 0
    del decoded
    gc.collect()
    assert observed() is None
    assert memo.info().entries == 0 and memo.info().bytes == 0


def test_gc_weakref_release_can_reenter_admission_without_deadlock():
    # Keep a regression to a nonreentrant lock bounded to this subprocess.
    # Ordinary allocations may trigger GC at exactly the admission weight call.
    code = r"""
import gc
import json
import weakref
import pm_nautilus.test_metadata as module

gc.disable()
memo = module.TestMetadataMemo()
data = dict(token_id="1", event_id="e", market_id="m", condition_id="c",
            direction="YES", result_count=2, neg_risk=False, opened_ns=0,
            ends_ns=1, fees=dict(enabled=False, rate=0, exponent=1),
            min_size=1, tick=1, category_ids=[], category_labels=[])
first = memo.decode("1", json.dumps(data), json.loads)
observed = weakref.ref(first)
cycle = [first]
cycle.append(cycle)
del first, cycle
assert memo.info().entries == 1 and observed() is not None
original = module._weight
collected = False

def admission_weight(value, seen):
    global collected
    if not collected:
        collected = True
        gc.collect()
        assert observed() is None
    return original(value, seen)

module._weight = admission_weight
data["token_id"] = "2"
second = memo.decode("2", json.dumps(data), json.loads)
assert collected and second.token_id == "2"
assert memo.info().entries == 1 and memo.info().bytes > 0
del second
assert memo.info().entries == 0 and memo.info().bytes == 0
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        env=os.environ | {"PYTHONPATH": "src"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_concurrent_decoders_publish_one_immutable_result():
    memo = TestMetadataMemo()
    token = token_set(1)[0]
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(
            pool.map(
                lambda _: memo.decode("1", encoded(token), Store.decode_token_payload), range(20)
            )
        )
    assert all(value is values[0] for value in values)
    assert memo.info().entries == 1


def test_test_stores_share_only_immutable_values_live_still_uses_original_decoder(
    tmp_path, monkeypatch
):
    memo = TestMetadataMemo()
    monkeypatch.setattr("pm_nautilus.test_metadata._MEMO", memo)
    stores = [
        Store(tmp_path / name, mode) for name, mode in (("1", "TEST"), ("2", "TEST"), ("L", "LIVE"))
    ]
    token = replace(token_set(1)[0], fees=Fees(True, 40_000))
    try:
        for store in stores:
            store.save_token(token)
        changes = [store.db.total_changes for store in stores]
        first, second, live = [store.tokens(active_only=True) for store in stores]
        assert first == second == live
        assert first is not second and first["1"] is second["1"]
        assert live["1"] is not first["1"]
        assert memo.info().hits == 1 and memo.info().misses == 1
        first.clear()
        assert second["1"] == token
        stores[1].save_token(replace(token, tick=10_000))
        assert stores[0].tokens()["1"].tick == 1_000
        assert stores[1].tokens()["1"].tick == 10_000
        assert stores[2].tokens()["1"].tick == 1_000
        assert changes[0] == stores[0].db.total_changes
        assert changes[2] == stores[2].db.total_changes
    finally:
        for store in stores:
            store.close()
