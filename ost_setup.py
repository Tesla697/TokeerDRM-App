"""
ost_setup.py — install / detect the OpenSteamTool engine.

Denuvo DRM codes only work if official OpenSteamTool (`OpenSteamTool.dll`) is the
active Steam unlock engine. Plain Steam, vanilla SteamTools and the internal
`mktl.dll` fork do NOT bridge the registry ticket, so a redeemed code fails with
Denuvo `88500000`. This module:

  • detects whether such an engine is present, and
  • installs the latest official OpenSteamTool (downloaded from GitHub) — backing
    up + disabling whatever engine is there, pointing OST at the existing
    `config\\stplug-in` library so the user keeps every game, then restarting Steam.

Usable from the app (import) and standalone (`python ost_setup.py`).
"""

import hashlib
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
CUSTOM_OST_REPO   = "Tesla697/OpenSteamTool"
CUSTOM_OST_API    = f"https://api.github.com/repos/{CUSTOM_OST_REPO}/releases/latest"
_CUSTOM_MARKER    = ".tokeer_ost_custom"
OST_DLLS = ("dwmapi.dll", "xinput1_4.dll", "OpenSteamTool.dll")
ENGINE_CORES = ("OpenSteamTool.dll",)  # the only Denuvo-capable engine we support

# Competing unlock engines we neutralise so OpenSteamTool is the ONLY one active
# (this is what "switch to OST" really means, and what makes managers like LuaTools
# stop showing their own backend as active):
#   • FOREIGN_CORES   — other engines' core DLLs, disabled by name.
#   • FOREIGN_PROXIES — proxy-hijack DLLs an engine might use to inject that AREN'T
#     OST's (OST owns dwmapi.dll + xinput1_4.dll). Only disabled when their bytes tie
#     them to a known unlocker, so a legitimate Steam DLL is never touched.
# mktl.dll is an internal staff fork almost nobody runs. It used to count as a valid
# engine here, which backfired: its proxies were accepted as "OST is active", but
# install_custom_dll() always writes OpenSteamTool.dll — so on an mktl machine the
# enhanced DLL landed in a file Steam never loaded. It's now purely foreign, so those
# installs get repaired onto real OpenSteamTool instead.
FOREIGN_CORES = ("mktl.dll", "cloud_redirect.dll")
FOREIGN_PROXIES = ("hid.dll", "version.dll", "winhttp.dll")
_OWN_MARKERS = (b"OpenSteamTool",)
# Bytes that positively identify a foreign unlocker's proxy/core. SteamTools' hid.dll
# carries NO "SteamTools"/"stplug" string, so we fingerprint it by its update hosts
# (update.steamui.com, stools.oss-cn-shanghai.aliyuncs.com) and its typo'd IPC class
# name "Vale_SteamIPC" — all verified against a real SteamTools hid.dll.
_FOREIGN_MARKERS = (
    b"cloud_redirect", b"SteamTools", b"steamtools", b"stplug", b"LuaTools", b"luatools",
    b"steamui.com", b"stools.oss", b"Vale_SteamIPC",
)

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


# --- cross-process progress (UAC) ------------------------------------------
# The elevated helper runs headless (no window), so it can't push progress to the
# UI directly. It writes pct/msg to this file; the un-elevated UI instance polls it
# while it waits, so the bar actually MOVES during an elevated install/uninstall
# instead of freezing at the value it had when the UAC prompt appeared.
def _progress_file():
    # Prefer %PUBLIC% (same path no matter which account UAC elevates to) so the
    # un-elevated UI and the elevated helper read/write the SAME file. Fall back to TEMP.
    base = (os.environ.get("PUBLIC") or os.environ.get("TEMP")
            or os.environ.get("TMP") or os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "tokeerdrm_engine_progress.json")


def write_progress(pct, msg):
    """Called (as a progress sink) by the elevated helper to publish its progress."""
    try:
        with open(_progress_file(), "w", encoding="utf-8") as f:
            json.dump({"pct": int(pct), "msg": str(msg)}, f)
    except Exception:
        pass


