"""Read-only LIVE account checks before placing an order.

The CLOB check covers every open order visible to the configured credentials.
It deliberately calls ``get_open_orders`` without filters so the pinned SDK
fetches all pages. Neither check signs an order, submits, cancels, or stores one.
"""

import asyncio
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class OpenOrderAudit:
    ok: bool
    unknown_order_ids: tuple[str, ...] = ()
    error: str | None = None

    @property
    def unknown_count(self) -> int:
        return len(self.unknown_order_ids)


def _owned_open_venue_ids(intents: Mapping[str, dict]) -> set[str] | None:
    if not isinstance(intents, Mapping):
        return None
    ids = set()
    for intent in intents.values():
        if not isinstance(intent, dict):
            return None
        if intent.get("terminal") is not False:
            continue
        venue_id = intent.get("venue_id")
        if venue_id is None:
            continue  # An unsigned order cannot have reached the venue.
        if not isinstance(venue_id, str) or not venue_id or venue_id in ids:
            return None
        ids.add(venue_id)
    return ids


async def audit_open_orders(client, intents: Mapping[str, dict]) -> OpenOrderAudit:
    """Fail closed on unknown open orders or incomplete private CLOB responses.

    ``intents`` is the durable program journal, keyed by client order ID. No
    market or asset filter is passed to the SDK; its default fetches all pages.
    The result contains order IDs only, never order details or credentials.
    """
    own = _owned_open_venue_ids(intents)
    if own is None:
        return OpenOrderAudit(False, error="INVALID_OWN_INTENTS")
    try:
        rows = await asyncio.to_thread(client.get_open_orders)
    except Exception:
        return OpenOrderAudit(False, error="OPEN_ORDER_LOOKUP_FAILED")
    if not isinstance(rows, list):
        return OpenOrderAudit(False, error="INVALID_OPEN_ORDER_RESPONSE")
    observed = set()
    for row in rows:
        if not isinstance(row, dict):
            return OpenOrderAudit(False, error="INVALID_OPEN_ORDER_RESPONSE")
        venue_id = row.get("id")
        if not isinstance(venue_id, str) or not venue_id:
            return OpenOrderAudit(False, error="INVALID_OPEN_ORDER_RESPONSE")
        observed.add(venue_id)
    unknown = tuple(sorted(observed - own))
    return OpenOrderAudit(not unknown, unknown)


def condition_inventory_matches(
    condition_token_ids: tuple[str, str] | list[str],
    wallet_balances: Mapping[str, int],
    own_balances: Mapping[str, int],
) -> bool:
    """True only when both outcome balances equal this program's rights.

    Balances are raw CTF share units. Missing or invalid chain results fail
    closed; a missing program-owned balance means zero owned shares.
    """
    try:
        token_ids = tuple(condition_token_ids)
        if len(token_ids) != 2 or len(set(token_ids)) != 2:
            return False
        for token_id in token_ids:
            if not isinstance(token_id, str) or not token_id:
                return False
            wallet = wallet_balances.get(token_id)
            owned = own_balances.get(token_id, 0)
            if type(wallet) is not int or type(owned) is not int:
                return False
            if wallet < 0 or owned < 0 or wallet != owned:
                return False
        return True
    except (TypeError, ValueError, AttributeError):
        return False
