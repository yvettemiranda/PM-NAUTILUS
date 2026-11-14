"""Authenticated, disk-bounded historical replay jobs outside the strategy process."""

import asyncio
import gzip
import json
import os
import re
import shutil
import sys
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, ConfigDict, Field

from .config import SCALE, Preferences, micros

MIB = 1024**2
UPLOAD_CHUNK = 768 * 1024  # Below the standard HTTPS reverse-proxy body limit.
MAX_UPLOAD = 256 * MIB
MAX_DATASETS = 8
MAX_JOB_SECONDS = 900
MAX_WORKER_ANONYMOUS_KIB = 384 * 1024
MAX_HEADER = 16 * 1024
UPLOAD_TTL = 30 * 60
ROW_ID = re.compile(r"[0-9a-f]{32}")


def dataset_info(path, source_id):
    with path.open("rb") as stream:
        compressed = stream.read(2) == b"\x1f\x8b"
        stream.seek(0)
        if compressed:
            with gzip.GzipFile(fileobj=stream) as decoded:
                line = decoded.readline(MAX_HEADER + 1)
        else:
            line = stream.readline(MAX_HEADER + 1)
    if not line or len(line) > MAX_HEADER:
        raise ValueError("行情包缺少有效的文件头")
    obj = json.loads(line)
    from .backtest_engine import validate_header

    if not isinstance(obj, dict):
        raise ValueError("行情包缺少有效的文件头")
    label, first, last, coverage, notes = validate_header(obj)
    return {
        "id": source_id,
        "label": label,
        "startMs": first,
        "endMs": last,
        "coverage": coverage,
        "notes": notes,
        "bytes": path.stat().st_size,
    }


