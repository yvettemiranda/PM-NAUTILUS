"""Disk-bounded TEST observations, written off the market-data event loop."""

import asyncio
import fcntl
import gzip
import hashlib
import logging
import os
import re
import secrets
import stat
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import msgspec

MIB = 1024**2
GIB = 1024 * MIB
MAX_LINE_BYTES = 16 * MIB
WRITE_BYTES = 512 * 1024
WRITE_ROWS = 2048
WRITE_INTERVAL = 0.25
DISK_CHECK_INTERVAL = 15
MAX_TIME_MS = 4_102_444_800_000
ARCHIVE_NAME = re.compile(r"(?P<start>[0-9]{13})-(?P<end>[0-9]{13})-[0-9a-f]{12}\.ndjson\.gz")
PART_NAME = re.compile(r"\.capture-[0-9a-f]{12}\.part")
CHECKSUM = re.compile(r"[0-9a-f]{64}")
CAPTURE_SCOPE = {"maxBuyPriceMicros": 50_000, "marketCoverage": "test-subscribed"}

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Archive:
    id: str
    bytes: int
    start_ms: int
    end_ms: int

    def public(self):
        return {
            "id": self.id,
            "bytes": self.bytes,
            "startMs": self.start_ms,
            "endMs": self.end_ms,
        }


@dataclass
class Segment:
    nonce: str
    path: Path
    stream: BinaryIO
    start_ms: int
    end_ms: int
    bytes: int = 0


@dataclass(frozen=True)
class Batch:
    records: tuple[dict, ...]
    bytes: int


@dataclass(frozen=True)
class Seal:
    future: asyncio.Future


class CapturePressure(Exception):
    """The archive hit a configured retention or disk reserve."""

    def __init__(self, reason, needed):
        super().__init__(reason)
        self.needed = needed


def _record_storage(value):
    """Conservative size of a row's owned containers and shared references."""
    if type(value) is dict:
        return 96 + 40 * len(value) + sum(_record_storage(item) for item in value.values())
    if type(value) in (list, tuple):
        return 64 + 8 * len(value) + sum(_record_storage(item) for item in value)
    if type(value) is msgspec.Raw:
        return 96 + len(value)
    # Token is immutable and the producer passes existing registry references.
    if type(value) is str:
        return 64 + 4 * len(value)
    return 96 if type(value) not in (int, bool, type(None)) else 32


