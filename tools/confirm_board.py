#!/usr/bin/env python3
"""确认板型：看 'ESP32-2432S028R' 在固件里出现的上下文，
并检查 spiffs / app 里是否还列了别的板子名字（配置页的板子列表）。
"""
import re
from pathlib import Path

DUMP = (Path(__file__).resolve().parent.parent / "firmware" / "backup"
        / "nerdminer-v1.8.3-original-4MB.bin")
data = DUMP.read_bytes()

KEY = b"ESP32-2432S028R"
print("=== context of ESP32-2432S028R ===")
for m in re.finditer(re.escape(KEY), data):
    s = max(0, m.start() - 120)
    e = min(len(data), m.end() + 120)
    ctx = data[s:e]
    print(f"\n@0x{m.start():06x} ({'app0' if 0x10000 <= m.start() < 0x310000 else 'spiffs' if m.start() >= 0x310000 else 'other'})")
    print("  " + repr(ctx))

# spiffs 里是否有板子列表
print("\n\n=== board-name-ish strings in WHOLE dump ===")
pat = re.compile(rb"[A-Za-z0-9_\-\. ]{4,40}")
names = set()
for m in pat.finditer(data):
    t = m.group().decode("ascii", "replace").strip()
    if re.search(r"(ESP32|CYD|TFT|TTGO|LilyGo|Heltec|Lolin|Wemos|M5Stack|ODROID|Sunton|Display|ILI9|ST77|GC9|board)", t, re.I):
        names.add((t, m.start()))
for t, off in sorted(names):
    where = "app0" if 0x10000 <= off < 0x310000 else ("spiffs" if off >= 0x310000 else "low")
    print(f"  [{where}] 0x{off:06x}  {t}")
