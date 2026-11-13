"""The page can omit unused global diagnostics without changing visible data."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import pm_nautilus.views as views
from pm_nautilus.app import create_app
from pm_nautilus.books import Book
from test_runtime import setup
from test_token_storage import token


def without_update_time(data):
    data = deepcopy(data)
    data.pop("updatedAt")
    return data


@pytest.mark.parametrize("mode", ["TEST", "LIVE"])
@pytest.mark.parametrize("direction", ["ASC", "DESC"])
@pytest.mark.parametrize("limit", [0, 2, 20])
def test_compact_diagnostics_preserves_rows_money_counts_sort_and_errors(
    tmp_path, mode, direction, limit
):
    runtime, _, first = setup(tmp_path)
    try:
        runtime.book("1", [(15_000, 100_000_000)], [(20_000, 50_000_000)])
        runtime.start()
        runtime.pause()
        runtime.add_tokens(
            [
                replace(
                    first,
                    token_id=str(n),
                    event_id=f"event-{n}",
                    condition_id=f"condition-{n}",
                    ends_ns=first.ends_ns + n * 60_000_000_000,
                )
                for n in range(2, 10)
            ]
        )
        for n in range(2, 10):
            runtime.books[str(n)] = Book(ready=n % 2 == 0)
        runtime.evaluations = {eid: ("INCOMPLETE", None) for eid in runtime.events}
        runtime.evaluations[first.event_id] = ("READY", SimpleNamespace(token=first))
        runtime.preferences = runtime.preferences.model_copy(
            update={"candidateSortDirection": direction}
        )
        # Exercise the LIVE read projection without a private executor or venue.
        runtime.mode = mode
        streams = {
            "groups": [
                {
                    "state": "AWAITING_BOOKS",
                    "error": "ConnectionError",
                    "lastPongAt": "2026-11-13T00:00:00+00:00",
                    "tokenCount": 9,
                    "readyBookCount": 5,
                    "missingTokenIds": ["3", "5", "7", "9"],
                    "taskRunning": True,
                }
            ],
            "groupCount": 1,
        }
        service = SimpleNamespace(
            categories=[{"id": "category", "label": "Example"}],
            scan_status={"streamError": "ConnectionError", "lastStreamError": "retrying"},
            stream_diagnostics=lambda: streams,
        )
        before = (deepcopy(runtime.business), set(runtime.dirty), runtime.store.db.total_changes)
        full = views.dashboard(runtime, service, limit=limit, position_limit=1)
        explicit_full = views.dashboard(
            runtime, service, limit=limit, position_limit=1, compact_diagnostics=False
        )
        compact = views.dashboard(
            runtime, service, limit=limit, position_limit=1, compact_diagnostics=True
        )

        assert without_update_time(explicit_full) == without_update_time(full)
        pending = full["marketScan"]["diagnostics"].pop("pendingEvents")
        assert len(pending) == (8 if limit else 0)
        assert without_update_time(compact) == without_update_time(full)
        assert compact["marketScan"]["diagnostics"]["streams"] == streams
        assert (runtime.business, runtime.dirty, runtime.store.db.total_changes) == before
    finally:
        runtime.mode = "TEST"
        runtime.close()


def test_compact_market_page_only_reads_visible_candidate_book_details(tmp_path, monkeypatch):
    runtime, _, first = setup(tmp_path)
    try:
        runtime.add_tokens(
            [replace(first, token_id=str(n), event_id=f"event-{n}") for n in range(2, 102)]
        )
        runtime.evaluations = {eid: ("INCOMPLETE", None) for eid in runtime.events}
        read = []
        original = views.observed_book_reader

        def observed(owner):
            lookup = original(owner)

            def lookup_and_record(token_id):
                read.append(token_id)
                return lookup(token_id)

            return lookup_and_record

        monkeypatch.setattr(views, "observed_book_reader", observed)
        compact = views.dashboard(runtime, limit=2, position_limit=0, compact_diagnostics=True)
        visible = {
            outcome["tokenId"]
            for event in compact["marketScan"]["events"]
            for outcome in event["outcomes"]
        }
        assert set(read) == visible and len(visible) == 2
        assert compact["marketScan"]["pendingEventCount"] == 101
        assert "pendingEvents" not in compact["marketScan"]["diagnostics"]
        assert not runtime.books  # Diagnostics must not load persisted TEST books.

        read.clear()
        full = views.dashboard(runtime, limit=2, position_limit=0)
        assert set(read) == set(runtime.tokens)
        assert len(full["marketScan"]["diagnostics"]["pendingEvents"]) == 101
    finally:
        runtime.close()


@pytest.mark.parametrize("details", ["all", "market", "positions", "summary"])
def test_api_compact_diagnostics_is_explicit_and_keeps_existing_page_contract(
    tmp_path, monkeypatch, details
):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    monkeypatch.delenv("PM_UI_PASSWORD", raising=False)
    monkeypatch.setenv("PM_TEST_ACTIVE_ACCOUNT", "1")
    app = create_app(tmp_path, public_data=False)
    with TestClient(app) as client:
        client.portal.call(
            app.state.test_runtimes[1].add_tokens,
            [token(str(n), event_id=f"event-{n}") for n in range(1, 9)],
        )
        path = f"/api/dashboard?account_id=1&details={details}&limit=2&position_limit=1"
        full_response = client.get(path)
        compact_response = client.get(path + "&compact_diagnostics=true")
        false_response = client.get(path + "&compact_diagnostics=false")
        assert full_response.status_code == compact_response.status_code == 200
        assert false_response.status_code == 200
        full, compact, explicit_full = (
            response.json() for response in (full_response, compact_response, false_response)
        )
        assert full["accountId"] == compact["accountId"] == explicit_full["accountId"] == 1
        assert full["details"] == compact["details"] == details
        assert full["marketScan"] == explicit_full["marketScan"]
        full["marketScan"]["diagnostics"].pop("pendingEvents")
        for field in ("marketScan", "positions", "positionCount", "portfolio", "preferences"):
            assert compact[field] == full[field]
