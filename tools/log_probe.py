"""在 pythonw.exe（无控制台）下复现 log() 的行为，把每一步结果写到文件里。"""
import os
import sys
import traceback
from pathlib import Path

OUT = Path(__file__).resolve().parent / "_logprobe.txt"
lines = []


def note(msg):
    lines.append(str(msg))
    OUT.write_text("\n".join(lines), encoding="utf-8")


note(f"sys.executable = {sys.executable}")
note(f"sys.stdout     = {sys.stdout!r}")
note(f"sys.stderr     = {sys.stderr!r}")
note(f"cwd            = {os.getcwd()}")
note(f"LOCALAPPDATA   = {os.environ.get('LOCALAPPDATA')!r}")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pc"))
try:
    import launcher_server as L
    note(f"imported, LOG_FILE = {L.LOG_FILE}")
except Exception:
    note("IMPORT FAILED:\n" + traceback.format_exc())
    raise SystemExit(1)

# 1) 复刻 main() 里对 stdout/stderr 的 reconfigure
for name, stream in (("stdout", sys.stdout), ("stderr", sys.stderr)):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
        note(f"reconfigure {name}: OK")
    except Exception:
        note(f"reconfigure {name} FAILED:\n" + traceback.format_exc())

# 2) 直接调 log()，看它到底静默吞了什么
try:
    L.LOG_DIR.mkdir(parents=True, exist_ok=True)
    note(f"mkdir OK, exists={L.LOG_DIR.exists()}")
except Exception:
    note("mkdir FAILED:\n" + traceback.format_exc())

try:
    with L.LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write("direct write probe\n")
    note(f"direct file write OK, size={L.LOG_FILE.stat().st_size}")
except Exception:
    note("direct file write FAILED:\n" + traceback.format_exc())

try:
    L.log("probe via log() 中文测试")
    note("log() returned without raising")
except Exception:
    note("log() RAISED:\n" + traceback.format_exc())

note(f"final: LOG_FILE.exists() = {L.LOG_FILE.exists()}")
