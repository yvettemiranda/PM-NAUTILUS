from copy import deepcopy
from dataclasses import replace

from pm_nautilus.views import dashboard, portfolio_view, token_view
from test_runtime import setup


def unavailable(token, reason="archived"):
    return {
        "status": "UNAVAILABLE",
        "reason": reason,
        "tokenId": token.token_id,
        "marketId": token.market_id,
        "conditionId": token.condition_id,
        "since": "2026-10-20T05:00:00+00:00",
        "checkedAt": "2026-10-20T05:00:00+00:00",
        "lastError": None,
    }


def owned_positions(tmp_path, count=1):
    r, _, first = setup(tmp_path)
    tokens = [first] + [
        replace(
            first,
            token_id=str(n),
            event_id=f"event-{n}",
            market_id=f"market-{n}",
            condition_id=f"condition-{n}",
        )
        for n in range(2, count + 1)
    ]
    r.add_tokens(tokens[1:])
    for token in tokens:
        r.book(token.token_id, [(15_000, 100_000_000)], [(20_000, 50_000_000)])
    r.start()
    r.pause()
    assert len(r.business["cycles"]) == count
    return r, tokens


def test_unavailable_position_masks_even_ready_stale_book_and_preserves_rights(tmp_path):
    r, tokens = owned_positions(tmp_path)
    token = tokens[0]
    assert r.books[token.token_id].ready
    r.market_availability = {token.token_id: unavailable(token)}
    before = deepcopy(r.business)
    changes = r.store.db.total_changes
    positions, portfolio = portfolio_view(r)
    position = positions[0]
    assert position["currentSellPriceStatus"] == "UNAVAILABLE"
    assert position["marketAvailability"] == unavailable(token)
    assert position["currentSellPrice"] is None
    assert position["bestBid"] is None and position["executableBuyPrice"] is None
    assert position["executableSellValue"] is None
    assert position["executableSellQuantity"] is None
    assert position["quantity"] == "50"
    assert portfolio["totalFunds"] is None and portfolio["unrealizedPnl"] is None
    assert portfolio["positionValue"] is None and portfolio["knownPositionValue"] == "0"
    assert portfolio["availableCash"] == "99" and portfolio["realizedPnl"] == "0"
    assert portfolio["valuedPositionCount"] == 0
    assert portfolio["unavailablePositionCount"] == portfolio["unvaluedPositionCount"] == 1
    assert portfolio["notReadyPositionCount"] == 0
    assert r.business == before and r.store.db.total_changes == changes
    position["marketAvailability"]["reason"] = "changed by caller"
    assert r.market_availability[token.token_id]["reason"] == "archived"
    r.close()


def test_hidden_twenty_first_unavailable_position_is_in_summary_and_partial_totals(tmp_path):
    r, tokens = owned_positions(tmp_path, 21)
    r.market_availability = {tokens[-1].token_id: unavailable(tokens[-1], "halted")}
    for position_limit in (0, 20, None):
        view = dashboard(r, limit=0, position_limit=position_limit)
        assert len(view["positions"]) == (21 if position_limit is None else position_limit)
        assert view["positionCount"] == 21
        assert view["positionBooksReady"] is False
        assert view["valuedPositionCount"] == 20
        assert view["unvaluedPositionCount"] == view["unavailablePositionCount"] == 1
        assert view["notReadyPositionCount"] == 0
        portfolio = view["portfolio"]
        assert portfolio["knownPositionValue"] == "15"
        assert portfolio["availableCash"] == "79"
        assert portfolio["totalFunds"] is None
        assert portfolio["positionValue"] is None
        assert portfolio["unrealizedPnl"] is None
    r.close()


def test_unavailable_no_bid_recovering_and_failed_redemption_stay_distinct(tmp_path):
    r, tokens = owned_positions(tmp_path, 3)
    # A ready empty book has a known executable value of zero; missing and
    # unavailable books have unknown values, regardless of older prices.
    r.books[tokens[0].token_id].snapshot([], [(20_000, 50_000_000)], r.now)
    r.books[tokens[1].token_id].ready = False
    r.market_availability = {tokens[2].token_id: unavailable(tokens[2])}
    positions, portfolio = portfolio_view(r)
    assert [p["currentSellPriceStatus"] for p in positions] == [
        "NO_BID",
        "NOT_READY",
        "UNAVAILABLE",
    ]
    assert portfolio["valuedPositionCount"] == 1
    assert portfolio["notReadyPositionCount"] == portfolio["unavailablePositionCount"] == 1
    assert portfolio["unvaluedPositionCount"] == 2
    assert portfolio["knownPositionValue"] == "0"
    assert portfolio["totalFunds"] is None
    r.business["settled"].extend(t.condition_id for t in tokens)
    r.business["claims"][tokens[2].condition_id] = {
        "state": "FAILED",
        "amount": 1_000_000,
        "cost": 1_000_000,
    }
    view = dashboard(r, limit=0, position_limit=0)
    assert view["positionCount"] == view["unavailablePositionCount"] == 0
    assert view["portfolio"]["knownPositionValue"] == "0"
    assert view["portfolio"]["totalFunds"] is None
    assert view["portfolio"]["unrealizedPnl"] is None
    assert view["portfolio"]["pendingRedemption"] == "0"
    assert view["portfolio"]["failedRedemption"] == "1"
    r.close()


def test_old_fixture_without_observational_registry_keeps_normal_projection(tmp_path):
    r, tokens = owned_positions(tmp_path)
    if hasattr(r, "market_availability"):
        del r.market_availability
    position = token_view(r, tokens[0])
    assert position["marketAvailability"] is None
    assert position["currentSellPriceStatus"] == "READY"
    r.close()