def read_progress():
    """(pct, msg) the elevated helper last published, or None."""
    try:
        with open(_progress_file(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return int(d.get("pct", 0)), str(d.get("msg", ""))
    except Exception:
        return None


def clear_progress():
    try:
        os.remove(_progress_file())
    except OSError:
        pass


# The elevated helper runs headless and just sys.exit()s, so the un-elevated UI can't
# see its return value. It writes the result dict here; the UI reads it after the helper
# exits (e.g. to show the Defender -> LuaTools hint on a quarantine).
def _result_file():
    base = (os.environ.get("PUBLIC") or os.environ.get("TEMP")
            or os.environ.get("TMP") or os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "tokeerdrm_engine_result.json")


def write_result(d):
    try:
        with open(_result_file(), "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:
        pass


def read_result():
    try:
        with open(_result_file(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def clear_result():
    try:
        os.remove(_result_file())
    except OSError:
        pass


# Shown when Windows Defender (Tamper Protection) quarantines the PUA-flagged official
# OpenSteamTool and our exclusion can't be added. LuaTools ships an unflagged build, so
# we route the user there to install the engine, then they redeem here.
LUATOOLS_HINT = ("Windows Defender (Tamper Protection) is blocking OpenSteamTool. "
                 "Install the engine with LuaTools instead - open LuaTools, go to Mode, "
                 "and Switch to OpenSteamTools - then come back here and redeem. "
                 "Get LuaTools at lua.tools")
LUATOOLS_URL = "https://lua.tools"


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
    """True only if the dwmapi/xinput1_4 hijack proxies belong to official
    OpenSteamTool — i.e. they reference OpenSteamTool.dll.

    SteamTools uses the SAME proxy names but its DLLs don't reference OpenSteamTool,
    so when SteamTools is the active engine these proxies are SteamTools' and the
    registry ticket is never bridged (Denuvo 88500000 / code 00) even though
    OpenSteamTool.dll is sitting right there. Checking the proxy bytes catches that.

    An mktl-fork proxy fails here too, on purpose: it loads mktl.dll, which never
    receives the enhanced DLL, so such installs must fall through to a full install."""
    present = [d for d in ("dwmapi.dll", "xinput1_4.dll") if os.path.exists(os.path.join(sp, d))]
    if not present:
        return False
    for d in present:
        try:
            with open(os.path.join(sp, d), "rb") as f:
                data = f.read()
        except OSError:
            return False
        if b"OpenSteamTool" not in data:
            return False  # SteamTools' / mktl's / a stock DLL — OST isn't active
    return True


def _disable_file(path):
    """Rename path → path.bak (replacing any stale .bak). Returns True if moved."""
    if not os.path.exists(path):
        return False
    try:
        bak = path + ".bak"
        if os.path.exists(bak):
            os.remove(bak)
        os.replace(path, bak)
        return True
    except Exception:
        return False


def _disable_foreign_engines(sp):
    """Neutralise every OTHER unlock engine so ONLY OpenSteamTool is active.

    Without this, installing OST's proxies makes OST *function*, but a leftover core
    like cloud_redirect.dll stays on disk — so managers such as LuaTools, which judge
    the active backend by file presence, keep showing CloudRedirect as ACTIVE and ask
    the user to switch manually. Disabling the foreign core (and any foreign proxy)
    is exactly what their "Switch to OpenSteamTool" button does. Returns names disabled."""
    disabled = []
    for core in FOREIGN_CORES:
        if _disable_file(os.path.join(sp, core)):
            disabled.append(core)
    for proxy in FOREIGN_PROXIES:
        p = os.path.join(sp, proxy)
        if not os.path.exists(p):
            continue
        try:
            with open(p, "rb") as f:
                data = f.read()
        except OSError:
            continue
        if any(m in data for m in _OWN_MARKERS):
            continue  # it's ours — leave it
        # Only disable a proxy we can positively tie to a known unlocker, so we never
        # rename a legitimate Steam DLL out from under Steam.
        if any(s in data for s in _FOREIGN_MARKERS) and _disable_file(p):
            disabled.append(proxy)
    return disabled


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
    # The hijack proxies must belong to OpenSteamTool, not SteamTools/mktl.
    proxy_ok = _proxy_is_engine(sp)
    toml_ok = _toml_points_at_stplugin(sp)
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


def _ensure_stplugin_dir(sp):
    """OST's toml points at config\\stplug-in (the SteamTools library). A user who never
    had SteamTools won't have that folder, so OST's lua path would be missing. Create it
    if absent (no-op if it already exists) so the redirect always resolves."""
    try:
        os.makedirs(os.path.join(sp, "config", "stplug-in"), exist_ok=True)
    except OSError:
        pass


def _ensure_toml(sp):
    """Make sure OST reads config\\stplug-in WITHOUT clobbering an existing toml.
    Backs up any existing file, then merges the lua path. OST hot-reloads it."""
    _ensure_stplugin_dir(sp)
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
# Steam Cloud saves (CloudRedirect)
# ---------------------------------------------------------------------------
# Steam refuses Cloud RPCs for apps the account doesn't own, so unlocked games
# silently never persist their saves. OpenSteamTool can host CloudRedirect's
# cloud_redirect.dll in-process (Hooks_NetPacket → CloudRedirectHost) and answer
# those RPCs itself, which is the actual save fix.
#
# We install the DLL into <Steam>\opensteamtool\ rather than the Steam root on
# purpose: managers like LuaTools decide which backend is "active" purely by
# looking for <Steam>\cloud_redirect.dll, so a root copy makes them report
# CloudRedirect as the active engine and nag the user to switch. A subfolder copy
# (with [cloud].library pointing at it) gets the fix WITHOUT tripping that check —
# which is why cloud_redirect.dll stays in FOREIGN_CORES above.
#
# No sign-in is needed: with no %APPDATA%\CloudRedirect\config.json the DLL runs
# in local-only mode and stores saves on disk. Google Drive / OneDrive sync is an
# optional extra the user sets up in CloudRedirect's own app.
CLOUD_DLL = "cloud_redirect.dll"
CLOUD_DIR = "opensteamtool"
CLOUD_REL = CLOUD_DIR + "/" + CLOUD_DLL     # as written into the toml


def _cloud_dll_dest(sp):
    return os.path.join(sp, CLOUD_DIR, CLOUD_DLL)


def _cloud_dll_source(sp):
    """Find a cloud_redirect.dll we can install, or None.

    Ordered most-trusted first: one we already installed, then copies our own
    installer / _disable_foreign_engines set aside, then one bundled with the app."""
    bundled = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))),
                           CLOUD_DLL)
    for c in (_cloud_dll_dest(sp),
              os.path.join(sp, CLOUD_DLL),
              os.path.join(sp, CLOUD_DLL + ".bak"),
              os.path.join(sp, "tokeer-engine-backup", CLOUD_DLL),
              bundled):
        try:
            # A truncated/placeholder file would just fail to load inside Steam, so
            # require a plausible PE before we call cloud saves "available".
            if os.path.exists(c) and os.path.getsize(c) > 200_000:
                with open(c, "rb") as f:
                    if f.read(2) == b"MZ":
                        return c
        except OSError:
            continue
    return None


def _split_cloud_section(content):
    """Split a toml into (everything except [cloud], the [cloud] body lines).

    Done line-by-line rather than with a regex: section headers are trivially
    recognisable, and a multiline/dotall regex here silently eats the newline after
    "[cloud]" (producing "[cloud]enabled = true", which OST then ignores)."""
    other, body, in_cloud = [], [], False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_cloud = stripped.lower() == "[cloud]"
            if in_cloud:
                continue          # drop the header; we re-emit it ourselves
        (body if in_cloud else other).append(line)
    return other, body


def _active_core(sp):
    """Which engine core the hijack proxies actually load, or None.

    A leftover OpenSteamTool.dll often sits next to a foreign engine's proxy, but
    Steam only ever loads the core the proxy names — so "is this feature supported?"
    has to be answered against that DLL, not whichever one happens to be on disk.
    Returns None when the proxies belong to something else (SteamTools, mktl)."""
    seen_proxy = False
    for proxy in ("xinput1_4.dll", "dwmapi.dll"):
        try:
            with open(os.path.join(sp, proxy), "rb") as f:
                data = f.read()
        except OSError:
            continue
        seen_proxy = True
        for core in ENGINE_CORES:
            if core.encode() in data and os.path.exists(os.path.join(sp, core)):
                return core
    if seen_proxy:
        return None  # a proxy is installed, but it doesn't load our engine
    return next((c for c in ENGINE_CORES if os.path.exists(os.path.join(sp, c))), None)


def _toml_cloud_enabled(content):
    """True if the toml has [cloud] with enabled = true."""
    _, body = _split_cloud_section(content)
    return any(re.match(r'(?i)\s*enabled\s*=\s*true\s*$', l) for l in body)


def cloud_status():
    """Where Steam Cloud save redirection stands. Keys:
        available — we have a cloud_redirect.dll to install
        enabled   — toml says [cloud].enabled = true AND the DLL is in place
        supported — the active engine actually contains the CloudRedirect host"""
    sp = steam_path()
    if not sp:
        return {"steam_path": None, "available": False, "enabled": False,
                "supported": False}

    dll_ok = os.path.exists(_cloud_dll_dest(sp))
    try:
        with open(os.path.join(sp, "opensteamtool.toml"), "r",
                  encoding="utf-8", errors="ignore") as f:
            toml_on = _toml_cloud_enabled(f.read())
    except OSError:
        toml_on = False

    # Older engine builds predate CloudRedirect entirely — enabling the config
    # there would do nothing, so don't offer it. Test the core the proxies ACTUALLY
    # load, not just the first one on disk: both OpenSteamTool.dll and mktl.dll are
    # often present, and only the proxied one runs.
    supported = False
    core = _active_core(sp)
    if core:
        try:
            with open(os.path.join(sp, core), "rb") as f:
                supported = b"CR_InitCloudSave" in f.read()
        except OSError:
            pass

    return {
        "steam_path": sp,
        "available": bool(_cloud_dll_source(sp)),
        "enabled": bool(dll_ok and toml_on),
        "supported": supported,
    }


def _ensure_cloud_toml(sp):
    """Turn [cloud] on in opensteamtool.toml, preserving everything else."""
    p = os.path.join(sp, "opensteamtool.toml")
    try:
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except OSError:
        content = TOML

    # Lift out any existing [cloud] section, keep whatever settings we don't own
    # (so a hand-tuned key survives), and re-emit the section fresh. Rebuilding it
    # rather than patching in place keeps this idempotent.
    other, body = _split_cloud_section(content)
    keep = [l for l in body
            if l.strip()
            and not re.match(r'(?i)\s*(enabled|library)\s*=', l)]

    block = ["[cloud]", "enabled = true", f'library = "{CLOUD_REL}"'] + keep
    content = "\n".join(other).rstrip() + "\n\n" + "\n".join(block) + "\n"

    with open(p, "w", encoding="utf-8") as f:
        f.write(content)


def enable_cloud(progress=_noop):
    """Install cloud_redirect.dll next to OST and switch [cloud] on.

    CloudRedirectHost::Initialize only runs at DLL attach, so the config is read
    once when Steam starts — Steam has to be restarted for this to take effect."""
    sp = steam_path()
    if not sp:
        return {"ok": False, "message": "Steam not found."}

    src = _cloud_dll_source(sp)
    if not src:
        return {"ok": False, "message": "Cloud save support isn't available in this build."}

    progress(20, "Enabling cloud saves…")
    dest = _cloud_dll_dest(sp)
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.abspath(src) != os.path.abspath(dest):
            with open(src, "rb") as a, open(dest, "wb") as b:
                b.write(a.read())
    except PermissionError:
        return {"ok": False, "message": "Permission denied writing to the Steam folder. "
                                        "Run TokeerDRM as Administrator and retry."}
    except OSError as e:
        return {"ok": False, "message": f"Couldn't install cloud saves: {e}"}

    progress(50, "Configuring…")
    try:
        _ensure_cloud_toml(sp)
    except PermissionError:
        return {"ok": False, "message": "Permission denied writing the OST config. "
                                        "Run TokeerDRM as Administrator."}
    except Exception as e:
        return {"ok": False, "message": f"Couldn't update the config: {e}"}

    # Only bounce Steam if it's actually up; otherwise the next launch picks it up.
    restarted = _steam_running()
    if restarted:
        progress(65, "Restarting Steam…")
        _shutdown_steam(sp, progress, lo=65, hi=88)
        _start_steam(sp)
    progress(100, "Cloud saves enabled.")
    return {"ok": True, "restarted": restarted,
            "message": ("Cloud saves enabled — Steam is restarting. "
                        "Once it's back, redeem your code.") if restarted else
                       "Cloud saves enabled — they apply next time Steam starts."}


# ---------------------------------------------------------------------------
# Steam process control
# ---------------------------------------------------------------------------

def _shutdown_steam(sp, progress=None, lo=0, hi=0):
    """Ask Steam to close and wait for it. If a `progress` sink + lo/hi range are given,
    tick the bar up while we wait so it doesn't freeze for the (up to 30s) close."""
    exe = os.path.join(sp, "steam.exe")
    try:
        subprocess.run([exe, "-shutdown"], timeout=20, creationflags=_NO_WINDOW)
    except Exception:
        pass
    for i in range(30):
        if not _steam_running():
            if progress and hi > lo:
                progress(hi, "Closing Steam…")
            return
        if progress and hi > lo:
            progress(min(hi, lo + i), "Closing Steam…")
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


def relaunch_elevated(flag="--install-engine", timeout_s=240, on_progress=None):
    """Relaunch this program elevated (UAC prompt) with `flag`, and block until it
    finishes. Raises if the prompt is declined. The elevated instance handles the
    flag in the app's __main__ (install/uninstall) headless and exits.

    on_progress(): if given, called every ~250ms while we wait for the elevated
    helper, so the caller can pump cross-process progress to the UI."""
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
        # Poll-wait so we can pump the elevated helper's progress to the UI instead of
        # blocking opaquely. WAIT_TIMEOUT (0x102) = still running.
        deadline = time.time() + timeout_s
        while True:
            r = ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, 250)
            if r != 0x102:  # signalled / failed → done
                break
            if on_progress:
                try:
                    on_progress()
                except Exception:
                    pass
            if time.time() > deadline:
                break
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
    # Stream the download so the progress bar actually MOVES (20→38%) instead of sitting
    # frozen at 20% during a single blocking read of the whole zip.
    req = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": "TokeerDRM"})
    resp = urllib.request.urlopen(req, timeout=120)
    total = int(resp.headers.get("Content-Length") or 0)
    buf = io.BytesIO()
    got = 0
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        buf.write(chunk)
        got += len(chunk)
        if total:
            progress(20 + int(got * 18 / total), f"Downloading {asset['name']}…")
    return buf.getvalue()


