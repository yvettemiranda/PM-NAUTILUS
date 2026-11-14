"""Convert scoped Pendulum Flow v3.1 L2 files and dated market facts to replay NDJSON."""

import argparse
import json
import os
import tempfile
from collections import defaultdict
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

from .config import SCALE, micros
from .market import normalize, resolution

KINDS = {"book", "price_change", "tick_size_change", "market_resolved"}
BOOK_COLUMNS = [
    "event_type",
    "timestamp",
    "market",
    "asset_id",
    "row_ordinal",
    "recv_lag_ms",
    "mirror_dropped",
    "bids",
    "asks",
    "price",
    "size",
    "side",
    "new_tick_size",
    "winning_asset_id",
    "winning_outcome",
]
PROVENANCE_COLUMNS = ["row_ordinal", "sequence", "twin_seq_delta", "twin_recv_delta_ns"]
HOUR_MS = 3_600_000


def utc_ms(value):
    if type(value) is int and 0 < value <= 4_102_444_800_000:
        return value
    if not isinstance(value, str):
        raise ValueError("时间须为 ISO 8601（带时区）或毫秒时间戳")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("时间须带时区")
    at = round(dt.timestamp() * 1000)
    if not 0 < at <= 4_102_444_800_000:
        raise ValueError("时间超出支持范围")
    return at


def asset_id(blob):
    if not isinstance(blob, bytes) or len(blob) != 32:
        raise ValueError("档案合约 ID 无效")
    return str(int.from_bytes(blob, "big"))


def condition_id(string):
    if not isinstance(string, str) or len(string) != 66 or not string.startswith("0x"):
        raise ValueError("条件 ID 无效")
    return bytes.fromhex(string[2:])


def filtered_rows(path, column, selection, columns):
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    choices = pa.array(list(selection))
    for batch in pq.ParquetFile(path).iter_batches(columns=columns, batch_size=65_536):
        mask = pc.is_in(batch[column], value_set=choices)
        if column == "market":
            mask = pc.and_(mask, pc.is_in(batch["event_type"], value_set=pa.array(sorted(KINDS))))
        yield from batch.filter(mask).to_pylist()


def matching_archive(lean, provenance, identities):
    rows = list(filtered_rows(lean, "market", identities, BOOK_COLUMNS))
    if not rows:
        return []
    ordinals = {row["row_ordinal"] for row in rows}
    if len(ordinals) != len(rows):
        raise ValueError(f"{lean.name} 的盘口序号重复")
    aux = {}
    for row in filtered_rows(provenance, "row_ordinal", ordinals, PROVENANCE_COLUMNS):
        ordinal = row["row_ordinal"]
        if ordinal in aux:
            raise ValueError(f"{provenance.name} 的档案序号重复")
        aux[ordinal] = row
    if aux.keys() != ordinals:
        raise ValueError(f"{lean.name} 和 provenance 文件不匹配")
    result = []
    for row in rows:
        order = aux[row["row_ordinal"]]
        venue = round(row["timestamp"].timestamp() * 1000)
        received = venue + row["recv_lag_ms"]
        key = (received, order["sequence"], row["row_ordinal"])
        result.append((key, venue, row, False))
        if row["mirror_dropped"]:
            if order["twin_seq_delta"] is None or order["twin_recv_delta_ns"] is None:
                raise ValueError("档案缺少恢复镜像这笔盘口所需的顺序")
            twin_key = (
                received + order["twin_recv_delta_ns"] // 1_000_000,
                order["sequence"] + order["twin_seq_delta"],
                row["row_ordinal"],
            )
            result.append((twin_key, venue, row, True))
    return result


def levels(raw, mirror=False):
    return [
        [SCALE - micros(item["price"]) if mirror else micros(item["price"]), micros(item["size"])]
        for item in raw
    ]


