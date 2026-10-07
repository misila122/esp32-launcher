#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证"扫码配网"里最要紧也最容易出错的那一步：保存账号并真的用上它。

为什么必须单独测：二维码画得对、网页能打开，都不代表"存进去的账号"是对的 ——
表单字段名写错、NVS 键名不一致、重启后没读回来，任何一处出问题都表现为
"配网好像成功了，但板子还是连不上"，而这一整条路都发生在屏幕上和手机里，我看不见。

做法（用的是板子自己现在就在用的那套账号，所以哪怕逻辑写错，结果也应该一样）：
  1. 从 firmware/src/secrets.h 里读出当前账号（**不回显密码**）；
  2. GET http://<ip>/            确认页面和网络列表都正常；
  3. GET http://<ip>/save?s=..&p=..  板子存 NVS 后会自己重启；
  4. 等它起来，确认还能连上、而且 8269 报的是 cred=nvs（说明真的读的是 NVS 那份）。

用法：
    python _prov_test.py                 # 默认 esp32-launcher.local
"""

import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request

HOST_DEFAULT = "esp32-launcher.local"
PORT = 8269
TOKEN = "esp32launcher"
SECRETS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "..", "firmware", "src", "secrets.h")


def read_secrets():
    """从 secrets.h 里抠出 WIFI_SSID / WIFI_PASSWORD。"""
    with open(SECRETS, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    def grab(name):
        m = re.search(r'^\s*#define\s+%s\s+"([^"]*)"' % name, text, re.M)
        return m.group(1) if m else None

    return grab("WIFI_SSID"), grab("WIFI_PASSWORD")


def http_get(url, timeout=20):
    """返回 (状态码, 正文)。板子在 /save 之后会重启，连接被掐断也算成功。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as exc:                       # noqa: BLE001 - 重启时各种断法都要接住
        return None, "%s: %s" % (type(exc).__name__, exc)


def prov(host, cmd="PROV", wait=12.0):
    """问板子 8269 要一次回话，返回所有行。

    结束条件有两种：多行指令以 END 收尾，而 PING 只回一行 PONG ——
    早先只认 END，结果 PING 每次都卡到 socket 超时，wait_alive 永远等不到板子。
    """
    s = socket.create_connection((host, PORT), timeout=wait)
    try:
        f = s.makefile("rwb")
        f.write(b"HELLO " + TOKEN.encode() + b"\n")
        f.flush()
        f.readline()
        f.write(cmd.encode() + b"\n")
        f.flush()
        lines = []
        while True:
            ln = f.readline()
            if not ln:
                break
            t = ln.decode("utf-8", "replace").rstrip()
            lines.append(t)
            if t == "END" or t == "PONG" or t == "OK rebooting":
                break
        try:
            f.write(b"BYE\n")
            f.flush()
            f.readline()
        except OSError:
            pass
        return lines
    finally:
        s.close()


def wait_alive(host, seconds=75):
    """等板子重启完、8269 又能握手。返回耗时，失败返回 None。"""
    t0 = time.time()
    while time.time() - t0 < seconds:
        time.sleep(2.0)
        try:
            lines = prov(host, "PING", wait=6.0)
            if any("PONG" in x for x in lines):
                return time.time() - t0
        except OSError:
            pass
    return None


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else HOST_DEFAULT
    ssid, pw = read_secrets()
    if not ssid or not pw:
        print("读不到 secrets.h 里的账号：%s" % SECRETS)
        return 2
    print("secrets.h 里的账号：ssid=%s  pass=%s（%d 位，不回显）"
          % (ssid, "*" * len(pw), len(pw)))

    # 必须先重启一次：板子连上 WiFi 30 秒后会把热点和配网页一起关掉
    # （provPoll -> provStop），关掉之后再测就没得测了。刚开机的这半分钟里
    # 热点一定是开着的，那才是用户真正会去扫码的窗口。
    print("\n== -1. 先重启板子，抢在热点关闭前测 ==")
    try:
        prov(host, "REBOOT", wait=8.0)
    except OSError as exc:
        print("  发不了 REBOOT（%s），那就用现在这一刻的状态继续。" % exc)
    took = wait_alive(host)
    if took is None:
        print("  板子没回来，放弃。")
        return 2
    print("  起来了（%.0f 秒），热点这会儿是开的" % took)

    print("\n== 0. 先看看板子现在用的是什么 ==")
    try:
        before = prov(host, "PROV")
    except OSError as exc:
        print("连不上 %s:%d —— %s" % (host, PORT, exc))
        return 2
    for ln in before:
        print("  %s" % ln)
    cred_before = "nvs" if any("cred=nvs" in x for x in before) else "secrets"
    print("  重启前用的是：%s" % cred_before)

    print("\n== 1. 打开配网页 ==")
    code, body = http_get("http://%s/" % host)
    print("  GET /  -> %s, %d 字节" % (code, len(body)))
    if code != 200:
        print("  页面打不开，后面不用测了：%s" % body[:200])
        return 1
    if "<select name=s>" not in body:
        print("  页面里没有网络下拉框，配网表单可能坏了。")
        return 1
    print("  网络下拉框在，当前网络 %s 排在第 %d 位"
          % (ssid, body[:body.find("</select>")].count("<option")))

    print("\n== 2. 提交账号（板子会存 NVS 然后自己重启）==")
    url = "http://%s/save?s=%s&p=%s" % (
        host, urllib.request.quote(ssid), urllib.request.quote(pw))
    code, body = http_get(url, timeout=30)
    print("  GET /save -> %s" % code)
    if code is None:
        # 板子回完页面才重启，正常应该能拿到 200；拿不到也算它开始重启了
        print("  （连接被掐断，符合马上要重启的表现）")
    elif "已保存" in body or "重启" in body:
        print("  页面回了『已保存』，前 80 字：%s" % body[:80].replace("\n", " "))
    else:
        print("  回的不是预期页面，前 200 字：%s" % body[:200])

    print("\n== 3. 等板子重启回来 ==")
    took = wait_alive(host)
    if took is None:
        print("  75 秒内没回来。")
        print("  板子这时候应该还开着热点（它没连上过 WiFi，热点不会关），")
        print("  用手机扫屏幕上的二维码重新配一次网就能救回来。")
        return 1
    print("  回来了，耗时 %.0f 秒" % took)

    print("\n== 4. 确认它用的是 NVS 里那份账号 ==")
    after = prov(host, "PROV")
    for ln in after:
        print("  %s" % ln)
    cred_after = "nvs" if any("cred=nvs" in x for x in after) else "secrets"
    ap_line = [x for x in after if x.startswith("PROV ")]
    sta_ip_ok = True  # STA 的 IP 由 DHCP 决定，这里不断言具体地址

    print("\n== 结论 ==")
    if cred_after == "nvs" and took is not None:
        print("  通过：账号存进 NVS 了，重启后确实读的是 NVS 那份，而且连上了同一个网络。")
        print("  （提交前后用的是同一套账号，所以上网行为不变。）")
        if cred_before == "secrets":
            print("  注意：板子从此走 NVS 那份账号；想退回 secrets.h，串口发 'w clear' 再 'z'。")
        return 0
    print("  失败：重启后 cred=%s（期望 nvs）。上面那行 PROV 是现场状态。" % cred_after)
    return 1


if __name__ == "__main__":
    sys.exit(main())
