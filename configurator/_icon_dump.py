#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把板子上六格**当前真正用的**图标全读回来，拼成一张对比图。

这是回答"配置器里显示对了、板子上却是另一张图"这类问题的唯一硬手段 ——
`GET` 只回一个 0/1（有没有自定义图标），不回图标本身。

跑法： python _icon_dump.py [ip] [输出png]
产物： 六格横排的 PNG（左边是板子上的，右边是配置器本地抠的，方便并排看）
"""
import os
import struct
import sys
import zlib

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
import esp32_configurator as C                # noqa: E402

IP = sys.argv[1] if len(sys.argv) > 1 else "esp32-launcher.local"
OUT = sys.argv[2] if len(sys.argv) > 2 else "board_icons.png"
SCALE = 3


def rgb565_to_rgb888(blob):
    """8192 字节 RGB565 小端 -> 4096 个 (r,g,b)"""
    px = []
    for i in range(0, len(blob), 2):
        v = blob[i] | (blob[i + 1] << 8)
        r = (v >> 11) & 0x1F
        g = (v >> 5) & 0x3F
        b = v & 0x1F
        px.append(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)))
    return px


def png(path, w, h, rows):
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))
    open(path, "wb").write(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def main():
    dev = C.Device(IP)
    dev.connect()
    rows = dev.get()

    board_px = []
    for r in rows:
        blob = dev.get_icon(r["idx"])
        if blob is None or len(blob) != C.ICON_BYTES:
            print("卡片 %d：读图标失败" % (r["idx"] + 1))
            blob = b"\x00" * C.ICON_BYTES
        else:
            print("卡片 %d  %-9s  #%06X  iconFs=%d  -> 读回 %d 字节"
                  % (r["idx"] + 1, r["label"], r["rgb"], int(r["icon_fs"]), len(blob)))
        board_px.append(rgb565_to_rgb888(blob))
    dev.close()

    # 本地（配置器会用来预览的）图标：按 id 去 apps.json 找 exe 抠
    local_px = []
    for r in rows:
        rgba = None
        try:
            import esp32_configurator as _C
            exe = None
            p = C.find_apps_json()
            if p:
                data = C.load_apps_json(p)
                for a in (data or {}).get("apps", []):
                    if int(a.get("id", -1)) == int(r["id"]):
                        exe = a.get("path")
            if exe and os.path.exists(exe):
                rgba, _blob, _note = C.icon_from_exe(exe)
        except Exception as exc:                          # noqa: BLE001
            print("  本地抠图失败（%s）：%s" % (r["label"], exc))
        if rgba is None:
            rgba = C.placeholder_rgba(r["rgb"])
        local_px.append([(rgba[i * 4], rgba[i * 4 + 1], rgba[i * 4 + 2])
                         for i in range(C.ICON_W * C.ICON_H)])

    # 拼图：每格上面是板子的、下面是本地的，横排 6 列
    W = C.ICON_W * C.SLOTS
    H = C.ICON_H * 2
    img = [[(14, 17, 22)] * W for _ in range(H)]
    for s in range(C.SLOTS):
        for y in range(C.ICON_H):
            for x in range(C.ICON_W):
                img[y][s * C.ICON_W + x] = board_px[s][y * C.ICON_W + x]
                img[C.ICON_H + y][s * C.ICON_W + x] = local_px[s][y * C.ICON_W + x]

    # 放大（最近邻）
    big = []
    for row in img:
        r2 = []
        for p in row:
            r2.extend([p] * SCALE)
        for _ in range(SCALE):
            big.append(r2)
    png(OUT, W * SCALE, H * SCALE, [[c for p in row for c in p] for row in big])
    print()
    print("写好 %s（%dx%d）" % (os.path.abspath(OUT), W * SCALE, H * SCALE))
    print("上半排 = 板子上真正在用的；下半排 = 配置器从本机 exe 抠的。不一样就说明对不上。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
