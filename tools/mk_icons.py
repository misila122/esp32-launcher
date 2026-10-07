#!/usr/bin/env python3
"""Turn extracted app icons into an RGB565 C header for the ESP32 firmware.

Pure standard library: decodes PNG (RGBA8/RGB8, non-interlaced) and ICO
(32bpp / 24bpp DIB frames), area-average downscales to ICON_W x ICON_H,
alpha-composites onto the tile background colour, and emits

    static const uint16_t ICON_SLOT0[ICON_W * ICON_H] = { ... };

Also writes a preview.png contact sheet so the result can be eyeballed
before flashing.

Usage:
    python mk_icons.py <icons_dir> <out_header.h> [--size 64] [--preview p.png]
"""
import argparse
import os
import struct
import sys
import zlib

# Tile background (C_TILE in main.cpp) -- icons are flattened onto this.
BG = (0x17, 0x1C, 0x23)


# --------------------------------------------------------------------------
# PNG decoding
# --------------------------------------------------------------------------
def _png_decode(path):
    data = open(path, "rb").read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos = 8
    idat = b""
    w = h = depth = ctype = interlace = None
    while pos < len(data):
        (length,) = struct.unpack_from(">I", data, pos)
        ctag = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctag == b"IHDR":
            w, h, depth, ctype, _comp, _filt, interlace = \
                struct.unpack(">IIBBBBB", chunk)
        elif ctag == b"IDAT":
            idat += chunk
        elif ctag == b"IEND":
            break
    if depth != 8:
        raise ValueError(f"unsupported PNG bit depth {depth}")
    if interlace:
        raise ValueError("interlaced PNG not supported")
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(ctype)
    if channels is None:
        raise ValueError(f"unsupported PNG colour type {ctype}")

    raw = zlib.decompress(idat)
    stride = w * channels
    out = bytearray(h * stride)
    prev = bytearray(stride)
    p = 0
    for y in range(h):
        f = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if f == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif f == 3:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif f == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        elif f != 0:
            raise ValueError(f"bad PNG filter {f}")
        out[y * stride:(y + 1) * stride] = line
        prev = line

    # Normalise everything to RGBA.
    px = bytearray(w * h * 4)
    for i in range(w * h):
        s = i * channels
        d = i * 4
        if channels == 4:
            px[d:d + 4] = out[s:s + 4]
        elif channels == 3:
            px[d:d + 3] = out[s:s + 3]
            px[d + 3] = 255
        elif channels == 2:
            px[d] = px[d + 1] = px[d + 2] = out[s]
            px[d + 3] = out[s + 1]
        else:
            px[d] = px[d + 1] = px[d + 2] = out[s]
            px[d + 3] = 255
    return w, h, px


