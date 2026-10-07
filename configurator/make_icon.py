#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给配置器 exe 画一个图标（纯标准库，不需要 Pillow）。

画的是**那块板子本身**：圆角黑边框（塑料外壳）+ 中间 4:3 的屏幕 + 屏幕上按固件真实
几何排的六张彩色卡片。为什么不是"六个方块"抽象一下就算了：这个程序的全部作用就是
当那块屏的遥控器，图标直接画那块屏，一眼就知道是什么。

几何全部从 firmware/src/main.cpp 抄：
    屏幕 320x240，状态栏高 22
    卡片 3 列 x 2 行，tile0 在 (6,28)，每张 98x100，间距 6
颜色也从固件抄：外壳用界面里那圈边框的 #05070A，屏幕用 C_BG #0E1116，
状态栏用 #161B22，六张卡用 apps.h 里的六个应用色。

输出 app.ico，内含 16/32/48/64/128/256 六个尺寸。
跑法： python make_icon.py [输出路径]
"""
import os
import struct
import sys

# 六张卡的颜色，顺序和 apps.h 一致
COLORS = [(0x4C, 0x8B, 0xF5), (0x7B, 0x61, 0xFF), (0xFB, 0x72, 0x99),
          (0xF0, 0xB9, 0x0B), (0x00, 0xC2, 0xA8), (0x07, 0xC1, 0x60)]
BEZEL = (0x05, 0x07, 0x0A)     # 外壳。和界面里预览外面那圈一样
SCREEN = (0x0E, 0x11, 0x16)    # C_BG
BAR = (0x1B, 0x21, 0x2B)       # 状态栏。比 C_CHROME 略亮 —— 图标里只有十几像素高，
                               # 照抄 #161B22 会和屏幕底糊成一片，看不出是条状态栏
OK_C = (0x3F, 0xB9, 0x50)      # C_OK，状态栏左边那颗"连上了"的绿点
DIM_C = (0x7D, 0x85, 0x90)     # C_DIM，右边代表配置端口的那颗灰点
EDGE = (0x2A, 0x31, 0x3B)      # C_TILE_EDGE，屏幕描边

# ---- 固件里的真实几何（单位：屏幕像素）----
DEV_W, DEV_H = 320.0, 240.0
BAR_H = 22.0
TILE_W, TILE_H, TILE_GAP, TILE_X0, TILE_Y0 = 98.0, 100.0, 6.0, 6.0, 28.0
COLS, ROWS = 3, 2

SIZES = (16, 32, 48, 64, 128, 256)


def _rrect(x, y, x0, y0, x1, y1, r):
    """(x,y) 是否落在 [x0,x1) x [y0,y1) 的圆角矩形里。"""
    if x < x0 or x >= x1 or y < y0 or y >= y1:
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    dx, dy = x - cx, y - cy
    return dx * dx + dy * dy <= r * r


def _put(px, W, x, y, c):
    i = (y * W + x) * 4
    px[i], px[i + 1], px[i + 2], px[i + 3] = c[0], c[1], c[2], 255


def render(size):
    """渲染一张 size x size 的 RGBA（bytes，行序自上而下）。

    先按 ss 倍超采样再盒式缩小 —— 圆角和斜边才不会长锯齿。纯 Python 逐像素，
    所以大尺寸用低倍率：反正 256 的图标本来也没人拿放大镜看。
    """
    ss = 8 if size <= 32 else (4 if size <= 128 else 2)
    W = size * ss
    px = bytearray(W * W * 4)

    # ---- 外壳 ----
    outer_r = W * 0.20
    for y in range(W):
        for x in range(W):
            if _rrect(x + 0.5, y + 0.5, 0, 0, W, W, outer_r):
                _put(px, W, x, y, BEZEL)

    # ---- 屏幕：在外壳里按 4:3 居中放 ----
    margin = W * 0.055
    avail = W - margin * 2
    sw = avail
    sh = sw * DEV_H / DEV_W
    if sh > avail:                      # 理论上不会，留个保险
        sh = avail
        sw = sh * DEV_W / DEV_H
    sx0 = (W - sw) / 2.0
    sy0 = (W - sh) / 2.0
    sx1, sy1 = sx0 + sw, sy0 + sh
    scale = sw / DEV_W                  # 固件像素 -> 本图像素
    screen_r = max(1.0, sw * 0.05)

    # 屏幕描边单独一圈，否则 #0E1116 和 #05070A 挨在一起分不出边界
    stroke = max(1.0, W / float(size) * 0.6)
    for y in range(int(sy0), int(sy1) + 1):
        for x in range(int(sx0), int(sx1) + 1):
            fx, fy = x + 0.5, y + 0.5
            if not _rrect(fx, fy, sx0, sy0, sx1, sy1, screen_r):
                continue
            if not _rrect(fx, fy, sx0 + stroke, sy0 + stroke,
                          sx1 - stroke, sy1 - stroke, max(1.0, screen_r - stroke)):
                _put(px, W, x, y, EDGE)
            else:
                _put(px, W, x, y, SCREEN)

    # ---- 状态栏 ----
    # 左边一颗绿点（WiFi 连上了）、右边一颗灰点（配置端口），就是板上状态栏那两个指示。
    # 小于 64 时点只有一像素，画了反而像脏点，索性不画。
    bar_y1 = sy0 + BAR_H * scale
    if size >= 32:                      # 再小就只是一条脏边，不如不画
        for y in range(int(sy0), int(bar_y1) + 1):
            for x in range(int(sx0), int(sx1) + 1):
                fx, fy = x + 0.5, y + 0.5
                if _rrect(fx, fy, sx0, sy0, sx1, sy1, screen_r):
                    _put(px, W, x, y, BAR)
    if size >= 64:
        dot_r = 3.0 * scale
        bar_cy = sy0 + BAR_H * scale / 2.0
        for u, c in ((7.0, OK_C), (DEV_W - 7.0, DIM_C)):
            dcx = sx0 + u * scale
            for y in range(int(bar_cy - dot_r) - 1, int(bar_cy + dot_r) + 2):
                for x in range(int(dcx - dot_r) - 1, int(dcx + dot_r) + 2):
                    if 0 <= x < W and 0 <= y < W:
                        dx, dy = x + 0.5 - dcx, y + 0.5 - bar_cy
                        if dx * dx + dy * dy <= dot_r * dot_r:
                            _put(px, W, x, y, c)

    # ---- 六张卡 ----
    tw, th = TILE_W * scale, TILE_H * scale
    tr = max(1.0, tw * 0.18)
    for k in range(COLS * ROWS):
        col, row = k % COLS, k // COLS
        x0 = sx0 + (TILE_X0 + col * (TILE_W + TILE_GAP)) * scale
        y0 = sy0 + (TILE_Y0 + row * (TILE_H + TILE_GAP)) * scale
        c = COLORS[k % len(COLORS)]
        for y in range(int(y0), int(y0 + th) + 1):
            for x in range(int(x0), int(x0 + tw) + 1):
                # 也要求在屏幕圆角矩形里：卡片本身不越界，但屏幕的圆角会切掉
                # 左下/右下那块，不判一下将来改了几何就会露到外壳上去
                if _rrect(x + 0.5, y + 0.5, x0, y0, x0 + tw, y0 + th, tr) and \
                        _rrect(x + 0.5, y + 0.5, sx0, sy0, sx1, sy1, screen_r):
                    _put(px, W, x, y, c)

    # ---- 盒式缩小 ss 倍 ----
    out = bytearray(size * size * 4)
    n = ss * ss
    for y in range(size):
        for x in range(size):
            r = g = b = a = 0
            for dy in range(ss):
                base = ((y * ss + dy) * W + x * ss) * 4
                for dx in range(ss):
                    i = base + dx * 4
                    r += px[i]
                    g += px[i + 1]
                    b += px[i + 2]
                    a += px[i + 3]
            o = (y * size + x) * 4
            out[o] = r // n
            out[o + 1] = g // n
            out[o + 2] = b // n
            out[o + 3] = a // n
    return bytes(out)


def bmp_entry(rgba, size):
    """把一个尺寸打成 ICO 里的 BMP 条目（32bpp，自下而上，附全零 AND 掩码）。"""
    xor = bytearray()
    for y in range(size - 1, -1, -1):          # 自下而上
        row = rgba[y * size * 4:(y + 1) * size * 4]
        for x in range(size):
            rr, gg, bb, aa = row[x * 4:x * 4 + 4]
            xor += bytes((bb, gg, rr, aa))     # BGRA
    mask_row = ((size + 31) // 32) * 4         # 1bpp，行按 4 字节对齐
    and_mask = bytes(mask_row * size)          # 全 0 = 全不透明，靠 alpha 通道

    hdr = struct.pack("<IiiHHIIiiII",
                      40, size, size * 2, 1, 32, 0,
                      len(xor) + len(and_mask), 0, 0, 0, 0)
    return hdr + bytes(xor) + and_mask


def write_ico(path, sizes=SIZES):
    entries = []
    for s in sizes:
        entries.append((s, bmp_entry(render(s), s)))
    out = bytearray(struct.pack("<HHH", 0, 1, len(entries)))
    offset = 6 + 16 * len(entries)
    for s, blob in entries:
        out += struct.pack("<BBBBHHII",
                           s if s < 256 else 0,
                           s if s < 256 else 0,
                           0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    for _s, blob in entries:
        out += blob
    with open(path, "wb") as f:
        f.write(out)
    return len(out)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "app.ico")
    n = write_ico(out)
    print("写好 %s（%d 字节，%s）" % (
        out, n, "/".join(str(s) for s in SIZES)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