def install_ost(progress=_noop, fallback_zip=None, force=False):
    """Install official OpenSteamTool. Returns {ok, message}.

    fallback_zip: optional path to a bundled OpenSteamTool-Release.zip used if the
    GitHub download fails (offline).
    force: re-download and replace the engine DLLs even if they're already present —
    used for updates (the config-only shortcut would otherwise skip the new build)."""
    sp = steam_path()
    if not sp:
        return {"ok": False, "message": "Steam not found. Install/run Steam first."}

    # Read this BEFORE anything touches the toml — the full install path rewrites it
    # from scratch and we need to restore [cloud] afterwards.
    try:
        was_cloud_enabled = cloud_status().get("enabled", False)
    except Exception:
        was_cloud_enabled = False

    # Config-only path: OST is already installed (e.g. the user set it up
    # manually) but the toml isn't pointing at config\stplug-in. Don't re-download
    # or restart Steam — just allow it in Defender and merge the lua path (OST
    # hot-reloads the toml). Skipped on force (updates must replace the DLLs).
    dlls_present = (next((c for c in ENGINE_CORES if os.path.exists(os.path.join(sp, c))), None)
                    and all(os.path.exists(os.path.join(sp, d)) for d in ("dwmapi.dll", "xinput1_4.dll")))
    # Only take the fast config-only path when the proxies are genuinely OST's.
    # If SteamTools owns them, fall through to a full install so OST's proxies
    # OVERWRITE SteamTools' — that's the "switch to OpenSteamTool" fix.
    if dlls_present and not force and _proxy_is_engine(sp):
        progress(50, "OpenSteamTool found — finishing setup…")
        _add_defender_exclusion(sp)
        # A previous engine's core (e.g. cloud_redirect.dll) may still be sitting
        # here, so other managers keep showing it as active — clear it. Safe while
        # Steam runs: OST's proxies are active, so these files aren't loaded.
        _disable_foreign_engines(sp)
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
    _shutdown_steam(sp, progress, lo=55, hi=64)

    # Back up the current engine so nothing is lost.
    progress(65, "Backing up current engine…")
    backup = os.path.join(sp, "tokeer-engine-backup")
    os.makedirs(backup, exist_ok=True)
    for f in ("dwmapi.dll", "xinput1_4.dll", "mktl.dll", "cloud_redirect.dll",
              "hid.dll", "OpenSteamTool.dll", "opensteamtool.toml"):
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
    except OSError as e:
        # WinError 225 = "the file contains a virus or potentially unwanted software":
        # Defender quarantined the PUA-flagged official OST and Tamper Protection blocked
        # our exclusion. LuaTools ships an unflagged build — route the user there.
        _start_steam(sp)
        if getattr(e, "winerror", None) == 225 or "virus" in str(e).lower() or "unwanted" in str(e).lower():
            return {"ok": False, "defender": True, "message": LUATOOLS_HINT, "url": LUATOOLS_URL}
        return {"ok": False, "message": f"Couldn't install OpenSteamTool: {e}"}

    # Disable every competing engine (mktl fork, CloudRedirect, a SteamTools proxy…)
    # so the hijack DLLs only load OpenSteamTool.dll AND managers like LuaTools stop
    # showing another backend as active. This is the actual "switch to OST".
    _disable_foreign_engines(sp)

    # Point OST at the stplug-in library, creating it first if the user never had one.
    progress(85, "Configuring…")
    _ensure_stplugin_dir(sp)
    # This rewrites the toml from scratch, so remember whether cloud saves were on
    # and put the section back — otherwise a repair/update silently turns them off.
    try:
        with open(os.path.join(sp, "opensteamtool.toml"), "w", encoding="utf-8") as f:
            f.write(TOML)
        if was_cloud_enabled:
            _ensure_cloud_toml(sp)
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
    _shutdown_steam(sp, progress, lo=20, hi=49)

    progress(50, "Removing OpenSteamTool…")
    try:
        for f in ("OpenSteamTool.dll", "opensteamtool.toml", "opensteamtool.toml.tokeer.bak",
                  "dwmapi.dll", "xinput1_4.dll", _VERSION_FILE,
                  os.path.join(CLOUD_DIR, CLOUD_DLL)):
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

    # Un-orphan any engine core/proxy we disabled when switching to OST (mktl fork,
    # CloudRedirect, a SteamTools proxy…) so the user's previous backend comes back.
    for name in ("mktl.dll", "cloud_redirect.dll", "hid.dll", "version.dll", "winhttp.dll"):
        bak = os.path.join(sp, name + ".bak")
        live = os.path.join(sp, name)
        if os.path.exists(bak) and not os.path.exists(live):
            try:
                os.replace(bak, live)
                restored = True
            except OSError:
                pass

    progress(90, "Restarting Steam…")
    _start_steam(sp)
    progress(100, "Done.")
    return {"ok": True,
            "message": ("OpenSteamTool removed and your previous setup restored."
                        if restored else "OpenSteamTool removed.")}


