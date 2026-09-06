"""Export complete tracked text files, with no excerpts or private runtime files."""
from pathlib import Path
import subprocess

root=Path(__file__).resolve().parents[1]
files=subprocess.check_output(['git','ls-files','-z'],cwd=root).decode().split('\0')
out=root/'artifacts'/'FULL_SOURCE.md'
out.parent.mkdir(exist_ok=True)
with out.open('w') as dest:
    dest.write('# PM-NAUTILUS 完整源码\n\n全部受控文本文件按路径收录，内容无删节。锁文件随源仓库交付。\n')
    for name in files:
        if not name or name=='uv.lock' or name.startswith('artifacts/'):
            continue
        p=root/name
        try: data=p.read_text()
        except (UnicodeError,IsADirectoryError): continue
        dest.write(f'\n## {name}\n\n````\n{data}')
        dest.write('\n````\n')
print(out)
