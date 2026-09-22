from fastapi.testclient import TestClient

from pm_nautilus.app import create_app
from pm_nautilus.performance import PerformanceSampler
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
    assert len(sampler.view()["points"]) == 3
    sampler.sample(181000)
    assert sampler.view()["points"][-1]["pnl"] is None
    assert r.validate()["ok"]
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
