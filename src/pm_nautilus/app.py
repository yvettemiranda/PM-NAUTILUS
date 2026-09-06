"""Single-process authenticated UI/API; isolated TEST and opt-in LIVE contexts."""

import argparse
import base64
import fcntl
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import asyncio
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Preferences
from .market import MarketService
from .redemption import RedemptionService
from .store import Store
from .strategy import Runtime
from .views import dashboard, records


def create_app(data_dir=None, public_data=True, test_clock=None):
    root = Path(data_dir or os.getenv("PM_DATA_DIR", "runtime"))
    password = os.getenv("PM_UI_PASSWORD", "")
    allowed = set(os.getenv("PM_ALLOWED_HOSTS", "localhost,127.0.0.1,::1,testserver").split(","))
    if password and len(password) < 16:
        raise ValueError("服务端UI密码至少16字符")
    csrf = secrets.token_urlsafe(32)
    runtimes = {}
    services = {}
    cold = {}
    live_enabled = os.getenv("PM_LIVE_ENABLED") == "true"

    async def attach(mode, runtime, wallet=None):
        runtimes[mode] = runtime
        service = MarketService(runtime)
        services[mode] = service
        redeem = RedemptionService(runtime, service, wallet)
        service.on_resolution = redeem.run_once
        if public_data:
            service.loop_task = asyncio.create_task(service.run())

    @asynccontextmanager
    async def lifespan(app):
        root.mkdir(parents=True, exist_ok=True)
        lock = (root / "process.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lock.close()
            raise RuntimeError("此数据目录已有运行实例") from exc
        try:
            await attach("TEST", Runtime(root / "TEST" / "state.sqlite", clock=test_clock))
            if live_enabled:
                if not password:
                    raise ValueError("启用LIVE前必须配置UI身份验证")
                from .live import load_settings, live_factory
                from .redemption import PolygonWallet

                settings = load_settings()
                wallet = PolygonWallet(settings)
                await asyncio.to_thread(wallet.preflight)
                runtime = Runtime(
                    root / "LIVE" / "state.sqlite", mode="LIVE", live_factory=live_factory(settings)
                )
                await runtime.client.activate()
                await attach("LIVE", runtime, wallet)
            else:
                cold["LIVE"] = Store(root / "LIVE" / "state.sqlite", "LIVE")
            yield
        finally:
            for r in runtimes.values():
                r.pause()
            for service in services.values():
                await service.close()
            for r in runtimes.values():
                if r.mode == "LIVE":
                    await r.client.shutdown()
                r.close()
            for s in cold.values():
                s.close()
            lock.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtimes = runtimes
    app.state.services = services

    @app.middleware("http")
    async def security(request, call_next):
        host = request.url.hostname
        if host not in allowed:
            return JSONResponse({"error": "Host未列入服务器允许列表"}, 400)
        if not password and host not in {"localhost", "127.0.0.1", "::1", "testserver"}:
            return JSONResponse({"error": "公网访问需要服务器身份验证"}, 403)
        if request.url.path != "/api/health" and password:
            try:
                scheme, encoded = request.headers.get("authorization", "").split(" ", 1)
                user, pw = base64.b64decode(encoded, validate=True).decode().split(":", 1)
                ok = (
                    scheme.lower() == "basic"
                    and secrets.compare_digest(user, "pm")
                    and secrets.compare_digest(pw, password)
                )
            except (ValueError, UnicodeError):
                ok = False
            if not ok:
                return JSONResponse(
                    {"error": "需要登录"},
                    401,
                    headers={"WWW-Authenticate": 'Basic realm="PM-NAUTILUS"'},
                )
        if request.method not in {"GET", "HEAD"}:
            if not secrets.compare_digest(request.headers.get("x-pm-csrf", ""), csrf):
                return JSONResponse({"error": "控制请求来源校验失败"}, 403)
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return JSONResponse({"error": "跨来源控制被拒绝"}, 403)
        response = await call_next(request)
        response.headers["x-content-type-options"] = "nosniff"
        response.headers["referrer-policy"] = "no-referrer"
        response.headers["content-security-policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["cache-control"] = "no-store"
            if request.url.path != "/api/health":
                response.headers["x-pm-csrf"] = csrf
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"error": str(exc)}, 400)

    def mode_name(mode):
        mode = mode.upper()
        if mode not in {"TEST", "LIVE"}:
            raise HTTPException(404, "模式不存在")
        return mode

    def view(mode, limit=20):
        mode = mode_name(mode)
        if mode in runtimes:
            return dashboard(runtimes[mode], services[mode], limit, live_enabled)
        store = cold[mode]
        return {
            "version": "0.1.0",
            "executionMode": "LIVE",
            "liveExecutionEnabled": False,
            "strategy": {"status": "PAUSED", "initialCapital": None, "availableCash": None},
            "preferences": Preferences(**store.get("preferences")).public(),
            "capitalEditable": False,
            "positions": [],
            "portfolio": dict.fromkeys(
                [
                    "totalFunds",
                    "realizedPnl",
                    "unrealizedPnl",
                    "positionValue",
                    "availableCash",
                    "reservedCash",
                    "pendingRedemption",
                ]
            ),
            "redemptions": [],
            "marketScan": {
                "events": [],
                "eventCount": 0,
                "displayEventCount": 0,
                "pendingEventCount": 0,
                "diagnostics": {"availableCategories": services["TEST"].categories},
            },
        }

    @app.get("/api/health")
    async def health():
        return {
            "status": "ok",
            "version": "0.1.0",
            "mode": "TEST",
            "strategyStatus": runtimes["TEST"].status,
            "liveExecutionEnabled": live_enabled,
            "revision": os.getenv("PM_GIT_REVISION", "local"),
        }

    @app.get("/api/dashboard")
    async def get_dashboard(mode: str = "TEST", limit: int = 20):
        if limit < 1:
            raise ValueError("limit须大于0")
        return view(mode, limit)

    @app.get("/api/{mode}/preferences")
    async def get_preferences(mode: str):
        return view(mode)

    @app.put("/api/{mode}/preferences")
    async def preferences(mode: str, request: Request):
        mode = mode_name(mode)
        payload = await request.json()
        capital = payload.pop("initialCapital", None)
        if "selectedCategories" in payload and "selectedCategoryIds" not in payload:
            payload["selectedCategoryIds"] = payload.pop("selectedCategories")
        if mode == "LIVE" and capital is not None:
            raise ValueError("LIVE资金来自实际账户，不能设置模拟资金")
        count = 0
        if mode in runtimes:
            count = runtimes[mode].update_preferences(payload, capital)
            if public_data:
                await services[mode].sync_subscriptions()
        else:
            prefs = Preferences(**(cold[mode].get("preferences") | payload))
            cold[mode].put("preferences", prefs.model_dump(mode="json"))
        return view(mode) | {"cancelledBuyCount": count}

    @app.post("/api/{mode}/start")
    async def start(mode: str):
        mode = mode_name(mode)
        if mode not in runtimes:
            raise ValueError("LIVE尚未在服务器配置并明确启用")
        runtimes[mode].start()
        return view(mode)

    @app.post("/api/{mode}/pause")
    async def pause(mode: str):
        mode = mode_name(mode)
        if mode in runtimes:
            runtimes[mode].pause()
        return view(mode)

    @app.post("/api/{mode}/reset")
    async def reset(mode: str, request: Request):
        mode = mode_name(mode)
        if mode != "TEST":
            raise ValueError("LIVE真实账本不能重置")
        payload = await request.json()
        if payload != {"confirmation": "RESET TEST", "finalConfirmation": "RESET TEST AGAIN"}:
            raise ValueError("必须完成TEST双重确认")
        r = runtimes["TEST"]
        if r.status != "PAUSED":
            raise ValueError("先暂停TEST再重置")
        await services["TEST"].close()
        r.store.reset()
        r.close()
        await attach("TEST", Runtime(root / "TEST" / "state.sqlite", clock=test_clock))
        return view("TEST")

    @app.get("/api/{mode}/trade-records")
    async def trade_records(mode: str, limit: int = 100):
        mode = mode_name(mode)
        if not 1 <= limit <= 10000:
            raise ValueError("记录limit须为1–10000")
        return (
            records(runtimes[mode], limit) if mode in runtimes else {"records": [], "totalCount": 0}
        )

    @app.get("/api/{mode}/validation")
    async def validate(mode: str):
        mode = mode_name(mode)
        return (
            runtimes[mode].validate()
            if mode in runtimes
            else {"ok": False, "errors": ["LIVE未连接"]}
        )

    app.mount("/", StaticFiles(directory=Path(__file__).parent / "web", html=True), name="web")
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default=os.getenv("PM_DATA_DIR", "runtime"))
    parser.add_argument("--offline", action="store_true", help="仅本地UI与已有账本，不连公开行情")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not os.getenv("PM_UI_PASSWORD"):
        raise SystemExit("非回环监听必须配置 PM_UI_PASSWORD")
    import uvicorn

    uvicorn.run(
        create_app(args.data_dir, public_data=not args.offline),
        host=args.host,
        port=args.port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
