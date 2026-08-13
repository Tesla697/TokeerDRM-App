#!/usr/bin/env python
"""Build TokeerDRM.exe with Nuitka (native compile, far harder to reverse than a
PyInstaller bytecode bundle).

Why this script instead of a plain nuitka command:

1. Nuitka's bundled pywebview plugin has a gap: on Windows its platform allow-list
   omits `webview.platforms.win32`, but the edgechromium/winforms backend imports
   win32 at module load, so the built exe dies at startup with an ImportError.
   We patch that one line in the installed plugin (idempotent) before building.

2. Build OUTSIDE OneDrive. Building the ~22 MB onefile inside a OneDrive-synced
   folder had the whole output tree silently removed mid-build. Default output is
   C:\\tkbuild.

3. MSVC toolchain (the MinGW Nuitka pulls lacked the Win32 SDK headers -> windows.h
   not found). MSVC Build Tools ship the SDK.

Usage:  python build_nuitka.py [output_dir]
Requires: pip install nuitka  +  MSVC Build Tools  +  the app's runtime deps.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = sys.argv[1] if len(sys.argv) > 1 else r"C:\tkbuild"


def patch_pywebview_plugin() -> None:
    """Add webview.platforms.win32 to the Nuitka pywebview plugin's Windows
    allow-list. Idempotent — safe to run every build, and re-applies itself after
    a Nuitka reinstall/upgrade wipes site-packages."""
    import nuitka
    plugin = os.path.join(os.path.dirname(nuitka.__file__),
                          "plugins", "standard", "PywebViewPlugin.py")
    src = open(plugin, encoding="utf-8").read()
    if '"webview.platforms.win32"' in src:
        print("[build] pywebview plugin already patched")
        return
    anchor = '                    "webview.platforms.winforms",\n'
    if anchor not in src:
        raise SystemExit("[build] could not find patch anchor in " + plugin)
    src = src.replace(anchor, anchor + '                    "webview.platforms.win32",\n', 1)
    open(plugin, "w", encoding="utf-8").write(src)
    print("[build] patched pywebview plugin: allowed webview.platforms.win32")


def main() -> int:
    patch_pywebview_plugin()
    os.makedirs(OUT, exist_ok=True)
    cmd = [
        sys.executable, "-m", "nuitka",
        "--onefile",
        "--msvc=latest",
        "--assume-yes-for-downloads",
        "--windows-console-mode=disable",
        "--windows-icon-from-ico=app_icon.ico",
        "--include-data-dir=web=web",
        "--include-data-files=extract_tickets.exe=extract_tickets.exe",
        "--include-data-files=cloud_redirect.dll=cloud_redirect.dll",
        "--include-data-files=app_icon.ico=app_icon.ico",
        "--include-package=pythonnet",
        "--include-package-data=pythonnet",
        "--include-package=clr_loader",
        "--include-package-data=clr_loader",
        "--output-dir=" + OUT,
        "--output-filename=TokeerDRM.exe",
        "--company-name=LuaTools",
        "--product-name=TokeerDRM",
        "--file-version=1.0.25",
        "tokeer_drm.py",
    ]
    print("[build] output ->", OUT)
    return subprocess.call(cmd, cwd=HERE)


if __name__ == "__main__":
    raise SystemExit(main())
