"""Bounded history deltas preserve every changed order and legacy readers."""

import asyncio
from contextlib import contextmanager

import pytest

import pm_nautilus.views as views
from test_async_projections import append_fill, fill_history
from test_runtime import setup


@contextmanager
def history(path, count):
    runtime, _, _ = setup(path)
    try:
        payload, intent = fill_history(runtime, count)
        yield runtime, payload, intent
    finally:
        runtime.close()


def test_delta_batches_bound_materialization_and_legacy_head_catches_every_order(tmp_path):
    async def run():
        with history(tmp_path, 1_009) as (runtime, _, _):
            since = f"{runtime.store.generation}:0"
            seen = set()
            previous = 0
            calls = 0
            changes = runtime.store.db.total_changes
            while True:
                page = await views.records_page_async(runtime, 3, since=since)
                assert len(page["records"]) <= 3
                assert len(page["changes"]) <= views.RECORD_CHANGE_BATCH_ROWS
                seen.update(row["id"] for row in page["changes"])
                covered = int(page["head"].rsplit(":", 1)[1])
                assert covered > previous
                previous = covered
                calls += 1
                since = page["head"]
                assert all(
                    len(part.groups) <= views.RECORD_CHANGE_BATCH_ROWS + 3
                    for part in getattr(runtime, "_record_page_cache", {}).values()
                )
                if not page["nextChangesCursor"]:
                    break
            assert calls > 1
            assert seen == {f"HISTORY-{n}" for n in range(1_009)}
            assert runtime.store.db.total_changes == changes
            assert page["head"] == page["changesHead"]

    asyncio.run(run())


def test_legacy_ui_insertion_boundary_cannot_skip_intermediate_new_orders(tmp_path, monkeypatch):
    monkeypatch.setattr(views, "RECORD_CHANGE_BATCH_ROWS", 3)

    async def run():
        with history(tmp_path, 1) as (runtime, source, intent):
            initial = await views.records_page_async(runtime, 2)
            existing = {row["id"]: row for row in initial["records"]}
            since = initial["head"]
            for number in range(1, 12):
                oid = f"HISTORY-{number}"
                runtime.store.save_intent(oid, intent)
                append_fill(runtime, source, oid, oid)
            while True:
                page = await views.records_page_async(runtime, 2, since=since)
                # This is the existing client's actual insertion contract: each
                # response uses the newest row from the preceding response.
                newest = max(row["firstSeq"] for row in existing.values())
                for row in page["changes"]:
                    if row["id"] in existing or row["firstSeq"] > newest:
                        existing[row["id"]] = row
                existing.update((row["id"], row) for row in page["records"])
                since = page["head"]
                if not page["nextChangesCursor"]:
                    break
            assert set(existing) == {f"HISTORY-{n}" for n in range(12)}

    asyncio.run(run())


