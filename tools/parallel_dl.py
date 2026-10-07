#!/usr/bin/env python3
"""多连接并行下载器。

用途：PlatformIO 官方源在这台机器上单连接只有 ~20-40 KB/s，
      但多开连接能提到 ~100 KB/s 以上（实测 8 连接约 2.3 倍）。
      这个脚本把大文件切成 N 段并发下载，每段独立重试续传。

用法：
    python parallel_dl.py <url> <outfile> [connections]
"""
import os
import socket
import sys
import threading
import time
import urllib.request

socket.setdefaulttimeout(30)
UA = {"User-Agent": "Mozilla/5.0 (parallel-dl)"}


def head(url):
    req = urllib.request.Request(url, method="HEAD", headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return int(r.headers.get("Content-Length") or 0), r.headers.get("Accept-Ranges")


def fetch_range(url, start, end, out, idx, part_dir, progress, lock):
    """下载 [start, end] 闭区间，支持续传。"""
    part = os.path.join(part_dir, f"part{idx:03d}")
    for attempt in range(1, 11):
        have = os.path.getsize(part) if os.path.exists(part) else 0
        want = end - start + 1
        if have >= want:
            with lock:
                progress[idx] = want
            return True
        try:
            hdr = dict(UA)
            hdr["Range"] = f"bytes={start + have}-{end}"
            req = urllib.request.Request(url, headers=hdr)
            with urllib.request.urlopen(req, timeout=45) as r, open(part, "ab") as f:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    have += len(chunk)
                    with lock:
                        progress[idx] = have
                    if have >= want:
                        break
        except Exception as e:
            with lock:
                progress[idx] = os.path.getsize(part) if os.path.exists(part) else 0
            time.sleep(min(2 ** attempt * 0.3, 8))
            continue
    return os.path.getsize(part) >= (end - start + 1) if os.path.exists(part) else False


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    url = sys.argv[1]
    out = sys.argv[2]
    conns = int(sys.argv[3]) if len(sys.argv) > 3 else 16

    total, ranges = head(url)
    if not total:
        sys.exit("server did not report Content-Length")
    print(f"size={total/1048576:.2f} MB  accept-ranges={ranges}  connections={conns}")

    part_dir = out + ".parts"
    os.makedirs(part_dir, exist_ok=True)

    # 每段 1MB，避免最后一段太小
    chunk = max(1 << 20, total // conns)
    tasks = []
    pos = 0
    while pos < total:
        end = min(pos + chunk - 1, total - 1)
        tasks.append((pos, end))
        pos = end + 1
    print(f"segments: {len(tasks)} x ~{chunk/1048576:.1f} MB")

    progress = {i: 0 for i in range(len(tasks))}
    lock = threading.Lock()
    stop = threading.Event()

    def reporter():
        t0 = time.time()
        while not stop.wait(2.0):
            done = sum(progress.values())
            dt = time.time() - t0
            pct = done / total * 100
            rate = done / dt if dt else 0
            eta = (total - done) / rate if rate else 0
            print(f"  {pct:5.1f}%  {done/1048576:7.2f}/{total/1048576:.2f} MB  "
                  f"{rate/1024:7.1f} KB/s  ETA {eta:5.0f}s", flush=True)

    rep = threading.Thread(target=reporter, daemon=True)
    rep.start()

    threads = []
    for i, (s, e) in enumerate(tasks):
        t = threading.Thread(target=fetch_range,
                             args=(url, s, e, out, i, part_dir, progress, lock))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    stop.set()

    missing = [i for i in range(len(tasks)) if progress[i] < tasks[i][1] - tasks[i][0] + 1]
    if missing:
        sys.exit(f"FAILED: segments incomplete: {missing[:10]}")

    print("merging...")
    with open(out, "wb") as dst:
        for i in range(len(tasks)):
            p = os.path.join(part_dir, f"part{i:03d}")
            with open(p, "rb") as src:
                while True:
                    b = src.read(1 << 20)
                    if not b:
                        break
                    dst.write(b)
    got = os.path.getsize(out)
    print(f"DONE {out}  {got/1048576:.2f} MB  (expected {total/1048576:.2f} MB)"
          f"  {'OK' if got == total else 'SIZE MISMATCH'}")
    import shutil
    shutil.rmtree(part_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
