#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对真板子跑一遍 8269 配置协议（HELLO / GET / SET / ICON / COMMIT）。

跑法： python _e2e.py [ip]       默认 esp32-launcher.local
只做"写回同样的值 + 真的推一张图标"，不改用户的选择，可反复跑。

注意：推给第 0 格的图标是**从第 0 格自己那个程序里抠的**（按 id 去 pc/apps.json 里找），
所以这个测试是幂等的。早期版本这里写死过一个 exe 路径，跑一次就把那个程序的图标
塞进了第 0 格 —— 板子上那格的图标就再也不对了。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import esp32_configurator as C   # noqa: E402

IP = sys.argv[1] if len(sys.argv) > 1 else "esp32-launcher.local"


def exe_for_id(app_id):
    """去 pc/apps.json 里按 id 找这个程序真正的 exe 路径。"""
    p = C.find_apps_json()
    if not p:
        return None
    try:
        data = C.load_apps_json(p)
    except Exception:                                     # noqa: BLE001
        return None
    for a in (data or {}).get("apps", []):
        if int(a.get("id", -1)) == int(app_id):
            path = a.get("path") or ""
            return path if os.path.exists(path) else None
    return None


def main():
    dev = C.Device(IP)
    print("连接 %s:%d ..." % (IP, C.CFG_PORT))
    hello = dev.connect()
    print("  HELLO -> %s" % (hello,))

    rows = dev.get()
    print("  GET -> %d 格" % len(rows))
    for r in rows:
        print("    [%d] id=%-3d rgb=%06X iconFs=%d  %r"
              % (r["idx"], r["id"], r["rgb"], int(r["icon_fs"]), r["label"]))
    if not rows:
        print("板子没回任何卡片，中止")
        return 1

    # 1) SET：写回第 0 格原值（不改用户的东西），验证 SET/OK
    r0 = rows[0]
    dev.set_slot(r0["idx"], r0["id"], r0["rgb"], r0["label"])
    print("  SET 回写 [0] -> OK")

    # 2) ICON：真推一张 8192 字节的图标，验证裸字节分支。
    #    用第 0 格自己的 exe，这样跑完板子还是对的（幂等）。
    exe = exe_for_id(r0["id"])
    if exe:
        _rgba, blob, note = C.icon_from_exe(exe)
        print("  抠图标：%s -> %s（%d 字节）" % (os.path.basename(exe), note, len(blob)))
        dev.push_icon(r0["idx"], blob)
        print("  ICON [0] %d 字节 -> OK icon" % len(blob))
    else:
        print("  跳过 ICON：pc/apps.json 里找不到 id=%d 的程序" % r0["id"])

    # 3) COMMIT：落 NVS + 重画
    dev.commit()
    print("  COMMIT -> OK saved")

    # 4) 回读校验
    rows2 = dev.get()
    r2 = rows2[0]
    ok = (r2["id"] == r0["id"] and r2["rgb"] == r0["rgb"]
          and r2["label"] == r0["label"] and r2["icon_fs"])
    print("  GET 回读 [0]: id=%d rgb=%06X iconFs=%d %r"
          % (r2["id"], r2["rgb"], int(r2["icon_fs"]), r2["label"]))
    dev.close()

    print()
    if not ok:
        print("回读与写入不一致！")
        return 1
    print("端到端通过：SET / ICON / COMMIT / GET 全通。")

    # 5) 可选：重启板子，验证 NVS + SPIFFS 真的持久化了
    if "--reboot" in sys.argv:
        import time
        d2 = C.Device(IP)
        d2.connect()
        try:
            d2.cmd("REBOOT")
        except Exception:
            pass          # 板子会直接把连接掐断，属正常
        d2.close()
        print("\n已发 REBOOT，等板子重启（WiFi 大概 10 秒）...")
        time.sleep(16)
        for attempt in range(6):
            try:
                d3 = C.Device(IP)
                d3.connect()
                r3 = d3.get()[0]
                d3.close()
                same = (r3["id"] == r0["id"] and r3["rgb"] == r0["rgb"]
                        and r3["label"] == r0["label"] and r3["icon_fs"])
                print("重启后回读 [0]: id=%d rgb=%06X iconFs=%d %r"
                      % (r3["id"], r3["rgb"], int(r3["icon_fs"]), r3["label"]))
                print("持久化 %s" % ("通过（NVS + SPIFFS 都活过重启）" if same
                                    else "失败：重启后值变了"))
                return 0 if same else 1
            except Exception as e:
                print("  第 %d 次重连失败：%s" % (attempt + 1, e))
                time.sleep(5)
        print("重启后一直连不上")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
