"""Stopped-instance export for a final LIVE migration backup."""

import argparse
import getpass
import json
from pathlib import Path

from .live_backup import BackupError, export_final_live_bundle_file


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path("/data"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    password = getpass.getpass("LIVE 钱包解锁密码: ")
    try:
        digest = export_final_live_bundle_file(args.data_dir, password, args.output)
    except BackupError as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps({"path": str(args.output), "sha256": digest}, ensure_ascii=False))


if __name__ == "__main__":
    main()
