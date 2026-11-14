"""Single-account behavior with the original TEST 1 ledger and retired data."""

from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pm_nautilus.app import create_app
from pm_nautilus.store import Store
from pm_nautilus.test_accounts import TestAccounts
from test_runtime import setup


def test_application_exposes_only_test_one_and_preserves_archived_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("PM_TEST_ACTIVE_ACCOUNT", "1")
    old_accounts = TestAccounts(tmp_path)
    primary = old_accounts.path(1)
    with closing(Store(primary)) as store:
        store.put("preferences", store.get("preferences") | {"orderAmount": "3"})
        store.put("initial_capital", 222_000_000)
        saved_generation = store.generation
    for number in (2, 10):
        with closing(Store(old_accounts.path(number))) as store:
            store.put("preferences", store.get("preferences") | {"orderAmount": str(number)})
            store.put("business", store.get("business") | {"realized": 100 * number})

    def archives():
        return {
            file.relative_to(tmp_path): file.read_bytes()
            for file in (tmp_path / "TEST" / "accounts").rglob("*")
            if file.is_file()
        }

    before = archives()
    assert len(before) == 2
    app = create_app(tmp_path, public_data=False)
    with TestClient(app) as client:
        response = client.get("/api/dashboard")
        data = response.json()
        assert response.status_code == 200
        assert data["accountId"] == data["accountCount"] == 1
        assert data["generation"] == saved_generation
        assert data["strategy"]["initialCapital"] == "222"
        assert data["preferences"]["orderAmount"] == "3"
        health = client.get("/api/health").json()
        assert health["availableTestAccountCount"] == health["testAccountCount"] == 1
        assert health["activeTestAccountId"] == health["loadedTestAccountCount"] == 1
        assert health["coldTestAccountIds"] == []
        assert set(app.state.test_runtimes) == {1}
        assert client.get("/api/TEST/validation?account_id=1").json()["ok"]
        csrf = {"x-pm-csrf": response.headers["x-pm-csrf"]}
        for method, path in (
            ("GET", "/api/dashboard"),
            ("GET", "/api/TEST/trade-records"),
            ("GET", "/api/TEST/performance"),
            ("PUT", "/api/TEST/preferences"),
            ("POST", "/api/TEST/start"),
            ("POST", "/api/TEST/reset"),
        ):
            assert client.request(method, f"{path}?account_id=2", headers=csrf).status_code == 400
    assert archives() == before


def test_fresh_data_dir_creates_no_retired_ledger(tmp_path, monkeypatch):
    monkeypatch.delenv("PM_TEST_ACTIVE_ACCOUNT", raising=False)
    with TestClient(create_app(tmp_path, public_data=False)) as client:
        assert client.get("/api/health").json()["testAccountCount"] == 1
    assert (tmp_path / "TEST" / "state.sqlite").is_file()
    assert not (tmp_path / "TEST" / "accounts").exists()


@pytest.mark.parametrize("unfinished", ["running", "settlement", "wal", "symlink"])
def test_unsafe_retired_data_prevents_startup_before_primary_is_opened(tmp_path, unfinished):
    accounts = TestAccounts(tmp_path)
    archived = accounts.path(4)
    if unfinished == "symlink":
        archived.parent.mkdir(parents=True)
        archived.symlink_to(Path("outside.sqlite"))
    else:
        with closing(Store(archived)) as store:
            if unfinished == "running":
                store.put("status", "RUNNING")
            elif unfinished == "settlement":
                business = store.get("business")
                business["claims"]["condition"] = {"state": "CONFIRMED"}
                store.put("business", business)
        if unfinished == "wal":
            archived.with_name("state.sqlite-wal").write_bytes(b"unknown")
    with pytest.raises(ValueError, match="旧 TEST 4 账本尚不能归档"):
        with TestClient(create_app(tmp_path, public_data=False)):
            pytest.fail("unsafe archive became inaccessible")
    assert not accounts.path(1).exists()
    assert not (tmp_path / "LIVE" / "state.sqlite").exists()


def test_archive_refuses_open_fill_even_if_business_projection_was_cleared(tmp_path):
    accounts = TestAccounts(tmp_path)
    archived = accounts.path(8)
    runtime, _, _ = setup(archived.parent)
    try:
        runtime.book("1", [(15_000, 100_000_000)], [(20_000, 50_000_000)])
        runtime.start()
        assert runtime.store.has_history()
        runtime.pause()
        business = runtime.store.get("business")
        runtime.store.put("business", business | {"cycles": {}, "targets": {}})
    finally:
        runtime.close()
    runtime.store.path.rename(archived)
    assert accounts.cold_eligible(8)
    assert not (tmp_path / "LIVE" / "state.sqlite").exists()
    with pytest.raises(ValueError, match="旧 TEST 8 账本尚不能归档"):
        with TestClient(create_app(tmp_path, public_data=False)):
            pytest.fail("open native position was archived")
