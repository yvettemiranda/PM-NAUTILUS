"""The pre-LIVE checklist must fail closed without exposing wallet secrets."""

import json
from decimal import Decimal

import pytest

from pm_nautilus import live_readiness as readiness


SIGNER = "0x" + "11" * 20
FUNDER = "0x" + "22" * 20
SECRET = "private-secret-must-not-appear"
SETTINGS = {"private_key": SECRET}


class Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class Client:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, *, params=None, headers=None):
        self.calls.append((url, params, headers))
        return self.responses.pop(0)


def private_pass(settings, signer, funder, data_dir):
    assert settings is SETTINGS
    assert (signer, funder, data_dir) == (SIGNER, FUNDER, "/unused")
    return {
        "canStartNewBuys": True,
        "redemptionApprovalComplete": True,
        "redemptionApproval": {
            "status": "APPROVED",
            "standardApproved": True,
            "negRiskApproved": True,
            "pendingTxHash": None,
        },
    }


def page(rows, *, has_more=False, next_cursor=None):
    return {"data": rows, "pagination": {"has_more": has_more, "next_cursor": next_cursor}}


def position(size, *, wallet=FUNDER, token_id="123"):
    return {"proxy_wallet": wallet, "current_size": size, "token_id": token_id, "title": SECRET}


class ControlledCustody:
    def __init__(self, balances=None, zero_tokens=()):
        self.exact = dict(balances or {})
        self.zero_tokens = set(zero_tokens)
        self.calls = []

    def balances(self, token_ids):
        ids = tuple(token_ids)
        self.calls.append(("balances", ids))
        return {tid: self.exact.get(tid, 0) for tid in ids}

    def is_resolved_zero(self, row):
        self.calls.append(("zero", row["token_id"]))
        return row["token_id"] in self.zero_tokens


def check(
    client,
    *,
    private_check=private_pass,
    known_token_ids=None,
    known_positions=None,
    position_reader=None,
):
    if position_reader is None:
        # Existing fixtures deliberately supply controlled chain quantities. The
        # production reader never derives balances from these display responses.
        amounts = {}
        for response in client.responses:
            if not isinstance(response.payload, dict):
                continue
            for row in response.payload.get("data", []):
                if isinstance(row, dict) and row.get("token_id"):
                    try:
                        amount = int(Decimal(str(row["current_size"])) * 1_000_000)
                    except (ArithmeticError, ValueError, TypeError):
                        continue
                    tid = row["token_id"]
                    amounts[tid] = amounts.get(tid, 0) + amount
        position_reader = ControlledCustody(amounts)
    return readiness.run_readiness(
        SETTINGS,
        SIGNER,
        FUNDER,
        "/unused",
        private_check=private_check,
        http_client=client,
        known_token_ids=known_token_ids,
        known_positions=known_positions,
        position_reader=position_reader,
    )


def statuses(result):
    return {item["id"]: item["status"] for item in result["checks"]}


def test_all_checks_pass_with_official_get_endpoints_and_no_secret_output():
    client = Client(Response({"blocked": False, "ip": SECRET}), Response(page([])))
    result = check(client)

    assert result["ready"] is True
    assert result["redemptionApproval"]["status"] == "APPROVED"
    assert statuses(result) == {"account": "pass", "region": "pass", "positions": "pass"}
    assert client.calls == [
        (readiness.GEOBLOCK_URL, None, {"Accept": "application/json"}),
        (
            readiness.POSITIONS_URL,
            {
                "user": FUNDER,
                "status": "OPEN",
                "limit": 1000,
                "filter_type": "TOKENS",
                "filter_amount": 0,
                "include_archived": "true",
            },
            {"Accept": "application/json"},
        ),
    ]
    assert SECRET not in json.dumps(result)
    assert "ip" not in result


@pytest.mark.parametrize(
    ("geoblock", "expected"),
    [
        (Response({"blocked": True}), "blocked"),
        (Response({"blocked": "false"}), "unknown"),
        (Response({"blocked": False}, status_code=302), "unknown"),
        (Response(ValueError(SECRET)), "unknown"),
    ],
)
def test_geoblock_restriction_or_uncertainty_blocks_readiness(geoblock, expected):
    result = check(Client(geoblock, Response(page([]))))
    assert result["ready"] is False
    assert statuses(result)["region"] == expected
    assert SECRET not in json.dumps(result)


