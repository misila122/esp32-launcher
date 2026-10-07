#!/usr/bin/env python3
"""Extract the best application icon out of a Windows PE (.exe/.dll).

Pure standard library -- walks the PE resource directory for RT_GROUP_ICON
(type 14) / RT_ICON (type 3), picks the largest frame (preferring 32bpp),
and writes it out as a standalone file:

  * if the frame is PNG-compressed (Vista+ 256x256 icons) -> .png
  * otherwise it is a DIB -> wrapped into a valid single-frame .ico

Usage:
    python extract_icon.py <exe> <out_dir> [name]
"""
import collections
import os
import struct
import sys

RT_ICON = 3
RT_GROUP_ICON = 14

# GRPICONDIRENTRY. Note the last two fields: dwBytesInRes comes BEFORE nID.
_GrpEntry = collections.namedtuple(
    "_GrpEntry", "w h colors reserved planes bits nbytes rid")

_GRP_ENTRY = struct.Struct("<BBBBHHIH")   # 14 bytes


def _parse_pe(data):
    """Return (resource_rva, resource_size, sections)."""
    if data[:2] != b"MZ":
        raise ValueError("not a PE file (no MZ)")
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if data[e_lfanew:e_lfanew + 4] != b"PE\0\0":
        raise ValueError("not a PE file (no PE signature)")

    coff = e_lfanew + 4
    num_sections = struct.unpack_from("<H", data, coff + 2)[0]
    opt_size = struct.unpack_from("<H", data, coff + 16)[0]
    opt = coff + 20
    magic = struct.unpack_from("<H", data, opt)[0]
    pe32_plus = magic == 0x20B

    # DataDirectory starts after the fixed part of the optional header.
    dd_off = opt + (112 if pe32_plus else 96)
    res_rva, res_size = struct.unpack_from("<II", data, dd_off + 2 * 8)

    sections = []
    sec = opt + opt_size
    for i in range(num_sections):
        b = data[sec + i * 40: sec + i * 40 + 40]
        vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", b, 8)
        sections.append((vaddr, vsize, rawptr, rawsize))
    return res_rva, res_size, sections


def _rva_to_off(rva, sections):
    for vaddr, vsize, rawptr, rawsize in sections:
        if vaddr <= rva < vaddr + max(vsize, rawsize):
            return rawptr + (rva - vaddr)
    return None


def _walk(data, root, dir_off, sections, level=0, type_id=None, name_id=None):
    """Yield (type_id, name_id, lang_id, payload) for every leaf.

    `root` is the file offset of the resource directory root and never
    changes; `dir_off` is where the directory being read lives. Both the
    subdirectory pointers and the leaf IMAGE_RESOURCE_DATA_ENTRY offsets are
    relative to the ROOT, not to the enclosing directory -- mixing the two up
    sends the walk megabytes past the end of the resource table.
    """
    n_named, n_id = struct.unpack_from("<HH", data, root + dir_off + 12)
    count = n_named + n_id
    for i in range(count):
        ent = root + dir_off + 16 + i * 8
        nm, off = struct.unpack_from("<II", data, ent)
        cur_type = type_id
        cur_name = name_id
        if level == 0:
            cur_type = nm & 0x7FFFFFFF
        elif level == 1:
            cur_name = nm & 0x7FFFFFFF
        if off & 0x80000000:
            yield from _walk(data, root, off & 0x7FFFFFFF, sections,
                             level + 1, cur_type, cur_name)
        else:
            data_rva, size = struct.unpack_from("<II", data, root + off)
            file_off = _rva_to_off(data_rva, sections)
            if file_off is not None and file_off + size <= len(data):
                yield cur_type, cur_name, nm, data[file_off:file_off + size]


def _build_ico(entries, images):
    """entries: list of _GrpEntry; images: {resource_id: bytes}."""
    picked = [(e, images[e.rid]) for e in entries if e.rid in images]
    if not picked:
        raise ValueError("no RT_ICON payloads for the group")

    header = struct.pack("<HHH", 0, 1, len(picked))
    offset = len(header) + 16 * len(picked)
    dirs, blobs = b"", b""
    for e, blob in picked:
        dirs += struct.pack("<BBBBHHII", e.w, e.h, e.colors, 0,
                            e.planes, e.bits, len(blob), offset)
        blobs += blob
        offset += len(blob)
    return header + dirs + blobs


def extract(exe_path, out_dir, name=None):
    data = open(exe_path, "rb").read()
    res_rva, res_size, sections = _parse_pe(data)
    if not res_rva:
        raise ValueError("no resource directory")
    base = _rva_to_off(res_rva, sections)

    groups, icons = {}, {}
    for type_id, name_id, _lang, payload in _walk(data, base, 0, sections):
        if type_id == RT_GROUP_ICON:
            groups.setdefault(name_id, payload)
        elif type_id == RT_ICON:
            icons.setdefault(name_id, payload)

    if not groups:
        raise ValueError("no RT_GROUP_ICON resources")

    # Pick the group with the biggest frame available.
    best = None
    for gid, payload in groups.items():
        _res, _type, cnt = struct.unpack_from("<HHH", payload, 0)
        entries = [_GrpEntry(*_GRP_ENTRY.unpack_from(payload, 6 + i * 14))
                   for i in range(cnt)]
        have = [e for e in entries if e.rid in icons]
        if not have:
            continue
        score = max((e.w or 256) * (e.h or 256) for e in have)
        if best is None or score > best[0]:
            best = (score, gid, entries, have)

    if best is None:
        raise ValueError("no icon group had extractable frames")

    score, gid, entries, have = best
    name = name or os.path.splitext(os.path.basename(exe_path))[0]

    # Highest resolution frame wins; 32bpp breaks ties.
    top = max(have, key=lambda e: ((e.w or 256) * (e.h or 256), e.bits))
    blob = icons[top.rid]
    w, h, bits = (top.w or 256), (top.h or 256), top.bits

    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        out = os.path.join(out_dir, name + ".png")
        open(out, "wb").write(blob)
    else:
        out = os.path.join(out_dir, name + ".ico")
        open(out, "wb").write(_build_ico(entries, icons))

    frames = ", ".join(sorted({f"{e.w or 256}x{e.h or 256}" for e in have}))
    return out, f"{w}x{h} {bits}bpp", frames, len(entries)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    exe, out_dir = sys.argv[1], sys.argv[2]
    name = sys.argv[3] if len(sys.argv) > 3 else None
    os.makedirs(out_dir, exist_ok=True)
    try:
        out, chosen, frames, n = extract(exe, out_dir, name)
    except Exception as exc:  # noqa: BLE001 - CLI tool, report and continue
        print(f"FAIL {exe}: {exc}")
        return 1
    print(f"OK   {os.path.basename(exe)}")
    print(f"     wrote    {out}")
    print(f"     chose    {chosen}  (of {n} frames: {frames})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