def install_ost_custom(progress=_noop, fallback_zip=None, force=False):
    """Full OST install using our custom OpenSteamTool.dll as the core.
    Proxy DLLs (dwmapi.dll, xinput1_4.dll) come from the official OST release;
    the core (OpenSteamTool.dll) comes from our fork. Writes the custom marker.
    Replaces install_ost() so the very first install already uses our DLL."""
    sp = steam_path()
    if not sp:
        return {"ok": False, "message": "Steam not found. Install/run Steam first."}

    dlls_present = (next((c for c in ENGINE_CORES if os.path.exists(os.path.join(sp, c))), None)
                    and all(os.path.exists(os.path.join(sp, d)) for d in ("dwmapi.dll", "xinput1_4.dll")))
    if dlls_present and not force and _proxy_is_engine(sp) and custom_dll_installed(sp):
        progress(50, "Custom OpenSteamTool found — finishing setup…")
        _add_defender_exclusion(sp)
        _disable_foreign_engines(sp)
        progress(80, "Letting it read your existing library…")
        try:
            _ensure_toml(sp)
        except PermissionError:
            return {"ok": False, "message": "Permission denied writing the OST config. Run as Administrator."}
        _stamp_version(sp, latest_release_tag())
        progress(100, "Ready.")
        return {"ok": True, "message": "Custom OpenSteamTool configured — redeem your code."}

    # Proxy DLLs are already present and belong to the engine — skip the official
    # OST download and only fetch our custom core DLL. This avoids hitting the
    # GitHub rate limit on the official repo when the user already has OST installed
    # (e.g. via LuaTools) and just needs our DLL swapped in.
    # NOTE: we defer Steam restart until AFTER _ensure_toml() so OST doesn't start
    # and overwrite the toml before we configure the stplug-in path (race condition).
    if dlls_present and not force and _proxy_is_engine(sp):
        progress(5, "Proxy DLLs already present — downloading custom core only…")
        result = install_custom_dll(progress, restart_steam=False)
        if not result.get("ok"):
            return result
        progress(80, "Finishing setup…")
        _add_defender_exclusion(sp)
        _disable_foreign_engines(sp)
        try:
            _ensure_toml(sp)
        except PermissionError:
            return {"ok": False, "message": "Permission denied writing the OST config. Run as Administrator."}
        _stamp_version(sp, latest_release_tag())
        progress(92, "Starting Steam…")
        _start_steam(sp)
        return {"ok": True, "message": "Custom OpenSteamTool installed — redeem your code."}

    # Full install: proxy DLLs from official OST release zip.
    progress(5, "Downloading OpenSteamTool…")
    try:
        raw_official = _download_release_zip(progress)   # ticks progress 10→38
    except Exception as e:
        if fallback_zip and os.path.exists(fallback_zip):
            progress(20, "Download failed — using bundled OpenSteamTool…")
            with open(fallback_zip, "rb") as f:
                raw_official = f.read()
        else:
            return {"ok": False, "message": f"Couldn't download OpenSteamTool: {e}"}

    progress(38, "Extracting proxy DLLs…")
    proxy_files = {}
    with zipfile.ZipFile(io.BytesIO(raw_official)) as z:
        for name in z.namelist():
            base = os.path.basename(name)
            if base in ("dwmapi.dll", "xinput1_4.dll"):
                proxy_files[base] = z.read(name)
    if not all(d in proxy_files for d in ("dwmapi.dll", "xinput1_4.dll")):
        return {"ok": False, "message": "OpenSteamTool zip was missing expected proxy DLLs."}

    # Step 2: our custom core DLL.
    progress(42, "Downloading custom OpenSteamTool.dll…")
    try:
        req = urllib.request.Request(CUSTOM_OST_API, headers={"User-Agent": "TokeerDRM"})
        rel_data = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
    except Exception as e:
        return {"ok": False, "message": f"Couldn't reach custom DLL release: {e}"}

    asset = next((a for a in rel_data.get("assets", [])
                  if a.get("name", "").lower() == "opensteamtool.dll"), None)
    zip_asset = None if asset else next(
        (a for a in rel_data.get("assets", [])
         if "release" in a.get("name", "").lower() and a["name"].lower().endswith(".zip")), None)
    if not asset and not zip_asset:
        return {"ok": False, "message": "Custom DLL not found in the GitHub release assets."}

    try:
        url = (asset or zip_asset)["browser_download_url"]
        req = urllib.request.Request(url, headers={"User-Agent": "TokeerDRM"})
        resp = urllib.request.urlopen(req, timeout=120)
        total = int(resp.headers.get("Content-Length") or 0)
        buf = io.BytesIO()
        got = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            buf.write(chunk)
            got += len(chunk)
            if total:
                progress(42 + int(got * 13 / total), "Downloading custom DLL…")
        custom_raw = buf.getvalue()
    except Exception as e:
        return {"ok": False, "message": f"Couldn't download custom DLL: {e}"}

    if zip_asset:
        try:
            with zipfile.ZipFile(io.BytesIO(custom_raw)) as z:
                entry = next((n for n in z.namelist()
                              if os.path.basename(n).lower() == "opensteamtool.dll"), None)
                if not entry:
                    return {"ok": False, "message": "OpenSteamTool.dll not found in the release zip."}
                custom_raw = z.read(entry)
        except Exception as e:
            return {"ok": False, "message": f"Couldn't extract custom DLL from zip: {e}"}

    progress(57, "Closing Steam…")
    _shutdown_steam(sp, progress, lo=57, hi=66)

    progress(67, "Backing up current engine…")
    backup = os.path.join(sp, "tokeer-engine-backup")
    os.makedirs(backup, exist_ok=True)
    for f in ("dwmapi.dll", "xinput1_4.dll", "mktl.dll", "cloud_redirect.dll",
              "hid.dll", "OpenSteamTool.dll", "opensteamtool.toml"):
        src = os.path.join(sp, f)
        if os.path.exists(src):
            try:
                with open(src, "rb") as a, open(os.path.join(backup, f), "wb") as b:
                    b.write(a.read())
            except Exception:
                pass

    progress(70, "Allowing OpenSteamTool in Windows Security…")
    _add_defender_exclusion(sp)

    progress(75, "Installing custom OpenSteamTool…")
    try:
        for fname, blob in proxy_files.items():
            with open(os.path.join(sp, fname), "wb") as out:
                out.write(blob)
        with open(os.path.join(sp, "OpenSteamTool.dll"), "wb") as out:
            out.write(custom_raw)
        with open(os.path.join(sp, _CUSTOM_MARKER), "w", encoding="utf-8") as f:
            f.write(_marker_content(custom_raw, rel_data.get("tag_name", "")))
    except PermissionError:
        _start_steam(sp)
        return {"ok": False, "message": "Permission denied writing to Steam folder. Run as Administrator."}
    except OSError as e:
        _start_steam(sp)
        if getattr(e, "winerror", None) == 225 or "virus" in str(e).lower():
            return {"ok": False, "defender": True, "message": LUATOOLS_HINT, "url": LUATOOLS_URL}
        return {"ok": False, "message": f"Couldn't install: {e}"}

    _disable_foreign_engines(sp)
    progress(85, "Configuring…")
    _ensure_stplugin_dir(sp)
    try:
        with open(os.path.join(sp, "opensteamtool.toml"), "w", encoding="utf-8") as f:
            f.write(TOML)
    except Exception:
        pass

    progress(92, "Restarting Steam…")
    _start_steam(sp)
    _stamp_version(sp, latest_release_tag())
    progress(100, "Custom OpenSteamTool installed.")
    return {"ok": True, "message": "Custom OpenSteamTool installed. Sign in to Steam, then redeem your code."}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _marker_content(raw: bytes, tag: str = "") -> str:
    return f"{_sha256_bytes(raw)}:{len(raw)}:{tag}"


