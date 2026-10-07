#!/usr/bin/env python3
"""通用「把下载好的 tar.gz 装成 PlatformIO 包」脚本。

背景：PlatformIO 官方源在这台机器上只有 ~10-96 KB/s，所以凡是能从别处
（Espressif CDN、或自己多连接并行抓到本地）拿到的包，都用这个脚本离线安装，
避免 pio 自己去慢慢下。

用法：
    python install_pkg.py <tarball> <type> <name> <version> [owner] [destdir]

    type    : tool | framework | library | platform
    destdir : 不给就按类型推断
              tool/framework/platform -> ~/.platformio/packages|frameworks|platforms/<name>
              library                 -> 必须显式给（项目的 .pio/libdeps/<env>/<name>）

.tar.gz 里如果所有条目都在同一个顶层目录下，会自动剥掉那一层
（PlatformIO 的包都是直接铺开的，但保险起见做这个判断）。
"""
import json
import os
import shutil
import sys
import tarfile
from pathlib import Path

HOME_PIO = Path(os.path.expanduser("~")) / ".platformio"


def common_prefix(names):
    tops = {n.split("/", 1)[0] for n in names if n and not n.startswith("./")}
    if len(tops) != 1:
        return ""
    top = tops.pop()
    # 只有当一个名叫 top/ 的目录条目存在时才认为它是目录前缀
    if any(n == top + "/" for n in names):
        return top + "/"
    return ""


def default_dest(typex, name):
    sub = {"tool": "packages", "framework": "packages",
           "platform": "platforms"}.get(typex)
    if not sub:
        sys.exit(f"type {typex} needs an explicit destdir")
    return HOME_PIO / sub / name


def main():
    if len(sys.argv) < 5:
        sys.exit(__doc__)
    tarball = Path(sys.argv[1])
    typex = sys.argv[2]
    name = sys.argv[3]
    version = sys.argv[4]
    owner = sys.argv[5] if len(sys.argv) > 5 else None
    dest = Path(sys.argv[6]) if len(sys.argv) > 6 else default_dest(typex, name)

    if not tarball.is_file():
        sys.exit(f"missing {tarball}")

    if dest.exists():
        print(f"removing existing {dest}")
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)

    print(f"extracting {tarball.name} -> {dest}")
    with tarfile.open(tarball, "r:gz") as t:
        members = t.getmembers()
        strip = common_prefix([m.name for m in members])
        if strip:
            print(f"  stripping prefix: {strip}")
        n = 0
        for m in members:
            if strip:
                if not m.name.startswith(strip):
                    continue
                rel = m.name[len(strip):]
            else:
                rel = m.name
            if not rel or rel == "/":
                continue
            target = dest / rel
            # 防路径穿越
            if not str(target.resolve()).startswith(str(dest.resolve())):
                continue
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not m.isfile():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = t.extractfile(m)
            if src is None:
                continue
            with src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            n += 1
    print(f"  extracted {n} files")

    # ------------------------------------------------------------- .piopm
    meta = {
        "type": typex,
        "name": name,
        "version": version,
        "spec": {
            "owner": owner,
            "id": None,
            "name": name,
            "requirements": version,
            "uri": None,
        },
    }
    try:
        if typex == "library":
            from platformio.package.manager.library import LibraryPackageManager
            from platformio.package.meta import PackageSpec

            spec = PackageSpec(owner=owner, name=name, requirements=version)
            mgr = LibraryPackageManager(dest.parent)
            meta = mgr.build_metadata(str(dest), spec).as_dict()
        else:
            raise ImportError("use manual metadata for tools/frameworks")
    except Exception as e:
        print(f"  using manual metadata ({type(e).__name__})")
    (dest / ".piopm").write_text(json.dumps(meta), encoding="utf-8")
    print("  wrote .piopm:", json.dumps(meta))

    # 没有 package.json 时补一个（有些包 PlatformIO 会读）
    pj = dest / "package.json"
    if not pj.is_file() and typex in ("tool", "framework"):
        pj.write_text(json.dumps({
            "name": name, "version": version,
            "description": f"{name} (installed offline via install_pkg.py)",
        }, indent=2), encoding="utf-8")
        print("  wrote package.json")

    print(f"\ninstalled {typex} {name}@{version} -> {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
