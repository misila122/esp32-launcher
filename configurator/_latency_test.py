#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证「应用」这条路的两个硬指标：**真的写进板子** + **到底花了多久**。

顺带验证「没找到固件目录时只能导出」的降级路径。

跑法： python _latency_test.py [ip]
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
import tkinter as tk                          # noqa: E402
import esp32_configurator as C                # noqa: E402

IP = sys.argv[1] if len(sys.argv) > 1 else "esp32-launcher.local"
bad = []


def ck(name, cond, extra=""):
    print("%-44s %s %s" % (name, "OK  " if cond else "FAIL", extra))
    if not cond:
        bad.append(name)


# ---------------------------------------------------------------- 降级路径
# 把 _search_roots 指到一个空目录，find_firmware_dir / find_apps_json 就该找不到东西
print("== 1. 找不到固件目录时的降级路径 ==")
save = C._search_roots
C._search_roots = lambda: [tempfile.mkdtemp(prefix="cfg-empty-")]
try:
    ck("find_firmware_dir 返回 None", C.find_firmware_dir() is None,
       repr(C.find_firmware_dir()))
    ck("find_apps_json 返回 None", C.find_apps_json() is None,
       repr(C.find_apps_json()))
    # do_export 只能走 askdirectory；这里直接验底层两个 writer 还能用
    d = tempfile.mkdtemp(prefix="cfg-export-")
    cards = [{"idx": i, "id": i + 1, "label": "T%d" % i, "glyph": i,
              "rgb": (0x11 * (i + 1)) & 0xFFFFFF,
              "icon_rgba": C.placeholder_rgba((0x11 * (i + 1)) & 0xFFFFFF)}
             for i in range(C.SLOTS)]
    C.write_apps_h(os.path.join(d, "apps.h"), cards)
    C.write_icons_h(os.path.join(d, "icons.h"), cards)
    ck("照样能导出 apps.h", os.path.getsize(os.path.join(d, "apps.h")) > 300,
       "%d 字节" % os.path.getsize(os.path.join(d, "apps.h")))
    ck("照样能导出 icons.h", os.path.getsize(os.path.join(d, "icons.h")) > 150000,
       "%d 字节" % os.path.getsize(os.path.join(d, "icons.h")))
finally:
    C._search_roots = save

# ---------------------------------------------------------------- 应用延迟
print()
print("== 2. 「应用」的延迟（只改名字，不换图标）==")
root = tk.Tk()
root.withdraw()
app = C.App(root)
app.var_host.set(IP)
logs = []
app.log = lambda msg, color=None: logs.append(str(msg))


def pump(until, limit=30.0):
    end = time.time() + limit
    while time.time() < end:
        root.update()
        time.sleep(0.02)
        if until("\n".join(logs)):
            return True
    return False


app.do_read()
pump(lambda s: "读回 6 个卡片。" in s)
ck("先读到板子", "读回 6 个卡片。" in "\n".join(logs))

card0 = app.cards[0]["_w"]
orig = card0.var_label.get()

# 改成一个临时名字，测「从调 do_apply 到板子上真的变」要多久
card0.var_label.set("TMPX")
logs.clear()
t0 = time.time()
app.do_apply()
pump(lambda s: "已同步" in s or "没找到" in s or "板子已生效" in s, limit=40)
applied = time.time() - t0
ck("apply 走完", "板子已生效" in "\n".join(logs), "%.2f 秒" % applied)

# 另开一条连接直读板子，验证真的写进去了
t1 = time.time()
d = C.Device(IP)
d.connect()
rows = d.get()
d.close()
readback = time.time() - t1
ck("板子上第 0 格真的变成了 TMPX", rows[0]["label"] == "TMPX", repr(rows[0]["label"]))
print("    （回读本身花了 %.2f 秒）" % readback)

# 改回去
card0.var_label.set(orig)
logs.clear()
t2 = time.time()
app.do_apply()
pump(lambda s: "板子已生效" in s, limit=40)
ck("改回 %s" % orig, True, "%.2f 秒" % (time.time() - t2))
d = C.Device(IP)
d.connect()
rows = d.get()
d.close()
ck("板子上确实是 %s" % orig, rows[0]["label"] == orig, repr(rows[0]["label"]))

root.destroy()
print()
if bad:
    print("失败 %d 项：%s" % (len(bad), "、".join(bad)))
    sys.exit(1)
print("降级路径 + 应用延迟都通过。")
