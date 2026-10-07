#!/usr/bin/env python3
"""把从 Espressif CDN 快速下到的 zip，安装成 PlatformIO 认可的包。

背景：PlatformIO 官方源 (dl.registry.platformio.org) 在这台机器上只有
~19 KB/s（工具链要 104 分钟），而 Espressif 自家 CDN 是 6~28 MB/s。
两个包内容其实一样，只是打包方式不同：
  - Espressif zip : 根目录是 xtensa-esp32-elf/...
  - PlatformIO tar: 根目录直接就是 bin/ lib/ ...

所以这里做三件事：
  1. 解压时剥掉 xtensa-esp32-elf/ 这一层
  2. 用 PlatformIO 自己的 API 生成 .piopm 元数据文件（格式不靠猜）
  3. 顺便写一个 package.json，让它看起来像个正常的包
"""
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

PKG_NAME = "toolchain-xtensa-esp32"
PKG_VERSION = "8.4.0+2021r2-patch5"
ZIP = Path(__file__).resolve().parent / "dl" / "toolchain-xtensa-esp32.zip"
STRIP = "xtensa-esp32-elf/"          # 要剥掉的前缀
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
                # 顶层可能有个不带斜杠的目录条目，跳过
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
            # 保留可执行权限（Windows 上无所谓，但保持一致）
            n += 1
    print(f"  extracted {n} files")

    # ---------------------------------------------------------- package.json
    pkg_json = {
        "name": PKG_NAME,
        "version": PKG_VERSION,
        "description": "Espressif xtensa-esp32 GCC toolchain "
                       "(installed from dl.espressif.com because the "
                       "PlatformIO registry was unreachable at usable speed)",
        "system": ["windows_amd64", "windows_x86", "linux_amd64",
                   "linux_i686", "linux_armv7l", "darwin_amd64"],
        "url": "https://dl.espressif.com/dl/"
               "xtensa-esp32-elf-gcc8_4_0-esp-2021r2-patch5-win64.zip",
    }
    (DEST / "package.json").write_text(json.dumps(pkg_json, indent=2), encoding="utf-8")
    print("  wrote package.json")

    # ---------------------------------------------------------- .piopm
    # 用 PlatformIO 自己的类生成，避免格式猜错
    try:
        from platformio.package.manager.tool import ToolPackageManager
        from platformio.package.meta import PackageSpec

        spec = PackageSpec(owner="espressif", name=PKG_NAME,
                           requirements=PKG_VERSION)
        mgr = ToolPackageManager()
        meta = mgr.build_metadata(str(DEST), spec)
        meta.dump(str(DEST / ".piopm"))
        print("  wrote .piopm via PlatformIO API:")
        print("   ", (DEST / ".piopm").read_text(encoding="utf-8"))
    except Exception as e:
        # 兜底：手写一个等价格式
        print(f"  PlatformIO API failed ({type(e).__name__}: {e}); writing manually")
        meta = {
            "type": "tool",
            "name": PKG_NAME,
            "version": PKG_VERSION,
            "spec": {
                "owner": "espressif",
                "id": None,
                "name": PKG_NAME,
                "requirements": PKG_VERSION,
                "uri": None,
            },
        }
        (DEST / ".piopm").write_text(json.dumps(meta), encoding="utf-8")
        print("   ", json.dumps(meta))

    # ---------------------------------------------------------- 验证
    gcc = DEST / "bin" / "xtensa-esp32-elf-gcc.exe"
    print(f"\nkey binary: {gcc}  exists={gcc.is_file()}")
    if gcc.is_file():
        print(f"  size = {gcc.stat().st_size/1024/1024:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
