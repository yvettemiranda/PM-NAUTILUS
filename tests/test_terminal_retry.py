"""Short lived zero-fill orders retry on a control tick without quote changes."""

import pytest
from nautilus_trader.model.identifiers import VenueOrderId

from test_runtime import DAY, setup


@pytest.mark.parametrize("kind", ["BUY", "TARGET", "STOP"])
def test_zero_fill_terminal_retries_once_per_control_tick(tmp_path, kind):
    runtime, clock, _ = setup(tmp_path)
    original = runtime.client.submit_order

    def zero_fill(command):
        order = command.order
        if runtime.store.intent(order.client_order_id)["kind"] != kind:
            return original(command)
        common = dict(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            ts_event=runtime.now,
        )
        runtime.client.generate_order_submitted(**common)
        common["venue_order_id"] = VenueOrderId("ZERO-" + str(order.client_order_id))
        runtime.client.generate_order_accepted(**common)
        runtime.client.generate_order_canceled(**common)

    runtime.client.submit_order = zero_fill
    try:
        runtime.book("1", [(90_000, 100_000_000)], [(100_000, 10_000_000)])
        runtime.start()
        if kind == "TARGET":
            runtime.pause()
            runtime.book("1", [(150_000, 100_000_000)], [(100_000, 10_000_000)])
        elif kind == "STOP":
            runtime.pause()
            clock.set_time(DAY + 1_000_000_000)
            runtime.book("1", [(30_000, 4_000_000)], [(100_000, 10_000_000)])
            clock.set_time(DAY + 31_000_000_000)
            runtime.book("1", [(30_000, 5_000_000)], [(100_000, 10_000_000)])
            assert runtime.business["cycles"]["event"]["stop"]["state"] == "EXITING"

        def count():
            return sum(i["kind"] == kind for i in runtime.store.intents().values())

        assert count() > 0
        for tick in range(5):
            before = count()
            runtime.drain()
            assert count() == before  # A terminal event cannot spin the drain loop.
            clock.set_time(DAY + (36 + 5 * tick) * 1_000_000_000)
            runtime.refresh_eligibility()
            runtime.drain()
            assert count() == before + 1
            assert not runtime.active_intents("event")
            assert runtime.validate()["ok"]
    finally:
        runtime.close()


def test_inflight_order_keeps_event_lock_on_control_ticks(tmp_path):
    runtime, clock, _ = setup(tmp_path)

    def accept_only(command):
        order = command.order
        common = dict(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            ts_event=runtime.now,
        )
        runtime.client.generate_order_submitted(**common)
        common["venue_order_id"] = VenueOrderId("PENDING-" + str(order.client_order_id))
        runtime.client.generate_order_accepted(**common)

    runtime.client.submit_order = accept_only
    try:
        runtime.book("1", [(90_000, 100_000_000)], [(100_000, 10_000_000)])
        runtime.start()
        for tick in range(5):
            clock.set_time(DAY + (5 + 5 * tick) * 1_000_000_000)
            runtime.refresh_eligibility()
            runtime.drain()
            assert len(runtime.store.intents()) == 1
            assert len(runtime.active_intents("event")) == 1
            assert runtime.held_cash() == runtime.preferences.budget
            assert runtime.validate()["ok"]
    finally:
        runtime.close()