def _write_marker(sp, raw, tag):
    try:
        with open(os.path.join(sp, _CUSTOM_MARKER), "w", encoding="utf-8") as f:
            f.write(_marker_content(raw, tag))
    except Exception:
        pass


def _hash_cache_file():
    base = (os.environ.get("PUBLIC") or os.environ.get("TEMP")
            or os.environ.get("TMP") or os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "tokeerdrm_dll_hashes.json")


def _read_hash_cache():
    try:
        with open(_hash_cache_file(), "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _write_hash_cache(cache):
    try:
        with open(_hash_cache_file(), "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except Exception:
        pass


def _release_dll_hashes(rel_data):
    """sha256 hex of every OpenSteamTool.dll in a release — taken from each asset's
    `digest` field so NOTHING is downloaded. Returns a set (empty if GitHub didn't
    populate digests for this release)."""
    hashes = set()
    for a in rel_data.get("assets", []):
        if not a.get("name", "").lower().endswith(".dll"):
            continue
        dg = (a.get("digest") or "")
        if dg.lower().startswith("sha256:"):
            hashes.add(dg.split(":", 1)[1].strip().lower())
    return hashes


def _download_dll_hashes(rel_data):
    """Fallback when a release has no digests: download each DLL (or the inner DLL of a
    Release zip) once and hash it. Used only to seed the on-disk per-tag hash cache."""
    hashes = set()
    for a in rel_data.get("assets", []):
        name = a.get("name", "").lower()
        is_dll = name.endswith(".dll")
        is_zip = name.endswith(".zip") and "release" in name
        if not (is_dll or is_zip):
            continue
        try:
            req = urllib.request.Request(a["browser_download_url"], headers={"User-Agent": "TokeerDRM"})
            raw = urllib.request.urlopen(req, timeout=120).read()
        except Exception:
            continue
        if is_zip:
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as z:
                    for n in z.namelist():
                        if os.path.basename(n).lower() == "opensteamtool.dll":
                            hashes.add(_sha256_bytes(z.read(n)))
            except Exception:
                pass
        else:
            hashes.add(_sha256_bytes(raw))
    return hashes


_latest_custom_cache = {"at": 0.0, "tag": "", "hashes": frozenset()}


def _latest_custom_dll(timeout=10):
    """(latest_tag, frozenset of sha256 hex) for OUR custom OpenSteamTool.dll release.
    Hashes come from the GitHub asset `digest` field (no download). If GitHub didn't
    populate digests, we download each DLL once and cache the hashes on disk by tag.
    Cached ~10 min in-process. Returns ("", frozenset()) on total failure."""
    now = time.time()
    if _latest_custom_cache["tag"] and (now - _latest_custom_cache["at"]) < 600:
        return _latest_custom_cache["tag"], _latest_custom_cache["hashes"]
    try:
        req = urllib.request.Request(CUSTOM_OST_API, headers={"User-Agent": "TokeerDRM"})
        data = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
    except Exception:
        return _latest_custom_cache["tag"], _latest_custom_cache["hashes"]  # stale or empty
    tag = data.get("tag_name", "") or ""
    hashes = _release_dll_hashes(data)
    if not hashes and tag:  # no digests — fall back to download+disk-cache (once per tag)
        cache = _read_hash_cache()
        if tag in cache:
            hashes = set(cache[tag])
        else:
            hashes = _download_dll_hashes(data)
            if hashes:
                cache[tag] = sorted(hashes)
                _write_hash_cache(cache)
    if tag:
        _latest_custom_cache.update(at=now, tag=tag, hashes=frozenset(hashes))
    return tag, frozenset(hashes)


def _installed_dll_hash(sp):
    dll_path = os.path.join(sp, "OpenSteamTool.dll")
    if not os.path.exists(dll_path):
        return None, None
    try:
        with open(dll_path, "rb") as f:
            data = f.read()
        return data, _sha256_bytes(data)
    except OSError:
        return None, None


def custom_dll_installed(sp=None):
    """True if the installed OpenSteamTool.dll is one of OUR custom builds — decided by
    EXACT sha256, never by file size. Order:
      1. marker records this exact DLL          → fast, no network
      2. DLL hash ∈ latest release's DLL hashes  → yes (and refresh the marker)
      3. DLL hash ∈ the marker's stored-tag release hashes → yes (an older custom build;
         the UI will offer an Update)
    Anything else (incl. a swapped/foreign DLL whose marker no longer matches) → False,
    so the user is taken through Fix and ends up on the exact latest modified DLL."""
    sp = sp or steam_path()
    if not sp:
        return False
    dll_data, dll_hash = _installed_dll_hash(sp)
    if not dll_hash:
        return False

    stored_hash, stored_tag = "", ""
    marker_path = os.path.join(sp, _CUSTOM_MARKER)
    if os.path.exists(marker_path):
        try:
            with open(marker_path, "r", encoding="utf-8") as f:
                parts = f.read().strip().split(":")
            stored_hash = parts[0] if parts else ""
            stored_tag  = parts[2] if len(parts) >= 3 else ""
        except OSError:
            pass

    # 1) Marker matches THIS exact DLL (offline-safe fast path).
    if len(stored_hash) == 64 and dll_hash == stored_hash:
        return True

    # 2) Is it byte-identical to the current latest release DLL?
    latest_tag, latest_hashes = _latest_custom_dll()
    if dll_hash in latest_hashes:
        _write_marker(sp, dll_data, latest_tag)  # keep the marker honest
        return True

    # 3) Is it an older-but-still-ours build (matches the stored tag's release)?
    if stored_tag and stored_tag != latest_tag:
        try:
            tag_api = f"https://api.github.com/repos/{CUSTOM_OST_REPO}/releases/tags/{stored_tag}"
            req = urllib.request.Request(tag_api, headers={"User-Agent": "TokeerDRM"})
            rel = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
            if dll_hash in _release_dll_hashes(rel):
                return True
        except Exception:
            pass

    return False


def custom_dll_needs_update(sp=None):
    """True unless the installed DLL is byte-identical to the latest custom release DLL.
    Hash-exact: ANY DLL that isn't the current modified build → update, so everyone
    converges on the exact same DLL. If we can't reach GitHub (no latest hashes), we
    don't nag."""
    sp = sp or steam_path()
    if not sp:
        return False
    _, dll_hash = _installed_dll_hash(sp)
    if not dll_hash:
        return False
    _, latest_hashes = _latest_custom_dll()
    if not latest_hashes:
        return False  # offline / API down → can't tell, don't nag
    return dll_hash not in latest_hashes


def install_custom_dll(progress=_noop, restart_steam=True):
    """Download our fork's OpenSteamTool.dll and replace the official one.
    Assumes the engine (proxy DLLs) is already installed — only swaps the core.
    restart_steam=False skips the post-install Steam launch (caller handles it)."""
    sp = steam_path()
    if not sp:
        return {"ok": False, "message": "Steam not found."}

    progress(8, "Finding latest custom OpenSteamTool release…")
    try:
        req = urllib.request.Request(CUSTOM_OST_API, headers={"User-Agent": "TokeerDRM"})
        data = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
    except Exception as e:
        return {"ok": False, "message": f"Couldn't reach GitHub: {e}"}

    # Try standalone .dll asset first, then a release zip.
    asset = next((a for a in data.get("assets", [])
                  if a.get("name", "").lower() == "opensteamtool.dll"), None)
    zip_asset = None if asset else next(
        (a for a in data.get("assets", [])
         if "release" in a.get("name", "").lower() and a["name"].lower().endswith(".zip")), None)

    if not asset and not zip_asset:
        return {"ok": False, "message": "Custom DLL not found in the GitHub release assets."}

    progress(15, "Downloading custom OpenSteamTool.dll…")
    try:
        url = (asset or zip_asset)["browser_download_url"]
        req = urllib.request.Request(url, headers={"User-Agent": "TokeerDRM"})
        resp = urllib.request.urlopen(req, timeout=120)
        total = int(resp.headers.get("Content-Length") or 0)
        buf = io.BytesIO()
        got = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            buf.write(chunk)
            got += len(chunk)
            if total:
                progress(15 + int(got * 45 / total), "Downloading…")
        raw = buf.getvalue()
    except Exception as e:
        return {"ok": False, "message": f"Couldn't download custom DLL: {e}"}

    # Extract from zip if needed.
    if zip_asset:
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                entry = next((n for n in z.namelist()
                              if os.path.basename(n).lower() == "opensteamtool.dll"), None)
                if not entry:
                    return {"ok": False, "message": "opensteamtool.dll not found in the release zip."}
                raw = z.read(entry)
        except Exception as e:
            return {"ok": False, "message": f"Couldn't extract DLL from zip: {e}"}

    progress(62, "Closing Steam…")
    _shutdown_steam(sp, progress, lo=62, hi=74)

    progress(75, "Installing custom OpenSteamTool.dll…")
    try:
        _add_defender_exclusion(sp)
        with open(os.path.join(sp, "OpenSteamTool.dll"), "wb") as f:
            f.write(raw)
        with open(os.path.join(sp, _CUSTOM_MARKER), "w", encoding="utf-8") as f:
            f.write(_marker_content(raw, data.get("tag_name", "")))
    except PermissionError:
        _start_steam(sp)
        return {"ok": False, "message": "Permission denied writing to Steam folder. Run as Administrator."}
    except OSError as e:
        if getattr(e, "winerror", None) == 225 or "virus" in str(e).lower():
            _start_steam(sp)
            return {"ok": False, "defender": True, "message": LUATOOLS_HINT, "url": LUATOOLS_URL}
        _start_steam(sp)
        return {"ok": False, "message": f"Couldn't install custom DLL: {e}"}

    if restart_steam:
        progress(90, "Restarting Steam…")
        _start_steam(sp)
    progress(100, "Custom DLL installed.")
    return {"ok": True, "message": "Enhanced DLL installed. Sign in to Steam, then launch your game."}


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
