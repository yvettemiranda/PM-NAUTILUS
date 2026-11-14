"""Run one historical simulation with no inherited service secrets."""

import json
import os
import resource
import sys
from pathlib import Path


def main():
    source, request = (Path(arg) for arg in sys.argv[1:])
    root = request.parent
    os.nice(15)
    resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024**2,) * 2)
    # Numerical libraries imported by Nautilus must not start one thread per core.
    for name in (
        "OPENBLAS_NUM_THREADS",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "POLARS_MAX_THREADS",
    ):
        os.environ[name] = "1"
    try:
        from .backtest_engine import run_backtest
        from .capture_replay import open_replay_input

        args = json.loads(request.read_text(encoding="utf-8"))
        with open_replay_input(source) as stream:
            result = run_backtest(
                stream,
                preferences=args["preferences"],
                starting_cash=args["initialCash"],
                start_ms=args["startMs"],
                end_ms=args["endMs"],
                workspace=root,
                ledger_max_bytes=128 * 1024**2,
            )
        temporary = root / "result.tmp"
        with temporary.open("w", encoding="utf-8") as out:
            json.dump(
                {"id": args["id"], "status": "completed", "result": result},
                out,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        temporary.replace(root / "result.json")
    except (ValueError, UnicodeError, EOFError) as exc:
        (root / "error.txt").write_text(str(exc)[:500], encoding="utf-8")
        return 2
    except (ImportError, MemoryError, OSError, RuntimeError) as exc:
        (root / "error.txt").write_text(f"回测进程异常（{type(exc).__name__}）", encoding="utf-8")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