# --------------------------------------------------------------------------
# ICO decoding
# --------------------------------------------------------------------------
def _ico_best(path):
    data = open(path, "rb").read()
    reserved, itype, count = struct.unpack_from("<HHH", data, 0)
    if reserved or itype != 1:
        raise ValueError("not an ICO")
    best = None
    for i in range(count):
        w, h, _colors, _res, _planes, bits, nbytes, offset = \
            struct.unpack_from("<BBBBHHII", data, 6 + i * 16)
        w = w or 256
        h = h or 256
        blob = data[offset:offset + nbytes]
        score = (w * h, bits)
        if best is None or score > best[0]:
            best = (score, w, h, bits, blob)
    if best is None:
        raise ValueError("empty ICO")
    _score, w, h, bits, blob = best

    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        tmp = path + ".tmp.png"
        open(tmp, "wb").write(blob)
        try:
            return _png_decode(tmp)
        finally:
            os.remove(tmp)

    hdr = struct.unpack_from("<IiiHHIIiiII", blob, 0)
    bi_size, bi_w, bi_h2, _planes, bi_bits, bi_comp = hdr[:6]
    if bi_size < 40 or bi_comp != 0:
        raise ValueError("unsupported DIB frame")
    real_h = bi_h2 // 2
    if bi_bits not in (24, 32):
        raise ValueError(f"unsupported DIB depth {bi_bits}")

    src = blob[bi_size:]
    bpp = bi_bits // 8
    row = ((bi_w * bi_bits + 31) // 32) * 4
    px = bytearray(bi_w * real_h * 4)
    for y in range(real_h):
        # DIB rows are stored bottom-up.
        s = (real_h - 1 - y) * row
        for x in range(bi_w):
            o = s + x * bpp
            b, g, r = src[o], src[o + 1], src[o + 2]
            a = src[o + 3] if bpp == 4 else 255
            d = (y * bi_w + x) * 4
            px[d], px[d + 1], px[d + 2], px[d + 3] = r, g, b, a
    # 32bpp frames with a fully transparent alpha channel fall back to the
    # AND mask; for icon artwork a zero alpha almost always means "no alpha
    # data supplied", so treat it as opaque.
    if bpp == 4 and not any(px[3::4]):
        for i in range(3, len(px), 4):
            px[i] = 255
    return bi_w, real_h, px


def load_rgba(path):
    if path.lower().endswith(".png"):
        return _png_decode(path)
    return _ico_best(path)


# --------------------------------------------------------------------------
# Scaling / encoding
# --------------------------------------------------------------------------
def downscale(w, h, px, tw, th):
    """Area-average resample -- averages every source pixel that lands in a
    destination cell, which keeps small icons legible when shrinking hard."""
    out = bytearray(tw * th * 4)
    for ty in range(th):
        y0 = ty * h // th
        y1 = max(y0 + 1, (ty + 1) * h // th)
        for tx in range(tw):
            x0 = tx * w // tw
            x1 = max(x0 + 1, (tx + 1) * w // tw)
            r = g = b = a = n = 0
            for y in range(y0, y1):
                base = y * w
                for x in range(x0, x1):
                    d = (base + x) * 4
                    al = px[d + 3]
                    # Weight colour by alpha so transparent edges do not
                    # darken the result.
                    r += px[d] * al
                    g += px[d + 1] * al
                    b += px[d + 2] * al
                    a += al
                    n += 1
            o = (ty * tw + tx) * 4
            if a:
                out[o] = min(255, r // a)
                out[o + 1] = min(255, g // a)
                out[o + 2] = min(255, b // a)
            out[o + 3] = a // n if n else 0
    return out


# Colour key handed to pushImage() as its transparent argument. Magenta
# appears in none of the six icons, so it is safe as a key.
KEY = 0xF81F


def to565(w, h, px, alpha_min=128):
    """Emit RGB565 with a colour key standing in for transparent pixels.

    Transparency has to survive into the bitmap: the firmware draws each icon
    over whatever tile background the current state needs (idle / pressed /
    ok / err), so flattening onto one fixed colour would leave a visible
    rectangle behind every icon on a highlighted tile. Edges are hard
    thresholded rather than alpha blended for the same reason.
    """
    words = []
    for i in range(w * h):
        d = i * 4
        if px[d + 3] < alpha_min:
            words.append(KEY)
            continue
        r, g, b = px[d], px[d + 1], px[d + 2]
        words.append(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3))
    return words


def write_png(path, w, h, rgb):
    rows = b"".join(b"\x00" + bytes(rgb[y * w * 3:(y + 1) * w * 3])
                    for y in range(h))
    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(rows, 9))
           + chunk(b"IEND", b""))
    open(path, "wb").write(png)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("icons_dir")
    ap.add_argument("header")
    ap.add_argument("--size", type=int, default=64)
    ap.add_argument("--preview")
    ap.add_argument("--names", default="slot0,slot1,slot2,slot3,slot4,slot5",
                    help="逗号分隔；会拼成 ICON_<NAME>，要和 apps.h 的槽位顺序一致")
    args = ap.parse_args()

    size = args.size
    names = [n.strip() for n in args.names.split(",") if n.strip()]

    out = ["// Generated by tools/mk_icons.py -- do not edit by hand.",
           "// Icons extracted from the real application executables.",
           "#pragma once",
           "#include <stdint.h>",
           "",
           f"#define ICON_W {size}",
           f"#define ICON_H {size}",
           f"#define ICON_TRANSPARENT 0x{KEY:04X}u",
           ""]

    sheet_w = size * len(names)
    sheet = bytearray(sheet_w * size * 3)

    for idx, name in enumerate(names):
        src = None
        for ext in (".png", ".ico"):
            cand = os.path.join(args.icons_dir, name + ext)
            if os.path.exists(cand):
                src = cand
                break
        if src is None:
            print(f"FAIL {name}: no .png/.ico in {args.icons_dir}")
            return 1
        w, h, px = load_rgba(src)
        small = downscale(w, h, px, size, size)
        words = to565(size, size, small)
        print(f"OK   {name:7s} {w}x{h} -> {size}x{size}  ({os.path.basename(src)})")

        for y in range(size):
            for x in range(size):
                i = y * size + x
                c = words[i]
                if c == KEY:
                    r, g, b = BG
                else:
                    r = ((c >> 11) & 0x1F) * 255 // 31
                    g = ((c >> 5) & 0x3F) * 255 // 63
                    b = (c & 0x1F) * 255 // 31
                d = (y * sheet_w + idx * size + x) * 3
                sheet[d], sheet[d + 1], sheet[d + 2] = r, g, b

        out.append(f"static const uint16_t ICON_{name.upper()}"
                   f"[ICON_W * ICON_H] = {{")
        for y in range(size):
            row = words[y * size:(y + 1) * size]
            out.append("  " + " ".join(f"0x{v:04X}," for v in row))
        out.append("};")
        out.append("")

    out.append("// Index order matches APPS[] in apps.h.")
    out.append("static const uint16_t* const ICONS[] = {")
    for name in names:
        out.append(f"  ICON_{name.upper()},")
    out.append("};")
    out.append("")

    open(args.header, "w", newline="\n").write("\n".join(out))
    print(f"\nwrote {args.header}  ({len(names)} icons @ {size}x{size}, "
          f"{len(names) * size * size * 2} bytes of pixel data)")

    if args.preview:
        write_png(args.preview, sheet_w, size, sheet)
        print(f"wrote {args.preview}  ({sheet_w}x{size} contact sheet)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
