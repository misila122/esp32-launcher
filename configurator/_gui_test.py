#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""不开窗口点按钮，直接驱动 GUI 的代码路径：读取板子 -> 改一格的显示 -> 应用。

比合成鼠标点击可靠得多（tkinter 对 UIA / SendInput 的响应很不稳定），
测的还是同一批函数：do_read / do_apply / UiQ 回调。

跑法： python _gui_test.py [ip]        默认 esp32-launcher.local
注意：会往板子上写一次"和现在完全一样"的配置（外加一次 COMMIT），不会改变外观。
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tkinter as tk                     # noqa: E402
import esp32_configurator as C           # noqa: E402

IP = sys.argv[1] if len(sys.argv) > 1 else "esp32-launcher.local"
bad = []


def ck(name, cond, extra=""):
    print("%-46s %s %s" % (name, "OK  " if cond else "FAIL", extra))
    if not cond:
        bad.append(name)


def pump(root, seconds=20.0, until=None):
    """跑 Tk 的事件循环，直到 until() 为真或超时。"""
    t0 = time.time()
    while time.time() - t0 < seconds:
        root.update()
        if until and until():
            return True
        time.sleep(0.05)
    return until() if until else True


def main():
    root = tk.Tk()
    root.withdraw()
    app = C.App(root)

    app.var_host.set(IP)
    app.log_lines = []
    _orig_log = app.log

    def spy(msg, color=None):
        app.log_lines.append(msg)
        _orig_log(msg, color)

    app.log = spy

    # ---- 1. 读取板子 ------------------------------------------------------
    app.do_read()
    ok = pump(root, 20.0, lambda: any("读取" in s and ("完成" in s or "成功" in s)
                                      or "读到" in s for s in app.log_lines))
    joined = "\n".join(app.log_lines)
    print("读取板子的日志：")
    for s in app.log_lines:
        print("   ", s)
    ck("do_read 跑完没报错", "失败" not in joined and "出错" not in joined)
    labels = [c["_w"].var_label.get() for c in app.cards]
    rgbs = [C.parse_color(c["_w"].var_hex.get(), 0) for c in app.cards]
    ck("六张卡的名字都填上了", all(labels), str(labels))
    ck("六张卡的颜色都填上了", all(rgbs), " ".join("%06X" % r for r in rgbs))
    ck("第一张卡有名字", bool(labels[0]), labels[0])

    # ---- 2. 改一格的显示（名字后面加个点），然后"应用" -------------------
    w0 = app.cards[0]["_w"]
    orig = w0.var_label.get()
    w0.var_label.set(orig + ".")
    app.do_apply()
    pump(root, 20.0, lambda: any("已写入板子" in s or "失败" in s or "出错" in s
                                 for s in app.log_lines))
    print("应用之后的日志：")
    for s in app.log_lines[len(app.log_lines) - 6:]:
        print("   ", s)
    ck("do_apply 没报错", "失败" not in app.log_lines[-1]
       and "出错" not in app.log_lines[-1], app.log_lines[-1])

    # ---- 3. 直接问板子，确认真的写进去了 ---------------------------------
    dev = C.Device(IP)
    dev.connect()
    rows = dev.get()
    dev.close()
    ck("板子上第 0 格变成了 %r" % (orig + "."),
       rows[0]["label"] == orig + ".", repr(rows[0]["label"]))

    # ---- 4. 改回去，别给用户留垃圾 ---------------------------------------
    w0.var_label.set(orig)
    app.do_apply()
    pump(root, 20.0, lambda: True)
    dev = C.Device(IP)
    dev.connect()
    rows = dev.get()
    dev.close()
    ck("改回 %r 成功" % orig, rows[0]["label"] == orig, repr(rows[0]["label"]))

    root.destroy()
    print()
    if bad:
        print("失败 %d 项：%s" % (len(bad), "、".join(bad)))
        return 1
    print("GUI 代码路径测试通过：读取 / 应用 / 回读 都对得上。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
