#!/usr/bin/env python3
"""串口抓日志 + 定时发指令（用来在没人手点屏幕时验证固件）。

用法示例：
    # 抓 25 秒启动日志，第 18 / 20 秒各发一条指令
    python serial_cli.py --seconds 25 --at 18:"s" --at 20:"l 1"

注意：打开 COM5 会通过 DTR/RTS 复位板子，这是正常的（每次都会重启）。
"""
import argparse
import sys
import threading
import time

import serial


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM5")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--at", action="append", default=[],
                    help="秒数:指令，可重复，例如 18:s")
    ap.add_argument("--no-reset", action="store_true",
                    help="不通过 DTR/RTS 复位板子")
    args = ap.parse_args()

    sends = []
    for item in args.at:
        sec, _, cmd = item.partition(":")
        sends.append((float(sec), cmd))
    sends.sort()

    ser = serial.Serial()
    ser.port = args.port
    ser.baudrate = args.baud
    ser.timeout = 0.2
    if args.no_reset:
        ser.dtr = False
        ser.rts = False
    ser.open()

    # 打开后主动复位一次，保证从 setup() 开始看
    if not args.no_reset:
        ser.dtr = False
        ser.rts = True
        time.sleep(0.15)
        ser.rts = False
        time.sleep(0.05)

    t0 = time.time()
    sent = set()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    buf = b""
    try:
        while time.time() - t0 < args.seconds:
            el = time.time() - t0
            for i, (sec, cmd) in enumerate(sends):
                if i not in sent and el >= sec:
                    sent.add(i)
                    print(f"\n>>> [{el:5.2f}s] SEND: {cmd!r}", flush=True)
                    ser.write((cmd + "\n").encode())
                    ser.flush()
            chunk = ser.read(4096)
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                text = line.decode("utf-8", "replace").rstrip("\r")
                print(f"[{time.time()-t0:5.2f}s] {text}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