class CaptureRecorder:
    """Accept owned observations without blocking and write immutable gzip files.

    Control and quote rows share one FIFO. Callers can use `epoch` to detect lost
    observations and publish a fresh baseline. The recorder precedes resumed
    submissions with an explicit gap and never deletes sealed archives on pressure.
    """

    def __init__(
        self,
        path,
        *,
        max_bytes=4 * GIB,
        min_free_bytes=12 * GIB,
        min_free_percent=20,
        max_queue_bytes=16 * MIB,
        segment_seconds=900,
        segment_bytes=32 * MIB,
    ):
        if (
            min(max_bytes, max_queue_bytes, segment_seconds, segment_bytes) <= 0
            or min_free_bytes < 0
            or not 0 <= min_free_percent < 100
        ):
            raise ValueError("无效的 TEST 采集容量配置")
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.min_free_bytes = min_free_bytes
        self.min_free_percent = min_free_percent
        self.max_queue_bytes = max_queue_bytes
        self.segment_seconds = segment_seconds
        self.segment_bytes = segment_bytes

        self.epoch = 0
        self._queue = deque()
        self._queue_bytes = 0
        self._queue_dropped = 0
        self._gap_count = 0
        self._last_accepted = 0
        self._last_write = None
        self._pending_gap = True
        self._activity = "idle"
        self._error = None
        self._state = "idle"
        self._worker = None
        self._closing = False
        self._wake = asyncio.Event()
        self._io_lock = asyncio.Lock()
        self._seal_lock = asyncio.Lock()

        # Accessed in the I/O worker under _io_lock. The app copies the published
        # archive view from the event loop without probing the filesystem.
        self._lock_file = None
        self._catalog = ()
        self._latest_catalog = ()
        self._latest_bytes = 0
        self._active = None
        self._sealed_bytes = 0
        self._previous_archive = None
        self._disk_deadline = 0
        self._encoder = msgspec.json.Encoder()

    @property
    def accepting(self):
        return (
            self._worker is not None
            and not self._worker.done()
            and not self._closing
            and self._state == "ready"
        )

    def start(self):
        if self._closing:
            raise RuntimeError("TEST 采集已关闭")
        if self._worker is None:
            self._state = "pending"
            self._worker = asyncio.create_task(self._run())

    def status(self):
        return {
            "enabled": True,
            "status": self._activity if self._state == "ready" else self._state,
            "bytes": self._latest_bytes,
            "maxBytes": self.max_bytes,
            "queueBytes": self._queue_bytes,
            "maxQueueBytes": self.max_queue_bytes,
            "queueDropped": self._queue_dropped,
            "gapCount": self._gap_count,
            "lastWriteMs": self._last_write,
            "sealedCount": len(self._latest_catalog),
            "error": self._error,
        }

    def archives(self):
        return tuple(item.public() for item in self._latest_catalog)

    def archive(self, identifier):
        if type(identifier) is not str or not ARCHIVE_NAME.fullmatch(identifier):
            return None
        if not any(item.id == identifier for item in self._latest_catalog):
            return None
        path = self.path / identifier
        try:
            return path if stat.S_ISREG(path.lstat().st_mode) else None
        except FileNotFoundError:
            return None

    def submit(self, record):
        return self.submit_many((record,))

    def _loss(self, dropped):
        self._queue_dropped += dropped
        if not self._pending_gap:
            self.epoch += 1
        self._pending_gap = True

    def submit_many(self, records):
        """Enqueue a chronological group. Producers own and freeze its containers."""
        records = tuple(records)
        if not records or not self.accepting:
            return False
        # Timestamp comes from local observation, not the exchange clocks.
        previous = self._last_accepted
        size = 0
        for row in records:
            at = row.get("atMs")
            if type(at) is not int or not max(previous, 1) <= at <= MAX_TIME_MS:
                self._loss(len(records))
                return False
            previous = at
            estimate = _record_storage(row)
            if estimate > MAX_LINE_BYTES:
                self._loss(len(records))
                return False
            size += estimate
            if self._queue_bytes + size > self.max_queue_bytes:
                self._loss(len(records))
                return False
        if self._pending_gap:
            gap = {"type": "gap", "atMs": records[0]["atMs"]}
            size += _record_storage(gap)
            if self._queue_bytes + size > self.max_queue_bytes:
                self._loss(len(records))
                return False
            if records[0].get("type") != "gap" or "tokenIds" in records[0]:
                records = (gap, *records)
            self._pending_gap = False
        self._queue.append(Batch(records, size))
        self._queue_bytes += size
        self._last_accepted = previous
        self._wake.set()
        return True

    async def seal(self):
        """Seal all accepted rows up to this point and return the latest archive."""
        async with self._seal_lock:
            if self._worker is None or self._worker.done() or self._closing:
                return None
            future = asyncio.get_running_loop().create_future()
            self._queue.append(Seal(future))
            self._wake.set()
            return await future

    async def release(self, identifier, sha256):
        """Free a sealed local copy after checking the caller's downloaded checksum."""
        if type(sha256) is not str or not CHECKSUM.fullmatch(sha256):
            raise ValueError("SHA-256 须为 64 位小写十六进制")
        if self.archive(identifier) is None:
            raise FileNotFoundError("采集分段不存在")
        try:
            await self._io(self._release, identifier, sha256)
        finally:
            self._wake.set()

    async def _owned_io(self, function, *args):
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled():
                task.exception()
            raise

    async def close(self):
        self._closing = True
        self._wake.set()
        if self._worker is not None:
            try:
                await asyncio.shield(self._worker)
            except asyncio.CancelledError:
                # Preserve the queued writer's lock ownership during shutdown.
                while not self._worker.done():
                    try:
                        await asyncio.shield(self._worker)
                    except asyncio.CancelledError:
                        continue
                raise
        self._state = "closed"

    def _refresh_view(self):
        self._latest_catalog = self._catalog
        self._latest_bytes = self._sealed_bytes + (self._active.bytes if self._active else 0)

    async def _io(self, function, *args):
        async with self._io_lock:
            try:
                return await self._owned_io(function, *args)
            finally:
                self._refresh_view()

    def _discard_queue(self):
        while self._queue:
            part = self._queue.popleft()
            if isinstance(part, Seal):
                if not part.future.done():
                    part.future.set_result(None)
            else:
                self._loss(len(part.records))
        self._queue_bytes = 0
        self._pending_gap = True

    async def _run(self):
        try:
            await self._io(self._prepare)
            if self._catalog:
                self._last_accepted = self._catalog[-1].end_ms
            next_probe = 0
            probe_bytes = 1
            while not self._closing or self._queue:
                now = time.monotonic()
                if self._state != "ready" and now >= next_probe:
                    try:
                        await self._io(self._available, probe_bytes)
                    except CapturePressure as exc:
                        self._state = str(exc)
                        next_probe = now + DISK_CHECK_INTERVAL
                    else:
                        self._state = "ready"
                        self._error = None
                self._wake.clear()
                if self._queue:
                    if self._state != "ready":
                        self._discard_queue()
                        continue
                    if isinstance(self._queue[0], Seal):
                        commit = self._queue.popleft()
                        try:
                            await self._io(self._close_segment)
                        finally:
                            if not commit.future.done():
                                commit.future.set_result(
                                    self._catalog[-1].public() if self._catalog else None
                                )
                        continue
                    if not self._closing and len(self._queue) < WRITE_ROWS:
                        if self._queue_bytes < WRITE_BYTES and not isinstance(
                            self._queue[-1], Seal
                        ):
                            await asyncio.sleep(WRITE_INTERVAL)
                    records, size = self._take_batch()
                    try:
                        await self._io(self._write, records)
                    except CapturePressure as exc:
                        self._loss(len(records))
                        self._discard_queue()
                        self._state = str(exc)
                        self._activity = "idle"
                        probe_bytes = exc.needed
                        next_probe = time.monotonic() + DISK_CHECK_INTERVAL
                        await self._io(self._close_segment)
                    except Exception:
                        self._loss(len(records))
                        raise
                    else:
                        self._last_write = records[-1]["atMs"]
                        self._gap_count += sum(row.get("type") == "gap" for row in records)
                        for row in records:
                            if row.get("type") == "control":
                                status = row.get("status")
                                self._activity = "recording" if status == "RUNNING" else "paused"
                    finally:
                        self._queue_bytes -= min(size, self._queue_bytes)
                    continue
                if self._active and time.monotonic() >= self._segment_deadline:
                    await self._io(self._close_segment)
                if self._closing:
                    break
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1)
                    if self._state in {"disk_low", "quota_full"}:
                        next_probe = 0
                except TimeoutError:
                    pass
        except Exception as exc:
            if not self._pending_gap:
                self.epoch += 1
            self._state = "error"
            self._error = type(exc).__name__
            log.warning("TEST recording stopped: %s", self._error)
            self._discard_queue()
        finally:
            try:
                await self._io(self._finish)
            except OSError as exc:
                self._state = "error"
                self._error = type(exc).__name__
                log.warning("TEST recording could not seal: %s", self._error)

    def _take_batch(self):
        records = []
        size = 0
        while self._queue and isinstance(self._queue[0], Batch):
            part = self._queue[0]
            if records and (size + part.bytes > WRITE_BYTES or len(records) >= WRITE_ROWS):
                break
            self._queue.popleft()
            records.extend(part.records)
            size += part.bytes
        return records, size

    def _prepare(self):
        self.path.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.path.is_symlink() or not self.path.is_dir():
            raise OSError("invalid TEST recording directory")
        self._lock_file = (self.path / ".capture.lock").open("ab")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock_file.close()
            self._lock_file = None
            raise
        catalog = []
        for path in self.path.iterdir():
            info = path.lstat()
            if PART_NAME.fullmatch(path.name) and stat.S_ISREG(info.st_mode):
                path.unlink()
                continue
            match = ARCHIVE_NAME.fullmatch(path.name)
            if match and stat.S_ISREG(info.st_mode):
                first, last = int(match["start"]), int(match["end"])
                if not 0 < first <= last <= MAX_TIME_MS:
                    continue
                item = Archive(path.name, info.st_size, first, last)
                catalog.append(item)
                self._sealed_bytes += item.bytes
        self._catalog = tuple(sorted(catalog, key=lambda row: row.id))
        if self._catalog:
            self._previous_archive = self._catalog[-1]

    def _available(self, needed=1):
        if (
            self._sealed_bytes + (self._active.bytes if self._active else 0) + needed
            > self.max_bytes
        ):
            raise CapturePressure("quota_full", needed)
        now = time.monotonic()
        if now < self._disk_deadline:
            return
        volume = os.statvfs(self.path)
        block = volume.f_frsize or volume.f_bsize
        total = volume.f_blocks * block
        free = volume.f_bavail * block
        if (
            block <= 0
            or free - needed < max(self.min_free_bytes, total * self.min_free_percent / 100)
            or (volume.f_files > 0 and volume.f_favail <= 5000)
        ):
            raise CapturePressure("disk_low", needed)
        self._disk_deadline = now + DISK_CHECK_INTERVAL

    def _pack(self, data):
        return gzip.compress(data, compresslevel=1, mtime=0)

    def _json_line(self, value):
        encoded = self._encoder.encode(value)
        if len(encoded) + 1 > MAX_LINE_BYTES:
            raise ValueError("TEST 采集单条观察超长")
        return encoded + b"\n"

    def _header(self, first):
        return self._pack(
            self._json_line(
                {
                    "type": "capture_segment",
                    "version": 1,
                    "startMs": first,
                    "captureScope": CAPTURE_SCOPE,
                    "previous": self._previous_archive.id if self._previous_archive else None,
                }
            )
        )

    def _footer(self, nonce, first, last):
        identifier = f"{first:013d}-{last:013d}-{nonce}.ndjson.gz"
        body = self._pack(self._json_line({"type": "capture_end", "endMs": last, "id": identifier}))
        return identifier, body

    def _nonce(self, first):
        previous = self._previous_archive
        if previous and previous.start_ms == previous.end_ms == first:
            # Preserve filename order if a manual seal splits observations
            # bearing the same millisecond. The first nonce leaves headroom.
            previous_nonce = previous.id.split("-")[2].split(".")[0]
            return f"{int(previous_nonce, 16) + 1:012x}"
        return "00" + secrets.token_hex(5)

    def _write(self, records):
        first, last = records[0]["atMs"], records[-1]["atMs"]
        compressed = self._pack(b"".join(self._json_line(item) for item in records))
        stage = self._active
        if stage and (
            time.monotonic() >= self._segment_deadline
            or stage.bytes + len(compressed) > self.segment_bytes
        ):
            self._close_segment()
            stage = None
        nonce = stage.nonce if stage else self._nonce(first)
        header = b"" if stage else self._header(first)
        _, footer = self._footer(nonce, stage.start_ms if stage else first, last)
        self._available(len(header) + len(compressed) + len(footer))
        if stage is None:
            path = self.path / f".capture-{nonce}.part"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            self._active = stage = Segment(
                nonce, path, os.fdopen(fd, "wb", buffering=0), first, first
            )
            self._segment_deadline = time.monotonic() + self.segment_seconds
        try:
            self._write_bytes(stage.stream, header + compressed)
        except OSError:
            self._rollback(stage)
            if not stage.bytes:
                self._remove_open_segment()
            raise
        stage.bytes += len(header) + len(compressed)
        stage.end_ms = last

    @staticmethod
    def _rollback(stage):
        stage.stream.truncate(stage.bytes)
        stage.stream.seek(stage.bytes)

    @staticmethod
    def _write_bytes(stream, data):
        remaining = memoryview(data)
        while remaining:
            count = stream.write(remaining)
            if not count:
                raise OSError("short capture write")
            remaining = remaining[count:]

    def _close_segment(self):
        stage = self._active
        if not stage:
            return
        if not stage.bytes:
            self._remove_open_segment()
            return
        identifier, footer = self._footer(stage.nonce, stage.start_ms, stage.end_ms)
        try:
            self._write_bytes(stage.stream, footer)
            os.fsync(stage.stream.fileno())
        except OSError:
            self._rollback(stage)
            raise
        stage.stream.close()
        self._active = None
        path = self.path / identifier
        # Hard link publish is atomic and will not replace another file.
        os.link(stage.path, path)
        item = Archive(identifier, stage.bytes + len(footer), stage.start_ms, stage.end_ms)
        self._catalog += (item,)
        self._sealed_bytes += item.bytes
        self._previous_archive = item
        stage.path.unlink()
        self._sync_directory()

    def _remove_open_segment(self):
        stage = self._active
        self._active = None
        stage.stream.close()
        stage.path.unlink(missing_ok=True)

    def _sync_directory(self):
        fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _finish(self):
        try:
            self._close_segment()
        finally:
            if self._active:
                self._remove_open_segment()
            if self._lock_file:
                self._lock_file.close()
                self._lock_file = None

    def _release(self, identifier, sha256):
        if not self._catalog or self._catalog[0].id != identifier:
            raise ValueError("请从最早的采集分段依次释放空间")
        path = self.path / identifier
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != self._catalog[0].bytes:
                raise ValueError("采集分段大小与清单不符")
            if not secrets.compare_digest(
                hashlib.file_digest(source, "sha256").hexdigest(), sha256
            ):
                raise ValueError("下载校验值与服务器文件不符")
            current = path.lstat()
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError("采集分段已换位，请重新校验")
        path.unlink()
        self._sealed_bytes -= self._catalog[0].bytes
        self._catalog = self._catalog[1:]
        self._disk_deadline = 0
        self._sync_directory()
