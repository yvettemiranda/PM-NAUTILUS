from fastapi.testclient import TestClient

from pm_nautilus.app import create_app
from pm_nautilus.performance import DAY_MS, HOUR_MS, PerformanceSampler
from pm_nautilus.strategy import Runtime
from test_runtime import setup


def test_observed_pnl_unknown_books_no_bid_and_restart(tmp_path):
    r, clock, _ = setup(tmp_path)
    r.book("1", [(15000, 100_000_000)], [(20000, 50_000_000)])
    r.start()
    r.pause()
    sampler = PerformanceSampler(r)
    before_events = len(r.store.events())
    sampler.sample(1000)
    assert sampler.view()["points"][-1]["pnl"] == "-0.25"
    r.disconnect(["1"])
    sampler.sample(61000)
    assert sampler.view()["points"][-1]["pnl"] is None
    r.book("1", [], [(20000, 50_000_000)])
    sampler.sample(121000)
    assert sampler.view()["points"][-1]["pnl"] == "-1"
    assert len(r.store.events()) == before_events
    session = sampler.session
    r.close()
    r = Runtime(tmp_path / "test.sqlite", clock=clock)
    sampler = PerformanceSampler(r)
    assert sampler.session != session
    assert len(sampler.view()["points"]) == 1
    sampler.sample(181000)
    assert sampler.view()["points"][-1]["pnl"] is None
    assert r.validate()["ok"]
    r.close()


def test_hourly_daily_history_and_discontinuities(tmp_path):
    r, _, _ = setup(tmp_path)
    sampler = PerformanceSampler(r)
    generation = r.store.generation
    rows = [(generation, "first", minute * 60_000, 1_000_000, 101_000_000) for minute in range(61)]
    rows.extend(
        [
            (generation, "first", HOUR_MS + 60_000, None, None),
            (generation, "first", HOUR_MS + 120_000, 2_000_000, 102_000_000),
            (generation, "first", 2 * DAY_MS, 3_000_000, 103_000_000),
            (generation, "second", 35 * DAY_MS, 4_000_000, 104_000_000),
            ("another-generation", "other", 36 * DAY_MS, 999_000_000, 999_000_000),
        ]
    )
    r.store.db.executemany(
        "INSERT INTO equity_samples(generation,session,at_ms,pnl,total) VALUES(?,?,?,?,?)", rows
    )
    view = sampler.view()
    hourly, daily = view["series"]["H"], view["series"]["D"]
    assert view["points"] == hourly
    assert [p["bucket"] for p in hourly] == [0, 1, 48, 840]
    assert [p["pnl"] for p in daily] == ["2", "3", "4"]
    assert hourly[1]["breakBefore"] is True
    assert daily[0]["breakBefore"] is True
    assert daily[1]["breakBefore"] is True
    assert daily[-1]["session"] == "second"

    r.store.db.execute(
        "INSERT INTO equity_samples(generation,session,at_ms,pnl,total) VALUES(?,?,?,?,?)",
        (generation, "second", 36 * DAY_MS, 5_000_000, 105_000_000),
    )
    refreshed = sampler.view()["series"]["D"]
    assert [p["pnl"] for p in refreshed] == ["2", "3", "4", "5"]
    r.close()


def test_performance_mode_generation_reset_and_read_only_get(tmp_path, monkeypatch):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    app = create_app(tmp_path, public_data=False)
    with TestClient(app) as c:
        response = c.get("/api/dashboard")
        csrf = {"x-pm-csrf": response.headers["x-pm-csrf"]}
        sampler = app.state.samplers["TEST"]
        c.portal.call(sampler.sample, 1000)
        before = c.portal.call(lambda: sampler.runtime.store.db.total_changes)
        initial = c.get("/api/TEST/performance").json()
        assert initial["points"][-1]["pnl"] == "0"
        assert c.get("/api/LIVE/performance").json()["points"] == []
        assert c.portal.call(lambda: sampler.runtime.store.db.total_changes) == before
        assert c.get("/api/OTHER/performance").status_code == 404
        old_generation = initial["generation"]
        assert (
            c.post(
                "/api/test/reset",
                headers=csrf,
                json={"confirmation": "RESET TEST", "finalConfirmation": "RESET TEST AGAIN"},
            ).status_code
            == 200
        )
        current = c.get("/api/test/performance").json()
        assert current["generation"] != old_generation
        assert all(p["pnl"] == "0" for p in current["points"])
        # Previous analytics are retained but never mixed into the new generation.
        assert (
            c.portal.call(
                lambda: app.state.runtimes["TEST"]
                .store.db.execute(
                    "SELECT count(*) FROM equity_samples WHERE generation=?", (old_generation,)
                )
                .fetchone()[0]
            )
            >= 1
        )
