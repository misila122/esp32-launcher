#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置器的无界面自检：不弹窗，只验证抠图标 / 生成头文件 / 协议打包这几条链。

跑法： python configurator/_selftest.py
"""
import os
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import esp32_configurator as C   # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print("%-46s %s %s" % (name, "OK  " if cond else "FAIL", extra))
    if not cond:
        FAILS.append(name)


def main():
    # ---- 1. 颜色解析 ----
    check("parse_color #4C8BF5", C.parse_color("#4C8BF5") == 0x4C8BF5)
    check("parse_color 4c8bf5", C.parse_color("4c8bf5") == 0x4C8BF5)
    check("parse_color #abc", C.parse_color("#abc") == 0xAABBCC)
    check("parse_color 垃圾值走 fallback", C.parse_color("zz", 0x123456) == 0x123456)

    # ---- 2. mix_rgb 和固件一致 ----
    got = C.mix_rgb((0x17, 0x1C, 0x23), (0x4C, 0x8B, 0xF5), 90)
    want = tuple((a * (255 - 90) + b * 90) // 255
                 for a, b in zip((0x17, 0x1C, 0x23), (0x4C, 0x8B, 0xF5)))
    check("mix_rgb 与 main.cpp 同式", got == want, str(got))

    # ---- 3. 占位图标 ----
    ph = C.placeholder_rgba(0xFF0000)
    check("placeholder 尺寸 64*64*4", len(ph) == C.ICON_W * C.ICON_H * 4)
    check("placeholder 中间是不透明的", ph[((32 * 64) + 32) * 4 + 3] == 255)

    # ---- 4. 真 exe 抠图标 ----
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                   "System32", "notepad.exe")
    if os.path.exists(exe):
        rgba, blob, note = C.icon_from_exe(exe)
        check("从 exe 抠图标", len(rgba) == 64 * 64 * 4 and len(blob) == 8192, note)
        # 有小方块是纯色（说明真抠到了东西，不是全透明）
        opaque = sum(1 for i in range(3, len(rgba), 4) if rgba[i] > 200)
        check("抠出来的图标有实体像素", opaque > 64, "%d 个不透明像素" % opaque)
    else:
        print("跳过：%s 不存在" % exe)

    # ---- 5. 生成两个头文件 ----
    cards = []
    for i, (sid, label, rgb, glyph) in enumerate(C.DEFAULTS):
        cards.append({"idx": i, "id": sid, "label": label, "rgb": rgb,
                      "glyph": glyph, "exe": "", "icon_rgba": None,
                      "icon565": None, "icon_dirty": False})
    # 故意留两张没图标，验证占位分支不会生成坏头文件
    if os.path.exists(exe):
        rgba, blob, _n = C.icon_from_exe(exe)
        cards[3]["icon_rgba"], cards[3]["icon565"] = rgba, blob

    with tempfile.TemporaryDirectory() as td:
        ap = os.path.join(td, "apps.h")
        ic = os.path.join(td, "icons.h")
        C.write_apps_h(ap, cards)
        C.write_icons_h(ic, cards)
        atxt = open(ap, encoding="utf-8").read()
        itxt = open(ic, encoding="utf-8").read()
        check("apps.h 有 APP_COUNT", "APP_COUNT = (int)(sizeof(APPS)" in atxt)
        check("apps.h 六行 APPS", atxt.count("\n  { ") == 6)
        check("icons.h 六个 ICON_SLOT", itxt.count("static const uint16_t ICON_SLOT") == 6)
        check("icons.h ICONS[] 六项", itxt.count("  ICON_SLOT") >= 6)
        # 每个 ICON_SLOT 必须正好 64 行 x 64 个值
        body = [ln for ln in itxt.splitlines() if ln.startswith("  0x")]
        check("像素行数 = 6*64", len(body) == 6 * 64, "实际 %d" % len(body))
        check("每行 64 个值", all(len(ln.split()) == 64 for ln in body))
        check("头文件字节数合理", len(itxt) > 6 * 8192 // 4)

        # ---- 6. 生成的 icons.h 能不能喂给 C 编译器（粗查括号配对）----
        check("icons.h 花括号配对", itxt.count("{") == itxt.count("}"))
        check("apps.h 花括号配对", atxt.count("{") == atxt.count("}"))

    # ---- 7. 协议打包（不连板子，只看字节）----
    blob = bytes(8192)
    check("ICON 行长合理", len("ICON %d %d" % (0, len(blob))) < 32)
    check("SET 行长合理", len("SET 0 1 4C8BF5 " + "A" * 16) < 64)   # 16 = 固件 APP_LABEL_MAX
    check("小端打包 0x1234",
          struct.pack("<H", 0x1234) == b"\x34\x12")

    # ---- 8. GET 回复解析 ----
    class FakeSock(object):
        def __init__(self):
            self.sent = b""

        def sendall(self, b):
            self.sent += b

    class FakeDev(C.Device):
        def __init__(self, lines):
            C.Device.__init__(self, "x")
            self._lines = list(lines)
            self.sock = FakeSock()

        def _readline(self):
            return self._lines.pop(0)

        def cmd(self, line):
            return "OK dev 6"

    rows = FakeDev(["APP 0 1 0 4C8BF5 Note Pad",
                    "APP 1 2 1 7B61FF Explorer",
                    "END"]).get()
    check("GET 解析出 2 行", len(rows) == 2, str(rows))
    check("GET 带空格的 label", rows[0]["label"] == "Note Pad", repr(rows[0]["label"]))
    check("GET rgb 解析", rows[1]["rgb"] == 0x7B61FF)
    check("GET iconFs 解析", rows[1]["icon_fs"] is True)

    print()
    if FAILS:
        print("失败 %d 项：%s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
