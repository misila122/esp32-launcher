#!/usr/bin/env python3
"""远程 ZIP 目录窥探：只下载 ZIP 尾部中央目录，列出文件名。

用途：判断 espressif 的 arduino-esp32 发布包（dl.espressif.com，28 MB/s）
      里是否带 PlatformIO 需要的 tools/platformio-build.py。
      如果有，就能用 254MB/10秒 的快源替代 235MB 的慢源。
"""
import io
import sys
import urllib.request

URL = ("https://dl.espressif.com/github_assets/espressif/arduino-esp32/"
       "releases/download/2.0.17/esp32-2.0.17.zip")


class HttpFile:
    """只读、可通过 HTTP Range 随机访问的远端文件（zipfile 只要求 read/seek/tell）。"""

    def __init__(self, url):
        self.url = url
        self.pos = 0
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "x"})
        with urllib.request.urlopen(req, timeout=30) as r:
            self.size = int(r.headers["Content-Length"])
            self.ranges = r.headers.get("Accept-Ranges")
        self.bytes_read = 0

    def seek(self, off, whence=0):
        if whence == 0:
            self.pos = off
        elif whence == 1:
            self.pos += off
        else:
            self.pos = self.size + off
        return self.pos

    def tell(self):
        return self.pos

    def seekable(self):
        return True

    def readable(self):
        return True

    def close(self):
        pass

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = min(n, self.size - self.pos)
        if n <= 0:
            return b""
        end = self.pos + n - 1
        rq = urllib.request.Request(
            self.url, headers={"User-Agent": "x",
                               "Range": f"bytes={self.pos}-{end}"})
        with urllib.request.urlopen(rq, timeout=90) as r:
            b = r.read()
        self.pos += len(b)
        self.bytes_read += len(b)
        return b


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else URL
    f = HttpFile(url)
    print(f"remote size: {f.size/1048576:.2f} MB  accept-ranges={f.ranges}")
    z = __import__("zipfile").ZipFile(f)
    names = z.namelist()
    print(f"entries: {len(names)}")
    print("first 5:", names[:5])
    hits = [n for n in names if "platformio" in n.lower()]
    print("platformio-related entries:", hits)
    for key in ("tools/", "sdkconfig", "package.json", "platform.txt"):
        m = [n for n in names if key in n][:6]
        print(f"  ~{key}: {m}")
    print(f"network used: {f.bytes_read/1048576:.2f} MB")


if __name__ == "__main__":
    main()
