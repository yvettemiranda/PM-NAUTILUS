"""Real pinned SDK rounding without a signer or network, shared with TEST."""

from types import SimpleNamespace
import asyncio

import pytest
from nautilus_trader.model.identifiers import ClientOrderId
from py_clob_client_v2.order_builder.builder import OrderBuilder, ROUNDING_CONFIG

from pm_nautilus.order_terms import (
    OrderTermsError,
    check_signed_sell,
    normalize_sell,
    sell_minimum,
)
from pm_nautilus.strategy import Runtime
from pm_nautilus.redemption import RedemptionService
from test_live_recovery import token


@pytest.mark.parametrize("tick", [100_000, 10_000, 1_000, 100])
@pytest.mark.parametrize("floor", [1, 33_001, 963_001])
def test_sell_terms_match_sdk_without_lowering_floor(tick, floor):
    if (floor + tick - 1) // tick * tick > 1_000_000 - tick:
        with pytest.raises(OrderTermsError):
            normalize_sell(5_009_000, floor, tick, 5_000_000)
        return
    terms = normalize_sell(5_009_000, floor, tick, 5_000_000)
    sdk_tick = f"{tick / 1_000_000:g}"
    _, maker, taker = OrderBuilder(None).get_order_amounts(
        "SELL",
        terms.quantity / 1_000_000,
        terms.limit / 1_000_000,
        ROUNDING_CONFIG[sdk_tick],
    )
    assert check_signed_sell(SimpleNamespace(makerAmount=maker, takerAmount=taker), terms)
    assert maker == 5_000_000 and taker * 1_000_000 >= maker * floor
    assert sell_minimum(5_000_001) == 5_010_000


@pytest.mark.parametrize("maker,taker", [(5_010_000, 4_815_000), (5_000_000, 4_800_000)])
def test_signed_sell_rejects_excess_quantity_or_lower_price(maker, taker):
    terms = normalize_sell(5_009_000, 963_000, 1_000, 5_000_000)
    with pytest.raises(OrderTermsError):
        check_signed_sell(SimpleNamespace(makerAmount=maker, takerAmount=taker), terms)


def test_test_sell_leaves_same_rounding_tail_and_restart_preserves_it(tmp_path):
    path = tmp_path / "test-tail.sqlite"
    runtime = Runtime(path)
    t = token(runtime)
    runtime.add_tokens([t])
    runtime.book(t.token_id, [(90_000, 100_000_000)], [(100_000, 5_009_000)])
    runtime.submit(t, "BUY", 5_009_000, 100_000, cash=510_000)
    assert runtime.business["cycles"][t.event_id]["quantity"] == 5_009_000
    # Set execution depth without running the strategy's automatic target exit.
    runtime.books[t.token_id].snapshot(
        [(501_000, 100_000_000)], [(502_000, 100_000_000)], runtime.now
    )
    oid = runtime.submit(t, "SELL", 5_009_000, 500_001, targets=list(runtime.business["targets"]))
    intent = runtime.store.intent(oid)
    assert intent["quantity"] == 5_009_000 and intent["limit"] == 500_001
    order = runtime.native.cache.order(ClientOrderId(oid))
    assert order.quantity.as_double() == 5
    assert order.price.as_double() == 0.501
    assert runtime.business["cycles"][t.event_id]["quantity"] == 9_000
    before = len(runtime.store.intents())
    assert runtime.submit(t, "SELL", 9_000, 500_000) is None
    assert len(runtime.store.intents()) == before
    assert runtime.validate()["ok"]
    runtime.close()
    restored = Runtime(path)
    try:
        assert restored.business["cycles"][t.event_id]["quantity"] == 9_000
        assert restored.validate()["ok"]
        # Chain settlement rights keep their exact precision; they are not CLOB
        # SELL orders and must still be able to close a sub-0.01-share tail.
        redeem = RedemptionService(restored, None)
        redeem.identify(t, {t.token_id: 1_000_000, "2": 0})
        claim = restored.business["claims"][t.condition_id]

        async def advance():
            await redeem.advance(claim)
            await redeem.advance(claim)

        asyncio.run(advance())
        assert claim["state"] == "CREDITED"
        assert t.event_id not in restored.business["cycles"]
        assert restored.validate()["ok"]
    finally:
        restored.close()
