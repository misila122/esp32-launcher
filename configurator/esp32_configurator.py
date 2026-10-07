#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ESP32 启动器配置器 (ESP32 Launcher Configurator)

一个窗口改完 6 张卡片的名字 / 主题色 / 图标 / 对应的 exe，点「应用」两秒生效。

三条路，按需要选：

  1. 应用（走 WiFi，8269 端口）   —— 只把文字/颜色/图标写进板子的 NVS + SPIFFS，
                                     板上立刻重画，不重启。日常用这个。
  2. 烧进固件（OTA，3232 端口）   —— 把当前配置写成 firmware/src/apps.h + icons.h，
                                     再调 PlatformIO 走 OTA 刷进去，变成出厂默认值。
                                     需要本机装了 PlatformIO 且能找到固件目录。
  3. 导出 apps.h / icons.h        —— 本机没有工具链时，导出两个头文件自己拿去编译。

顺手把 exe 路径写回 pc/apps.json，省得在两个地方各改一遍。

纯标准库（tkinter + socket + struct + zlib），没有第三方依赖，
所以 PyInstaller 打出来的 exe 很小，拷到别的电脑上直接能跑。
"""
import base64
import json
import math
import os
import queue
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import zlib
from tkinter import filedialog, messagebox, colorchooser
from tkinter import font as tkfont
from tkinter import ttk

APP_TITLE = "ESP32 启动器配置器"
APP_VERSION = "1.0"

SLOTS = 6                       # 板子上的卡片数，编译期 APP_COUNT 定死
ICON_W = ICON_H = 64
ICON_BYTES = ICON_W * ICON_H * 2
CFG_PORT = 8269                 # 配置通道（板子上单独的第二个 TCP 端口）
OTA_PORT = 3232                 # espota
TOKEN = "esp32launcher"

C_TILE = (0x17, 0x1C, 0x23)     # 和 main.cpp 的 C_TILE 一致，用来铺卡片底色


# ===========================================================================
#  外观
#
#  这套颜色和几何尺寸**不是配出来的，是从固件里抄出来的**，所以窗口里的预览
#  和板子上真画出来的东西逐像素对得上。改任何一个值之前先看这两个地方：
#
#    firmware/src/main.cpp:31-38     C_BG / C_TILE / C_TILE_EDGE / C_TEXT /
#                                    C_DIM / C_OK / C_WARN / C_ERR
#    firmware/src/main.cpp:359       状态栏底色 0x161B22
#    firmware/src/config.h           STATUS_BAR_H / TILE_GAP / TILE_RADIUS
#
#  六个应用色不算主题色，那是**数据** —— 用户自己会改，
#  只出现在预览和色块里，不参与界面配色。
# ===========================================================================
INK    = "#0A0D11"      # 窗口底：比板子屏幕更深一档，让屏幕看起来是"亮着"的
PANEL  = "#0E1116"      # 板子屏幕底 = C_BG
CHROME = "#161B22"      # 状态栏 / 表头 / 输入框底 = 板子状态栏那个 0x161B22
TILE   = "#171C23"      # 卡片底 = C_TILE
FIELD  = "#12171E"      # 输入框内部，比卡片再深一点
EDGE   = "#2A313B"      # 分隔线 / 描边 = C_TILE_EDGE
EDGE_HI= "#3A424D"      # 悬停时的描边
TEXT   = "#E6EDF3"      # C_TEXT
DIM    = "#7D8590"      # C_DIM
LIVE   = "#58A6FF"      # 自检箭头和 OTA 进度条用的那个蓝，只用于"连着 / 选中"
OK     = "#3FB950"
WARN   = "#D29922"
ERR    = "#F85149"

STATUS_BAR_H = 22       # = config.h
TILE_GAP     = 6        # = config.h
TILE_RADIUS  = 10       # = config.h
PREVIEW_W    = 320      # 横屏时板子的实际分辨率
PREVIEW_H    = 240
PREVIEW_SCALE = 1.5     # 预览放大倍数。只允许能写成 zoom(n)/subsample(m) 的值，
                        # 全是整数运算 —— 非整数缩放 Tk 做不了，会糊或者跑版。
                        # 1.5 = zoom(3)/subsample(2)
BEZEL = 13              # 预览外面那圈黑边，宽度（像素）。板子是真的有一圈塑料边框的，
                        # 画出来预览才像"那台设备"，而不是"一块画布"。
BEZEL_C = "#05070A"     # 比窗口底(INK)更黑，看起来才是凹进去的
ICON_SCALE = (2, 3)     # 卡片小图标：zoom(2)/subsample(3) = 64 -> 42，放进 44 的框里

# 字体：只用 Windows 自带的两种，不打包字体文件 —— 自带字体就得注册，
# 注册在别的机器上失败的话界面会整个崩掉，不值得。
_FONTS = {}


def ui_font(size=9, bold=False, mono=False):
    key = (size, bold, mono)
    if key not in _FONTS:
        fam = "Consolas" if mono else "Segoe UI"
        _FONTS[key] = tkfont.Font(family=fam, size=size,
                                  weight="bold" if bold else "normal")
    return _FONTS[key]


def tile_rect(i, sw=PREVIEW_W, sh=PREVIEW_H):
    """和固件 main.cpp:317 的 tileRect() 同一套算法，一个像素都不差。

    网格随长宽比自适应：横屏 3 列 × 2 行，竖屏 2 列 × 3 行。
    """
    cols = 3 if sw >= sh else 2
    rows = (SLOTS + cols - 1) // cols
    grid_h = sh - STATUS_BAR_H
    w = (sw - TILE_GAP * (cols + 1)) // cols
    h = (grid_h - TILE_GAP * (rows + 1)) // rows
    c, r = i % cols, i // cols
    return (TILE_GAP + c * (w + TILE_GAP),
            STATUS_BAR_H + TILE_GAP + r * (h + TILE_GAP), w, h)


def rr_points(x, y, w, h, r, steps=6):
    """圆角矩形的顶点表。tk.Canvas 没有圆角矩形，只能自己算。"""
    r = max(0.0, min(r, w / 2.0, h / 2.0))
    pts = []
    for cx, cy, a0 in ((x + w - r, y + r, -90.0), (x + w - r, y + h - r, 0.0),
                       (x + r, y + h - r, 90.0), (x + r, y + r, 180.0)):
        for s in range(steps + 1):
            a = math.radians(a0 + 90.0 * s / steps)
            pts.extend((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def mix_hex(a, b, t):
    """和固件 main.cpp:40 的 mix() 同式。a/b 是 #RRGGBB，t 是 0..255 的混合量。"""
    ar, ag, ab = hex_to_rgb(parse_color(a, 0))
    br, bg_, bb = hex_to_rgb(parse_color(b, 0))
    return "#%02X%02X%02X" % (
        (ar * (255 - t) + br * t) // 255,
        (ag * (255 - t) + bg_ * t) // 255,
        (ab * (255 - t) + bb * t) // 255)


def contrast_on(hexcolor):
    """在这个底色上该用深字还是浅字。"""
    r, g, b = hex_to_rgb(parse_color(hexcolor, 0))
    return "#0A0D11" if (r * 299 + g * 587 + b * 114) / 255000.0 > 0.55 else "#FFFFFF"


GLYPHS = ["GLYPH_TERMINAL", "GLYPH_BOLT", "GLYPH_TV",
          "GLYPH_CHART", "GLYPH_GAMEPAD", "GLYPH_CHAT"]

DEFAULTS = [
    # id, label,     rgb,      glyph
    (1, "Notepad",   0x4C8BF5, 0),
    (2, "Calc",      0x7B61FF, 3),
    (3, "Terminal",  0xF0B90B, 0),
    (4, "Paint",     0xFB7299, 4),
    (5, "Explorer",  0x00C2A8, 2),
    (6, "Chat",      0x07C160, 5),
]


# ===========================================================================
#  借用 tools/ 里那两个纯标准库模块（图标抠取 + 缩放/编码）
# ===========================================================================
def _load_helpers():
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "tools"), here,
                 os.path.join(here, os.pardir, "tools"),
                 os.path.join(here, os.pardir, os.pardir, "tools")):
        p = os.path.abspath(cand)
        if os.path.exists(os.path.join(p, "mk_icons.py")):
            if p not in sys.path:
                sys.path.insert(0, p)
            return
    # 冻结成 exe 时 PyInstaller 已经把这两个模块打进包里，直接 import 就行


_load_helpers()
try:
    import extract_icon          # noqa: E402
    import mk_icons              # noqa: E402
except Exception as _exc:        # pragma: no cover
    raise SystemExit("找不到 tools/extract_icon.py 和 tools/mk_icons.py：%s" % _exc)


