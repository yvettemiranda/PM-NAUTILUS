"""Read-only, privacy-safe discovery and owned-settlement query diagnostics."""

from datetime import datetime, timezone


STALE_SECONDS = 900
FAILURE_GRACE_SECONDS = 60
PUBLIC_QUERY_ERRORS = {
    "HTTPStatusError",
    "RequestError",
    "TransportError",
    "TimeoutException",
    "ConnectTimeout",
    "ReadTimeout",
    "WriteTimeout",
    "PoolTimeout",
    "ConnectError",
    "ReadError",
    "WriteError",
    "CloseError",
    "NetworkError",
    "ProtocolError",
    "LocalProtocolError",
    "RemoteProtocolError",
    "ProxyError",
    "UnsupportedProtocol",
    "DecodingError",
    "TooManyRedirects",
    "TimeoutError",
    "ConnectionError",
    "ValueError",
    "RuntimeError",
    "query_failed",
}


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp.astimezone(timezone.utc) if stamp.tzinfo is not None else None


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def freshness_view(state, *, now=None):
    """Project allowlisted fields without copying raw errors or held market identities.

    This view never changes strategy state. A stopped query is observable even
    without a completed failing round, while short failures receive a grace period.
    """
    state = state if isinstance(state, dict) else {}
    now = now or datetime.now(timezone.utc)
    stamps = {
        key: _timestamp(state.get(key))
        for key in (
            "monitoringSince",
            "lastAttemptAt",
            "lastSuccessAt",
            "failureSince",
            "freshnessSince",
        )
    }
    enabled = state.get("enabled") is True
    failures = _count(state.get("consecutiveFailures"))
    raw_error = state.get("error")
    error = (
        raw_error
        if isinstance(raw_error, str) and raw_error in PUBLIC_QUERY_ERRORS
        else "query_failed"
        if raw_error
        else None
    )
    baseline = stamps["freshnessSince"] or stamps["lastSuccessAt"] or stamps["monitoringSince"]
    age = max(0, int((now - baseline).total_seconds())) if baseline is not None else None
    failure_age = (
        max(0, int((now - stamps["failureSince"]).total_seconds()))
        if stamps["failureSince"] is not None
        else None
    )
    stale = enabled and age is not None and age >= STALE_SECONDS
    sustained = (
        enabled
        and error is not None
        and (
            failures >= 3
            and failure_age is not None
            and failure_age >= FAILURE_GRACE_SECONDS
            or failure_age is not None
            and failure_age >= STALE_SECONDS
        )
    )
    status = (
        "off"
        if not enabled
        else "deferred"
        if state.get("storageDeferred") is True
        else "attention"
        if stale or sustained
        else "retrying"
        if error
        else "ok"
        if stamps["lastSuccessAt"] is not None
        else "starting"
    )
    return {
        "status": status,
        "reason": "query_failed" if sustained else "stale" if stale else None,
        "enabled": enabled,
        **{key: stamp.isoformat() if stamp is not None else None for key, stamp in stamps.items()},
        "error": error,
        "consecutiveFailures": failures,
        "affectedMarketCount": _count(state.get("affectedMarketCount")),
        "checkedMarketCount": _count(state.get("checkedMarketCount")),
        "pendingQueryCount": _count(state.get("pendingQueryCount")),
        "secondsSinceSuccess": age,
        "failureDurationSeconds": failure_age,
        "staleAfterSeconds": STALE_SECONDS,
    }
