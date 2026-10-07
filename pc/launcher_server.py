#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ESP32 触摸屏启动器 —— PC 端常驻服务
=====================================

角色分工（重要，决定了为什么不用配防火墙）:

    ESP32  = TCP 服务端 (监听 8266) + UDP 发现响应 (8267)
    PC     = TCP 客户端，主动连 ESP32

因为连接是 PC 主动往外发起的，Windows 防火墙不会拦（出站默认放行、
回包走状态化放行），所以**不需要管理员权限、不需要加防火墙规则**。

协议（UTF-8，一行一条，\\n 结尾）:
    PC  -> ESP : HELLO <token> <hostname>
    ESP -> PC  : WELCOME <device_name>
    ESP -> PC  : LAUNCH <id>
    PC  -> ESP : OK <id> <name>     启动成功（或已在前台）
    PC  -> ESP : ERR <id> <reason>  失败
    PC  -> ESP : PING               每 20 秒心跳
    ESP -> PC  : PONG

用法:
    python launcher_server.py                # 正常运行（常驻，自动发现并重连）
    python launcher_server.py --list         # 只打印配置，不联网
    python launcher_server.py --once 3       # 不联网，直接启动 id=3 的程序（测试用）
    python launcher_server.py --config x.json
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------

def _pick_log_dir() -> Path:
    """挑一个"写入后真的能在那个路径找到文件"的日志目录。

    坑：微软商店版 Python 是 MSIX 打包应用，往 %LOCALAPPDATA% 写东西会被系统
    静默重定向到
        %LOCALAPPDATA%\\Packages\\PythonSoftwareFoundation.Python.3.12_*\\LocalCache\\Local\\
    于是日志实际落在那个沙箱里，用户照着提示路径去找会扑空。
    所以优先放在脚本旁边的 logs\\，只写一个探针文件确认真的可写、真的可见。
    """
    here = Path(__file__).resolve().parent
    candidates = [here / "logs"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "esp32-launcher")
    candidates.append(Path(tempfile.gettempdir()) / "esp32-launcher")
    for cand in candidates:
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            # 再确认从"这个路径"真能看到它（防止被重定向）
            if cand.is_dir():
                return cand
        except OSError:
            continue
    return Path(tempfile.gettempdir())


LOG_DIR = _pick_log_dir()
LOG_FILE = LOG_DIR / "agent.log"
_log_lock = threading.Lock()
_log_warned = False


def _elevation_label() -> str:
    """服务自己是否以管理员身份运行 —— 决定点需要提权的程序时会不会弹 UAC。"""
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return "管理员（点需要提权的程序不会弹 UAC）"
    except Exception:
        pass
    return "普通用户（点需要提权的程序时会弹一次 UAC，点「是」即可）"


def log(msg: str) -> None:
    global _log_warned
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    with _log_lock:
        print(line, flush=True)
        try:
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > 2 * 1024 * 1024:
                LOG_FILE.replace(LOG_FILE.with_suffix(".log.1"))
            with LOG_FILE.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            # 别静默吞掉：日志写不了是要让人知道的
            if not _log_warned:
                _log_warned = True
                print(f"[warn] 日志文件写不进去 ({LOG_FILE}): {exc}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Windows 窗口操作（纯 ctypes，不依赖 pywin32）
# --------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

SW_RESTORE = 9
SW_SHOW = 5
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM), wt.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsIconic.argtypes = [wt.HWND]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.BringWindowToTop.argtypes = [wt.HWND]
user32.GetForegroundWindow.restype = wt.HWND
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
user32.GetWindow.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetAncestor.restype = wt.HWND
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)
]
kernel32.CloseHandle.argtypes = [wt.HANDLE]


def _pid_image_path(pid: int) -> str:
    """拿某个 pid 的完整 exe 路径；拿不到返回空串。"""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wt.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(h)