# ===========================================================================
#  小工具
# ===========================================================================
def mix_rgb(a, b, t):
    """和 main.cpp 的 mix() 一样：t=0 全 a，t=255 全 b。"""
    return tuple((a[i] * (255 - t) + b[i] * t) // 255 for i in range(3))


def hex_to_rgb(v):
    return ((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF)


def parse_color(text, fallback=0x4C8BF5):
    """认 #RRGGBB / RRGGBB / #RGB，认不出来就返回 fallback。"""
    s = (text or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return fallback
    try:
        return int(s, 16)
    except ValueError:
        return fallback


def png_bytes(rgb, w, h):
    rows = b"".join(b"\x00" + bytes(rgb[y * w * 3:(y + 1) * w * 3]) for y in range(h))

    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9))
            + chunk(b"IEND", b""))


def photo_from_rgba(rgba, w, h, bg):
    """把 64x64 的 RGBA 铺到卡片底色上，返回一个 tk.PhotoImage。"""
    rgb = bytearray(w * h * 3)
    for i in range(w * h):
        a = rgba[i * 4 + 3]
        for c in range(3):
            rgb[i * 3 + c] = (rgba[i * 4 + c] * a + bg[c] * (255 - a)) // 255
    return tk.PhotoImage(data=base64.b64encode(png_bytes(rgb, w, h)))


def rgba_to_565(rgba):
    return b"".join(struct.pack("<H", v) for v in mk_icons.to565(ICON_W, ICON_H, rgba))


def icon_from_exe(path):
    """从 exe 里抠出最大的那个图标 -> (64x64 RGBA, 8192 字节 RGB565, 说明)"""
    with tempfile.TemporaryDirectory() as td:
        out, chosen, frames, _n = extract_icon.extract(path, td, "icon")
        w, h, px = mk_icons.load_rgba(out)
    small = mk_icons.downscale(w, h, px, ICON_W, ICON_H)
    return small, rgba_to_565(small), "抠到 %s（可选 %s）" % (chosen, frames)


def icon_from_image_file(path):
    """从 png/ico 读一张图 -> (64x64 RGBA, 8192 字节 RGB565)"""
    w, h, px = mk_icons.load_rgba(path)
    small = mk_icons.downscale(w, h, px, ICON_W, ICON_H)
    return small, rgba_to_565(small)


def placeholder_rgba(rgb):
    """没图标时给个圆角方块占位，免得 tiles 上光秃秃只剩字。"""
    r, g, b = hex_to_rgb(rgb)
    rgba = bytearray(ICON_W * ICON_H * 4)
    for y in range(ICON_H):
        for x in range(ICON_W):
            dx = min(x, ICON_W - 1 - x)
            dy = min(y, ICON_H - 1 - y)
            inside = not (dx < 8 and dy < 8 and (8 - dx) + (8 - dy) > 10)
            i = (y * ICON_W + x) * 4
            if inside:
                rgba[i], rgba[i + 1], rgba[i + 2], rgba[i + 3] = r, g, b, 255
    return rgba


def python_exe():
    """打包成 exe 之后 sys.executable 是 exe 自己，得另外找 python。"""
    if not getattr(sys, "frozen", False):
        return sys.executable
    for cand in ("python", "python3", "py"):
        p = shutil.which(cand)
        if p:
            return p
    return "python"


# ===========================================================================
#  8269 配置协议客户端
# ===========================================================================
class DeviceError(Exception):
    pass