def owned_dir(path):
    path.mkdir(mode=0o700, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise OSError("invalid backtest directory")
    return path


@dataclass
class Upload:
    path: Path
    touched: float
    received: int = 0


@dataclass
class Job:
    id: str
    source_id: str
    directory: Path
    process: asyncio.subprocess.Process
    status: str = "running"
    error: str | None = None
    task: asyncio.Task | None = None
    cancel_requested: bool = False

    @property
    def result(self):
        return self.directory / "result.json"


class BacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sourceId: str = Field(pattern=r"^[0-9a-f]{32}$")
    startMs: int = Field(strict=True, ge=1, le=4_102_444_800_000)
    endMs: int = Field(strict=True, ge=1, le=4_102_444_800_000)
    initialCash: str = Field(max_length=24, pattern=r"^[0-9]{1,7}(\.[0-9]{1,6})?$")
    preferences: dict


class BacktestService:
    def __init__(self, data_root):
        self.root = Path(data_root).absolute() / "backtest"
        self.datasets = self.root / "datasets"
        self.work = self.root / "jobs"
        self.mutex = asyncio.Lock()
        self.upload = None
        self.jobs = {}
        self.closed = False

    def prepare(self):
        owned_dir(self.root)
        owned_dir(self.datasets)
        owned_dir(self.work)

    def check_space(self):
        volume = os.statvfs(self.root)
        # Leave space for an existing ledger, even when a job and upload overlap.
        reserve = max(4 * 1024 * MIB, volume.f_blocks * volume.f_frsize // 5)
        if volume.f_bavail * volume.f_frsize < reserve + 1536 * MIB:
            raise HTTPException(507, "磁盘空间不足，暂停新增回测任务或行情数据")

    def catalog(self):
        result = []
        for path in sorted(self.datasets.glob("*.ndjson")):
            if ROW_ID.fullmatch(path.stem) and path.is_file() and not path.is_symlink():
                try:
                    result.append(dataset_info(path, path.stem))
                except (OSError, EOFError, ValueError, KeyError, RecursionError, zlib.error):
                    continue
        return result

    def start_upload(self):
        self.prepare()
        self.check_space()
        now = time.monotonic()
        if self.upload:
            key, pending = self.upload
            if now - pending.touched < UPLOAD_TTL:
                raise HTTPException(409, "已有行情包正在上传")
            pending.path.unlink(missing_ok=True)
        if len(self.catalog()) >= MAX_DATASETS:
            raise HTTPException(409, "已达到行情包数量上限，请先移除不用的回测行情包")
        # After a restart only application-owned incomplete uploads are discarded.
        for path in self.datasets.glob("*.upload"):
            if ROW_ID.fullmatch(path.stem):
                path.unlink(missing_ok=True)
        key = uuid4().hex
        path = self.datasets / f"{key}.upload"
        path.touch(mode=0o600, exist_ok=False)
        self.upload = key, Upload(path, now)
        return key

    def get_upload(self, key):
        if self.upload is None or self.upload[0] != key:
            raise HTTPException(404, "找不到此上传")
        return self.upload[1]

    def append(self, key, offset, content):
        pending = self.get_upload(key)
        if pending.received != offset:
            raise HTTPException(409, "上传分段偏移不匹配")
        if not content or offset + len(content) > MAX_UPLOAD:
            raise HTTPException(413, "行情包太大或上传分段为空")
        self.check_space()
        with pending.path.open("r+b") as stream:
            stream.seek(offset)
            try:
                stream.write(content)
                stream.flush()
            except OSError:
                stream.truncate(offset)
                raise
        pending.received += len(content)
        pending.touched = time.monotonic()
        return pending.received

    def finish_upload(self, key):
        pending = self.get_upload(key)
        try:
            info = dataset_info(pending.path, key)
        except (ValueError, KeyError, OSError, EOFError, RecursionError, zlib.error):
            self.drop_upload(key)
            raise HTTPException(400, "请按文档准备 NDJSON 或 gzip 压缩的 NDJSON 行情包") from None
        pending.path.replace(self.datasets / f"{key}.ndjson")
        self.upload = None
        return info

    def drop_upload(self, key):
        if self.upload is not None and self.upload[0] == key:
            self.upload[1].path.unlink(missing_ok=True)
            self.upload = None

    async def run(self, payload):
        if self.closed:
            raise HTTPException(503, "回测服务正在关闭")
        if any(j.status == "running" for j in self.jobs.values()):
            raise HTTPException(409, "已有回测在运行")
        provided = next((d for d in self.catalog() if d["id"] == payload.sourceId), None)
        if provided is None:
            raise HTTPException(404, "找不到此行情包")
        if not provided["startMs"] <= payload.startMs < payload.endMs <= provided["endMs"]:
            raise HTTPException(422, "回测日期超出行情包范围")
        prefs = Preferences.from_profile(payload.preferences)
        initial = micros(payload.initialCash)
        if not 0 < initial <= 1_000_000 * SCALE or prefs.budget > initial:
            raise HTTPException(422, "初始资金无效，且须覆盖单轮预算")
        self.prepare()
        self.check_space()
        # Bound disk use across restarts. Only these job folders belong to this service.
        if not self.jobs:
            for path in self.work.iterdir():
                if ROW_ID.fullmatch(path.name) and path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
        while len(self.jobs) >= 4:
            old = self.jobs.pop(next(iter(self.jobs)))
            shutil.rmtree(old.directory)
        key = uuid4().hex
        directory = self.work / key
        directory.mkdir(mode=0o700)
        request_path = directory / "request.json"
        request_path.write_text(
            json.dumps(
                {
                    "id": key,
                    "preferences": prefs.model_dump(mode="json"),
                    "initialCash": payload.initialCash,
                    "startMs": payload.startMs,
                    "endMs": payload.endMs,
                }
            ),
            encoding="utf-8",
        )
        source = self.datasets / f"{payload.sourceId}.ndjson"
        env = {
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "MALLOC_ARENA_MAX": "2",
            "RAYON_NUM_THREADS": "1",
        }
        if "PYTHONPATH" in os.environ:
            env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "pm_nautilus.backtest_worker",
                str(source),
                str(request_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
        except BaseException:
            shutil.rmtree(directory)
            raise
        job = Job(key, payload.sourceId, directory, process)
        self.jobs[key] = job
        job.task = asyncio.create_task(self.observe(job))
        return job

    @staticmethod
    def worker_memory_kib(pid):
        """Count resident anonymous memory; the runtime also maps shared libraries."""
        if sys.platform != "linux":
            return 0
        try:
            with Path(f"/proc/{pid}/status").open("rb") as stream:
                for line in stream:
                    if line.startswith(b"RssAnon:"):
                        return int(line.split()[1])
        except FileNotFoundError:
            pass  # The worker has just exited.
        return 0

    async def observe(self, job):
        waiter = asyncio.create_task(job.process.wait())
        error = None
        try:
            async with asyncio.timeout(MAX_JOB_SECONDS):
                while not waiter.done():
                    if self.worker_memory_kib(job.process.pid) > MAX_WORKER_ANONYMOUS_KIB:
                        error = "回测达到内存上限，请拆分日期或在独立机器上运行"
                        break
                    await asyncio.wait({waiter}, timeout=0.2)
        except TimeoutError:
            error = "回测超过运行时间限制，请拆分日期或在独立机器上运行"
        except (OSError, ValueError):
            error = "无法核对回测进程的资源用量"
        if error:
            await self.stop_process(job.process)
        code = await waiter
        if job.cancel_requested:
            job.status = "cancelled"
        elif error is None and code == 0 and job.result.is_file():
            job.status = "completed"
        else:
            job.status = "failed"
            failure = job.directory / "error.txt"
            job.error = error or (
                failure.read_text(encoding="utf-8")[:500]
                if failure.is_file()
                else "回测超过资源限制或任务异常，请检查行情包并缩小范围"
            )
        # SIGTERM can interrupt the engine before it can dispose its private ledger.
        for path in job.directory.glob("pm-backtest-*"):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)

    @staticmethod
    async def stop_process(process):
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                async with asyncio.timeout(2):
                    await process.wait()
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()

    async def cancel(self, job):
        if job.status == "running":
            job.cancel_requested = True
            await self.stop_process(job.process)
            await job.task

    async def close(self):
        async with self.mutex:
            self.closed = True
            if self.upload:
                self.drop_upload(self.upload[0])
            for job in self.jobs.values():
                await self.cancel(job)


async def request_chunk(request):
    data = bytearray()
    try:
        async with asyncio.timeout(25):
            async for chunk in request.stream():
                if len(data) + len(chunk) > UPLOAD_CHUNK:
                    raise HTTPException(413, "请分段上传行情包")
                data.extend(chunk)
    except TimeoutError:
        raise HTTPException(408, "上传分段超时") from None
    return data


def backtest_router(service, defaults):
    router = APIRouter(prefix="/api/backtest")

    @router.get("/defaults")
    async def get_defaults():
        return defaults()

    @router.get("/datasets")
    async def datasets():
        return {
            "datasets": service.catalog(),
            "maxUploadBytes": MAX_UPLOAD,
            "uploadChunkBytes": UPLOAD_CHUNK,
        }

    @router.post("/uploads", status_code=201)
    async def begin_upload():
        async with service.mutex:
            return {"id": service.start_upload()}

    @router.put("/uploads/{key}")
    async def upload_part(request: Request, key: str, offset: int = Query(ge=0, le=MAX_UPLOAD)):
        data = await request_chunk(request)
        async with service.mutex:
            return {"receivedBytes": service.append(key, offset, data)}

    @router.post("/uploads/{key}/finish")
    async def publish_upload(key: str):
        async with service.mutex:
            return {"dataset": service.finish_upload(key)}

    @router.delete("/uploads/{key}")
    async def abandon_upload(key: str):
        async with service.mutex:
            service.drop_upload(key)
            return {"ok": True}

    @router.put("/datasets", status_code=201)
    async def upload_small_dataset(request: Request):
        data = await request_chunk(request)
        async with service.mutex:
            key = service.start_upload()
            try:
                service.append(key, 0, data)
                return {"dataset": service.finish_upload(key)}
            finally:
                service.drop_upload(key)

    @router.delete("/datasets/{key}")
    async def remove_dataset(key: str):
        async with service.mutex:
            if not ROW_ID.fullmatch(key):
                raise HTTPException(404, "找不到此行情包")
            if any(j.source_id == key and j.status == "running" for j in service.jobs.values()):
                raise HTTPException(409, "当前回测正在读取此行情包")
            (service.datasets / f"{key}.ndjson").unlink(missing_ok=True)
            return {"ok": True}

    def get_job(key):
        if key not in service.jobs:
            raise HTTPException(404, "回测任务已结束或服务已重启")
        return service.jobs[key]

    def job_status(job):
        payload = {"id": job.id, "status": job.status}
        if job.error:
            payload["error"] = job.error
        return payload

    @router.post("/jobs", status_code=202)
    async def run_job(payload: BacktestRequest):
        async with service.mutex:
            return job_status(await service.run(payload))

    @router.get("/jobs/{key}")
    async def read_job(key: str):
        job = get_job(key)
        if job.status == "completed":
            stream = job.result.open("rb")
            return StreamingResponse(
                iter(lambda: stream.read(128 * 1024), b""),
                media_type="application/json",
                background=BackgroundTask(stream.close),
            )
        return job_status(job)

    @router.delete("/jobs/{key}")
    async def cancel_job(key: str):
        async with service.mutex:
            job = get_job(key)
            await service.cancel(job)
            return job_status(job)

    return router
