"""Native cleanup allocation must preserve deployed ownership and purge order."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
import tracemalloc

import pytest

from pm_nautilus.native import instrument_id
from pm_nautilus.strategy import Runtime, TokenRegistry
from pm_nautilus.test_runtime import TestRuntime

from test_hot_path_efficiency import drain_variant, quote_both, runtime, tokens


def reference_release(self):
    # Frozen a408b83 implementation; keep this oracle independent of production.
    # Called only after obsolete feed tasks have been cancelled and awaited.
    # Historical execution instruments stay available for native accounting.
    historical = self.store.historical_token_ids()
    for tid in self.books.keys() - self.monitored:
        del self.books[tid]
        self._preview_cache.pop(tid, None)
    for iid, tid in list(self.instrument_tokens.items()):
        if tid not in self.monitored and tid not in historical:
            self.native.cache.purge_instrument(instrument_id(self.tokens[tid]))
            del self.instrument_tokens[iid]
    # Every full subscription plan calls this for every account, several
    # times a second while a discovery scan is in flight. Collect only the
    # stale IDs from the live mapping: copying all (id, token) pairs first
    # allocated tens of thousands of tuples per call, which kept one server
    # core busy for minutes per scan (largely garbage collection). The same
    # tokens are removed in the same order.
    stale = [
        tid
        for tid, token in self.tokens.items()
        if not token.open and tid not in self.monitored and tid not in historical
    ]
    for tid in stale:
        del self.tokens[tid]


def controlled_owner(failure=None):
    base = tokens()[0]
    registry = TokenRegistry(
        {
            str(i): replace(base, token_id=str(i), condition_id=f"condition-{i}", open=i == 4)
            for i in range(1, 7)
        }
    )
    ids = {tid: str(instrument_id(t)) for tid, t in registry.items()}
    cached = set(ids.values())
    calls = []

    def purge(iid):
        calls.append(str(iid))
        if failure == "purge" and str(iid) == ids["4"]:
            raise RuntimeError("controlled native purge failure")
        cached.discard(str(iid))

    owner = SimpleNamespace(
        store=SimpleNamespace(historical_token_ids=lambda: {"3", "5"}),
        tokens=registry,
        monitored={"1"},
        books=dict.fromkeys(registry, "book"),
        _preview_cache=dict.fromkeys(registry, "preview"),
        # Deliberately differ from token order: purge must follow this mapping.
        instrument_tokens={ids[tid]: tid for tid in ("2", "1", "4", "3", "5", "6")},
        native=SimpleNamespace(cache=SimpleNamespace(purge_instrument=purge)),
    )
    if failure == "missing_metadata":
        del registry["4"]
    return owner, calls, cached, ids


def owner_snapshot(owner, calls, cached, error):
    return (
        list(owner.instrument_tokens.items()),
        list(owner.tokens.items()),
        deepcopy(owner.tokens._conditions),
        list(owner.books),
        list(owner._preview_cache),
        calls,
        cached,
        error,
    )


@pytest.mark.parametrize("failure", [None, "purge", "missing_metadata"])
def test_release_matches_frozen_purge_order_and_partial_failure(failure):
    outcomes = []
    for release in (reference_release, Runtime.release_unmonitored):
        owner, calls, cached, ids = controlled_owner(failure)
        error = None
        try:
            release(owner)
        except (RuntimeError, KeyError) as exc:
            error = type(exc), str(exc)
        outcomes.append(owner_snapshot(owner, calls, cached, error))
        assert ids["1"] in owner.instrument_tokens  # Still monitored.
        assert {ids["3"], ids["5"]} <= owner.instrument_tokens.keys()  # All history.
        assert calls[:1] == [ids["2"]]
        if failure:
            assert error is not None
            assert ids["2"] not in owner.instrument_tokens
            assert {ids["4"], ids["6"]} <= owner.instrument_tokens.keys()
        else:
            assert calls == [ids["2"], ids["4"], ids["6"]]
            assert set(owner.tokens) == {"1", "3", "4", "5"}  # Open metadata remains.
    assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize("runtime_type", [Runtime, TestRuntime])
@pytest.mark.parametrize("close_position", [False, True])
def test_release_keeps_native_execution_history_and_publishes_no_callbacks(
    tmp_path, runtime_type, close_position
):
    outcomes = []
    for index, release in enumerate((reference_release, Runtime.release_unmonitored)):
        with drain_variant(False):
            owner, _ = runtime(tmp_path / str(index) / "state.sqlite", runtime_type)
            try:
                quote_both(owner, 0)
                owner.start()
                owner.pause()
                if close_position:
                    for tid in ("00", "01"):
                        owner.book(tid, [(500_000, 1_000_000_000)], [(510_000, 100_000_000)])
                    assert not owner.business["cycles"]
                else:
                    assert owner.business["cycles"]
                protected = owner.store.historical_token_ids()
                assert protected
                unused = owner.tokens["10"]
                owner.native.ensure_instrument(unused)
                owner.add_tokens([replace(token, open=False) for token in owner.tokens.values()])
                owner.monitored.clear()  # Exercise history protection independently of feeds.
                financial = deepcopy((owner.business, owner.store.intents(), owner.cash()))
                journal = list(owner.store.events())
                callbacks = Mock()
                owner.native.bus.subscribe("*", callbacks)
                release(owner)
                callbacks.assert_not_called()
                assert (owner.business, owner.store.intents(), owner.cash()) == financial
                assert owner.store.events() == journal
                for tid in protected:
                    iid = instrument_id(owner.tokens[tid])
                    assert owner.instrument_tokens[str(iid)] == tid
                    assert owner.native.cache.instrument(iid) is not None
                assert unused.token_id not in owner.tokens
                assert str(instrument_id(unused)) not in owner.instrument_tokens
                assert owner.native.cache.instrument(instrument_id(unused)) is None
                # Runtime instance UUIDs differ between the independent runs;
                # the full values above must still stay unchanged by cleanup.
                for intent in financial[1].values():
                    intent.pop("generation", None)
                outcomes.append(
                    (
                        list(owner.instrument_tokens.items()),
                        list(owner.tokens.items()),
                        sorted(str(item.id) for item in owner.native.cache.instruments()),
                        financial,
                    )
                )
                owner.native.bus.unsubscribe("*", callbacks)
            finally:
                owner.close()
    assert outcomes[0] == outcomes[1]


def peak_bytes(call):
    tracemalloc.start()
    try:
        before, _ = tracemalloc.get_traced_memory()
        call()
        _, peak = tracemalloc.get_traced_memory()
        return peak - before
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize("runtime_type", [Runtime, TestRuntime])
def test_no_stale_instruments_does_not_copy_12719_live_mapping_pairs(tmp_path, runtime_type):
    owner, _ = runtime(tmp_path / "state.sqlite", runtime_type)
    try:
        base = tokens()[0]
        owner.tokens.clear()
        owner.tokens.update({str(i): replace(base, token_id=str(i)) for i in range(12_719)})
        owner.monitored = set(owner.tokens)
        owner.instrument_tokens = {
            str(instrument_id(token)): tid for tid, token in owner.tokens.items()
        }
        expected = list(owner.instrument_tokens.items())
        assert peak_bytes(lambda: reference_release(owner)) > 512 * 1024
        assert peak_bytes(owner.release_unmonitored) < 64 * 1024
        assert list(owner.instrument_tokens.items()) == expected
        assert set(owner.tokens) == owner.monitored
    finally:
        owner.close()
