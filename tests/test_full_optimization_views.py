"""Summary and keyset history preserve money, identity and full cost basis."""

import asyncio
from dataclasses import replace

import pytest

import pm_nautilus.views as views
from pm_nautilus.performance import PerformanceSampler
from test_async_projections import append_fill, fill_history
from test_runtime import setup


def test_summary_keeps_all_position_values_and_missing_book_status(tmp_path, monkeypatch):
    r, _, token = setup(tmp_path)
    r.book("1", [(15_000, 100_000_000)], [(20_000, 50_000_000)])
    r.start()
    r.pause()
    second = replace(token, token_id="2", event_id="second", condition_id="second-condition")
    r.add_tokens([second])
    r.business["cycles"]["second"] = r.business["cycles"][token.event_id] | {"token_id": "2"}
    # A hidden position still makes total valuation unknown.
    full_positions, full_portfolio = views.portfolio_view(r)
    assert len(full_positions) == 2 and full_portfolio["totalFunds"] is None
    calls = []
    original = views.token_view

    def observed(owner, t):
        calls.append(t.token_id)
        return original(owner, t)

    monkeypatch.setattr(views, "token_view", observed)
    summary = views.dashboard(r, limit=0, position_limit=0)
    assert summary["positions"] == [] and summary["marketScan"]["events"] == []
    assert summary["positionCount"] == 2 and summary["positionBooksReady"] is False
    assert summary["portfolio"] == full_portfolio and calls == []
    sampler = PerformanceSampler(r)
    sampler.sample(1_000)
    assert calls == []
    assert sampler.view()["points"][0]["total"] is None
    r.close()


def test_keyset_paging_exceeds_10000_without_duplicates_or_losing_late_fills(tmp_path, monkeypatch):
    async def run():
        r, _, _ = setup(tmp_path)
        source, intent = fill_history(r, 10_003)
        monkeypatch.setattr(views, "RECORD_CACHE_ORDERS", 4)
        first = await views.records_page_async(r, 3)
        assert [row["id"] for row in first["records"]] == [
            "HISTORY-10002",
            "HISTORY-10001",
            "HISTORY-10000",
        ]
        assert first["totalCount"] == 10_003
        # Jump beyond the retained cache with a stable first-fill sequence.
        seq = r.store.db.execute("SELECT min(seq) FROM native_events").fetchone()[0]
        old = await views.records_page_async(r, 3, f"{r.store.generation}:{seq + 3}")
        assert [row["id"] for row in old["records"]] == ["HISTORY-2", "HISTORY-1", "HISTORY-0"]
        append_fill(r, source, "HISTORY-0", "late", 10, 200_000)
        r.store.save_intent("new-sell", intent | {"side": "SELL", "kind": "TARGET"})
        append_fill(r, source, "new-sell", "new-sell", 500160, 10_004_300_000, "SELL")
        refreshed = await views.records_page_async(r, 3, since=first["head"])
        assert refreshed["totalCount"] == 10_004
        changed = {row["id"]: row for row in refreshed["changes"]}
        assert changed["HISTORY-0"]["quantity"] == "60"
        assert changed["HISTORY-0"]["firstSeq"] == seq
        assert changed["new-sell"]["type"] == "CLOSE"
        assert changed["new-sell"]["realizedPnl"] == "1.1"
        assert len(r._record_projection.groups) <= 4
        assert len(r._record_page_cache) <= 8
        r.store.reset()
        with pytest.raises(ValueError, match="游标已失效"):
            await views.records_page_async(r, 3, first["nextCursor"])
        assert (await views.records_page_async(r, 3))["totalCount"] == 0
        r.close()

    asyncio.run(run())


def test_summary_and_locked_history_api_keep_existing_access_gates(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from pm_nautilus.app import create_app

    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    with TestClient(create_app(tmp_path, public_data=False)) as client:
        summary = client.get("/api/dashboard?details=summary").json()
        assert summary["details"] == "summary"
        assert summary["positionCount"] == 0 and summary["positionBooksReady"] is True
        assert summary["portfolio"]["totalFunds"] == "100"
        assert client.get("/api/live/trade-records?paged=true").json()["locked"] is True
        assert client.get("/api/test/trade-records?paged=true").json()["totalCount"] == 0
        assert client.get("/api/dashboard?details=invalid").status_code == 400
        assert client.get("/api/test/trade-records?paged=true&cursor=wrong:1").status_code == 400
