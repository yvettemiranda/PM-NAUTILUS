"""Cheap disk reserves for new work; never pause trading or mutate a ledger.

statvfs uses the space available to the application's unprivileged UID. Polling
does not enumerate files, scan SQLite, checkpoint, VACUUM, or delete anything.
Existing exits and recovery must remain outside this guard's entry gate.
"""

import asyncio
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

GIB = 1024**3


class StoragePressure(ValueError):
    """Expected deferral before a new BUY can reach the venue."""


@dataclass(frozen=True)
class StorageThresholds:
    critical_bytes: int = GIB
    critical_percent: float = 5
    recovery_bytes: int = 2 * GIB
    recovery_percent: float = 10
    warning_bytes: int = 3 * GIB
    warning_percent: float = 15
    warning_recovery_bytes: int = 4 * GIB
    warning_recovery_percent: float = 20
    critical_inodes: int = 1000
    critical_inode_percent: float = 1
    recovery_inodes: int = 2000
    recovery_inode_percent: float = 2
    warning_inodes: int = 5000
    warning_inode_percent: float = 5
    warning_recovery_inodes: int = 7500
    warning_recovery_inode_percent: float = 7.5

    def __post_init__(self):
        for names in (
            ("critical_bytes", "recovery_bytes", "warning_bytes", "warning_recovery_bytes"),
            ("critical_percent", "recovery_percent", "warning_percent", "warning_recovery_percent"),
            ("critical_inodes", "recovery_inodes", "warning_inodes", "warning_recovery_inodes"),
            (
                "critical_inode_percent",
                "recovery_inode_percent",
                "warning_inode_percent",
                "warning_recovery_inode_percent",
            ),
        ):
            values = [getattr(self, name) for name in names]
            if not all(math.isfinite(value) and value > 0 for value in values):
                raise ValueError("Storage reserve thresholds must be positive and finite")
            if values != sorted(values):
                raise ValueError("Storage reserve thresholds must increase")
            if "percent" in names[0] and values[-1] > 100:
                raise ValueError("Storage reserve percentages cannot exceed 100")


async def _wait_owned(task):
    """A cancellation cannot abandon a running to_thread operation."""
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


