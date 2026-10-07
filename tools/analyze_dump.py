#!/usr/bin/env python3
"""分析从板子上 dump 下来的 4MB flash，用于：
1) 解析分区表
2) 找出板型/固件标识字符串（NerdMiner 的配置页里会列出所有支持的板子名字）
3) 定位 NVS 分区里的 WiFi 配置
"""
import re
import sys
from pathlib import Path

DUMP = (Path(__file__).resolve().parent.parent / "firmware" / "backup"
        / "nerdminer-v1.8.3-original-4MB.bin")

PTYPES = {0: "app", 1: "data"}
DSUBTYPES = {
    0x00: "ota", 0x01: "phy", 0x02: "nvs", 0x03: "coredump",
    0x04: "nvs_keys", 0x05: "efuse", 0x06: "undefined", 0x80: "esphttpd",
    0x81: "fat", 0x82: "spiffs", 0x83: "littlefs",
}


def parse_partition_table(data: bytes):
    parts = []
    pt = data[0x8000:0x9000]
    for i in range(0, len(pt), 32):
        e = pt[i:i + 32]
        if len(e) < 32 or e[0:2] != b"\xaa\x50":
            break
        ptype, subtype = e[2], e[3]
        off = int.from_bytes(e[4:8], "little")
        size = int.from_bytes(e[8:12], "little")
        label = e[12:28].rstrip(b"\x00").decode("utf-8", "replace")
        flags = int.from_bytes(e[28:32], "little")
        parts.append(dict(label=label, type=ptype, subtype=subtype,
                          offset=off, size=size, flags=flags))
    return parts


def strings(buf: bytes, minlen=5):
    """ASCII 字符串提取"""
    return re.findall(rb"[\x20-\x7e]{%d,}" % minlen, buf)


def main():
    data = DUMP.read_bytes()
    print(f"dump: {DUMP}  ({len(data)} bytes = {len(data)/1024/1024:.2f} MB)")

    # ---------------------------------------------------------- 分区表
    print("\n=== PARTITION TABLE ===")
    parts = parse_partition_table(data)
    for p in parts:
        tn = PTYPES.get(p["type"], f"?{p['type']}")
        sn = DSUBTYPES.get(p["subtype"], f"0x{p['subtype']:02x}") if p["type"] == 1 else f"0x{p['subtype']:02x}"
        print(f"  {p['label']:<12} {tn:<5}/{sn:<9} off=0x{p['offset']:06x} "
              f"size=0x{p['size']:06x} ({p['size']/1024:.0f}K)")

    # ---------------------------------------------------------- 板型字符串
    print("\n=== BOARD / CONFIG STRINGS ===")
    blob = b"\n".join(strings(data))
    pats = [
        r"ESP32[-_ ]?2432S\d+\w*", r"\bCYD\b", r"Sunton\w*", r"TZT?\w*\d+",
        r"ILI9341", r"ST7789", r"ST7796", r"GC9A01", r"ILI9488", r"SSD1306",
        r"XPT2046", r"FT6236", r"FT5\d{3}", r"GT911", r"CST816\w*",
        r"WROOM", r"WROVER", r"ESP32-S[23]", r"ESP32-C3",
        r"Lolin\w*", r"T-Display\w*", r"TTGO\w*", r"LilyGo\w*", r"Heltec\w*",
        r"ODROID\w*", r"M5Stack\w*", r"Wemos\w*", r"FireBeetle\w*",
        r"board\w*\s*[:=]", r"2\.4\s*inch", r"2\.8\s*inch", r"3\.5\s*inch",
    ]
    seen = set()
    for pat in pats:
        for m in re.finditer(pat.encode(), blob, re.I):
            s = m.group().decode("utf-8", "replace")
            key = (pat, s)
            if key in seen:
                continue
            seen.add(key)
            print(f"  [{pat}]  {s}")

    # ---------------------------------------------------------- NerdMiner 版本
    print("\n=== VERSION / IDENTITY ===")
    for pat in [rb"NerdMiner[\w\.\- ]{0,24}", rb"v?1\.8\.\d+", rb"nminer[\w\.\-]{0,20}",
                rb"bitmaker[\w\.\-]{0,20}", rb"public-pool[\w\.\-:]{0,20}"]:
        hits = sorted({m.group().decode("utf-8", "replace") for m in re.finditer(pat, blob, re.I)})
        for h in hits[:6]:
            print(f"  {h}")

    # ---------------------------------------------------------- NVS
    print("\n=== NVS PARTITIONS (strings) ===")
    nvs_parts = [p for p in parts if p["type"] == 1 and p["subtype"] == 0x02]
    if not nvs_parts:
        nvs_parts = [dict(label="guess", offset=0x9000, size=0x5000)]
    for p in nvs_parts:
        buf = data[p["offset"]:p["offset"] + p["size"]]
        ss = strings(buf, 4)
        print(f"  -- {p['label']} @0x{p['offset']:06x} ({len(ss)} strings)")
        for s in ss[:400]:
            t = s.decode("utf-8", "replace")
            print(f"     {t}")


if __name__ == "__main__":
    sys.exit(main())
