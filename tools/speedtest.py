#!/usr/bin/env python3
"""测速：找出哪个源能真正下动 ESP32 工具链。
每个源最多读 6MB / 12 秒，报告实际吞吐。"""
import socket
import time
import urllib.request

URLS = [
    ("platformio-registry",
     "https://dl.registry.platformio.org/download/platformio/tool/toolchain-xtensa-esp32/"
     "8.4.0+2021r2-patch5/toolchain-xtensa-esp32-windows_amd64-8.4.0+2021r2-patch5.tar.gz"),
    ("espressif-cdn",
     "https://dl.espressif.com/dl/xtensa-esp32-elf-gcc8_4_0-esp-2021r2-patch5-win64.zip"),
    ("github-release",
     "https://github.com/espressif/crosstool-NG/releases/download/esp-2021r2-patch5/"
     "xtensa-esp32-elf-gcc8_4_0-esp-2021r2-patch5-win64.zip"),
    ("tuna-mirror",
     "https://mirrors.tuna.tsinghua.edu.cn/github-release/espressif/crosstool-NG/"
     "esp-2021r2-patch5/xtensa-esp32-elf-gcc8_4_0-esp-2021r2-patch5-win64.zip"),
    ("ustc-mirror",
     "https://mirrors.ustc.edu.cn/github-release/espressif/crosstool-NG/"
     "esp-2021r2-patch5/xtensa-esp32-elf-gcc8_4_0-esp-2021r2-patch5-win64.zip"),
    ("arduino-framework-pio",
     "https://dl.registry.platformio.org/download/platformio/framework/"
     "framework-arduinoespressif32/3.20017.241212/framework-arduinoespressif32-3.20017.241212.tar.gz"),
]

CAP_BYTES = 6 * 1024 * 1024
CAP_SECS = 12.0

socket.setdefaulttimeout(10)

for name, url in URLS:
    t0 = time.time()
    got = 0
    note = ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "speedtest/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            total = r.headers.get("Content-Length")
            total = int(total) if total else 0
            while got < CAP_BYTES and time.time() - t0 < CAP_SECS:
                chunk = r.read(65536)
                if not chunk:
                    break
                got += len(chunk)
        dt = time.time() - t0
        kbps = got / dt / 1024 if dt > 0 else 0
        if total:
            note = f" / total {total/1024/1024:.1f} MB, ETA {total/(got/dt)/60:.1f} min" if got and dt else ""
        print(f"{name:<24} {got/1024/1024:5.2f} MB in {dt:5.1f}s = {kbps:8.1f} KB/s{note}")
    except Exception as e:
        print(f"{name:<24} FAILED after {time.time()-t0:.1f}s: {type(e).__name__}: {e}")
