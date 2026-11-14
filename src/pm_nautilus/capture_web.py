"""Authenticated export and verified space recovery for TEST recordings."""

import asyncio
import os
import re
import tarfile
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field


BEIJING = timezone(timedelta(hours=8))
BLOCK = 1024 * 1024
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def archive_day(item):
    return datetime.fromtimestamp(item["startMs"] / 1000, BEIJING).date().isoformat()


def tar_archives(paths):
    # The segments are already compressed. Stream the tar without a second disk copy.
    for path in paths:
        with path.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            info = tarfile.TarInfo(path.name)
            info.size = size
            info.mode = 0o600
            yield info.tobuf(format=tarfile.USTAR_FORMAT)
            while chunk := stream.read(BLOCK):
                yield chunk
            if padding := -size % tarfile.BLOCKSIZE:
                yield bytes(padding)
    yield bytes(2 * tarfile.BLOCKSIZE)


class ReleaseSegment(BaseModel):
    id: str = Field(max_length=64)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ArchiveDownload:
    def __init__(self, content, finished, **options):
        super().__init__(content, **options)
        self.finish_export = finished

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.finish_export()


class SegmentDownload(ArchiveDownload, FileResponse):
    pass


class DayDownload(ArchiveDownload, StreamingResponse):
    pass


def capture_router(recorder):
    router = APIRouter(prefix="/api/backtest/capture")
    exports = 0
    releasing = asyncio.Lock()

    def finish_export():
        nonlocal exports
        exports -= 1

    def active_recorder():
        if recorder is None:
            raise HTTPException(404, "当前实例未配置 TEST 录制")
        return recorder

    def state():
        if recorder is None:
            return {"enabled": False, "status": "disabled", "days": []}
        rows = recorder.archives()
        days = {}
        for row in rows:
            day = archive_day(row)
            item = days.setdefault(
                day, {"day": day, "segments": 0, "bytes": 0, "startMs": row["startMs"]}
            )
            item["segments"] += 1
            item["bytes"] += row["bytes"]
            item["endMs"] = row["endMs"]
        return recorder.status() | {"days": list(reversed(days.values()))}

    @router.get("")
    async def status():
        return state()

    @router.post("/seal")
    async def seal():
        await active_recorder().seal()
        return state()

    @router.get("/archives")
    async def archives():
        return {"archives": active_recorder().archives()}

    @router.get("/archives/{name}")
    async def archive(name: str):
        nonlocal exports
        async with releasing:
            path = active_recorder().archive(name)
            if path is None:
                raise HTTPException(404, "录制分段不存在")
            exports += 1
        return SegmentDownload(
            path, finish_export, filename=path.name, media_type="application/gzip"
        )

    @router.get("/days/{day}")
    async def download_day(day: str):
        nonlocal exports
        async with releasing:
            current = active_recorder()
            paths = (
                current.archive(row["id"]) for row in current.archives() if archive_day(row) == day
            )
            if not DAY.fullmatch(day) or not (
                files := [path for path in paths if path is not None]
            ):
                raise HTTPException(404, "这一天没有已封存的录制分段")
            exports += 1
        return DayDownload(
            tar_archives(files),
            finish_export,
            media_type="application/x-tar",
            headers={"Content-Disposition": f'attachment; filename="pm-test1-{day}.tar"'},
        )

    @router.post("/release")
    async def release(segment: ReleaseSegment):
        async with releasing:
            if exports:
                raise HTTPException(409, "有录制文件正在下载，请在下载完成后重试")
            try:
                await active_recorder().release(segment.id, segment.sha256)
            except FileNotFoundError:
                raise HTTPException(404, "录制分段不存在") from None
            except (ValueError, RuntimeError) as exc:
                raise HTTPException(409, str(exc)) from None
        return {"ok": True}

    return router