def test_existing_wallet_position_blocks_without_returning_position_details():
    client = Client(Response({"blocked": False}), Response(page([position("0.000001")])))
    result = check(client)
    assert result["ready"] is False
    assert statuses(result)["positions"] == "blocked"
    assert SECRET not in json.dumps(result)


def test_recorded_live_token_is_accepted_on_restart_but_unknown_token_blocks():
    tracked = check(
        Client(Response({"blocked": False}), Response(page([position("2", token_id="123")]))),
        known_token_ids={"123"},
    )
    unknown = check(
        Client(
            Response({"blocked": False}),
            Response(page([position("2", token_id="123"), position("1", token_id="456")])),
        ),
        known_token_ids={"123"},
    )
    assert tracked["ready"] is True
    assert statuses(tracked)["positions"] == "pass"
    assert unknown["ready"] is False
    assert statuses(unknown)["positions"] == "blocked"
    assert SECRET not in json.dumps(tracked) + json.dumps(unknown)


def test_recorded_token_mode_fails_closed_on_missing_token_identity():
    result = check(
        Client(Response({"blocked": False}), Response(page([position("1", token_id=None)]))),
        known_token_ids={"123"},
    )
    assert result["ready"] is False
    assert statuses(result)["positions"] == "unknown"


def test_invalid_recorded_token_ids_cannot_bypass_position_gate():
    result = check(
        Client(Response({"blocked": False}), Response(page([position("1")]))),
        known_token_ids={"not-a-token"},
    )
    assert result["ready"] is False
    assert statuses(result)["positions"] == "unknown"


def test_recorded_position_quantities_are_aggregated_across_all_pages():
    client = Client(
        Response({"blocked": False}),
        Response(page([position("0.250001", token_id="123")], has_more=True, next_cursor="next")),
        Response(page([position("0.749999", token_id="123")])),
    )
    result = check(client, known_positions={"123": 1_000_000})
    assert result["ready"] is True
    assert statuses(result)["positions"] == "pass"
    assert client.calls[2][1] == {"user": FUNDER, "cursor": "next"}


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ([position("1.000001", token_id="123")], {"123": 1_000_000}),
        ([position("0.999999", token_id="123")], {"123": 1_000_000}),
        ([position("1", token_id="456")], {"123": 1_000_000}),
        ([], {"123": 1_000_000}),
    ],
)
def test_extra_missing_or_unrecorded_shares_block_restart(rows, expected):
    result = check(
        Client(Response({"blocked": False}), Response(page(rows))), known_positions=expected
    )
    assert result["ready"] is False
    assert statuses(result)["positions"] == "blocked"


def test_sub_micro_data_and_invalid_ledger_quantities_remain_unknown():
    client = Client(Response({"blocked": False}), Response(page([position("1.0000001")])))
    fractional = check(client, known_positions={"123": 1_000_000})
    invalid = check(
        Client(Response({"blocked": False}), Response(page([]))),
        known_positions={"123": True},
    )
    assert statuses(fractional)["positions"] == "unknown"
    assert statuses(invalid)["positions"] == "unknown"
    assert fractional["ready"] is False and invalid["ready"] is False


def test_token_only_and_quantity_aware_inputs_must_agree_when_both_provided():
    result = check(
        Client(Response({"blocked": False}), Response(page([]))),
        known_token_ids={"456"},
        known_positions={"123": 1_000_000},
    )
    assert result["ready"] is False
    assert statuses(result)["positions"] == "unknown"


def test_position_cursor_is_followed_until_exhausted():
    client = Client(
        Response({"blocked": False}),
        Response(page([], has_more=True, next_cursor="opaque-cursor")),
        Response(page([])),
    )
    result = check(client)
    assert result["ready"] is True
    assert client.calls[2][1] == {"user": FUNDER, "cursor": "opaque-cursor"}


@pytest.mark.parametrize(
    "position_response",
    [
        page([position("1", wallet=SIGNER)]),
        page([position("NaN")]),
        page([position(True)]),
        {"data": [], "pagination": {"has_more": True, "next_cursor": None}},
        {"data": [], "pagination": {"has_more": "false"}},
    ],
)
def test_invalid_or_incomplete_position_result_is_unknown(position_response):
    result = check(Client(Response({"blocked": False}), Response(position_response)))
    assert result["ready"] is False
    assert statuses(result)["positions"] == "unknown"


