"""Export every tracked UTF-8 text file in full, including the dependency lock."""

from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess

root = Path(__file__).resolve().parents[1]
files = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root).decode().strip()
out = root / "artifacts" / "FULL_SOURCE.md"
out.parent.mkdir(exist_ok=True)
manifest = {}
with out.open("w") as dest:
    dest.write(
        f"# PM-NAUTILUS 完整源码\n\n提交：`{revision}`\n\n全部受控文本文件按路径收录，包含锁文件，内容无删节。\n"
    )
    for name in files:
        if not name or name.startswith("artifacts/"):
            continue
        raw = (root / name).read_bytes()
        data = raw.decode("utf-8")
        fence = "`" * max(4, max((len(x) + 1 for x in re.findall(r"`+", data)), default=0))
        dest.write(f"\n## {name}\n\n{fence}\n{data}")
        dest.write(f"\n{fence}\n")
        manifest[name] = sha256(raw).hexdigest()
(root / "artifacts" / "SOURCE_MANIFEST.json").write_text(
    json.dumps({"revision": revision, "files": manifest}, ensure_ascii=False, indent=2)
)
print(f"{out}: {len(manifest)} complete files")
