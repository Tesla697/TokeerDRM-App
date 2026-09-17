#!/usr/bin/env python3
"""
Tokeer Linux/Proton setup validator.

Checks that a game is ready to activate for a given AppID, then prints a signed
setup code (TLX1....) you paste into your ticket. The bot HMAC-verifies the code
and reads the checks; if everything's OK it sends your activation code.

Checks, for <appid>:
  - game installed on Steam (appmanifest + install dir), its size + .exe list
  - Proton prefix exists  (compatdata/<appid>/pfx)
  - native hook built      (~/.tokeer/ost_native_hook.so)
  - Steam launch option set to ost-run.sh for this game
  - which Proton it's mapped to (informational)
  - for a version-locked game, that the lock is applied and Steam has
    downgraded to the supported build (tokeer_version_lock.py)

Pure stdlib (Steam Deck system python). Run via:  tokeer verify <APPID>

The signing secret is shared with the bot - this is not a security boundary,
just a "don't waste a token on a broken setup" gate (mirrors the Windows
D-Report). A forged pass only lets you burn your own single-use code.
"""

import base64
import glob
import hashlib
import hmac
import json
import os
import re
import sys
import time

try:
    import tokeer_version_lock as vlock
except ImportError:
    vlock = None

# Shared with the bot's LINUX_VALIDATE_SECRET (cogs/tokeer.py). Keep in sync.
SECRET = b"tokeer_linux_setup_validate_2026_v1_shared"

TDIR = os.environ.get("TOKEER_HOME", os.path.expanduser("~/.tokeer"))


# --------------------------------------------------------------------------- #
def steam_roots():
    out = []
    for c in ("~/.steam/steam", "~/.local/share/Steam", "~/.steam/root",
              "~/.var/app/com.valvesoftware.Steam/data/Steam"):
        p = os.path.expanduser(c)
        if os.path.isdir(os.path.join(p, "steamapps")) or os.path.isdir(os.path.join(p, "userdata")):
            if p not in out:
                out.append(p)
    return out