def _window_title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _score_path(candidate: str, target: str) -> int:
    """给"这个进程是不是目标程序"打分。0 = 不相关。

    分层的理由：很多程序（聊天软件、Win11 记事本、各种 launcher）配置里写的
    那个 exe 只是引导壳，真正持有窗口的进程在版本子目录里、路径完全不同，
    但 basename 一样。所以路径全等优先，basename 相同次之。
    """
    c = os.path.normcase(os.path.abspath(candidate)) if candidate else ""
    t = os.path.normcase(os.path.abspath(target))
    if not c:
        return 0
    if c == t:
        return 100
    if os.path.basename(c) != os.path.basename(t):
        return 0
    cd, td = os.path.dirname(c), os.path.dirname(t)
    if cd == td:
        return 90
    # 子目录 / 父目录关系（...\Weixin\4.1.15\Weixin.exe vs ...\Weixin\Weixin.exe）
    if cd.startswith(td + os.sep) or td.startswith(cd + os.sep):
        return 85
    if os.path.splitdrive(c)[0] == os.path.splitdrive(t)[0]:
        return 70
    return 60


def find_windows_for_exe(exe_path: str, min_score: int = 60) -> list[tuple[int, str]]:
    """返回 [(hwnd, title)] —— 所有"看起来属于该程序"的可见顶层窗口。"""
    found: list[tuple[int, str]] = []
    pids_cache: dict[int, str] = {}

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _window_title(hwnd)
        if not title:
            return True  # 隐藏的工具窗/托盘窗，跳过
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return True
        if pid.value not in pids_cache:
            pids_cache[pid.value] = _pid_image_path(pid.value)
        if _score_path(pids_cache[pid.value], exe_path) >= min_score:
            found.append((hwnd, title))
        return True

    user32.EnumWindows(_cb, 0)
    return found


def best_window(exe_path: str) -> int | None:
    """挑出最像主窗口的那个 hwnd：优先路径全等的进程，其次标题最长的。"""
    wins = find_windows_for_exe(exe_path)
    if not wins:
        return None
    pids: dict[int, str] = {}
    best: tuple[tuple[int, int], int] | None = None
    for hwnd, title in wins:
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids:
            pids[pid.value] = _pid_image_path(pid.value)
        rank = (_score_path(pids[pid.value], exe_path), len(title))
        if best is None or rank > best[0]:
            best = (rank, hwnd)
    return best[1] if best else None


def _shell_execute_runas(exe: str, params: str, cwd: str) -> int:
    """用 ShellExecuteW 的 "runas" 动词启动程序。

    有些程序的清单里声明了 requireAdministrator，CreateProcess
    直接起会返回 WinError 740。ShellExecute 的 runas 动词会走 UAC 通道：
    调用方本身已经是管理员时不会弹窗，否则弹一次 UAC。
    返回值 >32 表示成功，<=32 是 ShellExecute 的错误码。
    """
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    except OSError:
        return 0
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    shell32.ShellExecuteW.argtypes = [
        wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_int
    ]
    hwnd = None                       # 不指定父窗口
    verb = "runas"
    rc = shell32.ShellExecuteW(hwnd, verb, exe, params or None, cwd or None, 1)
    return int(rc or 0)


