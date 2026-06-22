"""
ost_setup.py — install / detect the OpenSteamTool engine.

Denuvo DRM codes only work if a Denuvo-capable Steam unlock engine is active:
official OpenSteamTool (`OpenSteamTool.dll`) or its `mktl.dll` fork. Plain Steam
and vanilla SteamTools do NOT bridge the registry ticket, so a redeemed code
fails with Denuvo `88500000`. This module:

  • detects whether such an engine is present, and
  • installs the latest official OpenSteamTool (downloaded from GitHub) — backing
    up + disabling whatever engine is there, pointing OST at the existing
    `config\\stplug-in` library so the user keeps every game, then restarting Steam.

Usable from the app (import) and standalone (`python ost_setup.py`).
"""

import io
import os
import re
import sys
import json
import time
import zipfile
import subprocess
import urllib.request

OST_RELEASES_API = "https://api.github.com/repos/OpenSteam001/OpenSteamTool/releases/latest"
OST_DLLS = ("dwmapi.dll", "xinput1_4.dll", "OpenSteamTool.dll")
ENGINE_CORES = ("OpenSteamTool.dll", "mktl.dll")  # either = Denuvo-capable engine

# Marker we drop in the Steam folder recording the OST release tag we installed, so
# we can tell (a) that WE set OST up here (→ a later breakage is a clobber to repair,
# not a first-time install) and (b) when a newer release is available (→ auto-update).
_VERSION_FILE = ".tokeer_ost_version"
_LATEST_TTL = 6 * 3600  # don't re-hit GitHub more than ~every 6h
_latest_cache = {"tag": None, "at": 0.0}

# Run console helpers (tasklist/taskkill/powershell) without flashing a window —
# the app is a windowed exe, so any console child would otherwise pop up.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW

TOML = (
    "# Written by TokeerDRM — read the existing SteamTools library so every game\n"
    "# stays unlocked under OpenSteamTool.\n"
    "[manifest]\n"
    'url = "opensteamtool"\n\n'
    "[stats]\n"
    "enable_api = true\n\n"
    "[lua]\n"
    'paths = ["config/stplug-in"]\n'
)


def _noop(pct, msg):  # default progress sink
    pass


# ---------------------------------------------------------------------------
# Steam location + engine detection
# ---------------------------------------------------------------------------

def steam_path():
    """Resolve the Steam install dir from the registry (Windows only)."""
    try:
        import winreg
    except ImportError:
        return None
    for root, key, val in (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    ):
        try:
            with winreg.OpenKey(root, key) as k:
                p = winreg.QueryValueEx(k, val)[0].replace("/", "\\")
                if os.path.exists(os.path.join(p, "steam.exe")):
                    return p
        except OSError:
            continue
    return None


