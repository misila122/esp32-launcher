"""在原厂固件 dump 里翻触摸标定线索。

思路：这块板子原厂跑的是 NerdMiner，它有配置界面（Brightness / Invert Colors /
TimeZone 都是可改的），说明它把设置持久化在某个分区里。如果那套设置里带着
TFT_eSPI 的触摸标定数组（uint16_t calData[5]），就直接拿来用，不用瞎猜。
"""
import re
import struct
import sys
from pathlib import Path

DUMP = (Path(__file__).resolve().parent.parent / "firmware" / "backup"
        / "nerdminer-v1.8.3-original-4MB.bin")
data = DUMP.read_bytes()
print(f"dump: {DUMP.name}  {len(data)} bytes")

# --- 分区表（之前已解析过，这里硬编码，顺便复核） ------------------------------
PARTITIONS = {
    "nvs":       (0x009000, 0x005000),
    "otadata":   (0x00E000, 0x002000),
    "app0":      (0x010000, 0x300000),
    "spiffs":    (0x310000, 0x0E0000),
    "coredump":  (0x3F0000, 0x010000),
}

print("\n=== 各分区头部 32 字节 ===")
for name, (off, size) in PARTITIONS.items():
    head = data[off:off + 32]
    print(f"{name:<10} @0x{off:06X} +0x{size:06X}  {head.hex(' ')}")

# --- SPIFFS 里有什么文件？ -----------------------------------------------------
print("\n=== spiffs 分区里的字符串（找配置文件名/内容） ===")
sp_off, sp_size = PARTITIONS["spiffs"]
sp = data[sp_off:sp_off + sp_size]
strings = re.findall(rb"[\x20-\x7E]{4,}", sp)
for s in strings[:80]:
    print("  ", s.decode("ascii", "replace"))

# --- 全盘找 TFT_eSPI / 触摸标定相关的关键词 -----------------------------------
print("\n=== 全盘搜索关键词 ===")
KEYS = [b"calData", b"touchCal", b"TouchCal", b"calibration", b"Calibration",
        b"setTouch", b"XPT2046", b"xpt2046", b"touch", b"Touch",
        b"prefs", b"Preferences", b"config.json", b"settings"]
for k in KEYS:
    hits = [m.start() for m in re.finditer(re.escape(k), data)]
    if hits:
        locs = ", ".join(f"0x{h:06X}" for h in hits[:8])
        more = f" (+{len(hits)-8})" if len(hits) > 8 else ""
        # 顺便看第一个命中处周围的上下文
        ctx = data[max(0, hits[0]-24):hits[0]+40]
        printable = "".join(chr(c) if 32 <= c < 127 else "." for c in ctx)
        print(f"{k.decode():<16} {len(hits):>4} 处  首个 @{locs}")
        print(f"                 上下文: {printable}")

# --- 在 app 分区里找可能的 uint16 calData[5] -------------------------------
# TFT_eSPI 的 calData 是 5 个 uint16，典型值形如 {x0,y0,x1,y1,rotation}
# rotation 通常是 0..7，前 4 个是 0..4095 的 ADC 值。
print("\n=== app 分区里扫描 '像 calData[5]' 的序列（启发式，仅供参考） ===")
app_off, app_size = PARTITIONS["app0"]
app = data[app_off:app_off + app_size]
found = 0
for i in range(0, len(app) - 10, 2):
    vals = struct.unpack_from("<5H", app, i)
    a, b, c, d, r = vals
    if r > 7:
        continue
    if not (100 <= a <= 4000 and 100 <= c <= 4000):
        continue
    if not (100 <= b <= 4000 and 100 <= d <= 4000):
        continue
    # 标定值通常 x 方向一对、y 方向一对，且跨度要够大
    if abs(c - a) < 800 or abs(d - b) < 800:
        continue
    print(f"  @0x{app_off+i:06X}  x:{a}->{c}  y:{b}->{d}  rot={r}")
    found += 1
    if found > 40:
        print("  ... (太多，可能是误报)")
        break
if not found:
    print("  没有找到候选")
