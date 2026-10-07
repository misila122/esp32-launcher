#!/usr/bin/env python3
"""第二版测速：这次用正确的 URL 编码（version 里的 '+' 必须是 %2B）。"""
import json
import socket
import time
import urllib.request

socket.setdefaulttimeout(12)
UA = {"User-Agent": "speedtest/1.0"}


def timed_get(url, cap_bytes=6 * 1024 * 1024, cap_secs=15.0, label=""):
    t0 = time.time()
    got = 0
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=12) as r:
            total = r.headers.get("Content-Length")
            total = int(total) if total else 0
            while got < cap_bytes and time.time() - t0 < cap_secs:
                chunk = r.read(65536)
                if not chunk:
                    break
                got += len(chunk)
        dt = max(time.time() - t0, 1e-6)
        kbps = got / dt / 1024
        eta = ""
        if total and got:
            eta = f" | total {total/1048576:.1f} MB, ETA {total/(got/dt)/60:.1f} min"
        print(f"  {label:<22} {got/1048576:5.2f} MB in {dt:4.1f}s = {kbps:9.1f} KB/s{eta}")
        return kbps
    except Exception as e:
        print(f"  {label:<22} FAILED after {time.time()-t0:.1f}s: {type(e).__name__}: {e}")
        return 0.0


print("=== 1) registry API: exact framework version ===")
try:
    req = urllib.request.Request(
        "https://api.registry.platformio.org/v3/packages/platformio/framework/framework-arduinoespressif32",
        headers=UA)
    with urllib.request.urlopen(req, timeout=12) as r:
        d = json.load(r)
    vs = [v["name"] for v in d.get("versions", [])]
    print("  latest versions:", vs[:8])
    fw = next((v for v in vs if v.startswith("3.20017")), vs[0])
    print("  -> will test framework version:", fw)
except Exception as e:
    print("  API failed:", type(e).__name__, e)
    fw = "3.20017.241212"

print("\n=== 2) download speed ===")
BASE = "https://dl.registry.platformio.org/download"
timed_get(f"{BASE}/espressif/tool/toolchain-xtensa-esp32/8.4.0%2B2021r2-patch5/"
          f"toolchain-xtensa-esp32-windows_amd64-8.4.0+2021r2-patch5.tar.gz",
          label="registry toolchain")
timed_get(f"{BASE}/platformio/framework/framework-arduinoespressif32/{fw}/"
          f"framework-arduinoespressif32-{fw}.tar.gz",
          label="registry framework")
timed_get("https://dl.espressif.com/dl/xtensa-esp32-elf-gcc8_4_0-esp-2021r2-patch5-win64.zip",
          label="espressif toolchain")
