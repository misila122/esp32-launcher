#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开一个真的窗口、连板子读一遍、然后停在界面上不退出（给截图用）。

跑法： python _gui_shot.py [ip]
关窗口即退出。
"""
import sys
import time

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
import tkinter as tk                          # noqa: E402
import esp32_configurator as C                # noqa: E402

IP = sys.argv[1] if len(sys.argv) > 1 else "esp32-launcher.local"

root = tk.Tk()
app = C.App(root)
app.var_host.set(IP)


def pump(seconds):
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.05)


root.after(300, app.do_read)
pump(8)
print("读板子完成，窗口留着给截图用。")
root.mainloop()
