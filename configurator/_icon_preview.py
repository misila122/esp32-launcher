#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 app.ico 里的某一档解码成 PNG，方便肉眼检查图标画得对不对。

跑法： python _icon_preview.py [尺寸=128] [输出=app_icon_preview.png]
"""
import os
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))


def read_ico(path):
    d = open(path, "rb").read()
    _res, _type, n = struct.unpack("<HHH", d[:6])
    out = {}
    for i in range(n):
        e = d[6 + 16 * i:6 + 16 * (i + 1)]
        w, h, _c, _r, _p, _b, size, off = struct.unpack("<BBBBHHII", e)
        out[w or 256] = d[off:off + size]
    return out


def bmp_to_rgba(blob):
    (hsz, w, h2, planes, bpp, comp, imgsz, xr, yr, used, imp) = \
        struct.unpack("<IiiHHIIiiII", blob[:40])
    h = h2 // 2
    stride = w * 4
    px = bytearray(w * h * 4)
    base = hsz
    for y in range(h):
        src = base + (h - 1 - y) * stride
        for x in range(w):
            b, g, r, a = blob[src + x * 4:src + x * 4 + 4]
            i = (y * w + x) * 4
            px[i], px[i + 1], px[i + 2], px[i + 3] = r, g, b, a
    return bytes(px), w, h


def write_png(path, rgba, w, h, bg=(255, 255, 255)):
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        for x in range(w):
            i = (y * w + x) * 4
            r, g, b, a = rgba[i], rgba[i + 1], rgba[i + 2], rgba[i + 3]
            f = a / 255.0
            raw += bytes((int(r * f + bg[0] * (1 - f)),
                          int(g * f + bg[1] * (1 - f)),
                          int(b * f + bg[2] * (1 - f))))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + chunk(b"IEND", b""))
    open(path, "wb").write(png)
    return len(png)


def main():
    size = int(sys.argv[1]) if len(sys.argv) > 1 else 128
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "app_icon_preview.png")
    icos = read_ico(os.path.join(HERE, "app.ico"))
    print("ico 里有尺寸：", sorted(icos))
    blob = icos[size]
    rgba, w, h = bmp_to_rgba(blob)
    n = write_png(out, rgba, w, h)
    print("写好 %s（%dx%d，%d 字节）" % (out, w, h, n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
