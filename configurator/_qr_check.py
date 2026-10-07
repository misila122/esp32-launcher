#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把板子上那张配网二维码读回来，验证它真的能被扫。

为什么需要这个脚本：二维码画在屏幕上是"只写"的 —— 编码错了（版本不对、数据被截断、
静默区不够、掩码算错）我从电脑这边根本看不出来，只能等用户扫不出来再回头猜。
板子的 8269 端口有个 `PROV QR` 指令会把编好的模块矩阵原样吐出来，于是就能：

  1. 把矩阵在电脑上重建成 PNG（也就是屏幕上的图案，逐格一致）；
  2. 用 OpenCV 的 QRCodeDetector **真的解码一遍**，看解出来的字符串是不是
     板子自称的那串 WIFI: 文本；
  3. 顺手查几处结构不变量（尺寸 = 4*version+17、三个角上的定位图案、
     时序图案），万一解码器版本挑剔也好定位是哪一步坏的。

用法：
    python _qr_check.py                 # 默认 esp32-launcher.local
    python _qr_check.py esp32-launcher.local qr_board.png
"""

import os
import socket
import sys

HOST_DEFAULT = "esp32-launcher.local"
PORT = 8269
TOKEN = "esp32launcher"
PNG_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qr_board.png")

# 定位图案（7x7）的样子，用来查三个角
FINDER = [
    "1111111",
    "1000001",
    "1011101",
    "1011101",
    "1011101",
    "1000001",
    "1111111",
]


def talk(host, commands, timeout=10.0):
    """连上板子的 8269，握手，把每条命令的回话按行收下来。"""
    out = []
    s = socket.create_connection((host, PORT), timeout=timeout)
    try:
        f = s.makefile("rwb")
        f.write(b"HELLO " + TOKEN.encode() + b"\n")
        f.flush()
        hello = f.readline().decode("utf-8", "replace").strip()
        out.append(("hello", hello))
        for cmd in commands:
            f.write(cmd.encode() + b"\n")
            f.flush()
            lines = []
            while True:
                ln = f.readline()
                if not ln:
                    break
                text = ln.decode("utf-8", "replace").rstrip("\r\n")
                lines.append(text)
                # PROV 的回话以 END 收尾；别的命令一行就完事
                if text == "END" or (cmd != "PROV QR" and not text.startswith("QRMATRIX")):
                    break
            out.append((cmd, lines))
        try:
            f.write(b"BYE\n")
            f.flush()
            f.readline()
        except OSError:
            pass
    finally:
        s.close()
    return out


def parse_prov(lines):
    """从 PROV 的回话里挑出 QRTEXT / QRMATRIX / 矩阵行。"""
    text = None
    size = None
    version = None
    matrix = []
    for ln in lines:
        if ln.startswith("QRTEXT "):
            text = ln[7:]
        elif ln.startswith("QRMATRIX "):
            parts = ln.split()
            size = int(parts[1])
            version = int(parts[2])
        elif size and len(matrix) < size and set(ln) <= {"0", "1"} and len(ln) == size:
            matrix.append(ln)
    return text, size, version, matrix


def check_structure(matrix, size, version):
    """查二维码本身该有的结构。返回 (ok, 描述列表)。"""
    notes = []
    ok = True

    want = 4 * version + 17
    if size != want:
        ok = False
        notes.append("尺寸不对：version %d 应该是 %d 格，实际 %d 格" % (version, want, size))
    else:
        notes.append("尺寸 %dx%d 与 version %d 相符" % (size, size, version))

    # 三个角上的定位图案（左上、右上、左下）
    corners = [(0, 0), (size - 7, 0), (0, size - 7)]
    names = ["左上", "右上", "左下"]
    for (cx, cy), nm in zip(corners, names):
        bad = 0
        for y in range(7):
            for x in range(7):
                if matrix[cy + y][cx + x] != FINDER[y][x]:
                    bad += 1
        if bad:
            ok = False
            notes.append("%s定位图案有 %d 格不对" % (nm, bad))
        else:
            notes.append("%s定位图案正确" % nm)

    # 时序图案：第 6 行 / 第 6 列在定位图案之间必须黑白相间
    bad = 0
    for i in range(8, size - 8):
        if matrix[6][i] != ("1" if i % 2 == 0 else "0"):
            bad += 1
        if matrix[i][6] != ("1" if i % 2 == 0 else "0"):
            bad += 1
    if bad:
        ok = False
        notes.append("时序图案有 %d 格不对" % bad)
    else:
        notes.append("时序图案正确")

    return ok, notes


def render_png(matrix, size, path, scale=8, quiet=4):
    """把矩阵画成 PNG —— 和屏幕上那块是逐格一致的，只是放大到好扫。"""
    from PIL import Image

    total = (size + quiet * 2) * scale
    img = Image.new("L", (total, total), 255)
    px = img.load()
    for y in range(size):
        for x in range(size):
            if matrix[y][x] == "1":
                for dy in range(scale):
                    for dx in range(scale):
                        px[(x + quiet) * scale + dx, (y + quiet) * scale + dy] = 0
    img.save(path)
    return total


def decode_png(path):
    """用 OpenCV 真解码一遍。

    注意：矩阵是坏的时 OpenCV 不是老老实实返回空串，而是从解码器里抛
    cv2.error（例如 `Assertion failed: idx < data.size()`）。那本身就是
    "这张图扫不出来"的证据，所以要接住它当成解码失败，别让脚本崩掉。
    """
    import cv2

    img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None, "OpenCV 读不了这张图"
    det = cv2.QRCodeDetector()
    try:
        data, pts, _ = det.detectAndDecode(img)
    except cv2.error as exc:
        first = str(exc).strip().splitlines()[-1] if str(exc).strip() else "?"
        return None, "OpenCV 解码器直接报错（图是坏的）：%s" % first[:110]
    return data, ("检出 %d 个角点" % len(pts) if pts is not None else "没检出二维码")


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else HOST_DEFAULT
    png = sys.argv[2] if len(sys.argv) > 2 else PNG_DEFAULT

    print("== 从板子读二维码 ==")
    try:
        replies = talk(host, ["PROV QR"])
    except OSError as exc:
        print("连不上 %s:%d —— %s" % (host, PORT, exc))
        return 2

    for name, payload in replies:
        if name == "hello":
            print("HELLO -> %s" % payload)
        else:
            # 矩阵那几十行不打印，免得刷屏
            head = [x for x in payload if not (set(x) <= {"0", "1"} and len(x) > 20)]
            for ln in head:
                print("  %s" % ln)

    prov_lines = replies[-1][1] if replies[-1][0] != "hello" else []
    text, size, version, matrix = parse_prov(prov_lines)

    if not text or not size:
        print("\n板子没有返回二维码矩阵。")
        print("可能原因：热点已经关了（连上 WiFi 30 秒后会自动关）。")
        print("先让板子重来一次：8269 发 REBOOT，或按一下板子的 EN 键。")
        return 2

    print("\n== 内容 ==")
    print("  WIFI 串 : %s" % text)
    print("  版本/尺寸: version %d, %dx%d 格" % (version, size, size))
    if len(matrix) != size:
        print("矩阵行数不对：应该有 %d 行，只收到 %d 行" % (size, len(matrix)))
        return 2

    ok_struct, notes = check_structure(matrix, size, version)
    print("\n== 结构 ==")
    for n in notes:
        print("  %s" % n)

    total = render_png(matrix, size, png)
    print("\n== 重建的图 ==")
    print("  %s  (%dx%d 像素)" % (png, total, total))

    data, how = decode_png(png)
    print("\n== 解码 ==")
    print("  %s" % how)
    if not data:
        print("  解不出来 —— 二维码是坏的，手机也扫不到。")
        return 1
    print("  解出: %s" % data)

    same = data.strip() == text.strip()
    print("\n== 结论 ==")
    if same and ok_struct:
        print("  通过：板子上那张二维码能解出预期的 WIFI 串，结构也合法。")
        return 0
    if not same:
        print("  失败：解出来的是 %r，板子自称 %r" % (data, text))
        return 1
    print("  能解出来，但结构检查有问题（见上）。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
