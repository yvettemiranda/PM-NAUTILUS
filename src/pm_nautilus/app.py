"""Single-process authenticated UI/API; isolated TEST and opt-in LIVE contexts."""

import argparse
import base64
import fcntl
import json
import os
import secrets
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

import asyncio
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Preferences
from .market import MarketService
from .redemption import RedemptionService
from .store import Store
from .strategy import Runtime
from .views import dashboard, records
from .performance import PerformanceSampler
from .wallet_vault import VaultError, derive_signer, read_public_metadata, read_vault, write_vault
from .live_readiness import run_readiness


class _LiveBundleResponse(FileResponse):
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            from .live_backup import cleanup_live_bundle_file

            cleanup_live_bundle_file(self.path)


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
    samplers = {}
    legacy_live_enabled = os.getenv("PM_LIVE_ENABLED") == "true"
    vault_path = root / "wallet" / "live.vault"
    wallet_lock = asyncio.Lock()
    wallet_state = {
        "settings": None,
        "metadata": None,
        "readiness": None,
        "approval": None,
        "error": None,
    }
    unlock_failures = []

    async def attach(mode, runtime, wallet=None):
        runtimes[mode] = runtime
        service = MarketService(runtime)
        services[mode] = service
        redeem = RedemptionService(runtime, service, wallet)
        service.on_resolution = redeem.run_once
        if public_data:
            service.loop_task = asyncio.create_task(service.run())
        sampler = PerformanceSampler(runtime)
        samplers[mode] = sampler
        sampler.task = asyncio.create_task(sampler.run())

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
            if legacy_live_enabled:
                if not password:
                    raise ValueError("启用LIVE前必须配置UI身份验证")
                from .live import load_settings, live_factory
                from .redemption import PolygonWallet

                stage = "凭据读取"
                try:
                    settings = load_settings()
                    stage = "钱包只读核对"
                    wallet = PolygonWallet(settings)
                    await asyncio.to_thread(wallet.preflight)
                    stage = "执行账本构建"
                    runtime = Runtime(
                        root / "LIVE" / "state.sqlite",
                        mode="LIVE",
                        live_factory=live_factory(settings, wallet),
                    )
                    # Activation may fail after native queues/private tasks have started.
                    # Register ownership before awaiting it so cleanup still reaches them.
                    runtimes["LIVE"] = runtime
                    stage = "执行状态核对"
                    await runtime.client.activate()
                    stage = "行情服务初始化"
                    await attach("LIVE", runtime, wallet)
                except Exception as exc:
                    # SDK/RPC errors can include a credential-bearing URL or response.
                    raise RuntimeError(f"LIVE{stage}失败 ({type(exc).__name__})") from None
            else:
                cold["LIVE"] = Store(root / "LIVE" / "state.sqlite", "LIVE")
            yield
        finally:
            failures = []
            for sampler in samplers.values():
                sampler.task.cancel()
            await asyncio.gather(*(s.task for s in samplers.values()), return_exceptions=True)
            for r in runtimes.values():
                try:
                    r.pause()
                except Exception as exc:
                    failures.append(exc)
            for service in services.values():
                try:
                    await service.close()
                except Exception as exc:
                    failures.append(exc)
            for r in runtimes.values():
                try:
                    if r.mode == "LIVE":
                        # shutdown drains native callbacks while SQLite is still open.
                        await r.client.shutdown()
                    else:
                        r.native.stop()
                except Exception as exc:
                    failures.append(exc)
                finally:
                    r.store.close()
            for s in cold.values():
                s.close()
            lock.close()
            if failures:
                kinds = ",".join(sorted({type(error).__name__ for error in failures}))
                raise RuntimeError(f"组件关闭失败 ({kinds})") from None

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtimes = runtimes
    app.state.services = services
    app.state.samplers = samplers

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
            expected_origin = str(request.base_url).rstrip("/")
            if os.getenv("PM_TRUST_HTTPS_PROXY") == "true":
                forwarded_scheme = request.headers.get("x-forwarded-proto")
                if forwarded_scheme in {"http", "https"}:
                    expected_origin = f"{forwarded_scheme}://{request.headers['host']}"
            if origin and origin.rstrip("/") != expected_origin:
                return JSONResponse({"error": "跨来源控制被拒绝"}, 403)
        response = await call_next(request)
        response.headers["x-content-type-options"] = "nosniff"
        response.headers["referrer-policy"] = "no-referrer"
        response.headers["cache-control"] = "no-store"
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

    def wallet_public():
        enabled = "LIVE" in runtimes
        configured = (
            vault_path.exists() or vault_path.is_symlink() or (legacy_live_enabled and enabled)
        )
        unlocked = wallet_state["settings"] is not None or (legacy_live_enabled and enabled)
        metadata = wallet_state["metadata"]
        if configured and metadata is None and not (legacy_live_enabled and enabled):
            try:
                metadata = read_public_metadata(vault_path)
            except VaultError:
                return {
                    "configured": True,
                    "unlocked": False,
                    "enabled": False,
                    "status": "ERROR",
                    "signer": None,
                    "funder": None,
                    "signatureType": None,
                    "readiness": None,
                    "error": "加密钱包文件无法读取；请检查备份。",
                }
        readiness = wallet_state["readiness"]
        if wallet_state["error"]:
            status = "ERROR"
        elif enabled and (
            getattr(runtimes["LIVE"], "faulted", None) or runtimes["LIVE"].store.get("live_error")
        ):
            status = "ERROR"
        elif enabled:
            status = getattr(runtimes["LIVE"], "status", "PAUSED")
        elif not configured:
            status = "UNCONFIGURED"
        elif not unlocked:
            status = "LOCKED"
        elif readiness is None:
            status = "BLOCKED"
        else:
            status = "READY" if readiness["ready"] else "BLOCKED"
        return {
            "configured": configured,
            "unlocked": unlocked,
            "enabled": enabled,
            "replaceable": bool(
                configured
                and not enabled
                and cold.get("LIVE")
                and not ledger_has_activity(cold["LIVE"])
            ),
            "status": status,
            "signer": metadata["signer"] if metadata else None,
            "funder": metadata["funder"] if metadata else None,
            "signatureType": metadata["signature_type"] if metadata else None,
            "identityVerified": bool(wallet_state["metadata"]),
            "readiness": readiness,
            "redemptionApproval": wallet_state["approval"],
            "error": wallet_state["error"]
            or ("实盘账户需要重新核对" if status == "ERROR" else None),
        }

    def ledger_has_activity(store):
        for table in ("intents", "native_events"):
            if store.db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                return True
        business = store.get("business")
        return any(business.get(key) for key in ("cycles", "claims", "settled"))

    def ensure_wallet_identity(metadata):
        store = cold.get("LIVE") or runtimes["LIVE"].store
        bound = store.get("live_wallet_identity")
        if bound is None:
            if ledger_has_activity(store):
                raise ValueError("LIVE 已有交易记录，须先核对原钱包与账本再导入")
            return
        if (
            not isinstance(bound, dict)
            or not isinstance(bound.get("signer"), str)
            or not isinstance(bound.get("funder"), str)
            or bound.get("signer", "").lower() != metadata["signer"].lower()
            or bound.get("funder", "").lower() != metadata["funder"].lower()
            or bound.get("signature_type") != metadata["signature_type"]
        ):
            raise ValueError("钱包与已有 LIVE 账本不一致")

    def owned_positions():
        store = cold.get("LIVE") or runtimes["LIVE"].store
        positions = {}
        for cycle in store.get("business")["cycles"].values():
            quantity = cycle["quantity"]
            if type(quantity) is not int or quantity < 0:
                raise ValueError("LIVE 持仓账本无效")
            if quantity:
                token_id = cycle["token_id"]
                positions[token_id] = positions.get(token_id, 0) + quantity
        return positions

    async def refresh_readiness():
        if wallet_state["settings"] is None or wallet_state["metadata"] is None:
            raise ValueError("请先解锁钱包")
        wallet_state["readiness"] = None
        wallet_state["approval"] = None
        metadata = wallet_state["metadata"]
        settings = wallet_state["settings"]
        try:
            positions = owned_positions()
            result = await asyncio.to_thread(
                run_readiness,
                settings,
                metadata["signer"],
                metadata["funder"],
                root,
                known_positions=positions,
            )
        except Exception:
            result = {
                "ready": False,
                "checks": [
                    {
                        "id": "account",
                        "status": "unknown",
                        "message": "实盘账户检查失败，请稍后重试。",
                    }
                ],
            }
        wallet_state["readiness"] = result
        wallet_state["approval"] = result.get("redemptionApproval") or {
            "status": "UNKNOWN",
            "standardApproved": None,
            "negRiskApproved": None,
            "pendingTxHash": None,
            "networkError": False,
        }
        return result

    async def verified_vault_settings(password_input):
        now = time.monotonic()
        unlock_failures[:] = [stamp for stamp in unlock_failures if now - stamp < 60]
        if len(unlock_failures) >= 5:
            raise HTTPException(429, "解锁尝试过多，请一分钟后重试")
        try:
            settings = await asyncio.to_thread(read_vault, vault_path, password_input)
        except VaultError:
            unlock_failures.append(now)
            raise ValueError("钱包解锁失败；请检查密码或备份文件") from None
        unlock_failures.clear()
        return settings

    async def wallet_body(request, fields):
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 8192:
                raise ValueError("钱包输入内容过长")
            raw.extend(chunk)
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError, json.JSONDecodeError):
            raise ValueError("钱包输入格式无效") from None
        if not isinstance(payload, dict) or not set(payload) <= fields:
            raise ValueError("钱包输入字段无效")
        return payload

    def require_secure_wallet_request(request):
        host = request.url.hostname
        local = host in {"localhost", "127.0.0.1", "::1", "testserver"}
        scheme = request.url.scheme
        if os.getenv("PM_TRUST_HTTPS_PROXY") == "true":
            scheme = request.headers.get("x-forwarded-proto", scheme)
        if not local and scheme != "https":
            raise HTTPException(403, "钱包操作必须使用 HTTPS")
        if not local and not password:
            raise HTTPException(403, "钱包操作需要网页登录密码")

    def view(mode, limit=20):
        mode = mode_name(mode)
        live_status = runtimes["LIVE"].status if "LIVE" in runtimes else "LOCKED"
        if mode in runtimes:
            data = dashboard(runtimes[mode], services[mode], limit, "LIVE" in runtimes)
            data["liveStrategyStatus"] = live_status
            if mode == "LIVE":
                data["wallet"] = wallet_public()
                data["liveError"] = bool(runtimes[mode].store.get("live_error"))
            return data
        store = cold[mode]
        return {
            "version": "0.1.0",
            "executionMode": "LIVE",
            "liveStrategyStatus": live_status,
            "generation": store.generation,
            "liveExecutionEnabled": False,
            "wallet": wallet_public(),
            "positionsAvailable": False,
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
                    "failedRedemption",
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
        failures = {}
        for mode, service in services.items():
            runtime = runtimes[mode]
            issues = []
            if service.scan_status.get("serviceError"):
                # MarketService stores only the exception class name here.
                issues.append(service.scan_status["serviceError"])
            if service.loop_task is not None and service.loop_task.done() and not service.closed:
                issues.append("market_task_stopped")
            # These can include private RPC details; expose stable codes only.
            if runtime.faulted:
                issues.append("runtime_faulted")
            if runtime.business.get("recovery_error"):
                issues.append("recovery_error")
            if mode == "LIVE":
                if runtime.store.get("live_error"):
                    issues.append("live_account_error")
                task = runtime.client.reconciliation_task
                if task is None or task.done():
                    issues.append("live_maintenance_task_stopped")
            if issues:
                failures[mode] = ",".join(issues)
        body = {
            "status": "degraded" if failures else "ok",
            "version": "0.1.0",
            "mode": "TEST",
            "strategyStatus": runtimes["TEST"].status,
            "liveExecutionEnabled": "LIVE" in runtimes,
            "liveWalletStatus": wallet_public()["status"],
            "revision": os.getenv("PM_GIT_REVISION", "local"),
            "backgroundErrors": failures,
        }
        return JSONResponse(body, status_code=503 if failures else 200)

    @app.get("/api/dashboard")
    async def get_dashboard(mode: str = "TEST", limit: int = 20):
        if limit < 1:
            raise ValueError("limit须大于0")
        return view(mode, limit)

    @app.get("/api/live/wallet")
    async def get_live_wallet():
        return wallet_public()

    @app.post("/api/live/wallet/import")
    async def import_live_wallet(request: Request):
        require_secure_wallet_request(request)
        fields = {
            "kind",
            "secret",
            "mnemonicPassphrase",
            "accountIndex",
            "publicAddress",
            "vaultPassword",
        }
        payload = await wallet_body(request, fields)
        async with wallet_lock:
            if vault_path.exists() or vault_path.is_symlink() or legacy_live_enabled:
                raise ValueError("钱包已经配置，请解锁或先核对原 LIVE 账户")
            if "LIVE" in runtimes:
                raise ValueError("LIVE 正在运行，不能更换钱包")
            from web3 import Web3
            from .live import validate_settings
            from .live_web_setup import build_live_settings, LiveSetupError

            public_address = payload.get("publicAddress")
            if not isinstance(public_address, str) or not Web3.is_address(public_address):
                raise ValueError("请填写有效的 Polymarket 公开钱包地址")
            password_input = payload.get("vaultPassword")
            if not isinstance(password_input, str) or len(password_input) < 12:
                raise ValueError("钱包解锁密码至少 12 个字符")
            material = derive_signer(
                payload.get("kind"),
                payload.get("secret"),
                mnemonic_passphrase=payload.get("mnemonicPassphrase", ""),
                account_index=payload.get("accountIndex", 0),
            )
            public_address = Web3.to_checksum_address(public_address)
            proposed = {
                "signer": material.address,
                "funder": public_address,
                "signature_type": 0 if material.address == public_address else 2,
            }
            ensure_wallet_identity(proposed)
            rpc_url = os.getenv("PM_POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com")
            try:
                settings = await asyncio.to_thread(
                    build_live_settings, material, public_address, rpc_url=rpc_url
                )
            except LiveSetupError as exc:
                stage = exc.as_public()["stage"]
                raise ValueError(f"钱包连接失败（{stage}），请核对地址、网络和钱包类型") from None
            validate_settings(settings)
            metadata = await asyncio.to_thread(write_vault, vault_path, settings, password_input)
            try:
                cold["LIVE"].put("live_wallet_identity", metadata)
            except Exception:
                # The vault was durably created; unlock can safely finish the
                # binding only while the ledger has no trading activity.
                wallet_state["error"] = "钱包已加密保存；账本绑定未完成，请重新解锁核对。"
                raise ValueError(wallet_state["error"]) from None
            wallet_state.update(settings=settings, metadata=metadata, error=None)
            await refresh_readiness()
            return wallet_public()

    @app.post("/api/live/wallet/unlock")
    async def unlock_live_wallet(request: Request):
        require_secure_wallet_request(request)
        payload = await wallet_body(request, {"vaultPassword"})
        async with wallet_lock:
            if not vault_path.is_file():
                raise ValueError("尚未配置加密钱包")
            settings = await verified_vault_settings(payload.get("vaultPassword"))
            from eth_account import Account
            from web3 import Web3

            metadata = {
                "signer": Account.from_key(settings["private_key"]).address,
                "funder": Web3.to_checksum_address(settings["funder"]),
                "signature_type": settings["signature_type"],
            }
            ensure_wallet_identity(metadata)
            store = cold.get("LIVE")
            if store is not None and store.get("live_wallet_identity") is None:
                store.put("live_wallet_identity", metadata)
            wallet_state.update(settings=settings, metadata=metadata, error=None)
            await refresh_readiness()
            return wallet_public()

    @app.post("/api/live/wallet/remove")
    async def remove_empty_live_wallet(request: Request):
        require_secure_wallet_request(request)
        payload = await wallet_body(request, {"vaultPassword", "confirmation"})
        if payload.get("confirmation") != "REPLACE EMPTY WALLET":
            raise ValueError("请先确认更换空白钱包")
        async with wallet_lock:
            if "LIVE" in runtimes or legacy_live_enabled:
                raise ValueError("LIVE 已启用，不能直接更换钱包")
            if ledger_has_activity(cold["LIVE"]):
                raise ValueError("LIVE 已有交易记录，不能更换钱包")
            if not vault_path.is_file():
                raise ValueError("尚未配置加密钱包")
            await verified_vault_settings(payload.get("vaultPassword"))
            # Clear the empty ledger binding first. If unlink fails, the
            # existing encrypted wallet remains available for another try.
            cold["LIVE"].put("live_wallet_identity", None)
            try:
                vault_path.unlink()
            except OSError:
                raise ValueError("钱包文件未移除，请重试或检查服务器存储") from None
            wallet_state.update(
                settings=None, metadata=None, readiness=None, approval=None, error=None
            )
            return wallet_public()

    @app.post("/api/live/wallet/check")
    async def check_live_wallet(request: Request):
        require_secure_wallet_request(request)
        await wallet_body(request, set())
        async with wallet_lock:
            wallet_state["error"] = None
            await refresh_readiness()
            return wallet_public()

    @app.post("/api/live/wallet/approve-redemption")
    async def approve_live_redemption(request: Request):
        require_secure_wallet_request(request)
        payload = await wallet_body(request, {"confirmation"})
        if payload.get("confirmation") != "APPROVE REDEMPTION":
            raise ValueError("请先确认赎回授权")
        async with wallet_lock:
            if wallet_state["settings"] is None or wallet_state["metadata"] is None:
                raise ValueError("请先解锁钱包")
            if "LIVE" in runtimes or legacy_live_enabled:
                raise ValueError("LIVE 已启用；请先核对已有赎回交易")
            ensure_wallet_identity(wallet_state["metadata"])
            if any(
                claim.get("state") == "SUBMITTED"
                for claim in cold["LIVE"].get("business")["claims"].values()
            ):
                raise ValueError("已有待确认赎回交易，不能同时发起授权")
            from .redemption import PolygonWallet, RedemptionApprovalService, RedemptionCheckError

            approval_service = RedemptionApprovalService(
                PolygonWallet(wallet_state["settings"]), root / "LIVE" / "state.sqlite"
            )
            try:
                approval_result = await asyncio.to_thread(approval_service.start)
            except RedemptionCheckError as exc:
                raise ValueError(str(exc)) from None
            except Exception:
                raise ValueError("赎回授权未完成，请检查网络后重试") from None
            await refresh_readiness()
            if approval_result.get("networkError"):
                wallet_state["approval"]["networkError"] = True
            return wallet_public()

    @app.post("/api/live/wallet/enable")
    async def enable_live_wallet(request: Request):
        require_secure_wallet_request(request)
        payload = await wallet_body(request, {"confirmation"})
        if payload.get("confirmation") != "ENABLE LIVE":
            raise ValueError("请先确认启用 LIVE")
        async with wallet_lock:
            if "LIVE" in runtimes:
                return wallet_public()
            if wallet_state["settings"] is None or wallet_state["metadata"] is None:
                raise ValueError("请先导入或解锁钱包")
            ensure_wallet_identity(wallet_state["metadata"])
            if not (await refresh_readiness())["ready"]:
                raise ValueError("实盘检查未全部通过，暂不能启用 LIVE")
            from .live import live_factory
            from .redemption import PolygonWallet

            settings = wallet_state["settings"]
            runtime = None
            try:
                wallet = PolygonWallet(settings)
                await asyncio.to_thread(wallet.preflight)
                runtime = Runtime(
                    root / "LIVE" / "state.sqlite",
                    mode="LIVE",
                    live_factory=live_factory(settings, wallet),
                )
                await runtime.client.activate()
                cold["LIVE"].close()
                cold.pop("LIVE")
                await attach("LIVE", runtime, wallet)
            except Exception as exc:
                wallet_state["error"] = f"LIVE 启用失败（{type(exc).__name__}）；未开始自动交易。"
                # Make the cold dashboard available before any awaited cleanup
                # can let another request observe the application.
                if "LIVE" not in cold:
                    cold["LIVE"] = Store(root / "LIVE" / "state.sqlite", "LIVE")
                runtimes.pop("LIVE", None)
                service = services.pop("LIVE", None)
                sampler = samplers.pop("LIVE", None)
                if runtime is not None:
                    try:
                        await runtime.client.shutdown()
                    except Exception:
                        pass
                    try:
                        runtime.store.close()
                    except Exception:
                        pass
                if service is not None:
                    try:
                        await service.close()
                    except Exception:
                        pass
                if sampler is not None:
                    sampler.task.cancel()
                    await asyncio.gather(sampler.task, return_exceptions=True)
                raise ValueError(wallet_state["error"]) from None
            wallet_state["error"] = None
            return wallet_public()

    @app.post("/api/live/wallet/backup")
    async def backup_live_wallet(request: Request):
        require_secure_wallet_request(request)
        payload = await wallet_body(request, {"vaultPassword"})
        if not isinstance(payload.get("vaultPassword"), str):
            raise ValueError("请填写钱包解锁密码")
        async with wallet_lock:
            if not vault_path.is_file():
                raise ValueError("尚未配置加密钱包")
            from .live_backup import export_live_bundle_file

            store = cold.get("LIVE") or runtimes["LIVE"].store
            bundle_path = await asyncio.to_thread(
                export_live_bundle_file, store, vault_path, payload["vaultPassword"]
            )
            return _LiveBundleResponse(
                path=bundle_path,
                media_type="application/octet-stream",
                filename="pm-nautilus-live.pmnb",
            )

    @app.post("/api/live/wallet/restore")
    async def restore_live_wallet(request: Request):
        require_secure_wallet_request(request)
        from .live_backup import MAX_BUNDLE, restore_live_bundle_file

        if request.headers.get("content-type", "").split(";", 1)[0] != "application/octet-stream":
            raise ValueError("备份上传格式无效")
        with tempfile.NamedTemporaryFile(
            prefix=".pm-live-upload-", suffix=".pmnb", dir=root / "LIVE", delete=False
        ) as temporary:
            upload_path = Path(temporary.name)
            header = bytearray()
            password_size = None
            password_input = None
            uploaded = 0
            try:
                async for chunk in request.stream():
                    remaining = memoryview(chunk)
                    while remaining:
                        if password_size is None:
                            taken = min(2 - len(header), len(remaining))
                            header.extend(remaining[:taken])
                            remaining = remaining[taken:]
                            if len(header) < 2:
                                continue
                            password_size = int.from_bytes(header, "big")
                            header.clear()
                            if not 12 <= password_size <= 16_384:
                                raise ValueError("备份密码格式无效")
                        if password_input is None:
                            taken = min(password_size - len(header), len(remaining))
                            header.extend(remaining[:taken])
                            remaining = remaining[taken:]
                            if len(header) < password_size:
                                continue
                            try:
                                password_input = header.decode("utf-8")
                            except UnicodeError:
                                raise ValueError("备份密码格式无效") from None
                            header.clear()
                        if remaining:
                            uploaded += len(remaining)
                            if uploaded > MAX_BUNDLE:
                                raise ValueError("备份文件超过网页恢复大小限制")
                            temporary.write(remaining)
                            break
                if password_input is None or uploaded == 0:
                    raise ValueError("备份输入格式无效")
                temporary.flush()
                os.fsync(temporary.fileno())
                async with wallet_lock:
                    if "LIVE" in runtimes or legacy_live_enabled:
                        raise ValueError("LIVE 已启用，不能在运行中恢复账本")
                    await asyncio.to_thread(
                        restore_live_bundle_file,
                        upload_path,
                        password_input,
                        cold["LIVE"],
                        vault_path,
                    )
                    wallet_state.update(
                        settings=None, metadata=None, readiness=None, approval=None, error=None
                    )
                    return wallet_public()
            finally:
                upload_path.unlink(missing_ok=True)

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

        async def save():
            count = 0
            if mode in runtimes:
                count = runtimes[mode].update_preferences(payload, capital)
                if public_data:
                    await services[mode].sync_subscriptions()
            else:
                prefs = Preferences(**(cold[mode].get("preferences") | payload))
                cold[mode].put("preferences", prefs.model_dump(mode="json"))
            return view(mode) | {"cancelledBuyCount": count}

        if mode == "LIVE":
            async with wallet_lock:
                return await save()
        return await save()

    @app.post("/api/{mode}/start")
    async def start(mode: str):
        mode = mode_name(mode)
        if mode not in runtimes:
            raise ValueError("LIVE 钱包尚未启用；请先完成网页钱包检查")
        if mode == "LIVE" and vault_path.is_file():
            async with wallet_lock:
                if not (await refresh_readiness())["ready"]:
                    raise ValueError("实盘账户检查未通过，不能开始交易")
                services[mode].start()
        else:
            services[mode].start()
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
        samplers["TEST"].task.cancel()
        await asyncio.gather(samplers["TEST"].task, return_exceptions=True)
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
            records(runtimes[mode], limit)
            if mode in runtimes
            else {"records": [], "totalCount": 0, "locked": True}
        )

    @app.get("/api/{mode}/validation")
    async def validate(mode: str):
        mode = mode_name(mode)
        return (
            runtimes[mode].validate()
            if mode in runtimes
            else {"ok": False, "errors": ["LIVE未连接"]}
        )

    @app.get("/api/{mode}/performance")
    async def performance(mode: str):
        mode = mode_name(mode)
        return (
            samplers[mode].view()
            if mode in samplers
            else {
                "generation": cold[mode].generation,
                "sampleSeconds": 60,
                "error": None,
                "points": [],
                "locked": True,
            }
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
