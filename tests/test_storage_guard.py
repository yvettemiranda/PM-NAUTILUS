import asyncio
import threading
from types import SimpleNamespace

import pytest

from pm_nautilus.storage_guard import GIB, StorageGuard, StorageThresholds


def capacity(free=10 * GIB, total=20 * GIB, *, inodes=100_000, inode_total=100_000):
    return SimpleNamespace(
        f_frsize=1,
        f_bsize=4096,
        f_blocks=total,
        f_bavail=free,
        f_bfree=total,
        f_files=inode_total,
        f_favail=inodes,
    )


def test_guard_requires_first_valid_sample_and_reports_only_capacity(tmp_path):
    guard = StorageGuard(tmp_path, statvfs=lambda _: capacity())
    assert guard.status == "UNKNOWN"
    assert not guard.can_open
    with pytest.raises(ValueError, match="磁盘"):
        guard.require_safe()
    report = asyncio.run(guard.refresh())
    assert report["status"] == "OK"
    assert report["freeBytes"] == 10 * GIB
    assert report["freePercent"] == 50
    assert report["freeInodes"] == 100_000
    assert report["canOpen"]
    assert report["checkedAt"]
    assert str(tmp_path) not in str(report)
    guard.require_safe()
    assert list(tmp_path.iterdir()) == []


def test_byte_reserve_hysteresis_does_not_flap_and_warning_allows_entry(tmp_path):
    current = [capacity()]
    guard = StorageGuard(tmp_path, statvfs=lambda _: current[0])

    async def run():
        for free, expected, allowed in (
            (GIB, "CRITICAL", False),
            (GIB + 1, "CRITICAL", False),
            (2 * GIB - 1, "CRITICAL", False),
            (2 * GIB, "WARNING", True),
            (3 * GIB + 1, "WARNING", True),
            (4 * GIB, "OK", True),
            (3 * GIB + 1, "OK", True),
            (3 * GIB, "WARNING", True),
        ):
            current[0] = capacity(free)
            await guard.refresh()
            assert guard.status == expected
            assert guard.can_open is allowed

    asyncio.run(run())


def test_percent_reserve_protects_large_filesystems(tmp_path):
    current = [capacity(5 * GIB, 100 * GIB)]
    guard = StorageGuard(tmp_path, statvfs=lambda _: current[0])

    async def run():
        await guard.refresh()
        assert guard.status == "CRITICAL"
        assert guard.view()["reasons"] == ["disk_percent"]
        current[0] = capacity(9 * GIB, 100 * GIB)
        await guard.refresh()
        assert guard.status == "CRITICAL"
        assert guard.view()["reasons"] == ["recovery_margin"]
        current[0] = capacity(10 * GIB, 100 * GIB)
        await guard.refresh()
        assert guard.status == "WARNING"
        current[0] = capacity(20 * GIB, 100 * GIB)
        await guard.refresh()
        assert guard.status == "OK"

    asyncio.run(run())


def test_absolute_reserve_protects_small_filesystems(tmp_path):
    guard = StorageGuard(tmp_path, statvfs=lambda _: capacity(GIB, 4 * GIB))
    asyncio.run(guard.refresh())
    assert guard.status == "CRITICAL"
    assert guard.view()["reasons"] == ["disk_reserve"]


def test_stat_uses_unprivileged_availability_not_root_free_blocks(tmp_path):
    raw = capacity(1000, 10_000)
    raw.f_frsize = GIB // 1000
    raw.f_bfree = 9000
    guard = StorageGuard(tmp_path, statvfs=lambda _: raw)
    asyncio.run(guard.refresh())
    assert guard.view()["freeBytes"] == raw.f_bavail * raw.f_frsize
    assert guard.status == "CRITICAL"


