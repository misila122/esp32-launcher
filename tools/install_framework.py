#!/usr/bin/env python3
"""把 Espressif 官方的 arduino-esp32 发布包，安装成 PlatformIO 的
framework-arduinoespressif32 包。

为什么能这么干（已用 remote_zip_ls.py 远程查过 ZIP 目录验证）：
  esp32-2.0.17.zip 里自带
      esp32-2.0.17/tools/platformio-build.py      <-- 正是
      esp32-2.0.17/tools/platformio-build-esp32.py    platform-espressif32
      esp32-2.0.17/tools/sdk/esp32/...                /builder/frameworks
      esp32-2.0.17/package.json                       /arduino.py:37 要
      esp32-2.0.17/cores/esp32|variants|libraries     的那个文件
  也就是说官方发布包 == PlatformIO 的 framework 包，只差一层目录前缀。
  而官方 CDN (dl.espressif.com) 实测 ~28 MB/s，PlatformIO 官方源只有
  ~40 KB/s（234.91 MB 要 50+ 分钟），所以走这条路。

版本对应关系：Arduino core 2.0.17 == PlatformIO framework 3.20017.x
（platform.json 里的约束是 ~3.20017.0）。
"""
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

PKG_NAME = "framework-arduinoespressif32"
PKG_VERSION = "3.20017.241212"          # 满足 ~3.20017.0
ZIP = Path(__file__).resolve().parent / "dl" / "framework-arduinoespressif32.zip"
STRIP = "esp32-2.0.17/"                  # zip 里的顶层目录
DEST = Path(os.path.expanduser("~")) / ".platformio" / "packages" / PKG_NAME


def main():
    if not ZIP.is_file():
        sys.exit(f"missing {ZIP}")

    if DEST.exists():
        print(f"removing existing {DEST}")
        shutil.rmtree(DEST, ignore_errors=True)
    DEST.mkdir(parents=True, exist_ok=True)

    print(f"extracting {ZIP.name} -> {DEST}")
    n = 0
    with zipfile.ZipFile(ZIP) as z:
        for info in z.infolist():
            name = info.filename
            if not name.startswith(STRIP):
                continue
            rel = name[len(STRIP):]
            if not rel:
                continue
            target = DEST / rel
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            n += 1
    print(f"  extracted {n} files")

    # ---------------------------------------------------------- package.json
    pkg_json = {
        "name": PKG_NAME,
        "version": PKG_VERSION,
        "description": "Arduino Wiring-based Framework for ESP32 "
                       "(installed from Espressif's official arduino-esp32 "
                       "release because the PlatformIO registry was "
                       "unreachable at usable speed)",
        "system": ["windows_amd64", "windows_x86", "linux_amd64",
                   "linux_i686", "linux_armv7l", "darwin_amd64"],
        "url": "https://dl.espressif.com/github_assets/espressif/"
               "arduino-esp32/releases/download/2.0.17/esp32-2.0.17.zip",
    }
    (DEST / "package.json").write_text(json.dumps(pkg_json, indent=2),
                                       encoding="utf-8")
    print("  wrote package.json")

    # ---------------------------------------------------------- .piopm
    try:
        from platformio.package.manager.framework import FrameworkPackageManager
        from platformio.package.meta import PackageSpec

        spec = PackageSpec(owner="platformio", name=PKG_NAME,
                           requirements=PKG_VERSION)
        mgr = FrameworkPackageManager()
        meta = mgr.build_metadata(str(DEST), spec)
        meta.dump(str(DEST / ".piopm"))
        print("  wrote .piopm via PlatformIO API:")
    except Exception as e:
        print(f"  PlatformIO API failed ({type(e).__name__}: {e}); "
              f"writing manually")
        meta = {
            "type": "framework",
            "name": PKG_NAME,
            "version": PKG_VERSION,
            "spec": {
                "owner": "platformio",
                "id": None,
                "name": PKG_NAME,
                "requirements": PKG_VERSION,
                "uri": None,
            },
        }
        (DEST / ".piopm").write_text(json.dumps(meta), encoding="utf-8")
    print("   ", (DEST / ".piopm").read_text(encoding="utf-8"))

    # ---------------------------------------------------------- 验证
    checks = [
        DEST / "tools" / "platformio-build.py",
        DEST / "cores" / "esp32" / "Arduino.h",
        DEST / "variants" / "esp32" / "pins_arduino.h",
        DEST / "tools" / "sdk" / "esp32" / "sdkconfig",
        DEST / "libraries" / "Wire" / "src" / "Wire.cpp",
    ]
    print("\nverification:")
    ok = True
    for c in checks:
        e = c.is_file()
        ok = ok and e
        print(f"  {'OK  ' if e else 'MISS'} {c.relative_to(DEST)}")
    libs = DEST / "tools" / "sdk" / "esp32" / "lib"
    if libs.is_dir():
        print(f"  sdk libs: {len(list(libs.glob('*.a')))} .a files")
    print("\nRESULT:", "OK" if ok else "INCOMPLETE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
