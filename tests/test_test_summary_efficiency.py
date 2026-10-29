"""TEST display work stays account-local without caching money or market state."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import pm_nautilus.app as app_module
import pm_nautilus.views as views
from pm_nautilus.service_freshness import freshness_view
from pm_nautilus.test_runtime import TestRuntime
from test_lifecycle import controlled_factory
from test_runtime import setup


@pytest.mark.parametrize("position_limit", [0, 1, None])
@pytest.mark.parametrize("unavailable", [False, True])
def test_test_summary_matches_detailed_response_with_unpriced_owned_rights(
    tmp_path, position_limit, unavailable
):
    runtime, _, token = setup(tmp_path)
    try:
        runtime.book("1", [(15_000, 100_000_000)], [(20_000, 50_000_000)])
        runtime.start()
        runtime.pause()
        missing = replace(token, token_id="2", event_id="missing", condition_id="second")
        runtime.add_tokens([missing])
        runtime.business["cycles"]["missing"] = runtime.business["cycles"][token.event_id] | {
            "token_id": "2"
        }
        if unavailable:
            runtime.market_availability["2"] = {"status": "UNAVAILABLE", "tokenId": "2"}
        runtime.events["empty"] = set()
        runtime.evaluations["ignored"] = ("READY", None)
        before = deepcopy(runtime.business)
        changes = runtime.store.db.total_changes
        detailed = views.dashboard(runtime, limit=100, position_limit=position_limit)
        summary = views.dashboard(runtime, limit=0, position_limit=position_limit)
        # Only candidate details were omitted. Hidden/unpriced positions still
        # participate in the same complete, freshly valued financial response.
        detailed["marketScan"]["events"] = []
        detailed["marketScan"]["diagnostics"]["pendingEvents"] = []
        detailed.pop("updatedAt")
        summary.pop("updatedAt")
        assert summary == detailed
        assert summary["positionCount"] == 2
        assert summary["portfolio"]["totalFunds"] is None
        assert runtime.business == before
        assert runtime.store.db.total_changes == changes
    finally:
        runtime.close()


def test_test_summary_counts_pending_and_unknown_events_without_candidate_book_reads(
    tmp_path, monkeypatch
):
    reference, clock, token = setup(tmp_path / "reference")
    reference.close()
    runtime = TestRuntime(tmp_path / "test.sqlite", clock=clock)
    try:
        tokens = [
            replace(token, token_id=str(number), event_id=str(number)) for number in range(1, 5)
        ]
        runtime.add_tokens(tokens)
        runtime.events["empty"] = set()
        runtime.evaluations.update({"1": ("READY", None), "2": ("NO_WINNER", None)})
        runtime.evaluations["4"] = ("READY", None)
        runtime._test_preview_pending = {"1"}
        books = runtime.books

        class UnreadBooks(dict):
            def __getitem__(self, key):
                raise AssertionError("summary inspected an unowned candidate book")

        runtime.books = UnreadBooks(books)
        monkeypatch.setattr(
            runtime,
            "refresh_test_market_preview",
            lambda: pytest.fail("summary evaluated candidates instead of reporting pending state"),
        )
        first = views.dashboard(runtime, limit=0, position_limit=0)["marketScan"]
        assert (first["eventCount"], first["displayEventCount"], first["pendingEventCount"]) == (
            1,
            4,
            2,
        )
        assert first["events"] == first["diagnostics"]["pendingEvents"] == []
        runtime._test_preview_pending.clear()
        runtime.evaluations["3"] = ("READY", None)
        second = views.dashboard(runtime, limit=0, position_limit=0)["marketScan"]
        assert (second["eventCount"], second["pendingEventCount"]) == (3, 0)
        runtime.books = books
    finally:
        runtime.close()


def test_test_summary_revalues_owned_books_on_every_read(tmp_path):
    runtime, _, _ = setup(tmp_path)
    try:
        runtime.book("1", [(15_000, 100_000_000)], [(20_000, 50_000_000)])
        runtime.start()
        runtime.pause()
        first = views.dashboard(runtime, limit=0, position_limit=0)
        runtime.books["1"].snapshot([(10_000, 100_000_000)], [(20_000, 50_000_000)], runtime.now)
        second = views.dashboard(runtime, limit=0, position_limit=0)
        assert first["portfolio"]["totalFunds"] == "99.75"
        assert second["portfolio"]["totalFunds"] == "99.5"
        runtime.books["1"].disconnect()
        third = views.dashboard(runtime, limit=0, position_limit=0)
        assert third["portfolio"]["totalFunds"] is None
        assert third["notReadyPositionCount"] == 1
    finally:
        runtime.close()


@pytest.mark.parametrize("account_id", [1, 10])
def test_test_dashboard_projects_only_selected_service_freshness(tmp_path, monkeypatch, account_id):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    monkeypatch.delenv("PM_UI_PASSWORD", raising=False)
    app = app_module.create_app(tmp_path, public_data=False)
    now = datetime(2026, 10, 29, tzinfo=timezone.utc)
    observed = []

    def project(state):
        observed.append(state)
        return freshness_view(state, now=now)

    monkeypatch.setattr(app_module, "freshness_view", project)
    with TestClient(app) as client:
        service = app.state.test_services[account_id]
        shared = SimpleNamespace(
            discovery_status={"enabled": True, "consecutiveFailures": 3, "error": "ReadTimeout"},
            diagnostics_for=lambda _: {"groups": [], "groupCount": 0},
        )
        service.hub = shared
        service.resolution_status = {"enabled": True, "checkedMarketCount": account_id}
        try:
            # Health must still report every account; the selected dashboard's
            # projection must remain identical to its entry in that response.
            key = app.state.test_accounts.key(account_id)
            expected = client.get("/api/health").json()["serviceFreshness"][key]
            assert len(observed) == 20
            observed.clear()
            data = client.get(f"/api/dashboard?details=summary&account_id={account_id}").json()
            assert data["accountId"] == account_id
            assert data["serviceFreshness"] == expected
            assert len(observed) == 2
            assert observed[0] is shared.discovery_status
            assert observed[1] is service.resolution_status
            service.resolution_status = {"enabled": False}
            observed.clear()
            refreshed = client.get(
                f"/api/dashboard?details=positions&account_id={account_id}"
            ).json()
            assert refreshed["serviceFreshness"]["settlement"]["status"] == "off"
            assert len(observed) == 2  # No elapsed-time response cache.
        finally:
            service.hub = None


def test_live_summary_keeps_original_candidate_and_freshness_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("PM_LIVE_ENABLED", "true")
    monkeypatch.setenv("PM_UI_PASSWORD", "controlled-local-password")
    monkeypatch.setattr("pm_nautilus.live.load_settings", lambda: {})
    monkeypatch.setattr(
        "pm_nautilus.redemption.PolygonWallet",
        lambda _: SimpleNamespace(preflight=lambda: None),
    )

    def factory(owner):
        client = controlled_factory(owner)
        client.activate = AsyncMock()
        return client

    monkeypatch.setattr("pm_nautilus.live.live_factory", lambda *_: factory)
    reference, _, token = setup(tmp_path / "reference")
    reference.close()
    app = app_module.create_app(tmp_path / "app", public_data=False)
    observed = []

    def project(state):
        observed.append(state)
        return freshness_view(state)

    monkeypatch.setattr(app_module, "freshness_view", project)
    with TestClient(app) as client:
        client.auth = ("pm", "controlled-local-password")
        live = app.state.runtimes["LIVE"]
        day = 86_400_000_000_000
        client.portal.call(
            live.add_tokens,
            [replace(token, opened_ns=live.now - day, ends_ns=live.now + 9 * day)],
        )
        expected = client.get("/api/health").json()["serviceFreshness"]["LIVE"]
        aggregate_calls = len(observed)
        observed.clear()
        books = live.books
        lookups = []

        class ObservedBooks(dict):
            def __getitem__(self, key):
                lookups.append(key)
                return books[key]

        live.books = ObservedBooks()
        try:
            response = client.get("/api/dashboard?mode=LIVE&details=summary")
            assert response.status_code == 200
            assert response.json()["serviceFreshness"] == expected
            assert len(observed) == aggregate_calls  # Original aggregate path, including LIVE.
            assert lookups == ["1"]  # Original candidate diagnostics path.
            assert response.json()["marketScan"]["events"] == []
            assert live.client._submit_order.await_count == 0
            assert live.client._cancel_order.await_count == 0
        finally:
            live.books = books