def force_foreground(hwnd: int) -> None:
    """把窗口拉到最前并抢到焦点（绕过 Windows 的前台锁）。"""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    else:
        user32.ShowWindow(hwnd, SW_SHOW)

    fg = user32.GetForegroundWindow()
    tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    tid_me = kernel32.GetCurrentThreadId()
    attached = False
    if tid_fg and tid_fg != tid_me:
        attached = bool(user32.AttachThreadInput(tid_fg, tid_me, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(tid_fg, tid_me, False)


# --------------------------------------------------------------------------
# 启动逻辑
# --------------------------------------------------------------------------


class Launcher:
    def __init__(self, config: dict):
        self.config = config
        self.options = config.get("options", {}) or {}
        self.apps: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._activate_lock = threading.Lock()   # 同一时刻只处理一次点击
        self._last_launch: dict[int, float] = {}  # 防"启动很慢 -> 又点一次 -> 开两个"
        self.reload()

    # -- 配置 ---------------------------------------------------------------
    def reload(self) -> None:
        with self._lock:
            self.apps = {int(a["id"]): a for a in self.config.get("apps", [])}

    def get(self, app_id: int) -> dict | None:
        with self._lock:
            return self.apps.get(app_id)

    # -- 动作 ---------------------------------------------------------------
    def activate(self, app_id: int) -> tuple[bool, str]:
        app = self.get(app_id)
        if not app:
            return False, f"unknown id {app_id}"

        exe = app["path"]
        label = app.get("name") or app.get("label") or os.path.basename(exe)

        if not Path(exe).exists():
            return False, f"file not found: {exe}"

        with self._activate_lock:
            focus_first = self.options.get("focus_if_running", True)

            # 1) 已经在跑 -> 拉到前台，绝不重复启动
            if focus_first:
                hwnd = best_window(exe)
                if hwnd:
                    force_foreground(hwnd)
                    return True, f"{label} (focused)"

            # 2) 刚启动过但窗口还没出来（聊天软件这类要好几秒）
            #    -> 等它，而不是再开一个
            since = time.time() - self._last_launch.get(app_id, 0.0)
            if since < 10.0:
                for _ in range(16):
                    time.sleep(0.5)
                    if focus_first:
                        hwnd = best_window(exe)
                        if hwnd:
                            force_foreground(hwnd)
                            return True, f"{label} (focused after start)"
                return True, f"{label} (still starting)"

            # 3) 启动
            cwd = app.get("cwd") or os.path.dirname(exe)
            args = app.get("args") or []
            cmd = [exe] + [str(a) for a in args]
            creationflags = 0
            if os.name == "nt":
                creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            try:
                subprocess.Popen(
                    cmd,
                    cwd=cwd,
                    creationflags=creationflags,
                    close_fds=True,
                )
            except OSError as exc:
                # WinError 740 = ERROR_ELEVATION_REQUIRED
                # 有些启动器的清单里写着 requireAdministrator，
                # 用 CreateProcess 直接起会失败。退回 ShellExecute("runas")：
                # 如果本服务本身已经是管理员权限，这一步不会弹 UAC；
                # 如果没提权，用户会看到一次 UAC 弹窗（总比点不动强）。
                if getattr(exc, "winerror", None) != 740:
                    return False, f"spawn failed: {exc}"
                rc = _shell_execute_runas(exe, " ".join(str(a) for a in args), cwd)
                if rc <= 32:
                    return False, f"spawn failed (needs admin, ShellExecute rc={rc}): {exc}"
                log(f"  launched elevated via ShellExecute: {label}  <- {exe}")
            self._last_launch[app_id] = time.time()
            log(f"  launched: {label}  <- {exe}")

            # 4) 等窗口出现并顺手切前台（窗口没等到也算启动成功）
            for _ in range(16):
                time.sleep(0.5)
                if focus_first:
                    hwnd = best_window(exe)
                    if hwnd:
                        force_foreground(hwnd)
                        return True, f"{label} (started + focused)"
            return True, f"{label} (started)"


# --------------------------------------------------------------------------
# 设备发现 + 长连接
# --------------------------------------------------------------------------


class DeviceLink:
    def __init__(self, launcher: Launcher):
        dev = launcher.config.get("device", {}) or {}
        self.tcp_port = int(dev.get("tcp_port", 8266))
        self.udp_port = int(dev.get("udp_port", 8267))
        self.hostname = (dev.get("hostname") or "").strip()
        self.fixed_ip = (dev.get("ip") or "").strip()
        self.token = dev.get("token") or ""
        self.launcher = launcher
        self.host = ""
        # 设备每 ~20s 发一次 PING，链路静默超过 45s 视为已断
        self.idle_timeout = float(dev.get("idle_timeout", 45))
        self.ping_interval = float(dev.get("ping_interval", 20))
        self._tx_lock = threading.Lock()

    # -- 发现 ---------------------------------------------------------------
    def discover(self) -> str:
        for candidate, why in self._candidates():
            if self._probe(candidate):
                log(f"device found via {why}: {candidate}")
                return candidate
        return ""

    def _candidates(self):
        if self.fixed_ip:
            yield self.fixed_ip, "config ip"
        if self.hostname:
            yield self.hostname, "mDNS"
            for suffix in (".", ""):
                if not self.hostname.endswith("."):
                    yield self.hostname + suffix, "mDNS"

    def _probe(self, host: str) -> bool:
        try:
            with socket.create_connection((host, self.tcp_port), timeout=2.0):
                return True
        except OSError:
            return False

    def udp_broadcast(self) -> str:
        """兜底发现：广播一问，ESP32 一答，顺便让防火墙放行回包。"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.settimeout(1.5)
        except OSError:
            return ""
        try:
            # 除了受限广播，再补一个子网定向广播 —— 有些路由/系统会
            # 直接把 255.255.255.255 丢掉。本机地址用一个「连出去」的
            # UDP socket 问内核要，纯标准库、不真发包。
            targets = ["255.255.255.255"]
            try:
                probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    probe.connect(("8.8.8.8", 53))
                    local_ip = probe.getsockname()[0]
                finally:
                    probe.close()
                if local_ip and not local_ip.startswith("127."):
                    targets.append(local_ip.rsplit(".", 1)[0] + ".255")
            except OSError:
                pass
            for target in targets:
                try:
                    s.sendto(b"ESP32LAUNCHER?", (target, self.udp_port))
                except OSError:
                    continue
            deadline = time.time() + 2.0
            while time.time() < deadline:
                try:
                    data, addr = s.recvfrom(256)
                except socket.timeout:
                    break
                except OSError:
                    break
                if data.startswith(b"ESP32LAUNCHER"):
                    log(f"device answered broadcast: {addr[0]}")
                    return addr[0]
        finally:
            s.close()
        return ""

    def resolve(self) -> str:
        if self.host and self._probe(self.host):
            return self.host
        self.host = self.discover()
        if not self.host:
            self.host = self.udp_broadcast()
        return self.host

    # -- 会话 ---------------------------------------------------------------
    @staticmethod
    def _enable_keepalive(sock: socket.socket) -> None:
        """ESP32 掉电/复位时不会发 FIN，没有 keepalive 就会永久阻塞在 recv 上。"""
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if hasattr(socket, "SIO_KEEPALIVE_VALS"):  # Windows
                # (开启, 空闲 10s 后开始探测, 每 3s 一次) -> 约 13~16s 判死
                sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 10000, 3000))
            if hasattr(socket, "TCP_KEEPIDLE"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 10)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 3)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        except OSError:
            pass

    def _send(self, sock: socket.socket, text: str) -> None:
        data = text.encode("utf-8", "replace")
        with self._tx_lock:
            sock.sendall(data)

    def session(self, sock: socket.socket) -> None:
        """一次完整的设备会话。返回即代表链路已断，由 run_forever 重连。"""
        self._enable_keepalive(sock)
        sock.settimeout(1.0)  # 只为周期性醒来检查心跳/静默，不代表链路超时
        self._send(sock, f"HELLO {self.token} {socket.gethostname()}\n")
        log(f"connected to {self.host}:{self.tcp_port}, HELLO sent")

        buf = b""
        now = time.time()
        last_rx = now
        last_tx = now
        while True:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                chunk = None
            except OSError as exc:
                log(f"device connection lost: {exc}")
                return

            if chunk == b"":
                log("device closed the connection")
                return

            now = time.time()
            if chunk:
                last_rx = now
                buf += chunk
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    self._on_line(sock, line)

            # 设备静默过久：即便 keepalive 没判死，也主动断开重连
            if now - last_rx > self.idle_timeout:
                log(f"device silent for {self.idle_timeout:.0f}s, reconnecting")
                return

            if now - last_tx > self.ping_interval:
                try:
                    self._send(sock, "PING\n")
                except OSError:
                    return
                last_tx = now

    def _on_line(self, sock: socket.socket, line: bytes) -> None:
        text = line.decode("utf-8", "replace").strip()
        if not text:
            return
        log(f"  <- {text}")
        verb, _, rest = text.partition(" ")
        verb = verb.upper()

        if verb == "LAUNCH":
            try:
                app_id = int(rest.strip())
            except ValueError:
                self._send(sock, "ERR 0 bad id\n")
                return
            threading.Thread(
                target=self._handle_launch, args=(sock, app_id), daemon=True
            ).start()
        elif verb == "PING":
            self._send(sock, "PONG\n")
        elif verb in ("PONG", "WELCOME", "OK", "ERR"):
            pass
        else:
            log(f"  (ignored unknown command: {text})")

    def _handle_launch(self, sock: socket.socket, app_id: int) -> None:
        ok, detail = self.launcher.activate(app_id)
        msg = f"OK {app_id} {detail}\n" if ok else f"ERR {app_id} {detail}\n"
        log(f"  -> {msg.strip()}")
        try:
            self._send(sock, msg)
        except OSError:
            pass

    # -- 主循环 -------------------------------------------------------------
    def run_forever(self) -> None:
        backoff = 2.0
        while True:
            try:
                host = self.resolve()
                if not host:
                    log(f"no device on the network yet, retry in {backoff:.0f}s "
                        f"(target {self.hostname or self.fixed_ip}:{self.tcp_port})")
                    time.sleep(backoff)
                    backoff = min(backoff * 1.6, 15.0)
                    continue
                with socket.create_connection((host, self.tcp_port), timeout=5.0) as sock:
                    backoff = 2.0
                    self.session(sock)
            except OSError as exc:
                log(f"link error: {exc}; retry in {backoff:.0f}s")
            except Exception as exc:  # noqa: BLE001 - 常驻服务不能死
                log(f"unexpected error: {exc!r}; retry in {backoff:.0f}s")
            self.host = ""
            time.sleep(backoff)
            backoff = min(backoff * 1.6, 15.0)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def load_config(path: Path) -> dict:
    # utf-8-sig：记事本/PowerShell 存出来的 JSON 常带 BOM，直接 utf-8 读会炸
    with path.open("r", encoding="utf-8-sig") as fh:
        raw = fh.read()
    return json.loads(raw)


def main(argv: list[str] | None = None) -> int:
    # Windows 上 stdout 默认走本地代码页(cp936)，中文日志会变乱码/丢字
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="ESP32 触摸屏启动器 · PC 端服务")
    ap.add_argument("--config", default=str(here / "apps.json"))
    ap.add_argument("--list", action="store_true", help="打印配置后退出")
    ap.add_argument("--once", type=int, metavar="ID", help="直接启动某个 id（不联网，测试用）")
    args = ap.parse_args(argv)

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"配置文件不存在: {cfg_path}", file=sys.stderr)
        return 2
    try:
        config = load_config(cfg_path)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"配置文件解析失败: {exc}", file=sys.stderr)
        return 2

    launcher = Launcher(config)

    if args.list:
        print(f"配置文件: {cfg_path}")
        for app in config.get("apps", []):
            ok = "OK " if Path(app["path"]).exists() else "MISS"
            print(f"  [{ok}] id={app['id']:<2} {app.get('name',''):<14} {app['path']}")
        return 0

    if args.once is not None:
        ok, detail = launcher.activate(args.once)
        print(("OK  " if ok else "ERR ") + detail)
        return 0 if ok else 1

    dev = config.get("device", {}) or {}
    log("=" * 62)
    log("ESP32 触摸屏启动器 · PC 端服务已启动")
    log(f"  配置    : {cfg_path}")
    log(f"  目标设备: {dev.get('hostname') or dev.get('ip') or '?'}:{dev.get('tcp_port')}")
    log(f"  程序数  : {len(config.get('apps', []))}")
    log(f"  权限    : {_elevation_label()}")
    log(f"  日志    : {LOG_FILE}")
    log("=" * 62)

    try:
        DeviceLink(launcher).run_forever()
    except KeyboardInterrupt:
        log("收到 Ctrl+C，退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
