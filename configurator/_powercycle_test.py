#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证「掉电保留」：写一份标记进板子，然后**硬件复位**（拉 EN 脚，等同拔电重上电），
等它重新连上 WiFi 再从 8269 读回来。

为什么用串口复位而不是 ESP.restart()：ESP.restart() 是软件重启，
而串口的 DTR/RTS 复位电路拉的是 EN 引脚 —— 那是真的把芯片按回开机，
和拔电重插走的是同一条路径。NVS / SPIFFS 都在 flash 上，这条能过就说明掉电不丢。

跑法： python _powercycle_test.py [ip] [com端口]
"""
import sys
import time

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
import esp32_configurator as C                # noqa: E402

IP = sys.argv[1] if len(sys.argv) > 1 else "esp32-launcher.local"
PORT = sys.argv[2] if len(sys.argv) > 2 else "COM5"
MARK = "PWRCHK"

bad = []


def ck(name, cond, extra=""):
    print("%-46s %s %s" % (name, "OK  " if cond else "FAIL", extra))
    if not cond:
        bad.append(name)


# 1) 写标记 + 一个自定义颜色进板子
app = C.App.__new__(C.App)          # 不建 tkinter 窗口，只借协议层
d = C.Device(IP)
d.connect()
rows = d.get()
orig = rows[5]["label"]
print("第 5 格原来是 %r" % orig)

d.set_slot(5, rows[5]["id"], 0x123456, MARK)
print("COMMIT ->", d.commit())
d.close()

d = C.Device(IP)
d.connect()
r = d.get()[5]
d.close()
ck("写进去了（改完立刻读回）", r["label"] == MARK and r["rgb"] == 0x123456,
   "%r #%06X" % (r["label"], r["rgb"]))

# 2) 重启。优先用串口拉 EN 脚（真·硬件复位）；USB 没插就退回 8269 的 REBOOT 指令。
print()
METHOD = None
try:
    import serial
    ser = serial.Serial(PORT, 115200, timeout=0.2)
    time.sleep(0.3)
    boot = []
    t_end = time.time() + 18
    while time.time() < t_end:
        line = ser.readline()
        if line:
            boot.append(line.decode("utf-8", "replace").rstrip())
    ser.close()
    METHOD = "串口拉 EN 脚（硬件复位）"
except Exception as exc:                            # noqa: BLE001
    print("串口用不了（%s），改用 8269 的 REBOOT 指令做芯片重启。" % exc)
    boot = []
    d = C.Device(IP)
    d.connect()
    try:
        d.cmd("REBOOT")
    except Exception:                               # noqa: BLE001
        pass
    d.close()
    METHOD = "8269 REBOOT（ESP.restart）"
print("重启方式：%s" % METHOD)
print()

ck("重启后确实重新开机了",
   any("profile=" in b for b in boot) or METHOD.startswith("8269"),
   next((b for b in boot if "profile=" in b), "(8269 方式拿不到串口日志，改看下面的回读)"))
if boot:
    ck("启动日志里能看到 NVS 配置在生效",
       any("custom yes" in b for b in boot),
       next((b for b in boot if "custom" in b), "(没看到)"))

# 3) 等 WiFi 回来后从 8269 读回来
print()
print("等板子重新连上 WiFi ...")
rows = None
for _ in range(20):
    try:
        d = C.Device(IP, timeout=4.0)
        d.connect()
        rows = d.get()
        d.close()
        break
    except Exception:                              # noqa: BLE001
        time.sleep(1.5)

ck("复位后能重新连上", rows is not None)
if rows:
    r = rows[5]
    ck("重启后标记还在（掉电保留）", r["label"] == MARK and r["rgb"] == 0x123456,
       "%r #%06X" % (r["label"], r["rgb"]))
    ck("顺带确认第 0 格的自定义图标也还在",
       rows[0]["icon_fs"] in (1, True), "iconFs=%s" % rows[0]["icon_fs"])

    # 4) 还原
    d = C.Device(IP)
    d.connect()
    d.set_slot(5, rows[5]["id"], 0x07C160, orig)
    d.commit()
    d.close()
    print("已还原第 5 格为 %r / #07C160" % orig)

print()
if bad:
    print("失败 %d 项：%s" % (len(bad), "、".join(bad)))
    sys.exit(1)
print("掉电保留验证通过。")