def wire_event(raw, tokens_by_condition, observed, venue, mirror):
    kind = raw["event_type"]
    identity = raw["market"]
    pair = tokens_by_condition[identity]
    if kind == "market_resolved":
        # The venue resolution announcement makes this market non-tradable.
        # Only a separately timed, identity-checked official confirmation credits cash.
        return {
            "type": "market",
            "atMs": observed,
            "tokens": [asdict(replace(t, open=False)) for t in pair],
        }
    token = asset_id(raw["asset_id"])
    ids = [t.token_id for t in pair]
    if token not in ids:
        raise ValueError("档案盘口合约与官方市场资料不一致")
    if mirror:
        token = next(tid for tid in ids if tid != token)
    base = {"atMs": observed, "venueMs": venue, "tokenId": token}
    if kind == "book":
        return base | {
            "type": "book",
            "bids": levels(raw["asks"] if mirror else raw["bids"], mirror),
            "asks": levels(raw["bids"] if mirror else raw["asks"], mirror),
        }
    if kind == "tick_size_change":
        return base | {"type": "tick", "tick": micros(raw["new_tick_size"])}
    side = raw["side"]
    if side not in {"BUY", "SELL"}:
        raise ValueError("档案盘口方向无效")
    price = micros(raw["price"])
    return base | {
        "type": "delta",
        "side": ("SELL" if side == "BUY" else "BUY") if mirror else side,
        "levels": [[SCALE - price if mirror else price, micros(raw["size"])]],
    }


def facts(config, root, identities, first, last):
    """Apply only time-labelled official observations, without fabricating past fields."""
    changes = defaultdict(list)
    for item in config.get("metadata", []):
        at = utc_ms(item["observedAt"])
        if not first <= at <= last:
            raise ValueError("市场观测时间超出行情包范围")
        for name in item.get("events", []):
            official = json.loads((root / name).read_text(encoding="utf-8"))
            for event in official if isinstance(official, list) else [official]:
                normalized = normalize(event, include_independent=True, include_augmented=True)
                changes[at].extend(
                    token for token in normalized if condition_id(token.condition_id) in identities
                )
    for at in changes:
        tokens = changes[at]
        counts = defaultdict(list)
        for token in tokens:
            counts[condition_id(token.condition_id)].append(token)
        if any(len(pair) != 2 or len({t.token_id for t in pair}) != 2 for pair in counts.values()):
            raise ValueError("同一时刻的官方市场资料没有恰好两份合约或有重复")
    return changes


def settlements(config, root, first, last):
    result = defaultdict(list)
    for entry in config.get("settlements", []):
        at = utc_ms(entry["observedAt"])
        if not first <= at <= last:
            raise ValueError("结算观测时间超出行情包范围")
        result[at].append(json.loads((root / entry["market"]).read_text(encoding="utf-8")))
    return result


def _hour_files(config, first, last):
    hours = {}
    for entry in config.get("hours", []):
        hour = utc_ms(entry["hour"])
        if hour % HOUR_MS or hour in hours:
            raise ValueError("档案批次须是唯一的 UTC 整点")
        if first // HOUR_MS * HOUR_MS <= hour <= last:
            hours[hour] = entry["files"]
    return hours