def test_fixed_delta_coverage_defers_new_fills_without_losing_late_old_order_updates(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(views, "RECORD_CHANGE_BATCH_ROWS", 3)

    async def run():
        with history(tmp_path, 8) as (runtime, source, intent):
            since = f"{runtime.store.generation}:0"
            first = await views.records_page_async(runtime, 2, since=since, changes_paged=True)
            target = first["changesHead"]
            cursor = first["nextChangesCursor"]
            assert cursor is not None
            append_fill(runtime, source, "HISTORY-0", "late-after-target", 10, 200_000)
            runtime.store.save_intent("new-after-target", intent)
            append_fill(runtime, source, "new-after-target", "new-after-target")
            observed = {row["id"] for row in first["changes"]}
            while cursor:
                page = await views.records_page_async(
                    runtime, 2, since=since, changes_cursor=cursor, changes_paged=True
                )
                assert page["changesHead"] == target
                assert all(
                    row["firstSeq"] <= int(target.rsplit(":", 1)[1]) for row in page["records"]
                )
                observed.update(row["id"] for row in page["changes"])
                cursor = page["nextChangesCursor"]
            assert observed == {f"HISTORY-{n}" for n in range(8)}
            assert page["head"] == target
            next_round = await views.records_page_async(
                runtime, 2, since=target, changes_paged=True
            )
            changed = {row["id"]: row for row in next_round["changes"]}
            assert set(changed) == {"HISTORY-0", "new-after-target"}
            assert changed["HISTORY-0"]["quantity"] == "60"
            assert next_round["nextChangesCursor"] is None

    asyncio.run(run())


def test_many_fills_of_one_order_are_bounded_by_events_and_retries_are_idempotent(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(views, "RECORD_CHANGE_BATCH_ROWS", 2)

    async def run():
        with history(tmp_path, 1) as (runtime, source, _):
            initial = await views.records_page_async(runtime, 2)
            for number in range(5):
                append_fill(runtime, source, "HISTORY-0", f"late-{number}", 1, 20_000)
            first = await views.records_page_async(
                runtime, 2, since=initial["head"], changes_paged=True
            )
            assert len(first["changes"]) == 1
            assert first["changes"][0]["quantity"] == "55"
            assert first["totalCount"] == 1
            cursor = first["nextChangesCursor"]
            assert cursor
            second = await views.records_page_async(
                runtime, 2, since=initial["head"], changes_cursor=cursor, changes_paged=True
            )
            retried = await views.records_page_async(
                runtime, 2, since=initial["head"], changes_cursor=cursor, changes_paged=True
            )
            assert second == retried
            assert second["nextChangesCursor"]
            last = await views.records_page_async(
                runtime,
                2,
                since=initial["head"],
                changes_cursor=second["nextChangesCursor"],
                changes_paged=True,
            )
            assert last["nextChangesCursor"] is None
            assert last["head"] == first["changesHead"]
            assert last["changes"][0]["quantity"] == "55"

    asyncio.run(run())


@pytest.mark.parametrize(
    "suffix",
    ["wrong:0:1:0", "{g}:1:{h}:1", "{g}:0:{h}:999999", "{g}:0:999999:1", "{g}:0:{h}:-1", "broken"],
)
def test_delta_cursor_rejects_wrong_generation_base_or_range(tmp_path, suffix):
    async def run():
        with history(tmp_path, 3) as (runtime, _, _):
            current = await views.records_page_async(runtime, 2)
            generation = runtime.store.generation
            head = current["head"].rsplit(":", 1)[1]
            cursor = suffix.format(g=generation, h=head)
            with pytest.raises(ValueError, match="游标已失效"):
                await views.records_page_async(
                    runtime, 2, since=f"{generation}:0", changes_cursor=cursor, changes_paged=True
                )

    asyncio.run(run())


def test_delta_cursor_does_not_cross_reset_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(views, "RECORD_CHANGE_BATCH_ROWS", 2)

    async def run():
        with history(tmp_path, 3) as (runtime, _, _):
            since = f"{runtime.store.generation}:0"
            first = await views.records_page_async(runtime, 2, since=since, changes_paged=True)
            assert first["nextChangesCursor"]
            runtime.store.reset()
            with pytest.raises(ValueError, match="游标已失效"):
                await views.records_page_async(
                    runtime,
                    2,
                    since=since,
                    changes_cursor=first["nextChangesCursor"],
                    changes_paged=True,
                )

    asyncio.run(run())


def test_cold_large_delta_resyncs_before_replay_and_keeps_full_history(tmp_path, monkeypatch):
    monkeypatch.setattr(views, "RECORD_CACHE_ORDERS", 4)
    monkeypatch.setattr(views, "RECORD_CHANGE_BATCH_ROWS", 3)

    async def run():
        with history(tmp_path, 12) as (runtime, source, _):
            current = await views.records_page_async(runtime, 3)
            writes = runtime.store.db.total_changes
            original = views._SelectedRecords.apply_payload
            decoded = 0

            def counted(self, owner, seq, payload):
                nonlocal decoded
                decoded += 1
                return original(self, owner, seq, payload)

            monkeypatch.setattr(views._SelectedRecords, "apply_payload", counted)
            with pytest.raises(ValueError, match="游标已失效"):
                await views.records_page_async(
                    runtime, 3, since=f"{runtime.store.generation}:0", changes_paged=True
                )
            assert decoded == 0
            # A refresh starts a new latest-page view. Ordinary older pages
            # continue to provide every record, including evicted orders.
            page = await views.records_page_async(runtime, 3)
            seen = set()
            while page["records"]:
                seen.update(row["id"] for row in page["records"])
                page = await views.records_page_async(runtime, 3, cursor=page["nextCursor"])
            assert seen == {f"HISTORY-{n}" for n in range(12)}
            assert runtime.store.db.total_changes == writes
            append_fill(runtime, source, "HISTORY-0", "one-cold-late-fill", 10, 200_000)
            late = await views.records_page_async(
                runtime, 3, since=current["head"], changes_paged=True
            )
            assert late["nextChangesCursor"] is None
            assert [(row["id"], row["quantity"]) for row in late["changes"]] == [
                ("HISTORY-0", "60")
            ]

    asyncio.run(run())


def test_cold_delta_can_expire_midround_without_affecting_ordinary_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(views, "RECORD_CACHE_ORDERS", 4)
    monkeypatch.setattr(views, "RECORD_CHANGE_BATCH_ROWS", 2)

    async def run():
        with history(tmp_path, 8) as (runtime, source, _):
            current = await views.records_page_async(runtime, 2)
            # The first batch touches hot rows, the next batch old rows, and
            # there is still another batch beyond them.
            for number in (7, 6, 0, 1, 2, 3):
                append_fill(runtime, source, f"HISTORY-{number}", f"later-{number}", 1, 20_000)
            first = await views.records_page_async(
                runtime, 2, since=current["head"], changes_paged=True
            )
            assert first["nextChangesCursor"]
            with pytest.raises(ValueError, match="游标已失效"):
                await views.records_page_async(
                    runtime,
                    2,
                    since=current["head"],
                    changes_cursor=first["nextChangesCursor"],
                    changes_paged=True,
                )
            latest = await views.records_page_async(runtime, 2)
            assert latest["totalCount"] == 8
            assert latest["nextChangesCursor"] is None

    asyncio.run(run())
