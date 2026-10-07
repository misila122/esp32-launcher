#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跑一遍「烧进固件（OTA）」那条路：重写 apps.h/icons.h + 真的 OTA 刷一次。

这是配置器里唯一没法在无窗口环境点按钮验证的路径，所以直接调 App.do_bake()，
把两个 messagebox 弹窗打成"自动点是"。

跑法： python _bake_test.py [ip]
注意：会真的重写 firmware/src/apps.h 和 icons.h，并真的 OTA 刷一次板子。
"""
import os
import sys
import time

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
import tkinter as tk                          # noqa: E402
import esp32_configurator as C                # noqa: E402

IP = sys.argv[1] if len(sys.argv) > 1 else "esp32-launcher.local"
FW = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   os.pardir, "firmware"))
SRC = os.path.join(FW, "src")

before = {n: (os.path.getsize(os.path.join(SRC, n)),
              os.path.getmtime(os.path.join(SRC, n)))
          for n in ("apps.h", "icons.h")}

# 两个弹窗都自动点「是」
C.messagebox.askyesno = lambda *a, **k: True
C.filedialog.askdirectory = lambda *a, **k: ""

root = tk.Tk()
root.withdraw()
app = C.App(root)
app.var_host.set(IP)

logs = []
app.log = lambda msg, color=None: logs.append(str(msg))

print("固件目录 =", app.fw_dir)
assert app.fw_dir and os.path.normcase(app.fw_dir) == os.path.normcase(FW), app.fw_dir

# 先从板子读一遍，保证界面上的就是板子当前的样子（这样重写出来的头文件应当等价）
app.do_read()
end = time.time() + 20
while time.time() < end and "读回 6 个卡片。" not in "\n".join(logs):
    root.update()
    time.sleep(0.05)
print("读板子：", logs[-1] if logs else "(无)")

logs.clear()
print("开始 do_bake()（大概一分钟，中间别断电）…")
app.do_bake()

# 一直泵事件循环，直到日志里出现结果
verdict = None
end = time.time() + 240
while time.time() < end:
    root.update()
    time.sleep(0.1)
    joined = "\n".join(logs)
    if "OTA 烧录完成" in joined or "烧录失败" in joined or "返回码" in joined:
        verdict = joined
        break
    if "OTA 成功" in joined or "烧录失败" in joined or "退出码" in joined:
        verdict = joined
        break

joined = "\n".join(logs)
tail = [l for l in logs if l.strip()][-8:]
print("---- 日志末尾 ----")
for l in tail:
    print("   ", l)

after = {n: (os.path.getsize(os.path.join(SRC, n)),
             os.path.getmtime(os.path.join(SRC, n)))
         for n in ("apps.h", "icons.h")}

bad = []
def ck(name, cond, extra=""):
    print("%-40s %s %s" % (name, "OK  " if cond else "FAIL", extra))
    if not cond:
        bad.append(name)

ck("apps.h 被重写了", after["apps.h"][1] != before["apps.h"][1],
   "%d -> %d 字节" % (before["apps.h"][0], after["apps.h"][0]))
ck("icons.h 被重写了", after["icons.h"][1] != before["icons.h"][1],
   "%d -> %d 字节" % (before["icons.h"][0], after["icons.h"][0]))

# 最要命的一条：六个格子都必须是真图标，不能有占位方块。
# （曾经踩过：界面还在后台抠图就点了「烧进固件」，icons.h 里塞进了占位方块，
#   烧进板子以后那一格变成了一个纯色圆角方块。）
import re                                              # noqa: E402

src_txt = open(os.path.join(SRC, "icons.h"), encoding="utf-8").read()
blanks = []
for n in range(C.SLOTS):
    m = re.search(r"static const uint16_t ICON_SLOT%d\[[^\]]*\]\s*=\s*\{(.*?)\};"
                  % n, src_txt, re.S)
    vals = [int(x, 16) for x in re.findall(r"0x([0-9A-Fa-f]{4})", m.group(1))]
    mid = {vals[y * 64 + x] for y in range(16, 48) for x in range(16, 48)}
    if len(mid) <= 3:
        blanks.append(n + 1)
    print("    卡片 %d: 中心 32x32 有 %d 种颜色%s"
          % (n + 1, len(mid), "  <-- 占位方块！" if len(mid) <= 3 else ""))
ck("icons.h 里六格都是真图标（没有占位方块）", not blanks,
   ("占位的是卡片 %s" % blanks) if blanks else "")

ck("OTA 报成功（Result: OK）", "Result: OK" in joined and "OTA 成功" in joined)
ck("日志里没有失败字样", "烧录失败" not in joined and "error" not in joined.lower())

root.destroy()
print()
if bad:
    print("失败 %d 项：%s" % (len(bad), "、".join(bad)))
    sys.exit(1)
print("「烧进固件（OTA）」路径通过。")
