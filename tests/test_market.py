import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from pm_nautilus.market import normalize, resolution, MarketService
from test_runtime import setup
from websockets.exceptions import ConnectionClosedError


def event(i=1):
    return {
        "id": str(i),
        "active": True,
        "closed": False,
        "archived": False,
        "negRisk": False,
        "startDate": "2026-09-01T00:00:00Z",
        "endDate": "2026-09-11T00:00:00Z",
        "tags": [],
        "markets": [
            {
                "id": str(i),
                "active": True,
                "closed": False,
                "archived": False,
                "acceptingOrders": True,
                "enableOrderBook": True,
                "conditionId": "0x" + "a" * 64,
                "clobTokenIds": ["1", "2"],
                "outcomes": ["Yes", "No"],
                "feesEnabled": False,
                "orderPriceMinTickSize": "0.001",
                "orderMinSize": "5",
            }
        ],
    }


def category_response(req):
    if req.url.path == "/tags/102982/related-tags/tags":
        assert req.url.params["status"] == "active"
        assert req.url.params["omit_empty"] == "true"
        return httpx.Response(200, json=[{"id": "21", "slug": "crypto", "label": "Crypto"}])
    if req.url.path == "/tags/slug/esports":
        return httpx.Response(200, json={"id": "64", "slug": "esports", "label": "Esports"})
    if req.url.path == "/tags/slug/art":
        return httpx.Response(200, json={"id": "1422", "slug": "art", "label": "Art"})
    return None


def test_metadata_complete_n_and_final_resolution():
    e = event()
    assert len(normalize(e)) == 2
    del e["markets"][0]["feesEnabled"]
    assert normalize(e) == []
    e = event()
    e["negRisk"] = True
    e["markets"] *= 3
    e["markets"] = deepcopy(e["markets"])
    e["markets"][1]["closed"] = True
    assert all(t.result_count == 3 for t in normalize(e))
    e["negRiskAugmented"] = True
    assert normalize(e) == []
    e = event()
    t = normalize(e)[0]
    m = e["markets"][0]
    m.update(outcomePrices=["1", "0"], umaResolutionStatus="proposed", closed=True)
    assert resolution(m, t) is None
    m["umaResolutionStatus"] = "resolved"
    assert resolution(m, t) == {"1": 1_000_000, "2": 0}
    m["outcomePrices"] = ["0.5", "0.5"]
    assert resolution(m, t) == {"1": 500_000, "2": 500_000}


def test_full_pagination_without_hidden_limit(tmp_path):
    r, _, _ = setup(tmp_path)
    cursors = []

    def handler(req):
        response = category_response(req)
        if response is not None:
            return response
        assert req.url.path == "/events/keyset"
        assert req.url.params["active"] == "true"
        assert req.url.params["closed"] == "false"
        assert req.url.params["limit"] == "100"
        assert "offset" not in req.url.params
        cursor = req.url.params.get("after_cursor")
        cursors.append(cursor)
        page = {
            None: (0, "cursor-100"),
            "cursor-100": (100, "cursor-200"),
            "cursor-200": (200, "cursor-300"),
            "cursor-300": (300, None),
        }
        offset, next_cursor = page[cursor]
        count = 100 if offset < 300 else 3
        return httpx.Response(
            200,
            json={"events": [event(i + offset) for i in range(count)], "next_cursor": next_cursor},
        )

    async def run():
        s = MarketService(r, httpx.MockTransport(handler))
        s.sync_subscriptions = lambda: asyncio.sleep(0)
        s.scan_status["categoryError"] = "HTTPStatusError"
        await s.scan()
        assert not s.scan_status.get("lastError")
        assert not s.scan_status.get("categoryError")
        assert cursors == [None, "cursor-100", "cursor-200", "cursor-300"]
        assert s.categories == [
            {"id": "21", "label": "Crypto"},
            {"id": "64", "label": "Esports"},
            {"id": "1422", "label": "Art"},
        ]
        await s.close()

    asyncio.run(run())
    r.close()


