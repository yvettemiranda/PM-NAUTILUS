import pytest
from fastapi.testclient import TestClient

from pm_nautilus.app import create_app
from pm_nautilus.books import Book, Side


def test_non_ascii_credentials_get_401_not_500(tmp_path, monkeypatch):
    monkeypatch.setenv("PM_UI_PASSWORD", "a-long-local-test-password")
    with TestClient(create_app(tmp_path, public_data=False)) as c:
        assert (
            c.get("/api/dashboard", auth=("pm", "错误密码错误密码错误密码错误")).status_code == 401
        )
        assert (
            c.get("/api/dashboard", auth=("用户", "a-long-local-test-password")).status_code == 401
        )


def test_non_ascii_ui_password_can_log_in(tmp_path, monkeypatch):
    password = "交易界面登录密码-至少十六个字符"
    monkeypatch.setenv("PM_UI_PASSWORD", password)
    with TestClient(create_app(tmp_path, public_data=False)) as c:
        assert c.get("/api/dashboard").status_code == 401
        assert c.get("/api/dashboard", auth=("pm", password)).status_code == 200


def test_non_ascii_csrf_header_is_rejected_not_500(tmp_path, monkeypatch):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    with TestClient(create_app(tmp_path, public_data=False)) as c:
        c.get("/api/dashboard")
        response = c.post("/api/test/pause", headers={"x-pm-csrf": "é".encode("latin-1")})
        assert response.status_code == 403


def test_preferences_body_must_be_an_object(tmp_path, monkeypatch):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    with TestClient(create_app(tmp_path, public_data=False)) as c:
        csrf = {"x-pm-csrf": c.get("/api/dashboard").headers["x-pm-csrf"]}
        for mode in ("test", "live"):
            response = c.put(f"/api/{mode}/preferences", json=[1, 2], headers=csrf)
            assert response.status_code == 400
            assert "JSON对象" in response.json()["error"]


def test_dashboard_limit_is_bounded(tmp_path, monkeypatch):
    monkeypatch.delenv("PM_LIVE_ENABLED", raising=False)
    with TestClient(create_app(tmp_path, public_data=False)) as c:
        assert c.get("/api/dashboard?limit=10000").status_code == 200
        assert c.get("/api/dashboard?limit=10001").status_code == 400
        assert c.get("/api/dashboard?limit=0").status_code == 400


def test_invalid_side_update_changes_nothing():
    side = Side()
    side.update([(10_000, 5), (20_000, 7)])
    side.consume(10_000, 2)
    before = (dict(side.external), dict(side.consumed), side.version)
    with pytest.raises(ValueError):
        side.update([(10_000, 0), (30_000, 4), (1_000_000, 1)])
    with pytest.raises(ValueError):
        side.update([(20_000, 9), (40_000, -1)], snapshot=False)
    assert (dict(side.external), dict(side.consumed), side.version) == before


def test_invalid_ask_snapshot_leaves_bids_and_clock_untouched():
    book = Book()
    assert book.snapshot([(10_000, 5)], [(20_000, 5)], 100)
    with pytest.raises(ValueError):
        book.snapshot([(11_000, 9)], [(0, 3)], 200)
    assert book.bid.external == {10_000: 5}
    assert book.ask.external == {20_000: 5}
    assert (book.timestamp, book.revision, book.ready) == (100, 1, True)