class Device(object):
    def __init__(self, host, port=CFG_PORT, timeout=6.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = None

    # -- 连接 --------------------------------------------------------------
    def connect(self, retries=3):
        """连上并握手。失败会自动重试几次 —— 板子的配置端口只有一个槽位，
        前一次连接的尾巴还没被它清掉时，第一个新连接有可能撞上 RST。"""
        last = None
        for attempt in range(retries):
            try:
                return self._connect_once()
            except (DeviceError, OSError) as exc:
                last = exc
                self.close(say_bye=False)
                if attempt + 1 < retries:
                    time.sleep(0.7)
        if isinstance(last, DeviceError):
            raise last
        raise DeviceError(
            "连不上 %s:%d —— %s\n"
            "  检查：板子通电了吗？和电脑在同一个 WiFi 吗？地址对不对？"
            % (self.host, self.port, last))

    def _connect_once(self):
        self.close()
        try:
            infos = socket.getaddrinfo(self.host, self.port, socket.AF_INET,
                                       socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise DeviceError("解析不了主机名 %s：%s" % (self.host, exc))
        last = None
        for _fam, _type, _proto, _canon, addr in infos:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            try:
                s.connect(addr)
            except OSError as exc:
                last = exc
                s.close()
                continue
            self.sock = s
            break
        if self.sock is None:
            raise DeviceError(
                "连不上 %s:%d —— %s\n"
                "  检查：板子通电了吗？和电脑在同一个 WiFi 吗？地址对不对？"
                % (self.host, self.port, last))
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return self.hello()

    def close(self, say_bye=True):
        """关连接。默认先发一句 BYE。

        为什么要 BYE：板子的配置端口只有一个槽位，而 TCP 的 FIN 要等板子下一次
        recv 才被发现。不发 BYE 的话，"读板子 -> 应用 -> 再读板子"这种连续操作里，
        新连接会被晾在 backlog 里、最后收到 RST（WinError 10054）。
        """
        if self.sock:
            if say_bye:
                try:
                    self.sock.sendall(b"BYE\n")
                except OSError:
                    pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    # -- 收发 --------------------------------------------------------------
    def _readline(self):
        buf = bytearray()
        while True:
            c = self.sock.recv(1)
            if not c:
                raise DeviceError("板子把连接断掉了")
            if c == b"\n":
                return buf.decode("utf-8", "replace").rstrip("\r")
            buf += c
            if len(buf) > 512:
                raise DeviceError("板子回了一行超长的东西：%r" % bytes(buf[:80]))

    def cmd(self, line):
        if not self.sock:
            raise DeviceError("还没连上")
        self.sock.sendall((line + "\n").encode("utf-8"))
        return self._readline()

    def hello(self):
        rep = self.cmd("HELLO " + TOKEN)
        if not rep.startswith("OK"):
            raise DeviceError("握手被拒：%s" % rep)
        parts = rep.split()
        return {"device": parts[1] if len(parts) > 1 else "?",
                "slots": int(parts[2]) if len(parts) > 2 else SLOTS}

    def get(self):
        """GET -> [{idx,id,icon_fs,rgb,label}, ...]"""
        if not self.sock:
            raise DeviceError("还没连上")
        self.sock.sendall(b"GET\n")
        out = []
        while True:
            line = self._readline()
            if line == "END":
                return out
            if line.startswith("ERR"):
                raise DeviceError("板子说：%s" % line)
            # APP <i> <id> <iconFs> <rrggbb> <label...>
            parts = line.split(" ", 5)
            if len(parts) < 6 or parts[0] != "APP":
                continue
            out.append({"idx": int(parts[1]), "id": int(parts[2]),
                        "icon_fs": parts[3] == "1",
                        "rgb": parse_color(parts[4], 0x808080),
                        "label": parts[5].strip()})

    def _read_exact(self, n):
        """读满 n 个字节（图标是裸二进制，没有换行可以靠）。"""
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(min(4096, n - len(buf)))
            if not chunk:
                raise DeviceError("板子提前断了，图标还差 %d 字节" % (n - len(buf)))
            buf += chunk
        return bytes(buf)

    def get_icon(self, idx):
        """`GICON <i>` —— 把板子上第 idx 格**当前真正用的**图标读回来。

        返回 8192 字节 RGB565（小端），失败返回 None。
        这是唯一能回答"板子上到底是哪张图"的手段：`GET` 只回一个 0/1，
        告诉你有没有自定义图标，但不告诉你它长什么样。
        """
        rep = self.cmd("GICON %d" % idx)
        if not rep.startswith("OK"):
            return None
        n = int(rep.split()[1]) if len(rep.split()) > 1 else ICON_BYTES
        return self._read_exact(n)

    def set_slot(self, idx, sid, rgb, label):
        rep = self.cmd("SET %d %d %06X %s" % (idx, sid, rgb & 0xFFFFFF, label))
        if rep != "OK":
            raise DeviceError("SET %d 失败：%s" % (idx, rep))

    def push_icon(self, idx, blob):
        if len(blob) != ICON_BYTES:
            raise DeviceError("图标长度不对：%d != %d" % (len(blob), ICON_BYTES))
        rep = self.cmd("ICON %d %d" % (idx, len(blob)))
        if rep != "READY":
            raise DeviceError("板子没准备好收图标：%s" % rep)
        self.sock.sendall(blob)
        rep = self._readline()
        if rep != "OK icon":
            raise DeviceError("图标写入失败：%s" % rep)

    def commit(self):
        rep = self.cmd("COMMIT")
        if not rep.startswith("OK"):
            raise DeviceError("保存失败：%s" % rep)

    def clear(self):
        rep = self.cmd("CLEAR")
        if not rep.startswith("OK"):
            raise DeviceError("恢复出厂失败：%s" % rep)


# ===========================================================================
#  本机文件：apps.json / 固件目录 / 自己的设置
# ===========================================================================
def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def short_path(p):
    """日志里显示路径用：能相对当前目录说清楚就相对着说，不行再报绝对路径。

    纯粹为了好读 —— 一屏日志里挂着一条 D:\\... 的长绝对路径，把真正要看的东西
    挤到看不见的地方。
    """
    if not p:
        return p
    try:
        rel = os.path.relpath(p, os.getcwd())
    except ValueError:                  # 跨盘符，relpath 会抛
        return p
    return p if rel.startswith(".." + os.sep + ".." + os.sep) else rel


def settings_path():
    p = os.path.join(app_dir(), "configurator_settings.json")
    try:
        with open(p, "a"):
            pass
        return p
    except OSError:
        d = os.path.join(os.environ.get("APPDATA") or tempfile.gettempdir(),
                         "esp32-launcher")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "configurator_settings.json")


def load_settings():
    try:
        with open(settings_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(d):
    try:
        with open(settings_path(), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _search_roots():
    """可能的"项目根"候选：exe 所在目录往上两层 + 当前工作目录往上两层。

    exe 一般躺在 configurator\\dist\\ 里，所以必须往上找两层才能看见 pc\\ 和 firmware\\；
    同时把 cwd 也算进来，这样从项目根跑 `python configurator\\esp32_configurator.py` 也能找到。
    """
    roots = []
    for start in (app_dir(), os.getcwd()):
        d = os.path.abspath(start)
        for _ in range(3):
            if d not in roots:
                roots.append(d)
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    return roots


def find_apps_json(hint=None):
    if hint and os.path.exists(hint):
        return hint
    for root in _search_roots():
        for c in (os.path.join(root, "pc", "apps.json"),
                  os.path.join(root, "apps.json")):
            if os.path.exists(c):
                return os.path.abspath(c)
    return None


def find_firmware_dir(hint=None):
    """找一个带 platformio.ini + src/apps.h 的目录。"""
    def ok(d):
        return bool(d) and os.path.exists(os.path.join(d, "platformio.ini")) \
            and os.path.exists(os.path.join(d, "src", "apps.h"))
    if ok(hint):
        return hint
    for root in _search_roots():
        for c in (os.path.join(root, "firmware"), root):
            if ok(c):
                return os.path.abspath(c)
    return None


def load_apps_json(path):
    for enc in ("utf-8", "utf-8-sig", "gbk"):
        try:
            with open(path, "r", encoding=enc) as f:
                return json.load(f)
        except Exception:
            continue
    return None


# ===========================================================================
#  生成 apps.h / icons.h（「烧进固件」和「导出」共用）
# ===========================================================================
APPS_H_HEAD = """// ===========================================================================
//  apps.h —— 屏幕上这 6 个格子分别对应哪个程序（出厂默认值）
//
//  这个文件是 ESP32 启动器配置器生成的，别手改 —— 下次点「烧进固件」会覆盖。
//  想改就开配置器改，或者直接改板子上的（点「应用」，走 NVS + SPIFFS）。
//
//  id 必须和 PC 端 pc/apps.json 里的 id 一一对应！
//  改这里 = 改屏幕；改 apps.json = 改电脑上真正启动什么。两边 id 对上就行。
//
//  板子运行时用 NVS + SPIFFS 里的配置盖在这个默认值上面，
//  只有在「恢复出厂」或整片擦除之后才会回到这里。
// ===========================================================================
#pragma once
#include <stdint.h>

enum GlyphKind : uint8_t {
  GLYPH_TERMINAL = 0,
  GLYPH_BOLT,
  GLYPH_TV,
  GLYPH_CHART,
  GLYPH_GAMEPAD,
  GLYPH_CHAT,
};

struct AppDef {
  uint8_t     id;      // 发给 PC 的编号
  const char* label;   // 屏幕上显示的名字（只能是 ASCII，中文要额外做字库）
  uint8_t     glyph;   // 图标形状（没有自定义图标时用它兜底）
  uint32_t    rgb;     // 主题色 0xRRGGBB
};

static const AppDef APPS[] = {
"""

APPS_H_TAIL = """};

static const int APP_COUNT = (int)(sizeof(APPS) / sizeof(APPS[0]));
"""


def write_apps_h(path, cards):
    rows = []
    for c in cards:
        label = "".join(ch for ch in c["label"] if 0x20 <= ord(ch) < 0x7F)
        rows.append('  { %d, "%s", %s, 0x%06X },' % (
            c["id"], label.replace('\\', '').replace('"', ''),
            GLYPHS[c["glyph"] % len(GLYPHS)], c["rgb"] & 0xFFFFFF))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(APPS_H_HEAD + "\n".join(rows) + "\n" + APPS_H_TAIL)


def write_icons_h(path, cards):
    out = ["// 由 ESP32 启动器配置器生成 —— 别手改，改完会被下次覆盖。",
           "// 图标是 RGB565，透明处用色键。",
           "// 仓库里这份是纯色占位方块：出厂默认值故意不带任何第三方图标（商标问题），",
           "// 你在配置器里给每张卡选好图标后点「烧进固件」，这里就会变成真图标。",
           "#pragma once",
           "#include <stdint.h>",
           "",
           "#define ICON_W %d" % ICON_W,
           "#define ICON_H %d" % ICON_H,
           "#define ICON_TRANSPARENT 0x%04Xu" % mk_icons.KEY,
           ""]
    for i, c in enumerate(cards):
        rgba = c.get("icon_rgba") or placeholder_rgba(c["rgb"])
        words = mk_icons.to565(ICON_W, ICON_H, rgba)
        out.append("static const uint16_t ICON_SLOT%d[ICON_W * ICON_H] = {" % i)
        for y in range(ICON_H):
            row = words[y * ICON_W:(y + 1) * ICON_W]
            out.append("  " + " ".join("0x%04X," % v for v in row))
        out.append("};")
        out.append("")
    out.append("// 下标和 apps.h 里的 APPS[] 一一对应。")
    out.append("static const uint16_t* const ICONS[] = {")
    for i in range(len(cards)):
        out.append("  ICON_SLOT%d," % i)
    out.append("};")
    out.append("")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out))


# ===========================================================================
#  小控件：全是 tk 原生件，因为 ttk 在 Windows 上用的 vista 主题不给改颜色
# ===========================================================================
def flat_entry(parent, textvar, width=None, font=None):
    e = tk.Entry(parent, textvariable=textvar, font=font or ui_font(9),
                 bg=FIELD, fg=TEXT, insertbackground=LIVE, relief="flat",
                 highlightthickness=1, highlightbackground=EDGE,
                 highlightcolor=LIVE, disabledbackground=FIELD,
                 disabledforeground=DIM)
    if width:
        e.configure(width=width)
    return e


def flat_button(parent, text, command, primary=False, width=None, font=None):
    if primary:
        bg, fg, abg = LIVE, "#08121F", "#79B8FF"
    else:
        bg, fg, abg = CHROME, TEXT, "#222A34"
    b = tk.Button(parent, text=text, command=command, font=font or ui_font(9),
                  bg=bg, fg=fg, activebackground=abg, activeforeground=fg,
                  relief="flat", bd=0, highlightthickness=0, cursor="hand2",
                  padx=11, pady=4)
    if width:
        b.configure(width=width)
    return b


def hairline(parent, color=EDGE):
    """1 像素分隔线。tk 的 Frame 用 height=1 就能当线用。"""
    return tk.Frame(parent, bg=color, height=1, bd=0, highlightthickness=0)


# ===========================================================================
#  板子预览
# ===========================================================================
_PLACEHOLDER_CACHE = {}

# 预览放大倍数 -> (zoom, subsample)。必须都是整数，Tk 的 PhotoImage 只做整数缩放。
_ZOOM_FOR = {1.0: (1, 1), 1.25: (5, 4), 1.5: (3, 2), 2.0: (2, 1)}


class PanelView(object):
    """窗口里那块就是板子真正画出来的 320x240 画面，按 1.5 倍放大。

    为什么值得单独做这么一块：这个程序存在的全部意义就是回答"我板子上现在长什么样"，
    而那块屏只能写不能读 —— 改完得跑过去看。不如在这儿按**固件的真实几何**先画一遍。

    几何、配色、标签位置全部照抄 firmware/src/main.cpp 的 drawStatusBar() /
    drawTile() / tileRect()，改固件的时候记得同步这里，否则预览会说谎。
    """

    def __init__(self, parent, on_pick=None):
        self.on_pick = on_pick
        self.sw, self.sh = PREVIEW_W, PREVIEW_H
        self.scale = PREVIEW_SCALE
        self.cards = []
        self.sel = -1
        self.hover = -1
        self.link = 0                 # 0 没连上 / 1 连上了
        self.addr = ""
        self._photos = {}
        self._sig = {}

        self.canvas = tk.Canvas(parent,
                                width=int(self.sw * self.scale) + 2 * BEZEL,
                                height=int(self.sh * self.scale) + 2 * BEZEL,
                                bg=INK, highlightthickness=0, bd=0)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", lambda _e: self._set_hover(-1))

    # -- 坐标 / 命中 -------------------------------------------------------
    def _s(self, v):
        return v * self.scale

    def hit(self, mx, my):
        x, y = (mx - BEZEL) / self.scale, (my - BEZEL) / self.scale
        for i in range(SLOTS):
            tx, ty, tw, th = tile_rect(i, self.sw, self.sh)
            if tx <= x < tx + tw and ty <= y < ty + th:
                return i
        return -1

    def _on_click(self, ev):
        i = self.hit(ev.x, ev.y)
        if i >= 0 and self.on_pick:
            self.on_pick(i)

    def _on_motion(self, ev):
        self._set_hover(self.hit(ev.x, ev.y))

    def _set_hover(self, i):
        if i != self.hover:
            self.hover = i
            self.canvas.configure(cursor="hand2" if i >= 0 else "")
            self.redraw()

    # -- 图标 --------------------------------------------------------------
    def _icon(self, i, bg):
        card = self.cards[i]
        rgba = card.get("icon_rgba")
        if not rgba:
            rgb = card.get("rgb", 0x4C8BF5)
            if rgb not in _PLACEHOLDER_CACHE:
                _PLACEHOLDER_CACHE[rgb] = placeholder_rgba(rgb)
            rgba = _PLACEHOLDER_CACHE[rgb]
        key = (id(rgba), bg)
        if self._sig.get(i) != key:
            img = photo_from_rgba(rgba, ICON_W, ICON_H,
                                  hex_to_rgb(parse_color(bg, 0)))
            z, s = _ZOOM_FOR.get(self.scale, (1, 1))
            if z != 1 or s != 1:
                img = img.zoom(z).subsample(s)
            self._photos[i] = img
            self._sig[i] = key
        return self._photos.get(i)

    # -- 画 ----------------------------------------------------------------
    def _track_text(self, cx, cy, text, fill, font, track=1.0):
        """逐字画、手动加字距：tk 没有 letter-spacing，而板上的 Font2 是固定步进的。"""
        if not text:
            return
        ws = [font.measure(ch) for ch in text]
        x = cx - (sum(ws) + track * (len(text) - 1)) / 2.0
        for ch, w in zip(text, ws):
            self.canvas.create_text(x, cy, text=ch, fill=fill, font=font, anchor="w")
            x += w + track

    def redraw(self):
        c = self.canvas
        c.delete("all")
        f_small = ui_font(7, mono=True)
        f_label = ui_font(7, mono=True)
        bar_h = self._s(STATUS_BAR_H)
        sw, sh = self._s(self.sw), self._s(self.sh)

        # ---- 边框 ----
        # 画在 (-BEZEL,-BEZEL)，然后和所有内容一起 move(+BEZEL,+BEZEL)，
        # 于是边框落回 (0,0) 并正好比屏幕大一圈。省得每个坐标都手动加偏移。
        c.create_polygon(rr_points(-BEZEL, -BEZEL, sw + 2 * BEZEL,
                                   sh + 2 * BEZEL, BEZEL + 6),
                         fill=BEZEL_C, outline="", joinstyle="round")

        # ---- 状态栏（对应 drawStatusBar）----
        c.create_rectangle(0, 0, sw, bar_h, fill=CHROME, outline="")
        cy = bar_h / 2.0
        c.create_text(self._s(6), cy, anchor="w", text="WiFi",
                      fill=OK if self.link else ERR, font=f_small)
        c.create_text(sw / 2.0, cy, text=self.addr or "not connected",
                      fill=DIM, font=f_small)
        c.create_text(self._s(self.sw - 6), cy, anchor="e",
                      text="8269" if self.link else "--",
                      fill=OK if self.link else DIM, font=f_small)

        # ---- 六张卡（对应 drawTile）----
        for i, card in enumerate(self.cards):
            tx, ty, tw, th = tile_rect(i, self.sw, self.sh)
            rgb = "#%06X" % (card.get("rgb", 0x4C8BF5) & 0xFFFFFF)
            on = (i == self.sel)
            hovered = (i == self.hover)
            bg = mix_hex(TILE, rgb, 90) if (on or hovered) else TILE
            edge = rgb if on else (mix_hex(EDGE, rgb, 140) if hovered else
                                   mix_hex(EDGE, rgb, 70))
            x, y = self._s(tx), self._s(ty)
            w, h = self._s(tw), self._s(th)
            rad = self._s(TILE_RADIUS)

            c.create_polygon(rr_points(x, y, w, h, rad), fill=bg, outline=edge,
                             width=2 if on else 1, joinstyle="round")
            # 选中时外面再套一圈，和板上按下时那圈亮边一个意思
            if on:
                c.create_polygon(rr_points(x - 3, y - 3, w + 6, h + 6, rad + 3),
                                 fill="", outline=LIVE, width=2, joinstyle="round")

            # 图标：固件里是 boxY/boxH 里居中放一张 64x64，标签占底部 16px
            box_x, box_w = tx + 4, tw - 8
            box_y, box_h = ty + 4, th - 4 - 16
            img = self._icon(i, bg)
            if img is not None:
                c.create_image(self._s(box_x + box_w / 2.0),
                               self._s(box_y + box_h / 2.0),
                               image=img, anchor="center")

            # 标签：固件画在 r.y + r.h - 11 处居中
            self._track_text(self._s(tx + tw / 2.0), self._s(ty + th - 11),
                             (card.get("label") or "")[:16], TEXT, f_label)

        # ---- 屏幕外框 ----
        c.create_rectangle(0, 0, sw - 1, sh - 1, outline=EDGE, width=1)

        # 一次把整幅画平移进边框里（边框自己也在里面，所以先画在负坐标上）
        c.move("all", BEZEL, BEZEL)


# ===========================================================================
#  一张卡片的编辑区
# ===========================================================================
class CardWidget(object):
    def __init__(self, parent, idx, app, on_focus=None):
        self.idx = idx
        self.app = app
        self.icon_rgba = None
        self.icon565 = None
        self.icon_dirty = False
        self._photo = None
        self.on_focus = on_focus
        self._sel = False

        self.frame = tk.Frame(parent, bg=TILE, highlightthickness=1,
                              highlightbackground=TILE, bd=0)
        self.frame.bind("<Button-1>", lambda _e: self._focus())

        # ---- 左：序号 + 图标 ----
        left = tk.Frame(self.frame, bg=TILE)
        left.pack(side="left", padx=(9, 10), pady=7, anchor="n")
        self.idxlab = tk.Label(left, text="%d" % (idx + 1), font=ui_font(9, bold=True),
                               bg=TILE, fg=DIM, width=2, anchor="w")
        self.idxlab.pack(side="left", anchor="n", pady=(1, 0))
        self.canvas = tk.Canvas(left, width=44, height=44, bg=TILE,
                                highlightthickness=1, highlightbackground=EDGE, bd=0)
        self.canvas.pack(side="left", padx=(6, 0))
        self.canvas.bind("<Button-1>", lambda _e: self._focus())

        # ---- 右：字段 ----
        right = tk.Frame(self.frame, bg=TILE)
        right.pack(side="left", fill="x", expand=True, padx=(0, 9), pady=7)

        r1 = tk.Frame(right, bg=TILE)
        r1.pack(fill="x")
        tk.Label(r1, text="名字", font=ui_font(9), bg=TILE, fg=DIM).pack(side="left")
        self.var_label = tk.StringVar()
        self.ent_label = flat_entry(r1, self.var_label, width=17)
        self.ent_label.pack(side="left", padx=(6, 14))
        # 边打字边更新左边的预览 —— 这个程序的全部价值就是"改完立刻看见"
        self.var_label.trace_add("write", lambda *_a: self.refresh())
        tk.Label(r1, text="颜色", font=ui_font(9), bg=TILE, fg=DIM).pack(side="left")
        self.color_btn = tk.Button(r1, width=8, relief="flat", bd=0,
                                   highlightthickness=0, cursor="hand2",
                                   font=ui_font(8, mono=True),
                                   activebackground=EDGE_HI,
                                   command=self.pick_color)
        self.color_btn.pack(side="left", padx=(6, 6), ipady=2)
        self.var_hex = tk.StringVar()
        ent = flat_entry(r1, self.var_hex, width=9, font=ui_font(9, mono=True))
        ent.pack(side="left")
        ent.bind("<FocusOut>", lambda _e: self.sync_color())
        ent.bind("<Return>", lambda _e: self.sync_color())
        self.var_hex.trace_add("write", lambda *_a: self.refresh())
        self.status = tk.Label(r1, text="", font=ui_font(8), bg=TILE, fg=DIM,
                               anchor="e")
        self.status.pack(side="right")

        r2 = tk.Frame(right, bg=TILE)
        r2.pack(fill="x", pady=(5, 0))
        tk.Label(r2, text="程序", font=ui_font(9), bg=TILE, fg=DIM).pack(side="left")
        self.var_exe = tk.StringVar()
        flat_entry(r2, self.var_exe).pack(side="left", fill="x", expand=True,
                                          padx=(6, 6))
        flat_button(r2, "浏览…", self.browse_exe, width=6).pack(side="left")
        flat_button(r2, "图片…", self.browse_image, width=6).pack(side="left",
                                                                  padx=(5, 0))

        for w in (self.frame, right, r1, r2):
            w.bind("<Button-1>", lambda _e: self._focus())
        self.apply_app(app)

    # -- 选中态 ------------------------------------------------------------
    def _focus(self):
        if self.on_focus:
            self.on_focus(self.idx)

    def set_selected(self, on):
        if on == self._sel:
            return
        self._sel = on
        self.frame.configure(highlightbackground=LIVE if on else TILE)
        self.idxlab.configure(fg=LIVE if on else DIM)

    # -- 数据 <-> 界面 ------------------------------------------------------
    def apply_app(self, app):
        self.app = app
        self.var_label.set(app.get("label", ""))
        self.var_hex.set("#%06X" % (app.get("rgb", 0x4C8BF5) & 0xFFFFFF))
        self.var_exe.set(app.get("exe", ""))
        self.icon_rgba = app.get("icon_rgba")
        self.icon565 = app.get("icon565")
        self.icon_dirty = bool(app.get("icon_dirty"))
        self.refresh()

    def collect(self):
        self.app["label"] = self.var_label.get().strip() or ("APP%d" % (self.idx + 1))
        self.app["rgb"] = parse_color(self.var_hex.get(), self.app.get("rgb", 0x4C8BF5))
        self.app["exe"] = self.var_exe.get().strip()
        self.app["icon_rgba"] = self.icon_rgba
        self.app["icon565"] = self.icon565
        self.app["icon_dirty"] = self.icon_dirty
        return self.app

    # -- 显示 --------------------------------------------------------------
    def refresh(self):
        rgb = parse_color(self.var_hex.get(), self.app.get("rgb", 0x4C8BF5))
        bg = mix_rgb(C_TILE, hex_to_rgb(rgb), 90)
        hx = "#%06X" % (rgb & 0xFFFFFF)
        self.canvas.configure(bg="#%02X%02X%02X" % bg)
        self.canvas.delete("all")
        if self.icon_rgba:
            self._photo = photo_from_rgba(self.icon_rgba, ICON_W, ICON_H, bg)
            z, s = ICON_SCALE
            if z != 1 or s != 1:
                self._photo = self._photo.zoom(z).subsample(s)
            self.canvas.create_image(22, 22, anchor="center", image=self._photo)
        else:
            self._photo = None
            self.canvas.create_text(22, 22, fill=DIM, font=ui_font(7),
                                    justify="center", text="无图标\n板子用矢量图")
        self.color_btn.configure(bg=hx, activebackground=hx, text=hx,
                                 fg=contrast_on(hx))
        if self.app.get("on_change"):
            self.app["on_change"]()

    def set_status(self, text, color=DIM):
        self.status.configure(text=text, foreground=color)

    # -- 交互 --------------------------------------------------------------
    def pick_color(self):
        cur = parse_color(self.var_hex.get(), 0x4C8BF5)
        _rgb, hexs = colorchooser.askcolor(color="#%06X" % cur, title="选卡片主题色")
        if hexs:
            self.var_hex.set(hexs.upper())

    def sync_color(self):
        v = parse_color(self.var_hex.get(), self.app.get("rgb", 0x4C8BF5))
        self.var_hex.set("#%06X" % (v & 0xFFFFFF))

    def browse_exe(self):
        path = filedialog.askopenfilename(
            title="选一个程序（.exe）",
            filetypes=[("程序", "*.exe"), ("所有文件", "*.*")])
        if path:
            self.var_exe.set(path)
            self.app["on_icon"]("exe", path, self)

    def browse_image(self):
        path = filedialog.askopenfilename(
            title="选一张图标图片",
            filetypes=[("图片", "*.png *.ico"), ("所有文件", "*.*")])
        if path:
            self.app["on_icon"]("image", path, self)



# ===========================================================================
#  主窗口
# ===========================================================================
class App(object):
    def __init__(self, root):
        self.root = root
        self.settings = load_settings()
        self.ui_q = queue.Queue()
        self.sel = 0

        self.cards = []
        for i, (sid, label, rgb, glyph) in enumerate(DEFAULTS):
            self.cards.append({"idx": i, "id": sid, "label": label, "rgb": rgb,
                               "glyph": glyph, "exe": "", "args": "", "cwd": "",
                               "icon_rgba": None, "icon565": None,
                               "icon_dirty": False, "on_icon": self.start_icon_job,
                               "on_change": self.sync_panel})

        root.title("%s v%s" % (APP_TITLE, APP_VERSION))
        root.configure(bg=INK)
        root.minsize(1064, 730)

        self._build_header()
        self._build_progress()
        self._build_address()
        self._build_body()
        self._decorate_window()

        self.apps_json = find_apps_json(self.settings.get("apps_json"))
        self.fw_dir = find_firmware_dir(self.settings.get("firmware_dir"))
        self._json = None

        self._refresh_paths()
        self.select(0)
        self.root.after(60, self._pump)
        self.load_local(self.apps_json)
        self.set_conn(False, "没连上")
        self.log("就绪。设备 %s，配置端口 %d。" % (self.var_host.get(), CFG_PORT))
        self.log("本机 apps.json：%s" % (short_path(self.apps_json) if self.apps_json
                                        else "（没找到，只改板子）"))
        self.log("固件目录：%s" % (short_path(self.fw_dir) if self.fw_dir
                                  else "（没找到，只能「导出」不能「烧进固件」）"))

    # -- 线程 -> 主线程 -----------------------------------------------------
    def post(self, fn):
        self.ui_q.put(fn)

    def _pump(self):
        try:
            while True:
                self.ui_q.get_nowait()()
        except queue.Empty:
            pass
        except Exception as exc:      # 回调自己炸了也不能把界面搞停
            sys.stderr.write("ui callback: %r\n" % (exc,))
        self.root.after(60, self._pump)

    def run_bg(self, work, done=None):
        def runner():
            try:
                res, err = work(), None
            except Exception as exc:
                res, err = None, exc
            if done:
                self.post(lambda: done(res, err))
        threading.Thread(target=runner, daemon=True).start()

    # -- 界面搭建 ----------------------------------------------------------
    def _build_header(self):
        bar = tk.Frame(self.root, bg=CHROME)
        bar.pack(fill="x")
        tk.Label(bar, text=APP_TITLE, font=ui_font(11, bold=True),
                 bg=CHROME, fg=TEXT).pack(side="left", padx=(13, 7), pady=9)
        tk.Label(bar, text="v" + APP_VERSION, font=ui_font(8), bg=CHROME,
                 fg=DIM).pack(side="left", pady=(13, 0), anchor="n")
        self.lbl_conn = tk.Label(bar, text="", font=ui_font(9), bg=CHROME, fg=DIM)
        self.lbl_conn.pack(side="right", padx=13)
        hairline(self.root).pack(fill="x")

    def _build_progress(self):
        """窗口顶端这条 3 像素的线就是板子上那根 OTA 进度条，只是横过来了。"""
        self.pbar = tk.Canvas(self.root, height=3, bg=INK,
                              highlightthickness=0, bd=0)
        self.pbar.pack(fill="x")
        self._pbar_pct = -1
        self._pbar_anim = None
        self.pbar.bind("<Configure>", lambda _e: self._draw_pbar())

    def _draw_pbar(self):
        c = self.pbar
        c.delete("all")
        w = c.winfo_width()
        if w < 2:
            return
        if self._pbar_pct is None:
            return
        if self._pbar_pct < 0:                      # 不确定进度：来回跑的一小段
            return
        c.create_rectangle(0, 0, w * self._pbar_pct / 100.0, 3,
                           fill=LIVE, outline="")

    def set_progress(self, pct):
        self._pbar_pct = max(0, min(100, pct))
        self._draw_pbar()

    def _animate(self, step=0):
        if self._pbar_anim is None:
            return
        w = max(self.pbar.winfo_width(), 2)
        seg = w // 5
        x = (step * 14) % (w + seg) - seg
        self.pbar.delete("all")
        self.pbar.create_rectangle(x, 0, x + seg, 3, fill=LIVE, outline="")
        self._pbar_anim = self.root.after(24, lambda: self._animate(step + 1))

    def _build_address(self):
        row = tk.Frame(self.root, bg=INK)
        row.pack(fill="x", padx=13, pady=(10, 9))
        tk.Label(row, text="设备地址", font=ui_font(9), bg=INK, fg=DIM).pack(side="left")
        self.var_host = tk.StringVar(
            value=self.settings.get("host", "esp32-launcher.local"))
        e = flat_entry(row, self.var_host, width=23)
        e.pack(side="left", padx=(8, 9), ipady=3)
        e.bind("<Return>", lambda _ev: self.do_read())
        self.btn_read = tk.Button(row, text="读取板子", command=self.do_read,
                                  font=ui_font(9), bg=EDGE, fg=TEXT,
                                  activebackground=EDGE_HI, activeforeground=TEXT,
                                  relief="flat", bd=0, highlightthickness=0,
                                  cursor="hand2", padx=13, pady=4)
        self.btn_read.pack(side="left")
        flat_button(row, "选 apps.json…", self.pick_apps_json).pack(
            side="left", padx=(8, 0))
        flat_button(row, "选固件目录…", self.pick_fw_dir).pack(side="left", padx=(5, 0))
        paths = tk.Frame(row, bg=INK)
        paths.pack(side="right")
        self.lbl_fw = tk.Label(paths, text="", font=ui_font(8), bg=INK, fg=DIM)
        self.lbl_fw.pack(side="right", padx=(12, 0))
        self.lbl_json = tk.Label(paths, text="", font=ui_font(8), bg=INK, fg=DIM)
        self.lbl_json.pack(side="right")
        hairline(self.root).pack(fill="x")

    def _build_body(self):
        body = tk.Frame(self.root, bg=INK)
        body.pack(fill="both", expand=True)

        # ---- 左：板子预览 + 动作 + 日志（占满剩余高度）----
        left = tk.Frame(body, bg=INK)
        left.pack(side="left", fill="y", padx=(13, 11), pady=12, anchor="n")
        self.panel = PanelView(left, on_pick=self.select)
        self.panel.cards = self.cards
        self.panel.canvas.pack()
        cap = tk.Frame(left, bg=INK)
        cap.pack(fill="x", pady=(7, 0))
        tk.Label(cap, text="%d × %d · 3 列 × 2 行 · 横屏" % (PREVIEW_W, PREVIEW_H),
                 font=ui_font(8, mono=True), bg=INK, fg=DIM).pack(side="left")
        tk.Label(cap, text="点卡片可选中", font=ui_font(8), bg=INK,
                 fg=DIM).pack(side="right")

        acts = tk.Frame(left, bg=INK)
        acts.pack(fill="x", pady=(13, 0))
        self.btn_apply = flat_button(acts, "应用（WiFi 直写）", self.do_apply,
                                     primary=True)
        self.btn_apply.pack(side="left")
        self.btn_bake = flat_button(acts, "烧进固件（OTA）", self.do_bake)
        self.btn_bake.pack(side="left", padx=(6, 0))
        acts2 = tk.Frame(left, bg=INK)
        acts2.pack(fill="x", pady=(6, 0))
        self.btn_more = flat_button(acts2, "导出 apps.h / icons.h", self.do_export)
        self.btn_more.pack(side="left")
        flat_button(acts2, "从图片当图标…", self.pick_image_for).pack(
            side="left", padx=(6, 0))
        flat_button(acts2, "恢复出厂", self.do_clear).pack(side="left", padx=(6, 0))

        tk.Label(left, justify="left", anchor="w", font=ui_font(8), bg=INK,
                 fg=DIM, wraplength=470,
                 text="「应用」只改板子的 NVS + SPIFFS，2 秒内屏上就变，不重启。\n"
                      "「烧进固件」把当前配置写成 apps.h / icons.h 再 OTA 刷进去，"
                      "变成以后的出厂默认值。").pack(fill="x", pady=(12, 0))

        self.logbox = tk.Text(left, width=1, height=6, wrap="word", state="disabled",
                              font=ui_font(8, mono=True), bg=FIELD, fg=TEXT,
                              insertbackground=TEXT, relief="flat",
                              highlightthickness=1, highlightbackground=EDGE,
                              padx=9, pady=7)
        self.logbox.pack(fill="both", expand=True, pady=(12, 0))

        # ---- 右：六张卡 ----
        right = tk.Frame(body, bg=INK)
        right.pack(side="left", fill="both", expand=True, padx=(0, 13), pady=12)
        head = tk.Frame(right, bg=INK)
        head.pack(fill="x", pady=(0, 7))
        tk.Label(head, text="卡片", font=ui_font(10, bold=True), bg=INK,
                 fg=TEXT).pack(side="left")
        tk.Label(head, text="改一个字，左边立刻跟着变", font=ui_font(8), bg=INK,
                 fg=DIM).pack(side="left", padx=(9, 0))
        for i in range(SLOTS):
            w = CardWidget(right, i, self.cards[i], on_focus=self.select)
            w.frame.pack(fill="x", pady=(0, 5))
            self.cards[i]["_w"] = w

    def _build_log_note(self):
        pass
    # -- 选中 / 预览同步 ----------------------------------------------------
    def select(self, i):
        if not (0 <= i < SLOTS):
            return
        self.sel = i
        for c in self.cards:
            w = c.get("_w")
            if w is not None:
                w.set_selected(c["idx"] == i)
        self.panel.sel = i
        self.panel.redraw()

    def sync_panel(self):
        panel = getattr(self, "panel", None)
        if panel is None:
            return
        for c in self.cards:
            w = c.get("_w")
            if w is None:
                continue
            c["label"] = w.var_label.get()
            c["rgb"] = parse_color(w.var_hex.get(), c["rgb"])
            c["icon_rgba"] = w.icon_rgba
        panel.redraw()

    def set_conn(self, on, text):
        self.lbl_conn.configure(text=("●  " if on else "○  ") + text,
                                fg=(OK if on else DIM))
        self.panel.link = 1 if on else 0
        self.panel.addr = self.var_host.get() if on else ""
        self.panel.redraw()

    def _decorate_window(self):
        """窗口自己的外观：图标 + 深色标题栏。

        放在 App 里而不是 main() 里，是因为测试脚本会自己建 Tk root 直接 new App()，
        不经过 main() —— 那样跑出来的窗口会挂着 Tk 那只羽毛、顶着一条亮标题栏，
        和双击 exe 看到的不是一个东西。截图和肉眼检查都靠这个保持一致。
        """
        try:
            self.root.iconbitmap(resource_path("app.ico"))
        except Exception:
            pass
        # 窗口真正映射出来之后再改，否则 DWM 会把它覆盖回浅色
        self.root.after(10, lambda: dark_titlebar(self.root))

    def _refresh_paths(self):
        """右上角那两个字：找得到就淡灰打勾，找不到就琥珀色打叉。

        故意不写完整路径 —— 一行放不下，而且真正有用的信息只有一个：
        「烧进固件」这个按钮现在按下去会不会成功。完整路径在日志里。
        """
        for lbl, path, name in ((self.lbl_json, self.apps_json, "apps.json"),
                                (self.lbl_fw, self.fw_dir, "固件目录")):
            lbl.configure(text=("✓  " if path else "✗  ") + name,
                          fg=(DIM if path else WARN))

    # -- 日志 --------------------------------------------------------------
    def log(self, text, color=None):
        self.post(lambda: self._log_now(text, color))

    def _log_now(self, text, color):
        self.logbox.configure(state="normal")
        tag = None
        if color:
            tag = "c" + color.strip("#")
            self.logbox.tag_configure(tag, foreground=color)
        self.logbox.insert("end", time.strftime("[%H:%M:%S] ") + text + "\n", tag)
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def set_busy(self, busy, text=None):
        def go():
            state = "disabled" if busy else "normal"
            for b in (self.btn_apply, self.btn_bake, self.btn_more):
                b.configure(state=state)
            if busy:
                if self._pbar_anim is None:
                    self._pbar_pct = -1
                    self._pbar_anim = self.root.after(0, lambda: self._animate(0))
            else:
                if self._pbar_anim is not None:
                    self.root.after_cancel(self._pbar_anim)
                    self._pbar_anim = None
                self._pbar_pct = 0
                self._draw_pbar()
        self.post(go)
        if text:
            self.log(text)


    # -- 图标后台抠取 ------------------------------------------------------
    def start_icon_job(self, kind, path, widget):
        widget.set_status("正在读 %s…" % os.path.basename(path), OK)

        def done(res, err):
            if err:
                widget.set_status("抠图标失败：%s" % err, ERR)
                return
            rgba, blob, note = res
            widget.icon_rgba, widget.icon565, widget.icon_dirty = rgba, blob, True
            widget.app["icon_rgba"] = rgba
            widget.app["icon565"] = blob
            widget.app["icon_dirty"] = True
            widget.refresh()
            widget.set_status(note, OK)

        if kind == "exe":
            self.run_bg(lambda: icon_from_exe(path), done)
        else:
            def job():
                rgba, blob = icon_from_image_file(path)
                return rgba, blob, "用 %s 当了图标" % os.path.basename(path)
            self.run_bg(job, done)

    def pick_image_for(self):
        i = self.settings.get("last_slot", 0)
        if not (0 <= i < SLOTS):
            i = 0
        self.cards[i]["_w"].browse_image()
        self.settings["last_slot"] = (i + 1) % SLOTS
        save_settings(self.settings)

    def try_auto_icon(self, i):
        """本机有这个 exe 就顺手把图标抠出来当预览（不标 dirty —— 板子上多半已经是它）。"""
        card = self.cards[i]
        exe = card.get("exe") or ""
        if not exe or not os.path.exists(exe) or card.get("icon_rgba"):
            return
        widget = card["_w"]

        def done(res, err):
            if err or card.get("icon_rgba"):
                return
            rgba, blob, _note = res
            card["icon_rgba"], card["icon565"] = rgba, blob
            widget.icon_rgba, widget.icon565 = rgba, blob
            widget.refresh()

        self.run_bg(lambda: icon_from_exe(exe), done)

    # -- 本机 apps.json ----------------------------------------------------
    def pick_apps_json(self):
        p = filedialog.askopenfilename(title="选 apps.json",
                                       filetypes=[("JSON", "*.json"), ("所有文件", "*.*")])
        if p:
            self.apps_json = p
            self.settings["apps_json"] = p
            save_settings(self.settings)
            self._refresh_paths()
            self.load_local(p)
            self.log("已载入 %s" % p)

    def pick_fw_dir(self):
        p = filedialog.askdirectory(title="选 firmware 目录（里面有 platformio.ini 和 src/）")
        if p:
            self.fw_dir = p
            self.settings["firmware_dir"] = p
            save_settings(self.settings)
            self._refresh_paths()
            self.log("固件目录设为 %s" % p)

    def load_local(self, path):
        if not path:
            return
        data = load_apps_json(path)
        if not data:
            self.log("读不了 %s（JSON 坏了？）" % path, ERR)
            return
        self._json = data
        by_id = {}
        for entry in data.get("apps") or []:
            try:
                by_id[int(entry.get("id"))] = entry
            except (TypeError, ValueError):
                continue
        for card in self.cards:
            entry = by_id.get(card["id"])
            if not entry:
                continue
            card["exe"] = entry.get("path", "")
            card["args"] = entry.get("args", "")
            card["cwd"] = entry.get("cwd", "")
            if entry.get("label"):
                card["label"] = entry["label"]
            if entry.get("color"):
                card["rgb"] = parse_color(entry["color"], card["rgb"])
            w = card["_w"]
            w.var_exe.set(card["exe"])
            w.var_label.set(card["label"])
            w.var_hex.set("#%06X" % (card["rgb"] & 0xFFFFFF))
            w.refresh()
        for i in range(SLOTS):
            self.try_auto_icon(i)

    def save_local(self, cards):
        if not self.apps_json:
            return
        data = self._json if self._json is not None else load_apps_json(self.apps_json)
        if data is None:
            return
        by_id = {}
        for entry in data.get("apps") or []:
            try:
                by_id[int(entry.get("id"))] = entry
            except (TypeError, ValueError):
                continue
        changed = 0
        for c in cards:
            entry = by_id.get(c["id"])
            if not entry:
                continue
            entry["label"] = c["label"]
            entry["color"] = "#%06X" % (c["rgb"] & 0xFFFFFF)
            if c.get("exe"):
                entry["path"] = c["exe"]
                entry["name"] = os.path.splitext(os.path.basename(c["exe"]))[0]
            changed += 1
        if changed:
            try:
                with open(self.apps_json, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                self.log("已同步 %d 条到 %s" % (changed, self.apps_json), OK)
                self.log("提示：PC 端服务要重启一次才会读新的 exe 路径。")
            except OSError as exc:
                self.log("写 apps.json 失败：%s" % exc, ERR)

    # -- 按钮 --------------------------------------------------------------
    def do_read(self):
        host = self.var_host.get().strip()
        self.set_busy(True, "正在读板子…")

        def job():
            dev = Device(host)
            info = dev.connect()
            rows = dev.get()
            dev.close()
            return info, rows

        def done(res, err):
            self.set_busy(False)
            if err:
                self.set_conn(False, "没连上")
                self.log("读取失败：%s" % err, ERR)
                return
            info, rows = res
            self.set_conn(True, info["device"])
            self.log("连上了：%s，%d 个卡片位" % (info["device"], info["slots"]), OK)
            for r in rows:
                i = r["idx"]
                if not (0 <= i < SLOTS):
                    continue
                card = self.cards[i]
                card["id"] = r["id"]
                card["label"] = r["label"]
                card["rgb"] = r["rgb"]
                w = card["_w"]
                w.app["id"] = r["id"]
                w.var_label.set(r["label"])
                w.var_hex.set("#%06X" % r["rgb"])
                w.set_status("板子：id=%d%s" % (r["id"], "，有自己的图标" if r["icon_fs"] else ""),
                             OK)
                w.refresh()
            self.log("读回 %d 个卡片。" % len(rows), OK)

        self.run_bg(job, done)

    def do_apply(self):
        cards = [self.cards[i]["_w"].collect() for i in range(SLOTS)]
        host = self.var_host.get().strip()
        self.set_busy(True, "正在写入板子…")

        def job():
            dev = Device(host)
            info = dev.connect()
            lines = ["连上 %s" % info["device"]]
            n_icon = 0
            for i, c in enumerate(cards):
                dev.set_slot(i, c["id"], c["rgb"], c["label"])
                lines.append("  卡片 %d -> id=%d  %s  #%06X"
                             % (i + 1, c["id"], c["label"], c["rgb"]))
                if c.get("icon565") and c.get("icon_dirty"):
                    dev.push_icon(i, c["icon565"])
                    n_icon += 1
                    lines.append("  卡片 %d 图标已上传（%d 字节）" % (i + 1, ICON_BYTES))
                    c["icon_dirty"] = False
            dev.commit()
            dev.close()
            return lines, n_icon

        def done(res, err):
            self.set_busy(False)
            if err:
                self.log("写入失败：%s" % err, ERR)
                return
            lines, n_icon = res
            for ln in lines:
                self.log(ln)
            self.log("板子已生效（没重启），换了 %d 个图标。" % n_icon, OK)
            self.save_local(cards)

        self.run_bg(job, done)

    def do_export(self):
        cards = [self.cards[i]["_w"].collect() for i in range(SLOTS)]
        d = filedialog.askdirectory(title="导出到哪个目录")
        if not d:
            return
        try:
            write_apps_h(os.path.join(d, "apps.h"), cards)
            write_icons_h(os.path.join(d, "icons.h"), cards)
            self.log("导出到 %s：apps.h + icons.h" % d, OK)
        except Exception as exc:
            self.log("导出失败：%s" % exc, ERR)

    def do_bake(self):
        cards = [self.cards[i]["_w"].collect() for i in range(SLOTS)]
        fw = self.fw_dir or find_firmware_dir()
        if not fw:
            if messagebox.askyesno(
                    "找不到固件目录",
                    "没找到 firmware 目录（里面要有 platformio.ini 和 src/apps.h）。\n\n"
                    "现在选一个吗？"):
                self.pick_fw_dir()
            return
        self.fw_dir = fw
        self.settings["firmware_dir"] = fw
        save_settings(self.settings)
        if not messagebox.askyesno(
                "确认烧录",
                "把当前配置写成出厂默认值，并走 OTA 刷进板子？\n\n"
                "固件目录：%s\n\n"
                "大约一分钟，中间别断电。" % fw):
            return
        self.set_busy(True, "正在生成头文件…")

        out = []          # PlatformIO 的原始输出，用来在失败时给中文提示

        def job():
            src = os.path.join(fw, "src")
            # 先把没图标的格子补齐。
            # 为什么需要这一步：界面启动时是**后台异步**从各个 exe 抠图标的，
            # 手快的话可能在抠完之前就点了「烧进固件」；这时 icon_rgba 还是 None，
            # write_icons_h 会给这些格子写占位方块 —— 烧进去就把板子上的真图标顶掉了。
            missing = []
            for i, c in enumerate(cards):
                if c.get("icon_rgba"):
                    continue
                exe = (c.get("exe") or "").strip()
                if exe and os.path.exists(exe):
                    try:
                        self.log("卡片 %d 还没有图标，正在从 %s 抠…"
                                 % (i + 1, os.path.basename(exe)))
                        rgba, blob, _note = icon_from_exe(exe)
                        c["icon_rgba"], c["icon565"] = rgba, blob
                        continue
                    except Exception as exc:                 # noqa: BLE001
                        self.log("卡片 %d 抠图失败：%s" % (i + 1, exc), ERR)
                missing.append(i + 1)
            if missing:
                self.log("注意：卡片 %s 没有图标，icons.h 里会写成占位方块。"
                         % "、".join(str(x) for x in missing), ERR)
                self.log("      想保住板子上现有的图标，先给这几张卡选个程序或图片再烧。",
                         ERR)
            write_apps_h(os.path.join(src, "apps.h"), cards)
            write_icons_h(os.path.join(src, "icons.h"), cards)
            self.log("已重写 src/apps.h 和 src/icons.h")
            cmd = [python_exe(), "-m", "platformio", "run", "-e", "ota", "-t", "upload"]
            self.log("$ " + " ".join(cmd))
            p = subprocess.Popen(cmd, cwd=fw, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True,
                                 encoding="utf-8", errors="replace")
            for line in p.stdout:
                line = line.rstrip()
                if line:
                    out.append(line)
                    self.log("  " + line)
            return p.wait()

        def done(code, err):
            self.set_busy(False)
            if err:
                self.log("烧录失败：%s" % err, ERR)
                return
            if code == 0:
                self.log("OTA 成功，板子已在跑新固件。", OK)
                return
            self.log("PlatformIO 退出码 %s —— 看上面的输出。" % code, ERR)
            blob = "\n".join(out)
            if "另一个程序正在使用此文件" in blob:
                self.log("提示：上面说文件被占用，多半是你自己另开了一个终端在跑 "
                         "platformio，或者上次编译的进程还没退干净。关掉它再点一次就行。",
                         ERR)
            elif "Could not open" in blob and "port" in blob.lower():
                self.log("提示：这次走的是 OTA（UDP 3232），不该碰串口；"
                         "看到端口错误说明 upload_protocol 被改过了。", ERR)

        self.run_bg(job, done)

    def do_clear(self):
        if not messagebox.askyesno("恢复出厂",
                                   "抹掉板子上所有自定义配置，回到编译期的默认值？"):
            return
        host = self.var_host.get().strip()
        self.set_busy(True, "正在恢复出厂…")

        def job():
            dev = Device(host)
            dev.connect()
            dev.clear()
            dev.close()

        def done(_res, err):
            self.set_busy(False)
            if err:
                self.log("恢复出厂失败：%s" % err, ERR)
            else:
                self.log("板子已恢复出厂默认值。", OK)

        self.run_bg(job, done)


def _attach_console():
    """--windowed 打出来的 exe 自己没有控制台，stdout 是 None。

    从 cmd / PowerShell 里跑 `--selftest` 时借一下父进程的控制台，结果才看得见；
    双击运行时没有父控制台，这里静默失败即可。
    """
    if os.name != "nt" or sys.stdout is not None:
        return
    try:
        import ctypes
        if ctypes.windll.kernel32.AttachConsole(-1):        # ATTACH_PARENT_PROCESS
            sys.stdout = open("CONOUT$", "w", encoding="utf-8",
                              errors="replace", buffering=1)
            sys.stderr = sys.stdout
    except Exception:
        pass


def _selftest(out_path=None):
    """打包成 exe 之后用 `ESP32-Launcher-Configurator.exe --selftest` 自检。

    重点不是再测一遍逻辑（那是 _selftest.py 的事），而是证明**这个 exe 是完整的**：
    tkinter 的 tcl/tk 运行库被正确收进来了没有 —— 这是 PyInstaller + tkinter 最常见的翻车点，
    打包时不报错、双击才弹 "Failed to execute script"。

    `--selftest <文件>` 会把报告另存一份，方便没有控制台时查看（也方便自动化测试）。
    """
    import tempfile

    _attach_console()
    lines = []
    bad = []

    def say(s):
        lines.append(s)

    def ck(name, cond, extra=""):
        say("%-44s %s %s" % (name, "OK  " if cond else "FAIL", extra))
        if not cond:
            bad.append(name)

    say("ESP32 启动器配置器  自检")
    say("python  %s" % sys.version.split()[0])
    say("打包运行 = %s" % bool(getattr(sys, "frozen", False)))
    say("程序位置 = %s" % app_dir())
    say("-" * 60)

    ck("tools 里的两个模块已打包",
       hasattr(extract_icon, "extract") and hasattr(mk_icons, "main"))
    ck("parse_color", parse_color("#4C8BF5") == 0x4C8BF5)
    ck("mix_rgb 与固件同式",
       mix_rgb(C_TILE, (0x4C, 0x8B, 0xF5), 90) == (41, 67, 109))
    ck("placeholder_rgba",
       len(placeholder_rgba(0xFF0000)) == ICON_W * ICON_H * 4)

    with tempfile.TemporaryDirectory() as td:
        cards = [{"idx": i, "id": sid, "label": label, "rgb": rgb, "glyph": g,
                  "exe": "", "icon_rgba": None, "icon565": None, "icon_dirty": False}
                 for i, (sid, label, rgb, g) in enumerate(DEFAULTS)]
        ap = os.path.join(td, "apps.h")
        ic = os.path.join(td, "icons.h")
        write_apps_h(ap, cards)
        write_icons_h(ic, cards)
        ck("生成 apps.h", os.path.getsize(ap) > 400,
           "%d 字节" % os.path.getsize(ap))
        ck("生成 icons.h", os.path.getsize(ic) > 150000,
           "%d 字节" % os.path.getsize(ic))

    try:
        root = tk.Tk()
        root.withdraw()
        App(root)
        root.update()
        root.destroy()
        ck("tkinter 能开窗（tcl/tk 已收进 exe）", True)
    except Exception as exc:                       # pragma: no cover
        ck("tkinter 能开窗（tcl/tk 已收进 exe）", False, repr(exc))

    say("-" * 60)
    if bad:
        say("自检失败 %d 项：%s" % (len(bad), "、".join(bad)))
    else:
        say("自检全部通过，这个 exe 可以直接双击用。")

    text = "\n".join(lines) + "\n"
    if sys.stdout is not None:
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except Exception:
            pass
    if out_path:
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception as exc:
            print("写不了 %s：%s" % (out_path, exc))
    return 1 if bad else 0


def main():
    argv = sys.argv[1:]
    if "--selftest" in argv:
        i = argv.index("--selftest")
        out_path = argv[i + 1] if len(argv) > i + 1 and not argv[i + 1].startswith("-") else None
        return _selftest(out_path)
    if "--ip" in argv:
        try:
            ip = argv[argv.index("--ip") + 1]
        except IndexError:
            _attach_console()
            print("--ip 后面要跟一个地址")
            return 2
    else:
        ip = ""

    root = tk.Tk()
    try:
        # 只在系统缩放偏小时补一点，避免在高 DPI 机器上叠两次变大。
        if float(root.call("tk", "scaling")) < 1.2:
            root.call("tk", "scaling", 1.2)
    except (tk.TclError, ValueError):
        pass
    app = App(root)
    if ip:
        app.var_host.set(ip)
    root.mainloop()
    return 0


def resource_path(name):
    """找一个随程序一起发布的数据文件。

    打包成 onefile 之后，PyInstaller 会把数据解到 sys._MEIPASS，源码运行时就在
    脚本旁边 —— 两种跑法都要能找到，所以按这个顺序试。
    """
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


def dark_titlebar(win):
    """把系统标题栏也刷成深色。

    不做这件事的话，一块浅灰标题栏压在通体深色的界面上，看着像两个程序拼起来的。
    只在 Windows 上做；DWM 没这个属性（Win10 1809 以前）就安静放弃 —— 标题栏丑一点
    远好过因为一行 ctypes 让整个程序打不开。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
        on = ctypes.c_int(1)
        for attr in (20, 19):        # 20 = Win10 1809+，19 = 更早的预览版写法
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                return True
    except Exception:
        pass
    return False


if __name__ == "__main__":
    sys.exit(main())
