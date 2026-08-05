#!/usr/bin/env python3
"""Assemble dist/tokeer-linux.zip from the version-controlled bundle sources.

The Linux bundle used to exist only *inside* the release zip - there was no
reproducible way to rebuild it. This packs it from the repo instead, so the
sources in linux/ are the single source of truth.

Crucially, if a prebuilt `linux/ost_native_hook.so` is present it is included in
the zip. The installer (install_linux.sh) uses that prebuilt hook and SKIPS
compiling, so machines without gcc - Steam Deck / SteamOS above all - work out
of the box instead of hitting the pacman/keyring wall. Without the .so the
bundle still works on any machine that has gcc (it compiles on first setup).

Build the .so on Linux with `linux/build.sh` (see its glibc note) and drop it in
linux/, then run this. Run from anywhere; paths are resolved from this file.

    python3 pack_linux.py
"""
import os
import stat
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
LINUX = os.path.join(ROOT, "linux")
OUT = os.path.join(ROOT, "dist", "tokeer-linux.zip")

# (source path relative to repo root, executable?) in bundle order. The .so is
# handled separately below because it's optional.
FILES = [
    ("linux/build.sh", True),
    ("install_linux.sh", True),
    ("linux/ost_native_hook.c", False),
    ("linux/ost_native_probe.c", False),
    ("linux/ost-run.sh", True),
    ("README_LINUX.md", False),
    ("linux/tokeer", True),
    ("tokeer_redeem_linux.py", False),
    ("linux/tokeer_steam_config.py", False),
    ("linux/tokeer_validate_linux.py", False),
]

# The prebuilt native hook. Optional: without it the installer compiles on the
# target (needs gcc). With it, gcc-less machines (Steam Deck) work out of the box.
SO = ("linux/ost_native_hook.so", False)


def _add(zf: zipfile.ZipFile, rel: str, executable: bool):
    src = os.path.join(ROOT, rel)
    data = open(src, "rb").read()
    # Flat layout at the zip root (arcname is just the basename), matching what
    # install_linux.sh expects when it unzips the bundle.
    zi = zipfile.ZipInfo(os.path.basename(rel))
    zi.compress_type = zipfile.ZIP_DEFLATED
    mode = 0o755 if executable else 0o644
    zi.external_attr = (mode & 0xFFFF) << 16
    # Mark regular file so unix mode bits are honored on extraction.
    zi.external_attr |= (stat.S_IFREG) << 16
    zf.writestr(zi, data)
    return len(data)


def main() -> int:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    missing = [rel for rel, _ in FILES if not os.path.exists(os.path.join(ROOT, rel))]
    if missing:
        print("ERROR: missing required bundle files:", ", ".join(missing), file=sys.stderr)
        return 1

    have_so = os.path.exists(os.path.join(ROOT, SO[0]))
    n = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, ex in FILES:
            _add(zf, rel, ex)
            n += 1
        if have_so:
            _add(zf, SO[0], SO[1])
            n += 1

    size = os.path.getsize(OUT)
    print(f"wrote {OUT}  ({n} files, {size} bytes)")
    if have_so:
        so_size = os.path.getsize(os.path.join(ROOT, SO[0]))
        print(f"  INCLUDED prebuilt ost_native_hook.so ({so_size} bytes) "
              "- gcc-less machines (Steam Deck) will use it, no compile needed.")
    else:
        print("  WARNING: no linux/ost_native_hook.so - bundle ships source only, so the")
        print("           target must have gcc. Build it with linux/build.sh on the OLDEST")
        print("           glibc you support (Steam Deck-friendly) and drop it in linux/,")
        print("           then re-run this to ship a gcc-free bundle.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
