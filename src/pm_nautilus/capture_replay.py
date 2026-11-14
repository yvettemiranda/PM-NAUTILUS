"""Verify and stitch sealed TEST recording segments for historical replay."""

import argparse
import gzip
import json
import os
import re
from collections import namedtuple
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

from .backtest_engine import (
    MAX_LINE_BYTES,
    MAX_TIME_MS,
    _integer,
    _invalid_constant,
    _unique_object,
    validate_capture_scope,
)

SEGMENT_NAME = re.compile(r"(?P<start>[0-9]{13})-(?P<end>[0-9]{13})-[0-9a-f]{12}\.ndjson\.gz")
SOURCE = "PM-NAUTILUS TEST 录制"
NOTES = [
    "最多可回测 5¢ 买入。盘口范围为录制时 TEST 可见的市场。",
    "缺少快照或订阅失效时不推测成交。录制从空仓独立回测，既有仓位须有相应账本副本。",
]
Segment = namedtuple("Segment", "path id start end previous scope baseline")


def _row(stream):
    line = stream.readline(MAX_LINE_BYTES + 1)
    if not line:
        return None, None
    if len(line) > MAX_LINE_BYTES:
        raise ValueError("录制数据行超过大小限制")
    value = json.loads(line, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    if type(value) is not dict:
        raise ValueError("录制数据行必须是对象")
    return line, value


def _identity(value):
    if value is not None and (type(value) is not str or SEGMENT_NAME.fullmatch(value) is None):
        raise ValueError("录制文件编号无效")
    return value


def _segment_header(stream):
    _, row = _row(stream)
    if row is None or row.get("type") != "capture_segment" or type(row.get("version")) is not int:
        raise ValueError("缺少录制文件头")
    if row["version"] != 1:
        raise ValueError("不支持的录制文件版本")
    start = _integer(row.get("startMs"), "录制开始时间", 1, MAX_TIME_MS)
    previous = _identity(row.get("previous"))
    scope = validate_capture_scope(row.get("captureScope"))
    return start, previous, scope


def _segment_end(row, start, last):
    end = _integer(row.get("endMs"), "录制结束时间", last, MAX_TIME_MS)
    identity = _identity(row.get("id"))
    if identity is None:
        raise ValueError("录制数据没有封存文件编号")
    bounds = SEGMENT_NAME.fullmatch(identity)
    if (int(bounds["start"]), int(bounds["end"])) != (start, end):
        raise ValueError("录制文件编号与时间范围不一致")
    return identity, end


def _inspect(path):
    with gzip.open(path, "rb") as stream:
        start, previous, scope = _segment_header(stream)
        last = start
        baseline = potential_baseline = None
        while True:
            _, row = _row(stream)
            if row is None:
                raise ValueError("录制文件缺少封存结尾")
            kind = row.get("type")
            if kind == "capture_end":
                identity, end = _segment_end(row, start, last)
                if stream.read(1):
                    raise ValueError("录制文件的封存结尾之后有数据")
                return Segment(path, identity, start, end, previous, scope, baseline)
            if kind == "capture_segment":
                raise ValueError("录制文件包含重复文件头")
            last = _integer(row.get("atMs"), "录制观测时间", last, MAX_TIME_MS)
            if kind == "scope" and row.get("baseline") is True:
                potential_baseline = last
            elif kind == "gap" and not row.get("tokenIds"):
                potential_baseline = None
            elif kind == "control" and row.get("baselineEnd") is True:
                if baseline is None and potential_baseline is not None:
                    baseline = potential_baseline
                potential_baseline = None


def inspect_capture(path):
    """Read and validate one compressed segment, including its gzip checksum."""
    segment = _inspect(Path(path))
    return {
        "id": segment.id,
        "source": SOURCE,
        "startMs": segment.start,
        "endMs": segment.end,
        "previous": segment.previous,
        "baselineAtMs": segment.baseline,
        "captureScope": segment.scope,
        "coverage": "scoped",
        "notes": NOTES[:],
    }


def _encoded(row):
    return json.dumps(row, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"


def _linked(previous, current):
    return previous.id is not None and current.previous == previous.id


def _named_segment(path):
    match = SEGMENT_NAME.fullmatch(path.name)
    if match is None:
        return _inspect(path)
    with gzip.open(path, "rb") as stream:
        start, previous, scope = _segment_header(stream)
    if start != int(match["start"]):
        return _inspect(path)
    # The compressed stream and seal are verified on playback. Reading only
    # headers here lets a long replay start before every clip is decompressed.
    end = _integer(int(match["end"]), "录制结束时间", start, MAX_TIME_MS)
    return Segment(path, path.name, start, end, previous, scope, None)


def _segments(paths):
    segments = sorted(
        (_named_segment(Path(path)) for path in paths),
        key=lambda item: (item.start, item.end, item.id),
    )
    if not segments:
        raise ValueError("至少需要一个录制文件")
    seen = set()
    for index, segment in enumerate(segments):
        if segment.id in seen:
            raise ValueError("录制文件编号重复")
        seen.add(segment.id)
        if segment.scope != segments[0].scope:
            raise ValueError("录制文件头声明的采集范围不一致")
        if index and segment.start < segments[index - 1].end:
            raise ValueError("录制文件时间重叠")
    if segments[0].start >= segments[-1].end:
        raise ValueError("录制时间范围为空")
    return segments


def _payloads(segment):
    with gzip.open(segment.path, "rb") as stream:
        start, previous, scope = _segment_header(stream)
        if (start, previous, scope) != (segment.start, segment.previous, segment.scope):
            raise ValueError("录制文件读取期间发生变化")
        last = start
        while True:
            line, row = _row(stream)
            if row is None:
                raise ValueError("录制文件缺少封存结尾")
            if row.get("type") == "capture_end":
                identity, end = _segment_end(row, start, last)
                if (identity, end) != (segment.id, segment.end) or stream.read(1):
                    raise ValueError("录制文件读取期间发生变化")
                return
            if row.get("type") == "capture_segment":
                raise ValueError("录制文件包含重复文件头")
            last = _integer(row.get("atMs"), "录制观测时间", last, segment.end)
            yield line, row


def _replay_rows(segments, end_ms=None):
    yield _encoded(
        {
            "type": "header",
            "version": 1,
            "source": SOURCE,
            "startMs": segments[0].start,
            "endMs": segments[-1].end,
            "coverage": "scoped",
            "notes": NOTES,
            "captureScope": segments[0].scope,
        }
    )
    synchronized = baseline = False
    for index, segment in enumerate(segments):
        gap_at = segment.start if not index else segments[index - 1].end
        if end_ms is not None and gap_at > end_ms:
            return
        if not index or not _linked(segments[index - 1], segment):
            synchronized = baseline = False
            yield _encoded({"type": "gap", "atMs": gap_at})
        if end_ms is not None and segment.start > end_ms:
            return
        for line, row in _payloads(segment):
            if end_ms is not None and row["atMs"] > end_ms:
                # Verify the entire touched clip even when the chosen replay
                # ends inside it. Later clips need only their bounds.
                continue
            kind = row.get("type")
            if kind == "gap" and not row.get("tokenIds"):
                if synchronized or baseline:
                    yield line
                synchronized = baseline = False
                continue
            if kind == "scope" and row.get("baseline") is True:
                baseline = True
            if synchronized or baseline:
                yield line
            if kind == "control" and row.get("baselineEnd") is True and baseline:
                baseline = False
                synchronized = True


@contextmanager
def open_capture_tapes(paths, *, end_ms=None):
    """Stream a continuous replay header and events from one or more sealed files."""
    rows = _replay_rows(_segments(paths), end_ms)
    try:
        yield rows
    finally:
        rows.close()


@contextmanager
def open_replay_input(path, additional=(), *, end_ms=None):
    """Auto-detect old NDJSON, compressed NDJSON and sealed TEST recordings."""
    path = Path(path)
    if path.is_dir() or additional:
        paths = recordings_in(path) if path.is_dir() else [path]
        with open_capture_tapes((*paths, *additional), end_ms=end_ms) as stream:
            yield stream
        return
    with path.open("rb") as raw:
        if raw.peek(2)[:2] != b"\x1f\x8b":
            yield raw
            return
        with gzip.GzipFile(fileobj=raw) as stream:
            _, first = _row(stream)
            if first is None:
                raise ValueError("压缩行情文件为空")
        if first.get("type") == "capture_segment":
            with open_capture_tapes((path,), end_ms=end_ms) as rows:
                yield rows
        else:
            raw.seek(0)
            with gzip.GzipFile(fileobj=raw) as stream:
                yield stream


def recordings_in(directory):
    directory = Path(directory).resolve(strict=True)
    if not directory.is_dir():
        raise ValueError("录制目录不是文件夹")
    return sorted(
        path for path in directory.iterdir() if SEGMENT_NAME.fullmatch(path.name) and path.is_file()
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("segments", type=Path, nargs="*")
    parser.add_argument("--directory", type=Path, help="读取该目录中的所有已封存录制文件")
    args = parser.parse_args(argv)
    try:
        paths = args.segments + (recordings_in(args.directory) if args.directory else [])
        if args.output.exists() or args.output.is_symlink():
            raise FileExistsError("输出已存在")
        with open_capture_tapes(paths) as rows:
            with NamedTemporaryFile(
                prefix=".capture-export-", dir=args.output.parent, delete=False
            ) as destination:
                temporary = Path(destination.name)
                try:
                    if args.output.suffix == ".gz":
                        with gzip.GzipFile(
                            filename="", fileobj=destination, mode="wb", compresslevel=3, mtime=0
                        ) as packed:
                            packed.writelines(rows)
                    else:
                        destination.writelines(rows)
                    destination.flush()
                    os.link(temporary, args.output)
                finally:
                    temporary.unlink()
    except (EOFError, OSError, ValueError) as exc:
        parser.exit(2, f"录制文件合并失败：{exc}\n")


if __name__ == "__main__":
    main()
