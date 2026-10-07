#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试：连续快速开关连接（就是当初触发 WinError 10054 的那个场景）。

跑法： python _bye_test.py [ip]
"""
import socket
import sys
import time

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
import esp32_configurator as C   # noqa: E402

IP = sys.argv[1] if len(sys.argv) > 1 else "esp32-launcher.local"
bad = []


def ck(name, cond, extra=""):
    print("%-42s %s %s" % (name, "OK  " if cond else "FAIL", extra))
    if not cond:
        bad.append(name)


def read_line(sock, limit=4.0):
    """读到第一个 \\n 为止（可能一次 recv 拿到半行，也可能一次拿到两行）。"""
    buf = b""
    end = time.time() + limit
    while time.time() < end:
        try:
            chunk = sock.recv(200)
        except socket.timeout:
            continue
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        if b"\n" in buf:
            break
    return buf.decode("utf-8", "replace").strip()


def raw_bye():
    """裸 socket 发 HELLO 再发 BYE，看板子有没有回 OK bye。

    注意：这是**不带重试**的裸连接。前面刚有别的测试连着板子时，
    第一枪有可能撞上"旧连接还没被板子清掉"的窗口（GUI 里靠 connect() 的
    3 次重试兜住了），所以这里允许最多 3 次。
    """
    last = ("", "")
    for attempt in range(3):
        try:
            s = socket.create_connection((IP, C.CFG_PORT), timeout=4)
            s.settimeout(2.0)
            s.sendall(b"HELLO " + C.TOKEN.encode() + b"\n")
            hello = read_line(s)
            s.sendall(b"BYE\n")
            bye = read_line(s)
            s.close()
            last = (hello, bye)
            if bye == "OK bye":
                return hello, bye
        except OSError as exc:
            last = (last[0], "OSError: %s" % exc)
        time.sleep(0.5)
    return last


h, b = raw_bye()
ck("HELLO 回 OK", h.startswith("OK"), h)
ck("BYE 回 OK bye", b == "OK bye", b)

# 连续 8 次：每次都是"连上 -> 读一遍 -> 关"。这正是以前会偶发 10054 的模式。
ok = 0
err = None
for i in range(8):
    try:
        d = C.Device(IP, timeout=5.0)
        d.connect()
        rows = d.get()
        d.close()
        assert len(rows) == C.SLOTS, rows
        ok += 1
    except Exception as exc:                       # noqa: BLE001
        err = "第 %d 次：%r" % (i + 1, exc)
        break
ck("连续 8 次 连-读-关 全部成功", ok == 8, err or ("%d/8" % ok))

# 再来一轮更狠的：只握手就关，不读任何东西
ok2 = 0
err2 = None
for i in range(8):
    try:
        d = C.Device(IP, timeout=5.0)
        d.connect()
        d.close()
        ok2 += 1
    except Exception as exc:                       # noqa: BLE001
        err2 = "第 %d 次：%r" % (i + 1, exc)
        break
ck("连续 8 次 只握手就关 全部成功", ok2 == 8, err2 or ("%d/8" % ok2))

# 空闲超时兜底：连上不说话，等 33 秒，之后必须还能连上
s = socket.create_connection((IP, C.CFG_PORT), timeout=5)
print("占着连接不说话，等 33 秒看板子会不会踢人 ...")
time.sleep(33)
try:
    s.settimeout(2)
    leftover = s.recv(200)
except OSError:
    leftover = b""
s.close()
print("   被踢掉时收到：%r" % leftover)
ck("空闲超时后能立刻重新连上", (lambda: (
    (lambda d: (d.connect(), d.close(), True)[-1])(C.Device(IP, timeout=6.0))
))())

print()
if bad:
    print("失败 %d 项：%s" % (len(bad), "、".join(bad)))
    sys.exit(1)
print("连接稳健性测试全通过：BYE 有效、连开连关不炸、空闲能被踢掉。")
