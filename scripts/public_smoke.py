"""Bounded development smoke: public live books, native TEST fills, no wallet.

This deliberately sampled development fixture does not replace production's full
paginated discovery. It uses its own new temporary database and explicit test
settings, then leaves it PAUSED. It never starts the formal long-running TEST.
"""

import asyncio
import json
import tempfile
from pathlib import Path

import httpx
from nautilus_trader.model.events import OrderFilled

from pm_nautilus.market import GAMMA, MarketService, normalize
from pm_nautilus.rules import static_reason
from pm_nautilus.strategy import Runtime


async def run():
    directory = Path(tempfile.mkdtemp(prefix="pm-public-smoke-"))
    runtime = Runtime(directory / "test.sqlite")
    service = MarketService(runtime)
    try:
        runtime.update_preferences(
            {
                "orderAmount": "10",
                "minBidAskRatioPercent": 1,
                "maxMarketProgressPercent": 100,
                "maxMarketDurationDays": 365,
                "marketTypes": ["BINARY", "TERNARY", "MULTI"],
            }
        )
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{GAMMA}/events/keyset",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": 100,
                    "order": "id",
                    "ascending": "false",
                },
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
                raise ValueError("公开 Event keyset 响应格式变化")
            events = payload["events"]
        selected = []
        for event in events:
            tokens = normalize(event)
            eligible = [
                t for t in tokens if not static_reason(t, runtime.preferences, runtime.now, set())
            ]
            if eligible:
                selected.extend(tokens)
            if len({t.event_id for t in selected}) >= 10:
                break
        runtime.add_tokens(selected)
        await service.sync_subscriptions()
        deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < deadline:
            if any(status == "READY" for status, _ in runtime.evaluations.values()):
                break
            await asyncio.sleep(0.1)
        ready = sum(status == "READY" for status, _ in runtime.evaluations.values())
        runtime.start()
        await asyncio.sleep(2)
        runtime.pause()
        await service.close()
        fills = [e for _, e in runtime.store.events() if isinstance(e, OrderFilled)]
        result = {
            "mode": runtime.mode,
            "status": runtime.status,
            "sampled_events": len(events),
            "selected_events": len({t.event_id for t in selected}),
            "monitored_tokens": len(runtime.monitored),
            "ready_before_start": ready,
            "native_fill_count": len(fills),
            "native_position_count": len(runtime.native.cache.positions_open()),
            "cash_micros": runtime.cash(),
            "validation": runtime.validate(),
            "settings": runtime.preferences.model_dump(mode="json"),
            "data_directory": str(directory),
            "private_connections": 0,
        }
        runtime.close()
        restored = Runtime(directory / "test.sqlite")
        result["restart_validation"] = restored.validate()
        result["restart_cash_micros"] = restored.cash()
        restored.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not fills or not result["validation"]["ok"] or not result["restart_validation"]["ok"]:
            raise RuntimeError("公开行情短测未形成有效模拟成交或核对未通过；不能报告成功")
    finally:
        if not service.closed:
            await service.close()
            runtime.close()


if __name__ == "__main__":
    asyncio.run(run())
