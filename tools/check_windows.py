"""只读体检：不启动任何程序，检查 6 个程序当前的"能不能找到窗口"情况。

用来验证"exe 只是引导壳、真正带窗口的进程在别处"这类程序，
是不是也能被正确匹配到。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pc"))

import ctypes  # noqa: E402
import ctypes.wintypes as wt  # noqa: E402

from launcher_server import (  # noqa: E402
    Launcher,
    _pid_image_path,
    _score_path,
    best_window,
    find_windows_for_exe,
    load_config,
    user32,
)

cfg = load_config(Path(sys.argv[1]) if len(sys.argv) > 1
                  else Path(__file__).resolve().parent.parent / "pc" / "apps.json")
lc = Launcher(cfg)

print(f"{'id':<3} {'label':<9} {'exe存在':<7} {'匹配进程':<9} 会被切到前台的窗口")
print("-" * 100)
for app in cfg["apps"]:
    exe = Path(app["path"])
    exists = exe.exists()
    cands = find_windows_for_exe(str(exe)) if exists else []
    if not exists:
        detail = "(路径不存在)"
    elif not cands:
        detail = "(当前没有窗口 —— 程序没在运行，或没有可见窗口)"
    else:
        hwnd, title = cands[0]
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        img = _pid_image_path(pid.value)
        score = _score_path(img, str(exe))
        chosen = best_window(str(exe))
        detail = (f"score={score} 窗口数={len(cands)} 会被切到前台的=hwnd {chosen} "
                  f"“{title}”")
        detail += f"\n      实际进程镜像: {img}"
    print(f"{app['id']:<3} {app['label']:<9} {str(exists):<7} {len(cands):<9} {detail}")

print()
print("当前进程里，与本配置相关的可执行文件：")
seen = set()
import subprocess  # noqa: E402

out = subprocess.run(
    ["powershell", "-NoProfile", "-Command",
     "Get-CimInstance Win32_Process | Select-Object ProcessId,Name,ExecutablePath | ConvertTo-Json -Compress"],
    capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
import json  # noqa: E402

try:
    procs = json.loads(out)
except Exception:
    procs = []
if isinstance(procs, dict):
    procs = [procs]
targets = [Path(a["path"]).stem.lower() for a in cfg["apps"]]
for p in procs:
    ep = p.get("ExecutablePath") or ""
    if any(t in Path(ep).stem.lower() for t in targets if t):
        key = (p.get("Name"), ep)
        if key in seen:
            continue
        seen.add(key)
        print(f"  PID {p.get('ProcessId'):<8} {p.get('Name'):<22} {ep}")
