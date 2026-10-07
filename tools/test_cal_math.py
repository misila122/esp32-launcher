#!/usr/bin/env python3
"""
验证固件里触摸标定的数学（纯算术，不碰硬件）。

模拟链条：
  真实手指按在屏幕 (sx,sy)
    -> 反推该点对应的"面板原始方向"坐标      (screenToPanelSpace，C++ 里的同一个公式)
    -> 用【真实】标定值正推该点会读到的 ADC  (Panel_Device::convertRawXY + setCalibrate 的线性模型)
  然后把这两组数喂给 solveTouchCal()，看能不能把真实标定值原样解回来。

只要每一档旋转都能解回原值，就说明屏幕->面板的逆变换和 board.h 里的一致。
"""
import math

PANEL_W, PANEL_H = 240, 320          # ILI9341 原始方向
CAL_R = 16


def internal_rotation(r, panel_offset=0, touch_offset=0):
    """照抄 Panel_LCD::setRotation + convertRawXY 里叠加触摸 offset 的算法。"""
    ir = ((r + panel_offset) & 3) | ((r & 4) ^ (panel_offset & 4))
    ir = ((ir + touch_offset) & 3) | ((ir & 4) ^ (touch_offset & 4))
    return ir


def screen_dims(ir):
    return (PANEL_H, PANEL_W) if (ir & 1) else (PANEL_W, PANEL_H)


def screen_to_panel(sx, sy, ir, W, H):
    """C++ LGFX::screenToPanelSpace 的 Python 版。"""
    vflip = (1 << ir) & 0b10010110
    a, b = sx, sy
    if vflip:
        b = (H - 1) - b
    if ir & 2:
        a = (W - 1) - a
    if ir & 1:
        a, b = b, a
    return a, b


def panel_to_screen(px, py, ir, W, H):
    """Panel_Device::convertRawXY 的旋转部分（正向）。"""
    tx, ty = px, py
    if ir:
        if ir & 1:
            tx, ty = ty, tx
        if ir & 2:
            tx = (W - 1) - tx
        if (1 << ir) & 0b10010110:
            ty = (H - 1) - ty
    return tx, ty


def raw_from_panel(px, py, cal):
    """用真实标定值正推 ADC 读数（setCalibrate 建的是线性模型）。"""
    xmin, xmax, ymin, ymax = cal
    rx = xmin + px * (xmax - xmin) / (PANEL_W - 1)
    ry = ymin + py * (ymax - ymin) / (PANEL_H - 1)
    return rx, ry


def solve_touch_cal(rx1, px1, rx2, px2, ry1, py1, ry2, py2):
    """C++ LGFX::solveTouchCal 的 Python 版。"""
    ax = (px2 - px1) / (rx2 - rx1)
    x0 = rx1 - px1 / ax
    xmin = round(x0)
    xmax = round(x0 + (PANEL_W - 1) / ax)
    ay = (py2 - py1) / (ry2 - ry1)
    y0 = ry1 - py1 / ay
    ymin = round(y0)
    ymax = round(y0 + (PANEL_H - 1) / ay)
    return xmin, xmax, ymin, ymax


def run(cal, ir, report=False):
    W, H = screen_dims(ir)
    # 两个靶心：左上、右下，各内缩 26/27 像素（跟 main.cpp 里 calStart 一致）
    t1 = (26, 26)
    t2 = (W - 27, H - 27)

    pts = []
    for (sx, sy) in (t1, t2):
        px, py = screen_to_panel(sx, sy, ir, W, H)
        rx, ry = raw_from_panel(px, py, cal)
        pts.append((sx, sy, px, py, rx, ry))

    (_, _, p1x, p1y, r1x, r1y), (_, _, p2x, p2y, r2x, r2y) = pts

    got = solve_touch_cal(r1x, p1x, r2x, p2x, r1y, p1y, r2y, p2y)

    # 校验：拿解出来的标定值重建，看靶心和屏幕正中能不能对上
    def predict(sx, sy):
        px, py = screen_to_panel(sx, sy, ir, W, H)
        return raw_from_panel(px, py, cal)

    if report:
        print(f"  panel 靶心: ({p1x},{p1y}) ({p2x},{p2y})")
        print(f"  模拟 ADC  : ({r1x:.1f},{r1y:.1f}) ({r2x:.1f},{r2y:.1f})")
        print(f"  真实标定  : x {cal[0]}..{cal[1]}  y {cal[2]}..{cal[3]}")
        print(f"  解出标定  : x {got[0]}..{got[1]}  y {got[2]}..{got[3]}")

    ok = all(abs(g - c) <= 1 for g, c in zip(got, cal))

    # 用解出的标定值验一遍"屏幕正中"：正推 ADC -> 再走一遍固件的正向链路
    xmin, xmax, ymin, ymax = got
    cx, cy = W // 2, H // 2
    rx, ry = predict(cx, cy)
    px = (rx - xmin) * (PANEL_W - 1) / (xmax - xmin)
    py = (ry - ymin) * (PANEL_H - 1) / (ymax - ymin)
    bx, by = panel_to_screen(px, py, ir, W, H)
    err = max(abs(bx - cx), abs(by - cy))
    if report:
        print(f"  中心回代  : 想要 ({cx},{cy}) 得到 ({bx:.2f},{by:.2f})  误差 {err:.2f}px")
    return ok, err


def main():
    print(__doc__)
    cases = [
        ("官方 CYD 值", (300, 3900, 3700, 200)),
        ("另一块板子", (250, 3800, 3650, 280)),
    ]
    all_ok = True
    for name, cal in cases:
        print(f"\n=== {name}: x {cal[0]}..{cal[1]}  y {cal[2]}..{cal[3]} ===")
        for r in range(4):
            ir = internal_rotation(r)
            W, H = screen_dims(ir)
            ok, err = run(cal, ir, report=(r == 1))
            flag = "OK " if (ok and err <= 1.5) else "BAD"
            if not (ok and err <= 1.5):
                all_ok = False
            print(f"  rotation={r} (ir={ir}, {W}x{H})  {flag}  解回原值={ok}  中心误差={err:.2f}px")

    print("\n" + ("全部通过" if all_ok else "!!! 有失败项 !!!"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
