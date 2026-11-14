import asyncio
import gzip
import hashlib
import json
from pathlib import Path

import pytest

from pm_nautilus.capture import CaptureRecorder


BASE = 1_800_000_000_000


def recorder(path, **overrides):
    return CaptureRecorder(
        path,
        **({"min_free_bytes": 0, "min_free_percent": 0} | overrides),
    )


async def until(check):
    async with asyncio.timeout(5):
        while not check():
            await asyncio.sleep(0.005)


async def start(writer):
    writer.start()
    await until(lambda: writer.accepting)


def line(kind, offset=0, **fields):
    return {"type": kind, "atMs": BASE + offset, **fields}


def read_segment(path):
    with gzip.open(path, "rt") as stream:
        return [json.loads(row) for row in stream]


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def test_capture_seals_ordered_multi_member_gzip_and_restarts(tmp_path):
    async def run():
        writer = recorder(tmp_path)
        await start(writer)
        assert writer.submit_many((line("scope"), line("control", status="RUNNING")))
        first = await writer.seal()
        assert first == writer.archives()[0]
        assert writer.status()["status"] == "recording"
        assert writer.submit(line("book", 1, tokenId="a", asks=[], bids=[]))
        second = await writer.seal()
        await writer.close()

        rows = read_segment(writer.archive(first["id"]))
        assert rows[0] == {
            "type": "capture_segment",
            "version": 1,
            "startMs": BASE,
            "captureScope": {"maxBuyPriceMicros": 50_000, "marketCoverage": "test-subscribed"},
            "previous": None,
        }
        assert rows[1:-1] == [line("gap"), line("scope"), line("control", status="RUNNING")]
        assert rows[-1] == {"type": "capture_end", "id": first["id"], "endMs": BASE}

        rows = read_segment(writer.archive(second["id"]))
        assert rows[0]["previous"] == first["id"]
        assert rows[1:-1] == [line("book", 1, tokenId="a", asks=[], bids=[])]
        assert writer.archive(second["id"]).stat().st_size == second["bytes"]
        assert writer.status()["bytes"] == first["bytes"] + second["bytes"]

        stale = tmp_path / ".capture-001234567abc.part"
        stale.write_bytes(b"incomplete capture")
        unrelated = tmp_path / "ledger.db-wal"
        unrelated.touch()
        restarted = recorder(tmp_path)
        await start(restarted)
        assert restarted.archives() == (first, second)
        assert not stale.exists()
        assert unrelated.exists()
        assert restarted.submit(line("scope", 2))
        third = await restarted.seal()
        rows = read_segment(restarted.archive(third["id"]))
        assert rows[0]["previous"] == second["id"]
        assert rows[1:-1] == [line("gap", 2), line("scope", 2)]
        await restarted.close()

    asyncio.run(run())


def test_capture_queue_is_bounded_and_loss_is_global(tmp_path):
    async def run():
        writer = recorder(tmp_path, max_queue_bytes=2_048)
        await start(writer)
        assert writer.submit(line("scope"))
        oversized = line("book", 1, quote="x" * 1_024)
        assert not writer.submit_many((line("scope", 1), oversized))
        assert writer.status()["queueBytes"] <= 2_048
        assert writer.status()["queueDropped"] == 2
        assert writer.epoch == 1
        assert writer.submit(line("gap", 2, tokenIds=["token1"]))
        sealed = await writer.seal()
        rows = read_segment(writer.archive(sealed["id"]))
        assert rows[1:-1] == [
            line("gap"),
            line("scope"),
            line("gap", 2),
            line("gap", 2, tokenIds=["token1"]),
        ]
        assert writer.status()["queueBytes"] == 0
        assert writer.status()["gapCount"] == 3
        await writer.close()

    asyncio.run(run())


def test_capture_manual_release_requires_oldest_and_matching_hash(tmp_path):
    async def run():
        writer = recorder(tmp_path)
        await start(writer)
        assert writer.submit(line("scope"))
        first = await writer.seal()
        assert writer.submit(line("pulse"))
        second = await writer.seal()
        assert first["id"] < second["id"]
        first_path = writer.archive(first["id"])
        second_path = writer.archive(second["id"])
        with pytest.raises(ValueError, match="最早"):
            await writer.release(second["id"], checksum(second_path))
        with pytest.raises(ValueError, match="不符"):
            await writer.release(first["id"], "a" * 64)
        assert len(writer.archives()) == 2
        await writer.release(first["id"], checksum(first_path))
        await writer.release(second["id"], checksum(second_path))
        assert writer.archives() == ()
        assert not first_path.exists()
        assert not second_path.exists()
        assert writer.submit(line("pulse", 1))
        third = await writer.seal()
        rows = read_segment(writer.archive(third["id"]))
        assert rows[0]["previous"] == second["id"]
        assert rows[1:-1] == [line("pulse", 1)]
        await writer.close()

    asyncio.run(run())


def test_capture_preserves_sealed_files_at_quota_and_resumes_after_release(tmp_path):
    async def run():
        ready = recorder(tmp_path)
        await start(ready)
        assert ready.submit(line("scope"))
        first = await ready.seal()
        await ready.close()

        writer = recorder(tmp_path, max_bytes=first["bytes"] + 32)
        await start(writer)
        assert writer.submit(line("scope", 1))
        assert await writer.seal() is None
        assert writer.status()["status"] == "quota_full"
        assert writer.archives() == (first,)
        path = writer.archive(first["id"])
        await writer.release(first["id"], checksum(path))
        # Restart after a properly bounded writer hits cap again if even its
        # initial data cannot fit. This direct release still enables collection
        # when the next compressed batch fits in the recovered space.
        await until(lambda: writer.accepting)
        assert writer.submit(line("scope", 2))
        await writer.close()
        assert writer.status()["bytes"] <= writer.status()["maxBytes"]

    asyncio.run(run())


def test_capture_disk_guard_and_exclusive_lock(tmp_path):
    async def run():
        path = tmp_path / "capture"
        blocked = recorder(path, min_free_bytes=10**19)
        blocked.start()
        await until(lambda: blocked.status()["status"] == "disk_low")
        assert not blocked.submit(line("scope"))
        assert list(path.glob("*.ndjson.gz")) == []
        second = recorder(path)
        second.start()
        await until(lambda: second.status()["status"] == "error")
        assert not second.accepting
        await second.close()
        await blocked.close()

    asyncio.run(run())


def test_capture_rolls_by_age_while_idle(tmp_path: Path):
    async def run():
        writer = recorder(tmp_path, segment_seconds=0.01)
        await start(writer)
        assert writer.submit(line("scope"))
        await until(lambda: bool(writer.archives()))
        archive = writer.archives()[0]
        assert read_segment(writer.archive(archive["id"]))[-1]["id"] == archive["id"]
        await writer.close()

    asyncio.run(run())