def test_block_size_fallback_and_unsupported_inode_metrics(tmp_path):
    raw = capacity(10_000_000, 20_000_000, inodes=0, inode_total=0)
    raw.f_frsize = 0
    guard = StorageGuard(tmp_path, statvfs=lambda _: raw)
    asyncio.run(guard.refresh())
    assert guard.status == "OK"
    assert guard.view()["freeBytes"] == 10_000_000 * 4096
    assert guard.view()["freeInodes"] is None
    assert guard.view()["freeInodePercent"] is None


def test_inode_pressure_and_hysteresis_independent_of_bytes(tmp_path):
    current = [capacity(inodes=1000)]
    guard = StorageGuard(tmp_path, statvfs=lambda _: current[0])

    async def run():
        for free, expected in (
            (1000, "CRITICAL"),
            (1999, "CRITICAL"),
            (2000, "WARNING"),
            (5001, "WARNING"),
            (7500, "OK"),
        ):
            current[0] = capacity(inodes=free)
            await guard.refresh()
            assert guard.status == expected
        current[0] = capacity(inodes=0)
        await guard.refresh()
        assert not guard.can_open
        assert "inodes_reserve" in guard.view()["reasons"]

    asyncio.run(run())


def test_inode_percent_protects_high_inode_count_filesystems(tmp_path):
    guard = StorageGuard(tmp_path, statvfs=lambda _: capacity(inodes=10_000, inode_total=1_000_000))
    asyncio.run(guard.refresh())
    assert guard.status == "CRITICAL"
    assert guard.view()["reasons"] == ["inodes_percent"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("f_blocks", 0),
        ("f_bavail", -1),
        ("f_bavail", 30 * GIB),
        ("f_favail", -1),
        ("f_favail", 200_000),
    ],
)
def test_malformed_capacity_fails_closed_without_leaking_error_details(tmp_path, field, value):
    raw = capacity()
    setattr(raw, field, value)
    guard = StorageGuard(tmp_path, statvfs=lambda _: raw)
    report = asyncio.run(guard.refresh())
    assert report["status"] == "UNKNOWN"
    assert report["reasons"] == ["stat_failed"]
    assert report["error"] == "ValueError"
    assert report["freeBytes"] is None
    assert not guard.can_open


def test_stat_error_blocks_and_requires_recovery_margin(tmp_path):
    current = [capacity()]

    def probe(_):
        if current[0] is None:
            raise OSError(f"sensitive mount details {tmp_path}")
        return current[0]

    guard = StorageGuard(tmp_path, statvfs=probe)

    async def run():
        await guard.refresh()
        assert guard.can_open
        current[0] = None
        await guard.refresh()
        assert guard.status == "UNKNOWN"
        assert guard.view()["error"] == "OSError"
        assert str(tmp_path) not in str(guard.view())
        current[0] = capacity(GIB + 1)
        await guard.refresh()
        assert guard.status == "CRITICAL"
        current[0] = capacity(2 * GIB)
        await guard.refresh()
        assert guard.can_open

    asyncio.run(run())


def test_stale_or_backwards_clock_sample_is_unknown_without_io(tmp_path):
    now = [100]
    calls = []
    guard = StorageGuard(
        tmp_path,
        statvfs=lambda path: calls.append(path) or capacity(),
        monotonic=lambda: now[0],
        wall_time=lambda: 500,
    )
    asyncio.run(guard.refresh())
    now[0] = 190
    assert guard.can_open
    now[0] = 190.001
    assert guard.status == "UNKNOWN"
    assert guard.view()["reasons"] == ["sample_stale"]
    assert guard.view()["checkedAt"] == 500_000
    now[0] = 99
    assert not guard.can_open
    assert len(calls) == 1


