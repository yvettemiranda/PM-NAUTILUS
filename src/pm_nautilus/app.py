"""Single-process authenticated UI/API; isolated TEST and opt-in LIVE contexts."""

import argparse
import base64
import fcntl
import json
import os
import re
import secrets
import sqlite3
import tempfile
import time
from contextlib import asynccontextmanager, closing
from pathlib import Path

import asyncio
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Preferences
from .market import MarketService, PublicMarketHub
from .redemption import RedemptionService
from .store import Store
from .strategy import Runtime
from .views import dashboard, records_async, records_page_async
from .performance import PerformanceSampler
from .wallet_vault import VaultError, derive_signer, read_public_metadata, read_vault, write_vault
from .live_readiness import run_readiness
from .storage_guard import StorageGuard
from .metadata_retention import prune_cold_metadata
from .service_freshness import freshness_view
from .test_accounts import TestAccounts


class _LiveBundleResponse(FileResponse):
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            from .live_backup import cleanup_live_bundle_file

            cleanup_live_bundle_file(self.path)


async def _finish_before_cancel(operation, *, cleanup_cancelled_result=None):
    """Keep lifecycle ownership until work finishes, including repeated cancellation."""
    task = asyncio.create_task(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Shielding alone leaves the caller's wallet lock as soon as it is
        # cancelled. A to_thread worker cannot be stopped by cancelling its task.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            error = task.exception()
            if error is None and cleanup_cancelled_result is not None:
                cleanup_cancelled_result(task.result())
        raise


def create_app(data_dir=None, public_data=True, test_clock=None):
    root = Path(data_dir or os.getenv("PM_DATA_DIR", "runtime"))
    password = os.getenv("PM_UI_PASSWORD", "")
    allowed = set(os.getenv("PM_ALLOWED_HOSTS", "localhost,127.0.0.1,::1,testserver").split(","))
    if password and len(password) < 16:
        raise ValueError("服务端UI密码至少16字符")
    csrf = secrets.token_urlsafe(32)
    runtimes = {}
    activating = {}
    services = {}
    cold = {}
    samplers = {}
    test_accounts = TestAccounts(root)
    market_hub = None
    shutting_down = False
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
    storage_guard = StorageGuard(root)
    retention_task = None
    retention = {}

    def runtime_items(*, include_failed=False):
        primary = tuple(
            (mode, runtime)
            for mode, runtime in runtimes.items()
            if include_failed or mode != "TEST" or not test_accounts.contexts[1].error
        )
        return primary + tuple(
            (test_accounts.key(number), runtime)
            for number, runtime in test_accounts.runtimes.items()
            if number != 1 and (include_failed or not test_accounts.contexts[number].error)
        )

    def service_items(*, include_failed=False):
        primary = tuple(
            (mode, service)
            for mode, service in services.items()
            if include_failed or mode != "TEST" or not test_accounts.contexts[1].error
        )
        return primary + tuple(
            (test_accounts.key(number), service)
            for number, service in test_accounts.services.items()
            if number != 1 and (include_failed or not test_accounts.contexts[number].error)
        )

    def sampler_values():
        return tuple(samplers.values()) + tuple(
            sampler for number, sampler in test_accounts.samplers.items() if number != 1
        )

    def maintain_public_metadata_once():
        for mode, runtime in runtime_items():
            state = retention.setdefault(mode, {"removed": 0, "cursor": None})
            if storage_guard.status != "WARNING" or not storage_guard.can_open:
                state["status"] = "idle" if storage_guard.status == "OK" else "deferred"
                continue
            if dict(service_items())[mode].scan_status.get("scanning"):
                state["status"] = "scan_in_progress"
                continue
            result = prune_cold_metadata(
                runtime,
                time.time_ns() - 90 * 86_400_000_000_000,
                after_token_id=state["cursor"],
            )
            for token_id in result["removed_ids"]:
                runtime.tokens.pop(token_id, None)
                runtime._preview_cache.pop(token_id, None)
            state.update(
                removed=state["removed"] + result["removed"],
                cursor=result["next_token_id"],
                status=result["skipped"] or "checked",
                lastCheckedAt=time.time_ns() // 1_000_000,
                lastBatchRemoved=result["removed"],
                lastBatchScanned=result["scanned"],
            )

    async def maintain_public_metadata():
        # Bounded owner-loop work; SQLite free pages are reused without VACUUM.
        while True:
            await asyncio.sleep(60)
            maintain_public_metadata_once()

    def storage_view():
        return storage_guard.view() | {
            "metadataRetention": {
                mode: {key: value for key, value in state.items() if key != "cursor"}
                for mode, state in retention.items()
            },
            "retentionDays": 90,
            "retentionBatchLimit": 128,
            "retentionIntervalSeconds": 60,
        }

    def market_data_view():
        # Public health exposes aggregate availability only, never held token IDs,
        # market questions, wallet data, quantities or financial amounts.
        result = {}
        for mode, service in service_items():
            runtime = dict(runtime_items())[mode]
            availability = getattr(runtime, "market_availability", {})
            unavailable = {
                token_id
                for token_id, state in availability.items()
                if state.get("status") == "UNAVAILABLE"
            }
            unavailable_positions = 0
            not_ready_positions = 0
            settled = set(runtime.business.get("settled", []))
            for cycle in runtime.business.get("cycles", {}).values():
                token_id = cycle["token_id"]
                token = runtime.tokens.get(token_id)
                if cycle["quantity"] <= 0 or token is not None and token.condition_id in settled:
                    continue
                is_unavailable = getattr(runtime, "market_is_unavailable", unavailable.__contains__)
                if is_unavailable(token_id):
                    unavailable_positions += 1
                elif (
                    token is None
                    or token_id in getattr(runtime, "market_recovering", set())
                    or not runtime.books[token_id].ready
                ):
                    not_ready_positions += 1
            groups = service.stream_diagnostics()["groups"]
            ready_groups = sum(group.get("state") == "READY" for group in groups)
            recovering = (
                not_ready_positions > 0
                or bool(service.scan_status.get("streamError"))
                or ready_groups != len(groups)
            )
            result[mode] = {
                "status": "attention"
                if unavailable_positions
                else "recovering"
                if recovering
                else "ok",
                "unavailablePositionCount": unavailable_positions,
                "notReadyPositionCount": not_ready_positions,
                "readyGroupCount": ready_groups,
                "groupCount": len(groups),
                "readyBookCount": sum(group.get("readyBookCount", 0) for group in groups),
                "tokenCount": sum(group.get("tokenCount", 0) for group in groups),
            }
        return result

    def service_freshness_view():
        # Discovery belongs to the shared source, including its in-flight scan.
        # Settlement checks belong to each isolated runtime. Never replay stored
        # settlement_error as evidence of a currently failing process.
        return {
            mode: {
                "discovery": freshness_view(
                    getattr(getattr(service, "hub", None) or service, "discovery_status", {})
                ),
                "settlement": freshness_view(getattr(service, "resolution_status", {})),
            }
            for mode, service in service_items()
        }

    async def attach(mode, runtime, wallet=None, *, account_id=1):
        context = test_accounts.attach_runtime(account_id, runtime) if mode == "TEST" else None
        if context is None or account_id == 1:
            runtimes[mode] = runtime
        service = MarketService(runtime, hub=market_hub)
        if context is not None:
            context.service = service
            test_accounts.services[account_id] = service
        if context is None or account_id == 1:
            services[mode] = service
        redeem = RedemptionService(runtime, service, wallet)
        service.on_resolution = redeem.run_once
        if public_data:
            service.loop_task = asyncio.create_task(service.run())
        sampler = PerformanceSampler(runtime)
        if context is not None:
            context.sampler = sampler
            test_accounts.samplers[account_id] = sampler
        if context is None or account_id == 1:
            samplers[mode] = sampler
        sampler.task = asyncio.create_task(sampler.run())

    async def quarantine_test_account(context, exc):
        """A failed additional account never takes ownership of another consumer."""
        context.error = type(exc).__name__
        tasks = []
        if context.sampler is not None and context.sampler.task is not None:
            tasks.append(context.sampler.task)
        consumers = set()
        if context.service is not None:
            consumers.add(context.service)
        if market_hub is not None:
            consumers.update(
                service
                for service in getattr(market_hub, "services", ())
                if context.runtime is not None and service.runtime is context.runtime
            )
        for service in consumers:
            service.closed = True
            if market_hub is not None:
                getattr(market_hub, "services", set()).discard(service)
            tasks.extend(getattr(service, "tasks", {}).values())
            tasks.extend(
                task
                for task in (
                    getattr(service, "loop_task", None),
                    getattr(service, "scan_task", None),
                    getattr(service, "resolution_task", None),
                )
                if task is not None
            )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for service in consumers:
            try:
                await service.close()
            except Exception:
                pass
        runtime = context.runtime
        if runtime is not None and not getattr(runtime.store, "_closed", False):
            try:
                runtime.pause()
            except Exception:
                pass
            try:
                runtime.native.stop()
            except Exception:
                pass
            runtime.store.close()

    @asynccontextmanager
    async def lifespan(app):
        nonlocal market_hub, shutting_down, retention_task
        root.mkdir(parents=True, exist_ok=True)
        lock = (root / "process.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lock.close()
            raise RuntimeError("此数据目录已有运行实例") from exc
        try:
            shutting_down = False
            await storage_guard.start()
            from .live_backup import cleanup_stale_live_scratch

            cleanup_stale_live_scratch(root / "LIVE")
            if public_data:
                market_hub = PublicMarketHub()
                app.state.market_hub = market_hub
            await attach(
                "TEST",
                Runtime(
                    root / "TEST" / "state.sqlite", clock=test_clock, storage_guard=storage_guard
                ),
            )
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
                        storage_guard=storage_guard,
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
            for account_id in range(2, test_accounts.count + 1):
                try:
                    path = test_accounts.prepare(account_id, runtimes["TEST"].store)
                    await attach(
                        "TEST",
                        Runtime(path, clock=test_clock, storage_guard=storage_guard),
                        account_id=account_id,
                    )
                except Exception as exc:
                    await quarantine_test_account(test_accounts.contexts[account_id], exc)
            retention_task = asyncio.create_task(maintain_public_metadata())
            yield
        finally:
            shutting_down = True

            async def shutdown():
                try:
                    async with wallet_lock, test_accounts.quiesce():
                        failures = []
                        if retention_task is not None:
                            retention_task.cancel()
                            await asyncio.gather(retention_task, return_exceptions=True)
                        owned_runtimes = activating | dict(runtime_items(include_failed=True))
                        for sampler in sampler_values():
                            sampler.task.cancel()
                        await asyncio.gather(
                            *(s.task for s in sampler_values()), return_exceptions=True
                        )
                        for r in owned_runtimes.values():
                            if getattr(r.store, "_closed", False):
                                continue
                            try:
                                r.pause()
                            except Exception as exc:
                                failures.append(exc)
                        for _, service in service_items(include_failed=True):
                            try:
                                await service.close()
                            except Exception as exc:
                                failures.append(exc)
                        if market_hub is not None:
                            try:
                                await market_hub.close()
                            except Exception as exc:
                                failures.append(exc)
                        for r in owned_runtimes.values():
                            if getattr(r.store, "_closed", False):
                                continue
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
                        if failures:
                            kinds = ",".join(sorted({type(error).__name__ for error in failures}))
                            raise RuntimeError(f"组件关闭失败 ({kinds})") from None
                finally:
                    try:
                        await storage_guard.close()
                    finally:
                        lock.close()

            # Retain process ownership until every private worker and component
            # has finished, even if lifespan itself is cancelled repeatedly.
            await _finish_before_cancel(shutdown())

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtimes = runtimes
    app.state.services = services
    app.state.samplers = samplers
    app.state.test_accounts = test_accounts
    app.state.test_runtimes = test_accounts.runtimes
    app.state.test_services = test_accounts.services
    app.state.test_samplers = test_accounts.samplers
    app.state.storage_guard = storage_guard
    app.state.maintain_public_metadata_once = maintain_public_metadata_once

    @app.middleware("http")
    async def security(request, call_next):
        if shutting_down and request.method not in {"GET", "HEAD"}:
            return JSONResponse({"error": "服务正在关闭，请稍后重试"}, 503)
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

    def account_number(mode, account_id):
        if mode != "TEST":
            if account_id is not None:
                raise ValueError("account_id仅适用于TEST模式")
            return None
        return test_accounts.account_id(account_id)

    def test_context(account_id):
        context = test_accounts.contexts[test_accounts.account_id(account_id)]
        if shutting_down:
            raise HTTPException(503, "服务正在关闭")
        if context.error:
            raise HTTPException(503, "该TEST账户需重新启动程序后恢复")
        if context.resetting:
            raise HTTPException(503, "该TEST账户正在重置，请稍后重试")
        if context.runtime is None or getattr(context.runtime.store, "_closed", False):
            raise HTTPException(503, "该TEST账户暂不可用")
        return context

    async def test_operation(account_id, operation):
        context = test_accounts.contexts[test_accounts.account_id(account_id)]
        # The context/lock survives reset. Every TEST request resolves its runtime
        # after acquiring this account's lock, never from a global selection.
        # Synchronous reads and PAUSE instead use test_context without awaiting:
        # they remain responsive during an integrity reader, and PAUSE invalidates
        # the epoch of an in-flight START. A reset gates these through its phase.
        async with context.lock:
            test_context(account_id)
            # A canceled HTTP request must not release ownership while its native,
            # integrity worker or reset finalizer can still access the old store.
            return await _finish_before_cancel(operation(context))

    def test_identity(context, result):
        return result | {"accountId": context.account_id}

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
                and not ledger_has_activity(cold["LIVE"], cancellable_metadata=metadata)
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

    def ledger_has_activity(store, *, cancellable_metadata=None):
        for table in ("intents", "native_events"):
            if store.db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                return True
        business = store.get("business")
        approval = store.get("redemption_approval")
        bound = store.get("live_wallet_identity")
        cancellable = (
            isinstance(bound, dict)
            and bound == cancellable_metadata
            and type(bound.get("signature_type")) is int
            and unsigned_approval_preparation(approval, cancellable_metadata)
        )
        return any(business.get(key) for key in ("cycles", "claims", "settled")) or (
            approval is not None
            and (
                not isinstance(approval, dict)
                or (approval.get("status") == "ACTIVE" and not cancellable)
                or approval.get("pending") is not None
            )
        )

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
            token_id = cycle["token_id"]
            # Closed cycles remain program identities. A remaining balance in
            # one must still be reconciled, including a resolved losing token.
            positions[token_id] = positions.get(token_id, 0) + quantity
        return positions

    def approval_identity_matches(record, metadata):
        return (
            isinstance(record, dict)
            and type(record.get("version")) is int
            and record["version"] == 1
            and record.get("identity")
            == {
                "signer": metadata["signer"],
                "funder": metadata["funder"],
                "signatureType": metadata["signature_type"],
            }
            and type(record["identity"].get("signatureType")) is int
            and isinstance(record.get("remaining"), list)
            and bool(record["remaining"])
            and all(type(value) is bool for value in record["remaining"])
        )

    def unsigned_approval_failure(record, metadata):
        from .redemption import approval_error_public

        if (
            not approval_identity_matches(record, metadata)
            or record.get("status") != "FAILED"
            or "pending" not in record
            or record["pending"] is not None
            or record.get("stage") != "prepare"
            or not isinstance(record.get("errorCode"), str)
            or record.get("errorCode")
            not in {
                "INSUFFICIENT_POL",
                "PREPARATION_FAILED",
                "TRANSACTION_REVERTED",
                "APPROVAL_CHECK_FAILED",
            }
            or ("networkError" in record and record["networkError"] is not False)
        ):
            return None
        return approval_error_public(record)

    def unsigned_approval_preparation(record, metadata):
        # Only the canonical record saved before creating a transaction can be
        # cancelled by the user's explicit password-confirmed empty-wallet removal.
        return (
            isinstance(metadata, dict)
            and approval_identity_matches(record, metadata)
            and set(record) == {"version", "status", "identity", "remaining", "pending"}
            and record["status"] == "ACTIVE"
            and record["pending"] is None
            and record["remaining"] in ([False], [True], [False, True])
        )

    def saved_approval_diagnostic(metadata):
        from web3 import Web3

        from .redemption import ADAPTERS, APPROVAL_KEY, approval_error_public

        try:
            path = root / "LIVE" / "state.sqlite"
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                row = db.execute("SELECT value FROM meta WHERE key=?", (APPROVAL_KEY,)).fetchone()
            record = json.loads(row[0]) if row else None
            if not approval_identity_matches(record, metadata):
                return None
            if record.get("status") == "FAILED":
                error = unsigned_approval_failure(record, metadata)
                return (
                    None if error is None else {"status": "UNKNOWN", "networkError": False, **error}
                )
            if record.get("status") != "ACTIVE":
                return None
            pending = record.get("pending")
            if (
                not isinstance(pending, dict)
                or pending.get("operation") != "APPROVE"
                or type(pending.get("neg_risk")) is not bool
                or pending["neg_risk"] != record["remaining"][0]
                or type(pending.get("nonce")) is not int
                or pending["nonce"] < 0
                or not isinstance(pending.get("operator"), str)
                or pending["operator"].lower() != ADAPTERS[pending["neg_risk"]].lower()
                or not isinstance(pending.get("raw_tx"), str)
                or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})+", pending["raw_tx"])
                or not isinstance(pending.get("tx_hash"), str)
                or not re.fullmatch(r"0x[0-9a-fA-F]{64}", pending["tx_hash"])
                or Web3.to_hex(Web3.keccak(hexstr=pending["raw_tx"])).lower()
                != pending["tx_hash"].lower()
            ):
                return None
            error = approval_error_public(record)
            return {
                "status": "PENDING",
                "standardApproved": False,
                "negRiskApproved": False,
                "pendingTxHash": pending["tx_hash"],
                "networkError": error["errorCode"]
                in {"RPC_RESULT_UNKNOWN", "RPC_RECEIPT_UNAVAILABLE"},
                **error,
            }
        except (OSError, sqlite3.Error, ValueError, TypeError):
            return None

    async def refresh_readiness(*, known_approval=None):
        if wallet_state["settings"] is None or wallet_state["metadata"] is None:
            raise ValueError("请先解锁钱包")
        if known_approval is None:
            known_approval = wallet_state["approval"]
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
        if known_approval is None and wallet_state["approval"].get("status") == "UNKNOWN":
            known_approval = saved_approval_diagnostic(metadata)
            if known_approval is not None:
                result["ready"] = False
        if known_approval is not None:
            approval = wallet_state["approval"]
            known_pending = (
                known_approval.get("status") == "PENDING"
                and type(known_approval.get("standardApproved")) is bool
                and type(known_approval.get("negRiskApproved")) is bool
                and isinstance(known_approval.get("pendingTxHash"), str)
                and re.fullmatch(r"0x[0-9a-fA-F]{64}", known_approval["pendingTxHash"])
            )
            if approval.get("status") == "UNKNOWN" and known_pending:
                # A failed account read cannot erase a known saved transaction.
                # Its existence does not establish account readiness.
                wallet_state["approval"] = approval = {
                    key: known_approval[key]
                    for key in ("status", "standardApproved", "negRiskApproved", "pendingTxHash")
                }
                result["ready"] = False
            if approval.get("status") == "UNKNOWN" or (
                approval.get("status") == known_approval.get("status")
                and approval.get("pendingTxHash") == known_approval.get("pendingTxHash")
            ):
                for key in ("networkError", "errorCode", "errorMessage", "stage"):
                    if key in known_approval:
                        approval[key] = known_approval[key]
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

    def view(mode, limit=20, position_limit=None, account_id=None):
        mode = mode_name(mode)
        number = account_number(mode, account_id)
        live_status = runtimes["LIVE"].status if "LIVE" in runtimes else "LOCKED"
        if mode in runtimes:
            context = test_accounts.contexts[number] if number is not None else None
            runtime = context.runtime if context is not None else runtimes[mode]
            service = context.service if context is not None else services[mode]
            data = dashboard(runtime, service, limit, "LIVE" in runtimes, position_limit)
            data["liveStrategyStatus"] = live_status
            data["storage"] = storage_view()
            data["serviceFreshness"] = service_freshness_view()[
                test_accounts.key(number) if number is not None else mode
            ]
            if context is not None:
                data.update(accountId=number, accountCount=test_accounts.count)
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
            "storage": storage_view(),
            "serviceFreshness": {},
            "wallet": wallet_public(),
            "positionsAvailable": False,
            "strategy": {"status": "PAUSED", "initialCapital": None, "availableCash": None},
            "preferences": Preferences(**store.get("preferences")).public(),
            "capitalEditable": False,
            "positions": [],
            "positionCount": 0,
            "positionBooksReady": False,
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
        if not storage_guard.can_open:
            failures["storage"] = "storage_protection"
        if retention_task is not None and retention_task.done() and not shutting_down:
            failures["storageMaintenance"] = "metadata_maintenance_stopped"
        for number, context in test_accounts.contexts.items():
            if context.error:
                failures[test_accounts.key(number)] = "test_account_unavailable"
        for mode, service in service_items():
            runtime = dict(runtime_items())[mode]
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
        freshness = service_freshness_view()
        attention = any(
            state["status"] == "attention" for mode in freshness.values() for state in mode.values()
        )
        body = {
            "status": "degraded" if failures else "attention" if attention else "ok",
            "version": "0.1.0",
            "mode": "TEST",
            "strategyStatus": (
                "PAUSED" if test_accounts.contexts[1].error else runtimes["TEST"].status
            ),
            "liveExecutionEnabled": "LIVE" in runtimes,
            "liveWalletStatus": wallet_public()["status"],
            "revision": os.getenv("PM_GIT_REVISION", "local"),
            "backgroundErrors": failures,
            "storage": storage_view(),
            "marketData": market_data_view(),
            "serviceFreshness": freshness,
        }
        return JSONResponse(body, status_code=503 if failures else 200)

    @app.get("/api/dashboard")
    async def get_dashboard(
        mode: str = "TEST",
        limit: int = 20,
        details: str = "all",
        position_limit: int | None = None,
        account_id: str | None = None,
    ):
        if limit < 1:
            raise ValueError("limit须大于0")
        if details not in {"all", "positions", "market", "summary"}:
            raise ValueError("无效明细类型")
        if position_limit is not None and not 0 <= position_limit <= 10000:
            raise ValueError("持仓limit须为0–10000")
        mode = mode_name(mode)
        number = account_number(mode, account_id)

        def read():
            data = view(
                mode,
                limit if details in {"all", "market"} else 0,
                position_limit if details in {"all", "positions"} else 0,
                account_id=number,
            )
            data["details"] = details
            return data

        if number is not None:
            test_context(number)
        return read()

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
            from .live_web_setup import build_live_settings, discover_funder, LiveSetupError

            public_address = payload.get("publicAddress")
            if public_address is not None and (
                not isinstance(public_address, str)
                or (public_address.strip() and not Web3.is_address(public_address))
            ):
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
            rpc_url = os.getenv("PM_POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com")
            if public_address is None or not public_address.strip():
                try:
                    public_address = await asyncio.to_thread(
                        discover_funder, material.address, rpc_url=rpc_url
                    )
                except LiveSetupError:
                    raise ValueError(
                        "无法自动确认已部署的单签 Safe，请核对网络或填写官方资金地址"
                    ) from None
            public_address = Web3.to_checksum_address(public_address)
            proposed = {
                "signer": material.address,
                "funder": public_address,
                "signature_type": 0 if material.address == public_address else 2,
            }
            ensure_wallet_identity(proposed)
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
            wallet_state.update(settings=settings, metadata=metadata, approval=None, error=None)
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
            if not vault_path.is_file():
                raise ValueError("尚未配置加密钱包")
            removal_metadata = wallet_state["metadata"] or read_public_metadata(vault_path)
            if ledger_has_activity(cold["LIVE"], cancellable_metadata=removal_metadata):
                raise ValueError("LIVE 已有交易记录，不能更换钱包")
            await verified_vault_settings(payload.get("vaultPassword"))
            metadata = wallet_state["metadata"] or read_public_metadata(vault_path)
            # Clear the empty ledger binding first. If unlink fails, the
            # existing encrypted wallet remains available for another try.
            with cold["LIVE"].transaction():
                approval = cold["LIVE"].get("redemption_approval")
                unsigned_failure = unsigned_approval_failure(approval, metadata)
                unsigned_preparation = unsigned_approval_preparation(approval, metadata)
                if ledger_has_activity(cold["LIVE"], cancellable_metadata=metadata):
                    raise ValueError("LIVE 已有交易记录，不能更换钱包")
                if (
                    isinstance(approval, dict)
                    and approval.get("status") == "FAILED"
                    and unsigned_failure is None
                ):
                    raise ValueError("赎回授权失败记录尚未核对，不能更换钱包")
                cold["LIVE"].put("live_wallet_identity", None)
                if (
                    (
                        isinstance(approval, dict)
                        and approval.get("status") == "COMPLETE"
                        and approval.get("pending") is None
                    )
                    or unsigned_failure is not None
                    or unsigned_preparation
                ):
                    cold["LIVE"].put("redemption_approval", None)
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
                approval_result = await _finish_before_cancel(
                    asyncio.to_thread(approval_service.start)
                )
            except RedemptionCheckError as exc:
                raise ValueError(str(exc)) from None
            except Exception:
                raise ValueError("赎回授权未完成，请检查网络后重试") from None
            await refresh_readiness(known_approval=approval_result)
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
                    storage_guard=storage_guard,
                )
                # Own partial activation without presenting it as enabled.
                activating["LIVE"] = runtime
                await runtime.client.activate()
                cold["LIVE"].close()
                cold.pop("LIVE")
                await attach("LIVE", runtime, wallet)
                activating.pop("LIVE", None)
            except (Exception, asyncio.CancelledError) as exc:
                wallet_state["error"] = f"LIVE 启用失败（{type(exc).__name__}）；未开始自动交易。"

                async def cleanup():
                    try:
                        # Keep the cold dashboard available while cleanup awaits.
                        if "LIVE" not in cold:
                            cold["LIVE"] = Store(root / "LIVE" / "state.sqlite", "LIVE")
                    finally:
                        runtimes.pop("LIVE", None)
                        activating.pop("LIVE", None)
                        service = services.pop("LIVE", None)
                        sampler = samplers.pop("LIVE", None)
                        if sampler is not None and sampler.task is not None:
                            sampler.task.cancel()
                            await asyncio.gather(sampler.task, return_exceptions=True)
                        if service is not None:
                            try:
                                await service.close()
                            except Exception:
                                pass
                        if runtime is not None:
                            try:
                                await runtime.client.shutdown()
                            except Exception:
                                pass
                            finally:
                                runtime.store.close()

                await _finish_before_cancel(cleanup())
                if isinstance(exc, asyncio.CancelledError):
                    raise
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
            from .live_backup import cleanup_live_bundle_file, export_live_bundle_file

            store = cold.get("LIVE") or runtimes["LIVE"].store
            bundle_path = await _finish_before_cancel(
                asyncio.to_thread(
                    export_live_bundle_file, store, vault_path, payload["vaultPassword"]
                ),
                cleanup_cancelled_result=cleanup_live_bundle_file,
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

                    async def restore():
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

                    # Do not unlock the wallet or remove the upload until the
                    # worker has completed both the vault and ledger replacement.
                    return await _finish_before_cancel(restore())
            finally:
                upload_path.unlink(missing_ok=True)

    @app.get("/api/{mode}/preferences")
    async def get_preferences(mode: str, account_id: str | None = None):
        mode = mode_name(mode)
        number = account_number(mode, account_id)
        if number is not None:
            context = test_context(number)
            return view(mode, account_id=context.account_id)
        return view(mode)

    @app.put("/api/{mode}/preferences")
    async def preferences(mode: str, request: Request, account_id: str | None = None):
        mode = mode_name(mode)
        number = account_number(mode, account_id)
        payload = await request.json()
        capital = payload.pop("initialCapital", None)
        if "selectedCategories" in payload and "selectedCategoryIds" not in payload:
            payload["selectedCategoryIds"] = payload.pop("selectedCategories")
        if mode == "LIVE" and capital is not None:
            raise ValueError("LIVE资金来自实际账户，不能设置模拟资金")

        if number is not None:

            async def save_test(context):
                count = context.runtime.update_preferences(payload, capital)
                if public_data:
                    await context.service.sync_subscriptions()
                return view("TEST", account_id=number) | {"cancelledBuyCount": count}

            return await test_operation(number, save_test)

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
    async def start(mode: str, account_id: str | None = None):
        mode = mode_name(mode)
        number = account_number(mode, account_id)
        if number is not None:

            async def start_test(context):
                await context.service.start_async()
                return view("TEST", account_id=number)

            return await test_operation(number, start_test)
        if mode not in runtimes:
            raise ValueError("LIVE 钱包尚未启用；请先完成网页钱包检查")
        if mode == "LIVE" and vault_path.is_file():
            async with wallet_lock:
                if not (await refresh_readiness())["ready"]:
                    raise ValueError("实盘账户检查未通过，不能开始交易")
                await services[mode].start_async()
        else:
            await services[mode].start_async()
        return view(mode)

    @app.post("/api/{mode}/pause")
    async def pause(mode: str, account_id: str | None = None):
        mode = mode_name(mode)
        number = account_number(mode, account_id)
        if number is not None:
            context = test_context(number)
            context.runtime.pause()
            return view("TEST", account_id=number)
        if mode in runtimes:
            runtimes[mode].pause()
        return view(mode)

    @app.post("/api/{mode}/reset")
    async def reset(mode: str, request: Request, account_id: str | None = None):
        mode = mode_name(mode)
        number = account_number(mode, account_id)
        if mode != "TEST":
            raise ValueError("LIVE真实账本不能重置")
        payload = await request.json()
        if payload != {"confirmation": "RESET TEST", "finalConfirmation": "RESET TEST AGAIN"}:
            raise ValueError("必须完成TEST双重确认")

        async def reset_test(context):
            runtime = context.runtime
            if runtime.status != "PAUSED":
                raise ValueError("先暂停该TEST账户再重置")
            context.resetting = True
            try:
                context.sampler.task.cancel()
                await asyncio.gather(context.sampler.task, return_exceptions=True)
                await context.service.close()
                runtime.store.reset()
                runtime.close()
                await attach(
                    "TEST",
                    Runtime(context.path, clock=test_clock, storage_guard=storage_guard),
                    account_id=number,
                )
            except BaseException as exc:
                # Quarantine a partially replaced consumer before releasing its
                # lock. No orphan service may keep using a failed/closed ledger.
                await quarantine_test_account(context, exc)
                raise
            finally:
                context.resetting = False
            return view("TEST", account_id=number)

        return await test_operation(number, reset_test)

    @app.get("/api/{mode}/trade-records")
    async def trade_records(
        mode: str,
        limit: int = 100,
        paged: bool = False,
        cursor: str | None = None,
        since: str | None = None,
        account_id: str | None = None,
    ):
        mode = mode_name(mode)
        number = account_number(mode, account_id)
        if not 1 <= limit <= 10000:
            raise ValueError("记录limit须为1–10000")
        if number is not None:

            async def read_test(context):
                data = (
                    await records_page_async(context.runtime, limit, cursor, since)
                    if paged
                    else await records_async(context.runtime, limit)
                )
                return test_identity(context, data)

            return await test_operation(number, read_test)
        return (
            (
                await records_page_async(runtimes[mode], limit, cursor, since)
                if paged
                else await records_async(runtimes[mode], limit)
            )
            if mode in runtimes
            else {"records": [], "totalCount": 0, "locked": True}
        )

    @app.get("/api/{mode}/validation")
    async def validate(mode: str, account_id: str | None = None):
        mode = mode_name(mode)
        number = account_number(mode, account_id)
        if number is not None:

            async def validate_test(context):
                result = await context.runtime.validate_async()
                # Preserve the legacy validation shape when no account was
                # selected. New clients explicitly identify even account one.
                return test_identity(context, result) if account_id is not None else result

            return await test_operation(number, validate_test)
        if mode not in runtimes:
            return {"ok": False, "errors": ["LIVE未连接"]}
        if mode == "LIVE":
            return await runtimes[mode].client._validate_ledger()
        return await runtimes[mode].validate_async()

    @app.get("/api/{mode}/performance")
    async def performance(mode: str, account_id: str | None = None):
        mode = mode_name(mode)
        number = account_number(mode, account_id)
        if number is not None:

            async def read_test(context):
                return test_identity(context, await context.sampler.view_async())

            return await test_operation(number, read_test)
        return (
            await samplers[mode].view_async()
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