def test_keyset_cursor_must_advance(tmp_path):
    r, _, _ = setup(tmp_path)

    def handler(req):
        response = category_response(req)
        if response is not None:
            return response
        cursor = req.url.params.get("after_cursor")
        offset = 0 if cursor is None else 100
        return httpx.Response(
            200,
            json={"events": [event(i + offset) for i in range(100)], "next_cursor": "stuck"},
        )

    async def run():
        s = MarketService(r, httpx.MockTransport(handler))
        s.sync_subscriptions = lambda: asyncio.sleep(0)
        await s.scan()
        assert s.scan_status["lastError"] == "ValueError: 公开 Event keyset 游标没有前进"
        await s.close()

    asyncio.run(run())
    r.close()


def test_normal_websocket_disconnect_retries_without_pausing(monkeypatch):
    saved = {}
    runtime = SimpleNamespace(
        store=SimpleNamespace(
            get=lambda key, default=None: default,
            put=lambda key, value: saved.update({key: dict(value)}),
        ),
        disconnect=Mock(),
        pause=Mock(),
    )
    service = MarketService(runtime)

    class ClosedSocket:
        async def __aenter__(self):
            raise ConnectionClosedError(None, None)

        async def __aexit__(self, *args):
            return False

    async def stop_after_retry(_):
        raise asyncio.CancelledError

    async def run():
        monkeypatch.setattr("pm_nautilus.market.connect", lambda *args, **kwargs: ClosedSocket())
        monkeypatch.setattr("pm_nautilus.market.asyncio.sleep", stop_after_retry)
        try:
            with pytest.raises(asyncio.CancelledError):
                await service.socket(("1",))
            runtime.pause.assert_not_called()
            assert service.scan_status["streamError"] == "ConnectionClosedError"
            assert service.scan_status["lastStreamError"].startswith("ConnectionClosedError:")
            assert service.scan_status["lastStreamErrorAt"]
            assert saved["scan"]["streamError"] == "ConnectionClosedError"
        finally:
            await service.close()

    asyncio.run(run())


def test_stream_recovery_waits_for_all_books_and_preserves_other_errors(tmp_path, monkeypatch):
    import json
    from pm_nautilus.views import dashboard

    r, _, t = setup(tmp_path)
    r.add_tokens([replace(t, token_id="2", direction="NO")])
    service = MarketService(r)
    service.remember_error("streamError", ConnectionError("old disconnect"))
    service.stream_state(("1", "2"), "RECONNECTING", "ConnectionError")
    service.stream_state(("3",), "RECONNECTING", "TimeoutError")

    class Socket:
        count = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def send(self, _):
            pass

        async def recv(self):
            self.count += 1
            if self.count == 2:
                assert service.streams[("1", "2")]["error"] == "ConnectionError"
                service.streams.pop(("3",))
                d = dashboard(r, service)
                assert d["marketScan"]["diagnostics"]["pendingEvents"][0][
                    "missingBookTokenIds"
                ] == ["2"]
            if self.count == 3:
                assert service.streams[("1", "2")]["state"] == "READY"
                assert service.scan_status["streamError"] is None
                assert "old disconnect" in service.scan_status["lastStreamError"]
                raise asyncio.CancelledError
            return json.dumps(
                {
                    "event_type": "book",
                    "market": "condition",
                    "asset_id": str(self.count),
                    "bids": [],
                    "asks": [],
                    "timestamp": 0,
                }
            )

    async def run():
        monkeypatch.setattr("pm_nautilus.market.connect", lambda *a, **kw: Socket())
        try:
            # One recovered group must never hide another failed group.
            service.stream_state(("1", "2"), "READY")
            assert service.scan_status["streamError"] == "TimeoutError"
            service.stream_state(("1", "2"), "RECONNECTING", "ConnectionError")
            with pytest.raises(asyncio.CancelledError):
                await service.socket(("1", "2"))
            assert not r.books["1"].ready
        finally:
            await service.close()

    asyncio.run(run())
    r.close()


def test_sibling_unknown_blocks_empty_does_not(tmp_path):
    r, _, t = setup(tmp_path)
    r.add_tokens([replace(t, token_id="2", direction="NO")])
    r.book("1", [(15000, 100_000_000)], [(20000, 50_000_000)])
    r.start()
    assert not r.business["cycles"]
    r.book("2", [], [])
    assert r.business["cycles"]["event"]["token_id"] == "1"
    r.close()