def test_private_preflight_failure_and_unready_are_not_green():
    def failed(*_):
        raise RuntimeError(SECRET)

    def unready(*_):
        return private_pass(SETTINGS, SIGNER, FUNDER, "/unused") | {
            "canStartNewBuys": False,
            "unknownOpenOrderCount": 1,
        }

    failed_result = check(
        Client(Response({"blocked": False}), Response(page([]))), private_check=failed
    )
    unready_result = check(
        Client(Response({"blocked": False}), Response(page([]))), private_check=unready
    )
    assert statuses(failed_result)["account"] == "unknown"
    assert statuses(unready_result)["account"] == "blocked"
    assert failed_result["ready"] is False and unready_result["ready"] is False
    assert SECRET not in json.dumps(failed_result)


def test_missing_or_pending_redemption_approval_cannot_be_ready():
    missing = check(
        Client(Response({"blocked": False}), Response(page([]))),
        private_check=lambda *_: {
            "canStartNewBuys": False,
            "redemptionApprovalComplete": False,
            "redemptionApproval": {
                "status": "MISSING",
                "standardApproved": False,
                "negRiskApproved": True,
                "pendingTxHash": None,
            },
        },
    )
    incomplete = check(
        Client(Response({"blocked": False}), Response(page([]))),
        private_check=lambda *_: {"canStartNewBuys": True},
    )
    assert statuses(missing)["account"] == "blocked"
    assert statuses(incomplete)["account"] == "unknown"
    assert missing["ready"] is False and incomplete["ready"] is False


@pytest.mark.parametrize(
    "field,message",
    [
        ("collateralBalanceSufficient", "pUSD 余额不足"),
        ("collateralAllowancePresent", "pUSD 交易授权额度不足"),
        ("outcomeTokenApprovalPresent", "交易所卖出授权尚未完成"),
        ("signerGasAvailable", "Polygon POL 余额不足"),
    ],
)
def test_unready_account_names_the_blocking_resource(field, message):
    result = check(
        Client(Response({"blocked": False}), Response(page([]))),
        private_check=lambda *_: private_pass(SETTINGS, SIGNER, FUNDER, "/unused")
        | {"canStartNewBuys": False, field: False},
    )
    account = next(item for item in result["checks"] if item["id"] == "account")
    assert account["status"] == "blocked"
    assert message in account["message"]


def test_missing_approval_does_not_hide_other_confirmed_account_blockers():
    result = check(
        Client(Response({"blocked": True}), Response(page([]))),
        private_check=lambda *_: {
            **private_pass(SETTINGS, SIGNER, FUNDER, "/unused"),
            "canStartNewBuys": False,
            "redemptionApprovalComplete": False,
            "collateralBalanceSufficient": False,
            "collateralAllowancePresent": False,
            "outcomeTokenApprovalPresent": False,
            "signerGasAvailable": False,
            "unknownOpenOrderCount": 1,
            "redemptionApproval": {
                "status": "MISSING",
                "standardApproved": False,
                "negRiskApproved": False,
                "pendingTxHash": None,
            },
        },
    )
    account = next(item for item in result["checks"] if item["id"] == "account")
    assert account["status"] == "blocked"
    for message in (
        "本程序以外的挂单",
        "赎回适配器授权尚未确认",
        "pUSD 余额不足",
        "pUSD 交易授权额度不足",
        "交易所卖出授权尚未完成",
        "Polygon POL 余额不足",
    ):
        assert message in account["message"]
    assert result["ready"] is False
    assert statuses(result) == {"account": "blocked", "region": "blocked", "positions": "pass"}
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize("error_code", ["TX_FEE_REJECTED", "RPC_RESULT_UNKNOWN", SECRET])
def test_readiness_preserves_only_safe_durable_approval_diagnostics(error_code):
    tx_hash = "0x" + "ab" * 32
    result = check(
        Client(Response({"blocked": False}), Response(page([]))),
        private_check=lambda *_: {
            "canStartNewBuys": False,
            "redemptionApprovalComplete": False,
            "redemptionApproval": {
                "status": "PENDING",
                "standardApproved": False,
                "negRiskApproved": False,
                "pendingTxHash": tx_hash,
                "errorCode": error_code,
                "stage": "broadcast",
                "errorMessage": SECRET,
                "networkError": True,
            },
        },
    )
    approval = result["redemptionApproval"]
    assert result["ready"] is False
    assert SECRET not in json.dumps(result)
    if error_code == SECRET:
        assert statuses(result)["account"] == "unknown"
        assert approval["status"] == "UNKNOWN"
    else:
        assert statuses(result)["account"] == "blocked"
        assert approval["status"] == "PENDING"
        assert approval["pendingTxHash"] == tx_hash
        assert approval["errorCode"] == error_code
        assert approval["stage"] == "broadcast"
        assert approval["networkError"] is (error_code == "RPC_RESULT_UNKNOWN")


