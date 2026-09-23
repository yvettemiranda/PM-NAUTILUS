"""Controlled private-account responses; no credentials, network, or signing."""

import asyncio

from py_clob_client_v2.client import ClobClient, END_CURSOR, INITIAL_CURSOR

from pm_nautilus.live_account_guard import audit_open_orders, condition_inventory_matches


class PagedClob:
    """Exercise the pinned SDK's real pagination with controlled responses."""

    host = "https://example.invalid"
    get_open_orders = ClobClient.get_open_orders

    def __init__(self):
        self.calls = []

    def _l2_headers(self, method, route):
        return {}

    def _get(self, url, *, headers, params):
        self.calls.append(params)
        if params["next_cursor"] == INITIAL_CURSOR:
            return {"next_cursor": "page-2", "data": [{"id": "own-open"}]}
        if params["next_cursor"] == "page-2":
            return {"next_cursor": END_CURSOR, "data": [{"id": "manual-open"}]}
        raise AssertionError("Unexpected pagination cursor")


def test_open_order_audit_reads_all_pages_and_rejects_old_manual_order():
    client = PagedClob()
    intents = {
        "program-client-id": {"terminal": False, "venue_id": "own-open"},
        "closed-client-id": {"terminal": True, "venue_id": "manual-open"},
    }
    audit = asyncio.run(audit_open_orders(client, intents))
    assert not audit.ok
    assert audit.unknown_count == 1
    assert audit.unknown_order_ids == ("manual-open",)
    assert client.calls == [
        {"next_cursor": INITIAL_CURSOR},
        {"next_cursor": "page-2"},
    ], "The request must remain unfiltered and let the SDK fetch every page"


def test_open_order_audit_accepts_only_current_journal_orders():
    client = PagedClob()
    intents = {
        "first": {"terminal": False, "venue_id": "own-open"},
        "second": {"terminal": False, "venue_id": "manual-open"},
    }
    audit = asyncio.run(audit_open_orders(client, intents))
    assert audit.ok and audit.unknown_count == 0 and audit.error is None


def test_open_order_audit_fails_closed_without_leaking_private_error():
    class BrokenClob:
        def get_open_orders(self):
            raise RuntimeError("private API key and account response")

    audit = asyncio.run(audit_open_orders(BrokenClob(), {}))
    assert not audit.ok
    assert audit.unknown_count == 0
    assert audit.error == "OPEN_ORDER_LOOKUP_FAILED"
    assert "private" not in repr(audit)


def test_open_order_audit_rejects_incomplete_responses_and_bad_journal():
    class Clob:
        def get_open_orders(self):
            return [{"id": "own-open"}, {"asset_id": "missing-order-id"}]

    audit = asyncio.run(audit_open_orders(Clob(), {}))
    assert not audit.ok and audit.error == "INVALID_OPEN_ORDER_RESPONSE"

    audit = asyncio.run(
        audit_open_orders(
            Clob(),
            {
                "one": {"terminal": False, "venue_id": "duplicate"},
                "two": {"terminal": False, "venue_id": "duplicate"},
            },
        )
    )
    assert not audit.ok and audit.error == "INVALID_OWN_INTENTS"


def test_condition_inventory_checks_both_outcomes_against_own_rights():
    pair = ("yes-token", "no-token")
    own = {"yes-token": 10_000_000}
    assert condition_inventory_matches(pair, {"yes-token": 10_000_000, "no-token": 0}, own)
    assert not condition_inventory_matches(pair, {"yes-token": 10_000_001, "no-token": 0}, own), (
        "Manual shares of the same outcome must stop a new trade"
    )
    assert not condition_inventory_matches(pair, {"yes-token": 10_000_000, "no-token": 1}, own), (
        "Manual shares of the opposite outcome share the same Condition"
    )


def test_condition_inventory_missing_or_invalid_chain_result_fails_closed():
    pair = ("yes-token", "no-token")
    assert not condition_inventory_matches(pair, {"yes-token": 0}, {})
    assert not condition_inventory_matches(("yes-token",), {"yes-token": 0}, {})
    assert not condition_inventory_matches(pair, {"yes-token": True, "no-token": 0}, {})
    assert not condition_inventory_matches(pair, {"yes-token": 0, "no-token": -1}, {})