def records(config, root):
    first, last = utc_ms(config["start"]), utc_ms(config["end"])
    if last <= first:
        raise ValueError("结束时间须晚于开始时间")
    wanted = {condition_id(s) for s in config["conditions"]}
    if not wanted:
        raise ValueError("请声明历史回测中应覆盖的市场")
    hours = _hour_files(config, first, last)
    updates = facts(config, root, wanted, first, last)
    payouts = settlements(config, root, first, last)
    tokens_by_condition = defaultdict(list)
    metadata_markets = {condition_id(t.condition_id) for batch in updates.values() for t in batch}
    notes = [
        f"声明 {len(wanted)} 个条件；其中 {len(wanted - metadata_markets)} 个缺可用官方市场资料。",
        "Pendulum Flow v3.1 录像；范围限清单所列条件，档案缺口与市场当时可见性须另行核验。",
        "官方市场资料和结算只从清单声明的观测时刻开始生效；旧时点资料缺失会改变选市及收益。",
    ] + config.get("notes", [])
    yield {
        "type": "header",
        "version": 1,
        "source": config.get("source", "Pendulum Flow v3.1"),
        "startMs": first,
        "endMs": last,
        "coverage": "partial",
        "notes": notes,
    }
    tags = config.get("officialTags", [])
    manual_gaps = {utc_ms(gap) for gap in config.get("gaps", [])}
    fact_times = sorted(updates.keys() | payouts.keys() | manual_gaps)
    archive_events = 0
    closed = set()
    for hour in range(first // HOUR_MS * HOUR_MS, last + 1, HOUR_MS):
        begin, end = max(first, hour), min(last + 1, hour + HOUR_MS)
        timeline = []
        for at in fact_times:
            if begin <= at < end:
                timeline.append(((at, -2, 0), None, "facts", False))
        file_set = hours.get(hour, [])
        for files in file_set:
            for entry in matching_archive(root / files["lean"], root / files["provenance"], wanted):
                if begin <= entry[0][0] < end:
                    timeline.append(entry)
        if not file_set:
            timeline.append(((begin, -1, 0), None, "gap", False))
        for (at, _, _), venue, row, mirror in sorted(timeline, key=lambda item: item[0]):
            if row == "facts":
                grouped = defaultdict(list)
                for t in updates.get(at, []):
                    identity = condition_id(t.condition_id)
                    if identity not in closed:
                        grouped[identity].append(t)
                tokens_by_condition.update(grouped)
                if grouped:
                    yield {
                        "type": "market",
                        "atMs": at,
                        "tokens": [asdict(t) for pair in grouped.values() for t in pair],
                        "officialTags": tags,
                    }
                for raw in payouts.get(at, []):
                    condition = raw.get("conditionId")
                    pair = tokens_by_condition.get(condition_id(condition))
                    mapping = resolution(raw, pair[0]) if pair else None
                    if not mapping or set(mapping) != {t.token_id for t in pair}:
                        raise ValueError("官方结算不完整、身份不匹配或未确认")
                    closed.add(condition_id(condition))
                    yield {
                        "type": "settlement",
                        "atMs": at,
                        "conditionId": condition,
                        "payouts": mapping,
                    }
                if at in manual_gaps:
                    yield {"type": "gap", "atMs": at}
            elif row == "gap":
                yield {"type": "gap", "atMs": at}
            elif row["market"] in tokens_by_condition and row["market"] not in closed:
                archive_events += 1
                if row["event_type"] == "market_resolved":
                    closed.add(row["market"])
                yield wire_event(row, tokens_by_condition, at, venue, mirror)
    if not archive_events:
        raise ValueError("数据中没有任何已登记市场的有效盘口事件")


def prepare(manifest, output):
    manifest, output = Path(manifest), Path(output)
    config = json.loads(manifest.read_text(encoding="utf-8"))
    # The source files are local and the only output is the requested new tape.
    if output.exists() or output.is_symlink():
        raise ValueError("输出文件已存在，请指定新路径")
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=output.parent, prefix=".pm-archive-", delete=False
    ) as stream:
        temp = Path(stream.name)
        try:
            for row in records(config, manifest.parent):
                print(
                    json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
                    file=stream,
                )
            stream.close()
            os.link(temp, output)
        finally:
            temp.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="回测范围、数据文件及观测时间 JSON")
    parser.add_argument("output", type=Path, help="输出 .ndjson 行情包路径")
    args = parser.parse_args(argv)
    try:
        prepare(args.manifest, args.output)
    except (ValueError, TypeError, KeyError, OSError) as exc:
        parser.exit(2, f"生成行情包失败：{exc}\n")


if __name__ == "__main__":
    main()