class StorageGuard:
    def __init__(
        self,
        root,
        *,
        interval=30,
        max_sample_age=90,
        thresholds=None,
        statvfs=None,
        monotonic=None,
        wall_time=None,
    ):
        if not math.isfinite(interval) or not 0 < interval < max_sample_age:
            raise ValueError("Storage sample interval must be below its freshness limit")
        if not math.isfinite(max_sample_age):
            raise ValueError("Storage sample freshness limit must be finite")
        self.root = Path(root)
        self.interval = interval
        self.max_sample_age = max_sample_age
        self.thresholds = thresholds or StorageThresholds()
        self._statvfs = statvfs or os.statvfs
        self._monotonic = monotonic or time.monotonic
        self._wall_time = wall_time or time.time
        self._refresh_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._attempt = 0
        self._capacity = None
        self._sampled_at = None
        self._checked_at = None
        self._state = "UNKNOWN"
        self._reasons = ["not_checked"]
        self._error = None
        self._critical_latched = False
        self._warning_latched = False
        self._closed = False
        self._started = False
        self._worker = None
        self.task = None

    async def refresh(self):
        """Refresh before START, or join a probe already in flight."""
        attempt = self._attempt
        async with self._refresh_lock:
            if self._closed:
                return self.view()
            if attempt != self._attempt:
                return self.view()
            worker = asyncio.create_task(asyncio.to_thread(self._statvfs, self.root))
            self._worker = worker
            try:
                raw = await _wait_owned(worker)
                capacity = self._read_capacity(raw)
            except asyncio.CancelledError:
                self._unknown("probe_cancelled")
                raise
            except Exception as exc:
                self._unknown("stat_failed", type(exc).__name__)
            else:
                self._capacity = capacity
                self._error = None
                self._sampled_at = self._monotonic()
                self._checked_at = int(self._wall_time() * 1000)
                self._classify(capacity)
            finally:
                self._worker = None
                self._attempt += 1
            return self.view()

    check_now = refresh

    def _unknown(self, reason, error=None):
        self._capacity = None
        self._sampled_at = self._monotonic()
        self._checked_at = int(self._wall_time() * 1000)
        self._state = "UNKNOWN"
        self._reasons = [reason]
        self._error = error
        # Recovering an unknown measurement requires the larger safe reserve.
        self._critical_latched = True
        self._warning_latched = True

    @staticmethod
    def _read_capacity(raw):
        block_size = raw.f_frsize or raw.f_bsize
        if block_size <= 0 or raw.f_blocks <= 0 or not 0 <= raw.f_bavail <= raw.f_blocks:
            raise ValueError("Invalid available disk capacity")
        total = raw.f_blocks * block_size
        available = raw.f_bavail * block_size
        inode_total = getattr(raw, "f_files", 0)
        inode_available = getattr(raw, "f_favail", None)
        # Some filesystems (including common macOS test volumes) do not report
        # an inode limit. Zero total is unsupported, not inode exhaustion.
        if inode_total <= 0 or inode_available is None:
            inode_total = inode_available = None
        elif not 0 <= inode_available <= inode_total:
            raise ValueError("Invalid available inode capacity")
        return {
            "totalBytes": total,
            "freeBytes": available,
            "freePercent": available * 100 / total,
            "totalInodes": inode_total,
            "freeInodes": inode_available,
            "freeInodePercent": (
                inode_available * 100 / inode_total if inode_total is not None else None
            ),
        }

    def _level_reasons(self, capacity, level, *, recovering=False):
        limits = self.thresholds
        reasons = []
        dimensions = (
            ("freeBytes", "totalBytes", "bytes", "percent", "disk"),
            ("freeInodes", "totalInodes", "inodes", "inode_percent", "inodes"),
        )
        for free_key, total_key, absolute_name, percent_name, label in dimensions:
            free, total = capacity[free_key], capacity[total_key]
            if total is None:
                continue
            absolute = getattr(limits, f"{level}_{absolute_name}")
            percentage = math.ceil(total * getattr(limits, f"{level}_{percent_name}") / 100)
            below_absolute = free < absolute if recovering else free <= absolute
            below_percentage = free < percentage if recovering else free <= percentage
            if below_absolute:
                reasons.append(f"{label}_reserve")
            if below_percentage:
                reasons.append(f"{label}_percent")
        return reasons

    def _classify(self, capacity):
        critical = self._level_reasons(capacity, "critical")
        if critical:
            self._critical_latched = True
        elif not self._level_reasons(capacity, "recovery", recovering=True):
            self._critical_latched = False
        warning = self._level_reasons(capacity, "warning")
        if warning:
            self._warning_latched = True
        elif not self._level_reasons(capacity, "warning_recovery", recovering=True):
            self._warning_latched = False
        if self._critical_latched:
            self._state = "CRITICAL"
            self._reasons = critical or ["recovery_margin"]
        elif self._warning_latched:
            self._state = "WARNING"
            self._reasons = warning or ["warning_recovery_margin"]
        else:
            self._state = "OK"
            self._reasons = []

    def _effective(self):
        if self._closed:
            return "UNKNOWN", ["sampling_stopped"]
        if self._started and self.task is not None and self.task.done():
            return "UNKNOWN", ["sampling_stopped"]
        if self._sampled_at is not None:
            age = self._monotonic() - self._sampled_at
            if age < 0 or age > self.max_sample_age:
                return "UNKNOWN", ["sample_stale"]
        return self._state, self._reasons

    @property
    def status(self):
        return self._effective()[0]

    @property
    def can_open(self):
        return self.status in {"OK", "WARNING"}

    def require_safe(self):
        if not self.can_open:
            raise StoragePressure("磁盘安全储备不足或状态未知，暂不接受启动或新增买入")

    def view(self):
        status, reasons = self._effective()
        capacity = self._capacity or {
            key: None
            for key in (
                "totalBytes",
                "freeBytes",
                "freePercent",
                "totalInodes",
                "freeInodes",
                "freeInodePercent",
            )
        }
        age = self._monotonic() - self._sampled_at if self._sampled_at is not None else None
        return {
            "status": status,
            "canOpen": status in {"OK", "WARNING"},
            "checkedAt": self._checked_at,
            "sampleAgeSeconds": round(max(0, age), 3) if age is not None else None,
            "intervalSeconds": self.interval,
            "maxSampleAgeSeconds": self.max_sample_age,
            "reasons": list(reasons),
            "error": self._error,
            **capacity,
        }

    async def start(self):
        async with self._start_lock:
            if self._closed:
                raise RuntimeError("Storage guard is closed")
            if self.task is not None and not self.task.done():
                return
            await self.refresh()
            if self._closed:
                return
            self._started = True
            self.task = asyncio.create_task(self._run())

    async def _run(self):
        while not self._closed:
            await asyncio.sleep(self.interval)
            await self.refresh()

    async def close(self):
        self._closed = True

        async def finish():
            if self.task is not None:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            # An explicit START probe may be running outside the polling task.
            async with self._refresh_lock:
                pass

        await _wait_owned(asyncio.create_task(finish()))