def _steam_running():
    try:
        out = subprocess.run(["tasklist", "/fi", "imagename eq steam.exe"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=_NO_WINDOW).stdout.lower()
        return "steam.exe" in out
    except Exception:
        return False


def _toml_points_at_stplugin(sp):
    """True if opensteamtool.toml exists and tells OST to read config\\stplug-in."""
    p = os.path.join(sp, "opensteamtool.toml")
    try:
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
            return "stplug-in" in f.read()
    except OSError:
        return False


def _proxy_is_engine(sp):
    """True only if the dwmapi/xinput1_4 hijack proxies belong to a Denuvo engine
    (official OpenSteamTool or the mktl fork) — i.e. they reference its core DLL.

    SteamTools uses the SAME proxy names but its DLLs don't reference OpenSteamTool/
    mktl, so when SteamTools is the active engine these proxies are SteamTools' and
    the registry ticket is never bridged (Denuvo 88500000 / code 00) even though
    OpenSteamTool.dll is sitting right there. Checking the proxy bytes catches that."""
    present = [d for d in ("dwmapi.dll", "xinput1_4.dll") if os.path.exists(os.path.join(sp, d))]
    if not present:
        return False
    for d in present:
        try:
            with open(os.path.join(sp, d), "rb") as f:
                data = f.read()
        except OSError:
            return False
        if not (b"OpenSteamTool" in data or b"mktl" in data):
            return False  # this proxy is SteamTools' / a stock DLL — OST isn't active
    return True


def engine_status():
    """Is a Denuvo-capable engine present, ACTIVE, AND configured?  Returns a status dict.

    `installed` = engine DLLs present.  `ready` additionally requires (a) the toml to
    point OST at config\\stplug-in, and (b) the hijack proxies to actually be the
    engine's — not SteamTools' (which would leave SteamTools active → code 00)."""
    sp = steam_path()
    if not sp:
        return {"steam_path": None, "engine": None, "installed": False, "proxy_ok": False,
                "toml_ok": False, "ready": False, "steam_running": False}
    engine = next((c for c in ENGINE_CORES if os.path.exists(os.path.join(sp, c))), None)
    # The hijack proxies must also be there for the core to load.
    hijack = all(os.path.exists(os.path.join(sp, d)) for d in ("dwmapi.dll", "xinput1_4.dll"))
    installed = bool(engine and hijack)
    # The hijack proxies must belong to OST/mktl, not SteamTools.
    proxy_ok = _proxy_is_engine(sp)
    # The mktl fork reads config\stplug-in natively; only official OpenSteamTool
    # needs the toml redirect.
    toml_ok = True if engine == "mktl.dll" else _toml_points_at_stplugin(sp)
    return {
        "steam_path": sp,
        "engine": engine,
        "installed": installed,
        "proxy_ok": proxy_ok,
        "toml_ok": toml_ok,
        "ready": installed and toml_ok and proxy_ok,
        "steam_running": _steam_running(),
    }


def latest_release_tag(timeout=15):
    """Latest OpenSteamTool release tag from GitHub, cached ~6h. None on failure."""
    now = time.time()
    if _latest_cache["tag"] and (now - _latest_cache["at"]) < _LATEST_TTL:
        return _latest_cache["tag"]
    try:
        req = urllib.request.Request(OST_RELEASES_API, headers={"User-Agent": "TokeerDRM"})
        data = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
        tag = data.get("tag_name")
        if tag:
            _latest_cache.update(tag=tag, at=now)
        return tag
    except Exception:
        return None


def installed_tag(sp=None):
    """The OST release tag we last installed here (from the marker), or None."""
    sp = sp or steam_path()
    if not sp:
        return None
    try:
        with open(os.path.join(sp, _VERSION_FILE), "r", encoding="utf-8") as f:
            return (f.read().strip() or None)
    except OSError:
        return None


def _stamp_version(sp, tag):
    if not (sp and tag):
        return
    try:
        with open(os.path.join(sp, _VERSION_FILE), "w", encoding="utf-8") as f:
            f.write(tag)
    except OSError:
        pass


def ensure_engine():
    """Decide what the engine needs — WITHOUT elevating. Returns a dict:
        {action, status, installed_tag, latest_tag}
    action is one of:
      'none'   — ready and current; nothing to do.
      'install'— never set up by us (no marker) and not ready → first-time, let the
                 user click (don't surprise them with a UAC prompt on first launch).
      'repair' — we installed OST here before (marker present) but it's now broken
                 (Steam update clobbered the DLLs/toml) → safe to auto-fix.
      'update' — ready, but a newer OST release exists → auto-update.
    The caller (app) acts on 'repair'/'update' automatically and shows the banner for
    'install'."""
    st = engine_status()
    sp = st.get("steam_path")
    seen = installed_tag(sp)
    if not st.get("ready"):
        return {"action": ("repair" if seen else "install"), "status": st,
                "installed_tag": seen, "latest_tag": None}
    latest = latest_release_tag()
    if latest and seen and latest != seen:
        return {"action": "update", "status": st, "installed_tag": seen, "latest_tag": latest}
    return {"action": "none", "status": st, "installed_tag": seen, "latest_tag": latest}


def _ensure_toml(sp):
    """Make sure OST reads config\\stplug-in WITHOUT clobbering an existing toml.
    Backs up any existing file, then merges the lua path. OST hot-reloads it."""
    p = os.path.join(sp, "opensteamtool.toml")
    if not os.path.exists(p):
        with open(p, "w", encoding="utf-8") as f:
            f.write(TOML)
        return
    with open(p, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
    if "stplug-in" in content:
        return  # already configured
    try:
        with open(p + ".tokeer.bak", "w", encoding="utf-8") as f:
            f.write(content)
    except OSError:
        pass
    if re.search(r"(?im)^\s*\[lua\]", content):
        if re.search(r"(?im)^\s*paths\s*=\s*\[", content):
            content = re.sub(r'(?im)^(\s*paths\s*=\s*\[)',
                             r'\1"config/stplug-in", ', content, count=1)
        else:
            content = re.sub(r'(?im)^(\s*\[lua\][^\n]*\n)',
                             r'\1paths = ["config/stplug-in"]\n', content, count=1)
    else:
        content = content.rstrip() + '\n\n[lua]\npaths = ["config/stplug-in"]\n'
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)


# ---------------------------------------------------------------------------
# Steam process control
# ---------------------------------------------------------------------------

def _shutdown_steam(sp):
    exe = os.path.join(sp, "steam.exe")
    try:
        subprocess.run([exe, "-shutdown"], timeout=20, creationflags=_NO_WINDOW)
    except Exception:
        pass
    for _ in range(30):
        if not _steam_running():
            return
        time.sleep(1)
    # hard kill anything still alive (steam + a stray SteamTools manager)
    for name in ("steam.exe", "SteamTools.exe"):
        subprocess.run(["taskkill", "/f", "/im", name], capture_output=True, creationflags=_NO_WINDOW)
    time.sleep(2)


def _start_steam(sp):
    try:
        os.startfile(os.path.join(sp, "steam.exe"))  # noqa: S606
    except Exception:
        subprocess.Popen([os.path.join(sp, "steam.exe")], creationflags=_NO_WINDOW)


def is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_elevated(flag="--install-engine", timeout_s=240):
    """Relaunch this program elevated (UAC prompt) with `flag`, and block until it
    finishes. Raises if the prompt is declined. The elevated instance handles the
    flag in the app's __main__ (install/uninstall) headless and exits."""
    import ctypes
    from ctypes import wintypes

    if getattr(sys, "frozen", False):
        target, params = sys.executable, flag
    else:
        target = sys.executable  # python.exe
        params = f'"{os.path.abspath(__file__).replace("ost_setup.py", "tokeer_drm.py")}" {flag}'

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
        ]
    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = target
    sei.lpParameters = params
    sei.nShow = 1
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
        raise RuntimeError("Administrator approval was declined.")
    if sei.hProcess:
        ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, int(timeout_s * 1000))
        ctypes.windll.kernel32.CloseHandle(sei.hProcess)


