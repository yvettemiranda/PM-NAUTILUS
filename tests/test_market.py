import asyncio
from copy import deepcopy
from dataclasses import replace
import httpx
from pm_nautilus.market import normalize, resolution, MarketService
from test_runtime import setup


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
    requests = []

    def handler(req):
        if "/tags/" in str(req.url):
            return httpx.Response(
                200,
                json=[]
                if "filtered" in str(req.url)
                else {"id": "64", "slug": "esports", "label": "Esports"},
            )
        offset = int(req.url.params["offset"])
        requests.append(offset)
        count = 100 if offset < 300 else 3
        return httpx.Response(200, json=[event(i + offset) for i in range(count)])

    async def run():
        s = MarketService(r, httpx.MockTransport(handler))
        s.sync_subscriptions = lambda: asyncio.sleep(0)
        await s.scan()
        assert not s.scan_status.get("lastError")
        assert requests == [0, 100, 200, 300]
        await s.close()

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