@pytest.mark.parametrize("rows", [[position("1.0000")], [position("0")], []])
def test_custody_exact_quantity_passes_despite_api_rounding_or_missing_display_row(rows):
    reader = ControlledCustody({"123": 1_000_025})
    result = check(
        Client(Response({"blocked": False}), Response(page(rows))),
        known_positions={"123": 1_000_025},
        position_reader=reader,
    )
    assert result["ready"] is True
    assert statuses(result)["positions"] == "pass"
    assert reader.calls == [("balances", ("123",))]


@pytest.mark.parametrize("actual", [999_999, 1_000_001])
def test_even_one_micro_chain_custody_mismatch_blocks_when_api_displays_expected(actual):
    result = check(
        Client(Response({"blocked": False}), Response(page([position("1")]))),
        known_positions={"123": 1_000_000},
        position_reader=ControlledCustody({"123": actual}),
    )
    assert result["ready"] is False
    assert statuses(result)["positions"] == "blocked"


def test_program_zero_quantity_token_cannot_be_filtered_as_external_loss():
    reader = ControlledCustody({"123": 1}, zero_tokens={"123"})
    result = check(
        Client(Response({"blocked": False}), Response(page([position("0")]))),
        known_positions={"123": 0},
        position_reader=reader,
    )
    assert statuses(result)["positions"] == "blocked"
    assert reader.calls == [("balances", ("123",))]


def test_unobserved_closed_program_tokens_do_not_force_large_chain_inventory_scan():
    expected = {str(tid): 0 for tid in range(10_000)} | {"10000": 1_000_025}
    reader = ControlledCustody({"10000": 1_000_025})
    result = check(
        Client(Response({"blocked": False}), Response(page([]))),
        known_positions=expected,
        position_reader=reader,
    )
    assert result["ready"] is True
    assert reader.calls == [("balances", ("10000",))]


def test_external_zero_loss_proof_does_not_hide_unrecognized_position_on_later_page():
    reader = ControlledCustody(zero_tokens={"123"})
    client = Client(
        Response({"blocked": False}),
        Response(page([position("1")], has_more=True, next_cursor="next")),
        Response(page([position("0.000001", token_id="456")])),
    )
    result = check(client, known_positions={}, position_reader=reader)
    assert statuses(result)["positions"] == "blocked"
    assert client.calls[2][1] == {"user": FUNDER, "cursor": "next"}


def test_external_zero_loss_proof_passes_without_changing_owned_quantities():
    reader = ControlledCustody({"123": 1_000_025}, zero_tokens={"456"})
    result = check(
        Client(
            Response({"blocked": False}),
            Response(page([position("1.0000"), position("0", token_id="456")])),
        ),
        known_positions={"123": 1_000_025},
        position_reader=reader,
    )
    assert result["ready"] is True
    assert ("zero", "123") not in reader.calls
    assert ("zero", "456") in reader.calls


@pytest.mark.parametrize(
    "expected,rows", [({"123": 1_000_000}, [position("1")]), ({}, [position("1")])]
)
def test_chain_query_failure_never_falls_back_to_api_custody_or_zero_prices(expected, rows):
    class FailedCustody:
        def balances(self, token_ids):
            if token_ids:
                raise TimeoutError(SECRET)
            return {}

        def is_resolved_zero(self, row):
            raise TimeoutError(SECRET)

    result = check(
        Client(Response({"blocked": False}), Response(page(rows))),
        known_positions=expected,
        position_reader=FailedCustody(),
    )
    assert result["ready"] is False
    assert statuses(result)["positions"] == "unknown"
    assert SECRET not in json.dumps(result)


def test_production_dependency_requires_chain_reader_without_implicit_api_fallback():
    result = readiness.run_readiness(
        SETTINGS,
        SIGNER,
        FUNDER,
        "/unused",
        private_check=private_pass,
        http_client=Client(Response({"blocked": False}), Response(page([position("1")]))),
        known_positions={"123": 1_000_000},
    )
    assert result["ready"] is False
    assert statuses(result)["positions"] == "unknown"