def _add_defender_exclusion(path):
    """Best-effort: exclude the Steam folder from Defender so the OpenSteamTool
    DLLs aren't quarantined as PUA. Needs admin; silently no-ops otherwise."""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Add-MpPreference -ExclusionPath '{path}' -ErrorAction SilentlyContinue"],
            capture_output=True, timeout=40, creationflags=_NO_WINDOW,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Download + install
# ---------------------------------------------------------------------------

def _download_release_zip(progress):
    progress(10, "Finding latest OpenSteamTool release…")
    req = urllib.request.Request(OST_RELEASES_API, headers={"User-Agent": "TokeerDRM"})
    import json
    data = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
    asset = next((a for a in data.get("assets", [])
                  if "release" in a["name"].lower() and a["name"].lower().endswith(".zip")), None)
    if not asset:
        raise RuntimeError("Could not find the OpenSteamTool Release zip on GitHub.")
    progress(20, f"Downloading {asset['name']}…")
    req = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": "TokeerDRM"})
    return urllib.request.urlopen(req, timeout=120).read()


def install_ost(progress=_noop, fallback_zip=None, force=False):
    """Install official OpenSteamTool. Returns {ok, message}.

    fallback_zip: optional path to a bundled OpenSteamTool-Release.zip used if the
    GitHub download fails (offline).
    force: re-download and replace the engine DLLs even if they're already present —
    used for updates (the config-only shortcut would otherwise skip the new build)."""
    sp = steam_path()
    if not sp:
        return {"ok": False, "message": "Steam not found. Install/run Steam first."}

    # Config-only path: OST is already installed (e.g. the user set it up
    # manually) but the toml isn't pointing at config\stplug-in. Don't re-download
    # or restart Steam — just allow it in Defender and merge the lua path (OST
    # hot-reloads the toml). Skipped on force (updates must replace the DLLs).
    dlls_present = (next((c for c in ENGINE_CORES if os.path.exists(os.path.join(sp, c))), None)
                    and all(os.path.exists(os.path.join(sp, d)) for d in ("dwmapi.dll", "xinput1_4.dll")))
    # Only take the fast config-only path when the proxies are genuinely OST/mktl's.
    # If SteamTools owns them, fall through to a full install so OST's proxies
    # OVERWRITE SteamTools' — that's the "switch to OpenSteamTool" fix.
    if dlls_present and not force and _proxy_is_engine(sp):
        progress(50, "OpenSteamTool found — finishing setup…")
        _add_defender_exclusion(sp)
        progress(80, "Letting it read your existing library…")
        try:
            _ensure_toml(sp)
        except PermissionError:
            return {"ok": False, "message": "Permission denied writing the OST config. Run as Administrator."}
        _stamp_version(sp, latest_release_tag())
        progress(100, "OpenSteamTool is ready.")
        return {"ok": True, "message": "OpenSteamTool configured — redeem your code."}

    try:
        raw = _download_release_zip(progress)
    except Exception as e:
        if fallback_zip and os.path.exists(fallback_zip):
            progress(20, "Download failed — using bundled OpenSteamTool…")
            with open(fallback_zip, "rb") as f:
                raw = f.read()
        else:
            return {"ok": False, "message": f"Couldn't download OpenSteamTool: {e}"}

    # Extract just the 3 runtime DLLs into memory.
    progress(40, "Extracting engine…")
    files = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        for name in z.namelist():
            base = os.path.basename(name)
            if base in OST_DLLS:
                files[base] = z.read(name)
    if not all(d in files for d in OST_DLLS):
        return {"ok": False, "message": "OpenSteamTool zip was missing expected DLLs."}

    progress(55, "Closing Steam…")
    _shutdown_steam(sp)

    # Back up the current engine so nothing is lost.
    progress(65, "Backing up current engine…")
    backup = os.path.join(sp, "tokeer-engine-backup")
    os.makedirs(backup, exist_ok=True)
    for f in ("dwmapi.dll", "xinput1_4.dll", "mktl.dll", "OpenSteamTool.dll", "opensteamtool.toml"):
        src = os.path.join(sp, f)
        if os.path.exists(src):
            try:
                with open(src, "rb") as a, open(os.path.join(backup, f), "wb") as b:
                    b.write(a.read())
            except Exception:
                pass

    # Stop Defender quarantining the (PUA-flagged) engine DLLs.
    progress(70, "Allowing OpenSteamTool in Windows Security…")
    _add_defender_exclusion(sp)

    # Install OST DLLs.
    progress(75, "Installing OpenSteamTool…")
    try:
        for f, blob in files.items():
            with open(os.path.join(sp, f), "wb") as out:
                out.write(blob)
    except PermissionError:
        _start_steam(sp)
        return {"ok": False, "message": "Permission denied writing to Steam folder. "
                                        "Run TokeerDRM as Administrator and retry."}

    # Disable any old core so the hijack DLLs only load OpenSteamTool.dll.
    mktl = os.path.join(sp, "mktl.dll")
    if os.path.exists(mktl):
        try:
            bak = mktl + ".bak"
            if os.path.exists(bak):
                os.remove(bak)
            os.replace(mktl, bak)
        except Exception:
            pass

    # Point OST at the existing stplug-in library.
    progress(85, "Configuring…")
    try:
        with open(os.path.join(sp, "opensteamtool.toml"), "w", encoding="utf-8") as f:
            f.write(TOML)
    except Exception:
        pass

    progress(92, "Restarting Steam…")
    _start_steam(sp)
    # give Steam a moment to map the hijack DLLs
    for _ in range(20):
        time.sleep(1)
        if os.path.exists(os.path.join(sp, "OpenSteamTool.dll")):
            break

    _stamp_version(sp, latest_release_tag())
    progress(100, "OpenSteamTool installed.")
    return {"ok": True, "message": "OpenSteamTool installed. Sign in to Steam, then redeem your code."}


