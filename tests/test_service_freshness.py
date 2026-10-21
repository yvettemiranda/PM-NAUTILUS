"""Query freshness is observable without pausing trading or exposing holdings."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from pm_nautilus.app import create_app
from pm_nautilus.service_freshness import freshness_view


NOW = datetime(2026, 10, 21, 8, tzinfo=timezone.utc)


def query_state(*, now=NOW, age=30, failed=0, failure_age=None, **extra):
    return {
        "enabled": True,
        "monitoringSince": (now - timedelta(hours=1)).isoformat(),
        "lastAttemptAt": (now - timedelta(seconds=2)).isoformat(),
        "lastSuccessAt": (now - timedelta(seconds=age)).isoformat(),
        "consecutiveFailures": failed,
        "failureSince": (now - timedelta(seconds=failure_age)).isoformat()
        if failure_age is not None
        else None,
        "error": "HTTPStatusError" if failed else None,
        "affectedMarketCount": 1 if failed else 0,
        "checkedMarketCount": 2,
        **extra,
    }


def test_brief_failure_then_sustained_failure_and_recovery():
    assert freshness_view(query_state(), now=NOW)["status"] == "ok"
    brief = query_state(failed=1, failure_age=10)
    assert freshness_view(brief, now=NOW)["status"] == "retrying"
    assert freshness_view(query_state(failed=3, failure_age=59), now=NOW)["status"] == "retrying"
    sustained = freshness_view(query_state(failed=3, failure_age=60), now=NOW)
    assert sustained["status"] == "attention" and sustained["reason"] == "query_failed"
    assert sustained["affectedMarketCount"] == 1
    recovered = freshness_view(query_state(age=0), now=NOW)
    assert recovered["status"] == "ok" and recovered["error"] is None
    assert recovered["failureSince"] is None and recovered["consecutiveFailures"] == 0


def test_staleness_includes_never_completed_first_check_without_warn_on_normal_archive_poll():
    # Archived rights normally recheck every five minutes, without looking stale.
    assert freshness_view(query_state(age=301), now=NOW)["status"] == "ok"
    starting = query_state(lastSuccessAt=None, monitoringSince=NOW.isoformat(), pendingQueryCount=1)
    assert freshness_view(starting, now=NOW)["status"] == "starting"
    stalled = freshness_view(starting, now=NOW + timedelta(seconds=900))
    assert stalled["status"] == "attention" and stalled["reason"] == "stale"
    assert stalled["pendingQueryCount"] == 1
    assert freshness_view(query_state(age=900), now=NOW)["status"] == "attention"
    assert freshness_view(query_state(age=900, enabled=False), now=NOW)["status"] == "off"
    assert (
        freshness_view(query_state(age=900, storageDeferred=True), now=NOW)["status"] == "deferred"
    )


def test_projection_allowlists_fields_errors_counts_and_timestamps():
    private = query_state(
        error="https://rpc.private.example?token=private",
        marketId="private-market",
        tokenId="private-token",
        wallet="private-wallet",
        amount="private-funds",
        lastAttemptAt="private-raw-exception",
        consecutiveFailures=True,
        affectedMarketCount=-1,
        pendingQueryCount="private",
    )
    view = freshness_view(private, now=NOW)
    assert "private" not in str(view)
    assert view["error"] == "query_failed" and view["lastAttemptAt"] is None
    assert view["consecutiveFailures"] == view["affectedMarketCount"] == 0
    assert view["pendingQueryCount"] == 0
    assert (
        freshness_view({"enabled": True, "monitoringSince": "2026-10-21"}, now=NOW)["status"]
        == "starting"
    )


def test_new_owned_market_gets_its_own_grace_without_masking_stale_older_coverage():
    state = query_state(
        lastSuccessAt=None,
        monitoringSince=(NOW - timedelta(days=3)).isoformat(),
        # Existing rights succeeded 30 seconds ago, new rights are just watched.
        freshnessSince=(NOW - timedelta(seconds=30)).isoformat(),
        pendingQueryCount=1,
    )
    assert freshness_view(state, now=NOW)["status"] == "starting"
    assert freshness_view(state, now=NOW)["secondsSinceSuccess"] == 30
    state["freshnessSince"] = (NOW - timedelta(seconds=900)).isoformat()
    assert freshness_view(state, now=NOW)["status"] == "attention"


def test_public_health_and_dashboard_report_concern_recovery_without_pausing(tmp_path, monkeypatch):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    app = create_app(tmp_path, public_data=False)
    with TestClient(app) as client:
        service = app.state.services["TEST"]
        runtime = app.state.runtimes["TEST"]
        before = client.get("/api/health").json()["strategyStatus"]
        now = datetime.now(timezone.utc)
        service.discovery_status = query_state(now=now)
        service.resolution_status = query_state(now=now, failed=1, failure_age=10)
        runtime.business["settlement_error"] = "private-historical-error"
        response = client.get("/api/health")
        assert response.status_code == 200 and response.json()["status"] == "ok"
        assert response.json()["serviceFreshness"]["TEST"]["settlement"]["status"] == "retrying"
        assert "private" not in response.text

        service.resolution_status = query_state(now=now, failed=3, failure_age=90)
        response = client.get("/api/health")
        assert response.status_code == 200 and response.json()["status"] == "attention"
        assert response.json()["backgroundErrors"] == {}
        assert response.json()["marketData"]["TEST"]["status"] == "ok"
        data = client.get("/api/dashboard?mode=TEST&details=summary").json()
        assert data["serviceFreshness"]["settlement"]["status"] == "attention"
        assert client.get("/api/health").json()["strategyStatus"] == before

        service.resolution_status = query_state(now=now, age=0)
        assert client.get("/api/health").json()["status"] == "ok"
        # Observe the live shared source while its first scan is still pending.
        service.hub = SimpleNamespace(
            discovery_status=query_state(now=now, age=901, lastSuccessAt=None),
            diagnostics_for=lambda _service: {"groups": []},
        )
        try:
            response = client.get("/api/health")
            assert response.json()["status"] == "attention"
            assert response.json()["serviceFreshness"]["TEST"]["discovery"]["reason"] == "stale"
        finally:
            service.hub = None
