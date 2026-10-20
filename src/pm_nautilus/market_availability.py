"""Public market availability proof, independent of settlement or asset ownership."""

import json


class MarketUnavailable(ValueError):
    """An expected order deferral for a proven unavailable public market."""


def _array(value):
    result = json.loads(value) if isinstance(value, str) else value
    if not isinstance(result, list):
        raise ValueError("市场身份元数据无效")
    return result


def market_state(raw, token, known_tokens=()):
    """Validate immutable identity before classifying explicit venue flags.

    Absence, malformed fields and an unrelated market are never reopening or
    settlement evidence. A title, slug or outcome price cannot remap ownership.
    """
    if not isinstance(raw, dict):
        raise ValueError("市场身份元数据无效")
    if str(raw.get("id")) != token.market_id or raw.get("conditionId") != token.condition_id:
        raise ValueError("市场 Market/Condition 身份不匹配")
    ids = _array(raw.get("clobTokenIds"))
    outcomes = _array(raw.get("outcomes"))
    if (
        len(ids) != 2
        or len(outcomes) != 2
        or any(not isinstance(tid, str) or not tid.isdigit() or str(int(tid)) != tid for tid in ids)
        or len(set(ids)) != 2
        or any(not isinstance(outcome, str) or not outcome for outcome in outcomes)
        or len(set(outcomes)) != 2
        or token.token_id not in ids
        or outcomes[ids.index(token.token_id)] != token.direction
    ):
        raise ValueError("市场 Token/Outcome 身份不匹配")
    for known in known_tokens:
        if known.market_id == token.market_id and known.condition_id == token.condition_id:
            if known.token_id not in ids or outcomes[ids.index(known.token_id)] != known.direction:
                raise ValueError("市场 Token/Outcome 身份不匹配")
    if raw.get("version") not in {None, "v1"} or raw.get("positionIds"):
        raise ValueError("市场协议身份不匹配")
    flags = ("active", "closed", "archived", "acceptingOrders", "enableOrderBook")
    if any(type(raw.get(key)) is not bool for key in flags):
        raise ValueError("市场交易状态元数据无效")
    if raw["archived"]:
        if raw["active"] is False and raw["acceptingOrders"] is False:
            return "archived"
        raise ValueError("市场交易状态互相矛盾")
    stopped = sum(
        (not raw["active"], not raw["acceptingOrders"], not raw["enableOrderBook"], raw["closed"])
    )
    if raw["acceptingOrders"] is False and stopped >= 2:
        return "halted"
    if stopped == 0:
        return "open"
    raise ValueError("市场停止交易证据不足")


def missing_orderbook(response):
    """Only the documented application error is absent-book evidence."""
    if response.status_code != 404:
        return False
    try:
        raw = response.json()
    except (ValueError, TypeError):
        return False
    return (
        isinstance(raw, dict)
        and raw.get("error") == "No orderbook exists for the requested token id"
    )