def uninstall_ost(progress=_noop):
    """Remove OpenSteamTool. If we have a backup from our own install, restore the
    user's previous engine; otherwise just strip the OST files (→ plain Steam)."""
    sp = steam_path()
    if not sp:
        return {"ok": False, "message": "Steam not found."}

    progress(20, "Closing Steam…")
    _shutdown_steam(sp)

    progress(50, "Removing OpenSteamTool…")
    try:
        for f in ("OpenSteamTool.dll", "opensteamtool.toml", "opensteamtool.toml.tokeer.bak",
                  "dwmapi.dll", "xinput1_4.dll", _VERSION_FILE):
            try:
                os.remove(os.path.join(sp, f))
            except OSError:
                pass
    except PermissionError:
        _start_steam(sp)
        return {"ok": False, "message": "Permission denied removing files. Run as Administrator."}

    # Restore whatever we backed up at install time (their previous engine).
    restored = False
    backup = os.path.join(sp, "tokeer-engine-backup")
    if os.path.isdir(backup):
        progress(70, "Restoring your previous setup…")
        for f in os.listdir(backup):
            try:
                with open(os.path.join(backup, f), "rb") as a, open(os.path.join(sp, f), "wb") as b:
                    b.write(a.read())
                restored = True
            except OSError:
                pass

    # Un-orphan an mktl fork we may have disabled.
    mb = os.path.join(sp, "mktl.dll.bak")
    if os.path.exists(mb) and not os.path.exists(os.path.join(sp, "mktl.dll")):
        try:
            os.replace(mb, os.path.join(sp, "mktl.dll"))
            restored = True
        except OSError:
            pass

    progress(90, "Restarting Steam…")
    _start_steam(sp)
    progress(100, "Done.")
    return {"ok": True,
            "message": ("OpenSteamTool removed and your previous setup restored."
                        if restored else "OpenSteamTool removed.")}


if __name__ == "__main__":
    st = engine_status()
    print("engine status:", st)
    if st["installed"]:
        print("A Denuvo-capable engine is already active:", st["engine"])
        sys.exit(0)
    print("Installing official OpenSteamTool…")
    r = install_ost(progress=lambda p, m: print(f"  [{p:3d}%] {m}"))
    print(r["message"])
    sys.exit(0 if r["ok"] else 1)
