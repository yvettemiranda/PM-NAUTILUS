"""Run historical simulation locally without the web server."""

import argparse
import json
from pathlib import Path

from .backtest_archive import utc_ms
from .backtest_engine import run_backtest
from .capture_replay import open_replay_input
from .config import Preferences


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--segment",
        type=Path,
        action="append",
        default=[],
        help="追加独立导出的 TEST 录制文件，可重复传入",
    )
    parser.add_argument("--preferences", type=Path, required=True)
    parser.add_argument("--cash", required=True, help="初始模拟资金（U）")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--checkpoint", type=Path, help="本地一致性 TEST 1 SQLite 副本")
    parser.add_argument("--checkpoint-at", help="已核验的副本时刻，须与 --start 相等")
    args = parser.parse_args(argv)
    if bool(args.checkpoint) != bool(args.checkpoint_at):
        parser.error("--checkpoint 和 --checkpoint-at 必须同时提供")
    if args.output.exists() or args.output.is_symlink():
        parser.error("输出已存在")
    try:
        definition = json.loads(args.preferences.read_text(encoding="utf-8"))
        preferences = Preferences.from_profile(definition)
        end = None if args.end is None else utc_ms(args.end)
        with open_replay_input(args.dataset, args.segment, end_ms=end) as tape:
            result = run_backtest(
                tape,
                preferences=preferences.model_dump(mode="json"),
                starting_cash=args.cash,
                start_ms=None if args.start is None else utc_ms(args.start),
                end_ms=end,
                checkpoint=args.checkpoint,
                checkpoint_at_ms=None if args.checkpoint_at is None else utc_ms(args.checkpoint_at),
            )
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (EOFError, ValueError, OSError) as exc:
        parser.exit(2, f"回测失败：{exc}\n")


if __name__ == "__main__":
    main()