def test_polling_is_idempotent_off_event_loop_and_close_stops_it(tmp_path):
    calling_thread = threading.get_ident()
    calls = []

    def probe(_):
        calls.append(threading.get_ident())
        return capacity()

    async def run():
        guard = StorageGuard(tmp_path, interval=0.001, max_sample_age=1, statvfs=probe)
        await asyncio.gather(guard.start(), guard.start())
        task = guard.task
        await guard.start()
        assert guard.task is task
        while len(calls) < 2:
            await asyncio.sleep(0)
        await guard.close()
        assert guard.task.done()
        assert guard._worker is None
        assert guard.status == "UNKNOWN"
        assert guard.view()["reasons"] == ["sampling_stopped"]
        assert not guard.can_open
        await guard.close()
        with pytest.raises(RuntimeError, match="closed"):
            await guard.start()
        before = len(calls)
        await guard.refresh()
        assert len(calls) == before

    asyncio.run(run())
    assert all(thread != calling_thread for thread in calls)


def test_concurrent_explicit_probes_share_the_inflight_worker(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def probe(_):
        calls.append(1)
        entered.set()
        release.wait(5)
        return capacity()

    async def run():
        guard = StorageGuard(tmp_path, statvfs=probe)
        probes = [asyncio.create_task(guard.refresh()) for _ in range(4)]
        try:
            while not entered.is_set():
                await asyncio.sleep(0)
            assert not guard.can_open
            await asyncio.sleep(0)
            release.set()
            reports = await asyncio.gather(*probes)
            assert len(calls) == 1
            assert all(report["status"] == "OK" for report in reports)
            await guard.close()
        finally:
            release.set()

    asyncio.run(run())


def test_cancelled_refresh_retains_thread_ownership_until_worker_finishes(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def probe(_):
        entered.set()
        release.wait(5)
        return capacity()

    async def run():
        guard = StorageGuard(tmp_path, statvfs=probe)
        task = asyncio.create_task(guard.refresh())
        try:
            while not entered.is_set():
                await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert guard._worker is not None
            assert not guard._worker.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert guard._worker is None
            assert guard.status == "UNKNOWN"
        finally:
            release.set()
            await guard.close()

    asyncio.run(run())


def test_cancelled_close_joins_poll_probe_and_preserves_fail_closed_gate(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def probe(_):
        calls.append(1)
        if len(calls) == 2:
            entered.set()
            release.wait(5)
        return capacity()

    async def run():
        guard = StorageGuard(tmp_path, interval=0.001, max_sample_age=1, statvfs=probe)
        await guard.start()
        try:
            while not entered.is_set():
                await asyncio.sleep(0)
            close = asyncio.create_task(guard.close())
            await asyncio.sleep(0)
            assert not guard.can_open
            close.cancel()
            await asyncio.sleep(0)
            close.cancel()
            await asyncio.sleep(0)
            assert not close.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await close
            assert guard.task.done()
            assert guard._worker is None
        finally:
            release.set()
            await guard.close()

    asyncio.run(run())


def test_close_waits_for_explicit_start_probe_before_marking_complete(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def probe(_):
        entered.set()
        release.wait(5)
        return capacity()

    async def run():
        guard = StorageGuard(tmp_path, statvfs=probe)
        start = asyncio.create_task(guard.start())
        try:
            while not entered.is_set():
                await asyncio.sleep(0)
            close = asyncio.create_task(guard.close())
            await asyncio.sleep(0)
            assert not close.done()
            assert not guard.can_open
            release.set()
            await asyncio.gather(start, close)
            assert guard.task is None
            assert guard._worker is None
        finally:
            release.set()
            await guard.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "interval,max_age", [(0, 90), (30, 30), (float("nan"), 90), (30, float("inf"))]
)
def test_sampling_schedule_validation(tmp_path, interval, max_age):
    with pytest.raises(ValueError):
        StorageGuard(tmp_path, interval=interval, max_sample_age=max_age)


def test_threshold_validation():
    with pytest.raises(ValueError):
        StorageThresholds(critical_bytes=0)
    with pytest.raises(ValueError):
        StorageThresholds(critical_bytes=5 * GIB)
    with pytest.raises(ValueError):
        StorageThresholds(warning_recovery_percent=101)