def library_roots(root):
    roots = [root]
    for vdf in (os.path.join(root, "steamapps", "libraryfolders.vdf"),
                os.path.join(root, "config", "libraryfolders.vdf")):
        try:
            text = open(vdf, "r", encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for m in re.finditer(r'"path"\s*"([^"]+)"', text):
            p = m.group(1).replace("\\\\", "/")
            if os.path.isdir(os.path.join(p, "steamapps")) and p not in roots:
                roots.append(p)
    return roots


def find_install(appid):
    """Return (installdir_abs, appmanifest_found) for the appid, or (None, False)."""
    for root in steam_roots():
        for lib in library_roots(root):
            acf = os.path.join(lib, "steamapps", f"appmanifest_{appid}.acf")
            if os.path.isfile(acf):
                try:
                    txt = open(acf, "r", encoding="utf-8", errors="replace").read()
                    m = re.search(r'"installdir"\s*"([^"]+)"', txt)
                    if m:
                        d = os.path.join(lib, "steamapps", "common", m.group(1))
                        return (d if os.path.isdir(d) else None, True)
                except OSError:
                    pass
                return (None, True)
    return (None, False)


def find_prefix(appid):
    for root in steam_roots():
        for lib in library_roots(root):
            if os.path.isdir(os.path.join(lib, "steamapps", "compatdata", str(appid), "pfx")):
                return True
    return False


def folder_stats(path, cap_files=60000):
    """Total size (bytes) + up to a few hundred .exe basenames under path."""
    size = 0
    exes = set()
    seen = 0
    if not path or not os.path.isdir(path):
        return 0, []
    for dp, _dn, fn in os.walk(path):
        for f in fn:
            seen += 1
            if f.lower().endswith(".exe"):
                exes.add(f)
            try:
                size += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
        if seen > cap_files:
            break
    return size, sorted(exes)[:400]


def launch_option_set(appid):
    """True if any user's localconfig.vdf has an ost-run.sh launch option for the app."""
    for root in steam_roots():
        for lc in glob.glob(os.path.join(root, "userdata", "*", "config", "localconfig.vdf")):
            try:
                text = open(lc, "r", encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            # Find the app block, then its LaunchOptions value.
            m = re.search(r'"' + re.escape(str(appid)) + r'"\s*\{', text)
            if not m:
                continue
            seg = text[m.end():m.end() + 4000]
            lo = re.search(r'"LaunchOptions"\s*"((?:[^"\\]|\\.)*)"', seg, re.IGNORECASE)
            if lo and "ost-run.sh" in lo.group(1):
                return True
    return False


def compat_tool(appid):
    """Best-effort: the Proton name mapped to the app, or None."""
    for root in steam_roots():
        cfg = os.path.join(root, "config", "config.vdf")
        try:
            text = open(cfg, "r", encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        m = re.search(r'"' + re.escape(str(appid)) + r'"\s*\{([^}]*)\}', text)
        if m:
            nm = re.search(r'"name"\s*"([^"]*)"', m.group(1))
            if nm and nm.group(1):
                return nm.group(1)
    return None


# --------------------------------------------------------------------------- #
def main():
    if len(sys.argv) < 2 or not re.fullmatch(r"\d+", sys.argv[1] or ""):
        print("usage: tokeer verify <APPID>          (Steam)", file=sys.stderr)
        print("       tokeer verify-ubi <APPID>      (Ubisoft / Proton)", file=sys.stderr)
        sys.exit(2)
    appid = sys.argv[1]
    # "ubisoft" mode verifies the files for the upc_r2 + dbdata Proton flow and
    # does NOT require the native hook or the ost-run.sh launch option (Ubisoft
    # runs from the game folder, not through the Denuvo native hook).
    mode = "ubisoft" if (len(sys.argv) > 2 and sys.argv[2].lower() == "ubisoft") else "steam"

    # A version-locked game only activates on one build, so put the lock in
    # place (pinned script + the build's manifests + the slsteam pin) and stop
    # here until Steam has actually run the downgrade. No code is printed while
    # the game is on the broken build - it would only burn an activation.
    lock = None
    if vlock is not None and vlock.is_locked(appid):
        lock = vlock.apply(appid)
        vlock.print_status(lock)
        if not lock["ready"]:
            vlock.print_next_steps(lock)
            sys.exit(1)

    print(f"Checking Tokeer {mode} setup for app {appid} ...")
    installdir, manifest = find_install(appid)
    size, exes = folder_stats(installdir)
    report = {
        "v": 1,
        "mode": mode,
        "appid": appid,
        "generated": int(time.time()),
        "installed": bool(manifest and installdir),
        "folder_size": size,
        "exe_files": exes,
        "prefix": find_prefix(appid),
        "hook": os.path.isfile(os.path.join(TDIR, "ost_native_hook.so")),
        "launch_opt": launch_option_set(appid),
        "proton": compat_tool(appid),
    }
    if lock:
        report["lock"] = vlock.summary(lock)

    def mark(ok):
        return "OK " if ok else "-- "

    if mode == "ubisoft":
        # Ubisoft (Proton) verification confirms the GAME is set up and ready.
        # The care-package files (upc_r2 loader + dbdata) are handed to the user
        # in the NEXT step, so we do NOT check for them here, nor the native hook
        # or launch option (Ubisoft runs folder-based, not through the hook).
        print(f"  {mark(report['installed'])} game installed" + (f"  ({installdir})" if installdir else ""))
        print(f"  {mark(report['prefix'])} Proton prefix")
        print(f"  .. Proton: {report['proton'] or 'default/unknown'}")
        print()
        passed_all = report["installed"] and report["prefix"]
    else:
        print(f"  {mark(report['installed'])} game installed" + (f"  ({installdir})" if installdir else ""))
        print(f"  {mark(report['prefix'])} Proton prefix")
        print(f"  {mark(report['hook'])} native hook built")
        print(f"  {mark(report['launch_opt'])} launch option set")
        print(f"  .. Proton: {report['proton'] or 'default/unknown'}")
        print()
        passed_all = report["installed"] and report["prefix"] and report["hook"] and report["launch_opt"]

    raw = json.dumps(report, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(SECRET, raw, hashlib.sha256).digest()
    code = "TLX1." + _b64(raw) + "." + _b64(sig)

    print("Paste this whole code into your ticket:")
    print()
    print(code)
    print()
    if not passed_all:
        print("NOTE: some checks did not pass - the bot will tell you exactly what to fix.")


def _b64(b):
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


if __name__ == "__main__":
    main()
