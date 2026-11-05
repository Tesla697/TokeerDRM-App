"""
TokeerDRM — standalone desktop app.

  • Generate: run extract_tickets.exe → AppTicket + ETicket → server → 6-char code
  • Redeem:   code → server → write AppTicket + ETicket to the registry → play

UI is HTML/CSS/JS in a native webview (pywebview). The Python side below is the
privileged bridge: it runs the extractor, writes the registry, and talks to the
code-store server. Exposed to JS as `window.pywebview.api.*`.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse

import requests

import ost_setup
import save_watchdog
import save_vault

try:
    import webview
except ImportError:
    print("pywebview is required:  pip install pywebview")
    raise

# System DPI awareness — set at runtime (before any window) instead of via a
# custom manifest. A custom manifest *replaces* PyInstaller's default one, which
# strips dependency declarations and makes the exe fail to launch on other PCs.
# From source, Python's own manifest already set this, so the call is a no-op.
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.user32.SetProcessDPIAware()  # system-aware (not per-monitor)
    except Exception:
        pass

try:
    from server_config import SERVER_URL  # gitignored; bundled into the exe at build
except ImportError:
    SERVER_URL = "http://your-server:8091"  # see server_config.example.py
try:
    from server_config import SERVER_URLS  # tried in order, first one that connects wins
except ImportError:
    SERVER_URLS = [SERVER_URL]
APP_TITLE = "TokeerDRM"
APP_VERSION = "1.0.36"                       # bump on every release
UPDATE_REPO = "luatools-dedivision/TokeerDRM-App"  # Forgejo repo whose latest release gates the app
# Forgejo's own download links skip the CDN and crawl on some routes, so updates
# come through the luastools.xyz edge mirror and fall back to Forgejo.
UPDATE_MIRROR = "https://luastools.xyz/app"
UPDATE_DOWNLOAD = UPDATE_MIRROR + "/download"


def _latest_release(timeout):
    """Latest release JSON (tag_name + assets), mirror first. None if both fail."""
    for url in (UPDATE_MIRROR + "/latest",
                f"https://git.lua.tools/api/v1/repos/{UPDATE_REPO}/releases/latest"):
        try:
            r = requests.get(url, headers={"User-Agent": "TokeerDRM"}, timeout=timeout)
            if r.status_code == 200:
                d = r.json()
                if d.get("tag_name") and isinstance(d.get("assets"), list):
                    return d
        except Exception:
            pass
    return None
WINDOW = None  # set in main(); lets the API push install progress to the UI

# ---------------------------------------------------------------------------
# One-click deep link (tokeerdrm://redeem?code=XXXXXX)
# The Discord "Redeem in App" button opens https://luastools.xyz/redeem?code=…
# which redirects to this custom scheme. We register the handler, guard a single
# instance, and hand the code to the running UI so it auto-applies + launches.
# ---------------------------------------------------------------------------
URI_SCHEME = "tokeerdrm"
PENDING_CODE_FILE = os.path.join(tempfile.gettempdir(), "tokeerdrm_pending_code.txt")
_SINGLE_INSTANCE_HANDLE = None  # keep the mutex handle alive for the process lifetime


def _register_uri_scheme() -> None:
    """Register tokeerdrm:// under HKCU (per-user, no admin) pointing at this exe.
    Only meaningful in the packaged build — from source there's no stable exe to
    hand the OS. Idempotent: safe to run on every launch."""
    if sys.platform != "win32" or not ost_setup._packaged():
        return
    try:
        import winreg
        exe = ost_setup._self_exe()
        base = r"Software\Classes\%s" % URI_SCHEME
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as k:
            winreg.SetValueEx(k, None, 0, winreg.REG_SZ, "URL:TokeerDRM Protocol")
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\DefaultIcon") as k:
            winreg.SetValueEx(k, None, 0, winreg.REG_SZ, f'"{exe}",0')
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\shell\open\command") as k:
            winreg.SetValueEx(k, None, 0, winreg.REG_SZ, f'"{exe}" "%1"')
    except Exception:
        pass


def _code_from_argv(argv) -> str | None:
    """Pull a 6-char code out of a tokeerdrm://redeem?code=XXXXXX argument."""
    import urllib.parse
    for a in argv:
        if not a or not a.lower().startswith(URI_SCHEME + ":"):
            continue
        try:
            code = ""
            parsed = urllib.parse.urlparse(a)
            qs = urllib.parse.parse_qs(parsed.query)
            if qs.get("code"):
                code = qs["code"][0]
            code = "".join(c for c in code.upper() if c.isalnum())[:6]
            if len(code) == 6:
                return code
        except Exception:
            pass
    return None


def _acquire_single_instance() -> bool:
    """True if we're the first/primary instance. A named Win32 mutex survives for
    the life of the process; a second launch sees ERROR_ALREADY_EXISTS (183)."""
    global _SINGLE_INSTANCE_HANDLE
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\TokeerDRM_SingleInstance")
        already = ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
        _SINGLE_INSTANCE_HANDLE = handle  # keep alive
        return not already
    except Exception:
        return True  # if the guard fails, don't block the app from opening


def _deliver_code_to_ui(code: str) -> None:
    """Surface the running window and hand the code to the JS auto-redeem flow."""
    if WINDOW is None:
        return
    try:
        try: WINDOW.restore()   # un-minimize if needed
        except Exception: pass
        try: WINDOW.show()
        except Exception: pass
        WINDOW.evaluate_js(
            f"window.__ostIncomingCode && window.__ostIncomingCode({json.dumps(code)})")
    except Exception:
        pass


def _watch_pending_codes() -> None:
    """Primary-instance watcher: a second launch (from a fresh deep-link click while
    the app is already open) drops the code in PENDING_CODE_FILE; pick it up and run it."""
    def worker():
        while True:
            try:
                if os.path.exists(PENDING_CODE_FILE):
                    code = ""
                    try:
                        with open(PENDING_CODE_FILE, "r", encoding="utf-8") as f:
                            code = f.read()
                    finally:
                        try: os.remove(PENDING_CODE_FILE)
                        except OSError: pass
                    code = "".join(c for c in (code or "").upper() if c.isalnum())[:6]
                    if len(code) == 6:
                        _deliver_code_to_ui(code)
            except Exception:
                pass
            time.sleep(0.6)
    threading.Thread(target=worker, daemon=True).start()


def _version_gt(a, b):
    """True if version string a > b (numeric, dotted)."""
    def parts(v):
        out = []
        for x in str(v or "0").split("."):
            try:
                out.append(int("".join(ch for ch in x if ch.isdigit()) or 0))
            except Exception:
                out.append(0)
        return out
    pa, pb = parts(a), parts(b)
    n = max(len(pa), len(pb))
    pa += [0] * (n - len(pa)); pb += [0] * (n - len(pb))
    return pa > pb


# ---------------------------------------------------------------------------
# Paths (work from source, a PyInstaller one-file build, and a Nuitka onefile
# build). Each packager stages bundled data in a different place:
#   PyInstaller -> sys._MEIPASS
#   Nuitka      -> next to __file__ (the onefile unpack dir)
#   source      -> next to this script
# Probe them in order and return the first where the resource actually exists, so
# one code path covers all three without depending on which packager built us.
# ---------------------------------------------------------------------------

def resource_path(rel: str) -> str:
    bases = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bases.append(meipass)
    bases.append(os.path.dirname(os.path.abspath(__file__)))
    try:
        bases.append(os.path.dirname(os.path.abspath(sys.argv[0])))
    except Exception:
        pass
    for base in bases:
        candidate = os.path.join(base, rel)
        if os.path.exists(candidate):
            return candidate
    return os.path.join(bases[0], rel)


EXTRACT_EXE = resource_path("extract_tickets.exe")
WEB_INDEX = resource_path(os.path.join("web", "index.html"))


# ---------------------------------------------------------------------------
# Engine helpers
# ---------------------------------------------------------------------------

def _run_extract(app_id: str) -> dict | None:
    """Run extract_tickets.exe --pipe <appid>. Returns
    {app_id, appticket, eticket, steam_id} or None if the account doesn't own it."""
    if not os.path.exists(EXTRACT_EXE):
        raise RuntimeError("extract_tickets.exe is missing next to the app.")

    creationflags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    proc = subprocess.run(
        [EXTRACT_EXE, "--pipe", str(app_id)],
        capture_output=True, text=True, timeout=40,
        creationflags=creationflags,
        cwd=os.path.dirname(EXTRACT_EXE) or None,
    )
    lines = [ln for ln in (proc.stdout or "").splitlines() if "|" in ln]
    if not lines:
        return None
    parts = lines[-1].strip().split("|")
    if len(parts) < 4:
        return None
    appticket, eticket, steam_id = parts[1].strip(), parts[2].strip(), parts[3].strip()
    if not appticket or not eticket:
        return None
    return {
        "app_id": parts[0].strip(),
        "appticket": appticket,
        "eticket": eticket,
        "steam_id": steam_id,
    }


_STEAMID64_BASE = 76561197960265728  # 0x0110000100000000 — individual/public base

# appid -> SteamID64 of the account the last redeem activated it with. The save
# watchdog needs this to know which userdata folder the game will run under.
_LAST_ACTIVATION: dict = {}


def _current_steam_id() -> str:
    """SteamID64 of the account currently logged into Steam on this PC, or "".

    Read from HKCU\\Software\\Valve\\Steam\\ActiveProcess\\ActiveUser (the 32-bit
    account id of the running Steam user; 0 when Steam isn't running). This is the
    LIVE account, independent of any ticket, so it can prove whether an extracted
    ticket really belongs to the person generating a code."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Valve\Steam\ActiveProcess") as key:
            account_id, _ = winreg.QueryValueEx(key, "ActiveUser")
        account_id = int(account_id)
        if account_id <= 0:
            return ""
        return str(_STEAMID64_BASE + account_id)
    except Exception:
        return ""


def _machine_guid() -> str:
    """This machine's Windows MachineGuid, or "".

    Read from HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid — the same value
    the validator hashes into the ticket's HWID. A ticket-issued code is locked to
    the machine that opened the ticket, so the server compares this against the
    bound machine and refuses a code shared to a different PC. Reads bit-identical
    to the validator's PowerShell, so a legitimate redeem on the right machine
    always matches."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Cryptography") as key:
            guid, _ = winreg.QueryValueEx(key, "MachineGuid")
        return str(guid).strip()
    except Exception:
        return ""


def _appticket_owner_steamid(appticket_hex: str) -> str:
    """SteamID64 embedded in an App Ownership Ticket (owner), or "".

    Mirror of the server's parser: little-endian uint64 at offset 8."""
    try:
        data = bytes.fromhex((appticket_hex or "").strip())
        if len(data) < 16:
            return ""
        sid = int.from_bytes(data[8:16], "little")
        return str(sid) if sid >= _STEAMID64_BASE else ""
    except Exception:
        return ""


def _write_registry(app_id: str, appticket_hex: str, eticket_hex: str) -> None:
    """Write AppTicket (ownership) + ETicket (encrypted) + SteamID to Steam's
    credential store.

    SteamID matters as much as the tickets. The engine spoofs IClientUser::GetSteamID
    from this value FIRST and only falls back to the id inside AppTicket when it is
    absent, so a value left over from an earlier redeem outranks the tickets we just
    wrote. Denuvo then sees a SteamID from one account and an ownership ticket from
    another, and refuses the launch with 88500012 — on that machine, permanently,
    no matter which account the pool hands out next, because nothing here ever
    revisited the stale value.

    Written as REG_SZ decimal digits, which is the only form the engine parses."""
    import winreg
    _write_ticket_key(winreg.HKEY_CURRENT_USER, "", app_id, appticket_hex, eticket_hex)


def _write_ticket_key(root, prefix: str, app_id: str, appticket_hex: str, eticket_hex: str) -> None:
    """Write the ticket under <root>\\<prefix>Software\\Valve\\Steam\\Apps\\<appid>.
    prefix is "<SID>\\" when writing another account's hive under HKEY_USERS."""
    import winreg
    key_path = f"{prefix}Software\\Valve\\Steam\\Apps\\{app_id}"
    key = winreg.CreateKeyEx(root, key_path, 0,
                             winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE)
    try:
        winreg.SetValueEx(key, "AppTicket", 0, winreg.REG_BINARY, bytes.fromhex(appticket_hex))
        winreg.SetValueEx(key, "ETicket", 0, winreg.REG_BINARY, bytes.fromhex(eticket_hex))

        owner = _appticket_owner_steamid(appticket_hex)
        if owner:
            winreg.SetValueEx(key, "SteamID", 0, winreg.REG_SZ, owner)
        else:
            # Couldn't read an owner out of the ticket. Leaving whatever is there
            # would let a stale value keep outranking this ticket, so remove it and
            # let the engine derive the id from the ticket itself.
            try:
                winreg.DeleteValue(key, "SteamID")
            except OSError:
                pass
    finally:
        winreg.CloseKey(key)


def _write_ticket_other_accounts(app_id: str, appticket_hex: str, eticket_hex: str) -> int:
    """Admin only: copy the ticket into the hive of the account Steam runs as (or
    every loaded Steam account when Steam isn't running). Covers a standard user
    whose Steam or app got elevated onto an admin account. Returns hives written."""
    import winreg
    written = 0
    for sid in ost_setup.ticket_hives():
        try:
            _write_ticket_key(winreg.HKEY_USERS, sid + "\\", app_id, appticket_hex, eticket_hex)
            written += 1
        except OSError:
            pass
    return written


def _ticket_handoff_file() -> str:
    # %PUBLIC%, so the elevated helper finds it whichever account UAC elevates to.
    base = os.environ.get("PUBLIC") or tempfile.gettempdir()
    return os.path.join(base, "tokeerdrm_ticket_handoff.json")


def _write_ticket_elevated(app_id: str, appticket_hex: str, eticket_hex: str) -> bool:
    """Ask for admin once and let the elevated helper put the ticket into the
    account Steam is running as. Used when Steam runs under an account this
    process can't write to. False if the prompt was declined or it failed."""
    path = _ticket_handoff_file()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"app_id": app_id, "appticket": appticket_hex, "eticket": eticket_hex}, f)
        ost_setup.clear_result()
        ost_setup.relaunch_elevated("--write-ticket", timeout_s=60)
        res = ost_setup.read_result() or {}
        return bool(res.get("ok"))
    except Exception:
        return False
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _write_ticket_helper_main() -> dict:
    """Elevated side of _write_ticket_elevated."""
    path = _ticket_handoff_file()
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    # Our own HKCU here is the admin account UAC elevated to, which is usually the
    # account Steam got moved onto, so this write alone already fixes it.
    _write_registry(d["app_id"], d["appticket"], d["eticket"])
    n = _write_ticket_other_accounts(d["app_id"], d["appticket"], d["eticket"])
    return {"ok": True, "hives": n}


# Some ISPs block the code store's raw IP/port (the app sits on Offline until the
# user turns on WARP) and some filter .xyz at DNS. So every call walks SERVER_URLS
# (Cloudflare host first, raw IP last), sticks with whichever one answered, and if
# a host won't connect we look it up over DNS-over-HTTPS and retry once.
_DOH_ENDPOINTS = (
    "https://1.1.1.1/dns-query",
    "https://1.0.0.1/dns-query",
    "https://8.8.8.8/resolve",
)
_dns_pins: dict[str, str] = {}
_orig_getaddrinfo = socket.getaddrinfo
_good_server = None


def _pinned_getaddrinfo(host, *args, **kwargs):
    return _orig_getaddrinfo(_dns_pins.get(host, host), *args, **kwargs)


def _pin_host(host: str) -> bool:
    """Resolve host over DoH and pin it. TLS still checks the real name."""
    if not host or host in _dns_pins or host.replace(".", "").isdigit():
        return False
    for doh in _DOH_ENDPOINTS:
        try:
            r = requests.get(doh, params={"name": host, "type": "A"},
                             headers={"Accept": "application/dns-json"}, timeout=6)
            ips = [a["data"] for a in r.json().get("Answer", []) if a.get("type") == 1]
        except Exception:
            continue
        if ips:
            _dns_pins[host] = ips[0]
            socket.getaddrinfo = _pinned_getaddrinfo
            return True
    return False


def _server_request(method: str, path: str, timeout: int = 25, **kw) -> requests.Response:
    global _good_server
    servers = [s.rstrip("/") for s in SERVER_URLS if s]
    if _good_server in servers:
        servers.remove(_good_server)
        servers.insert(0, _good_server)
    last = None
    for base in servers:
        host = urllib.parse.urlparse(base).hostname or ""
        for attempt in range(2):
            try:
                r = requests.request(method, base + path, timeout=(6, timeout), **kw)
                _good_server = base
                return r
            except requests.exceptions.ConnectionError as e:
                # Only failed connects move on. A read timeout may already have
                # reached the server, and a redeem must not be sent twice.
                last = e
                if attempt or not _pin_host(host):
                    break
    raise last


def _server_post(path: str, body: dict, timeout: int = 25) -> tuple[int, dict]:
    r = _server_request("POST", path, json=body, timeout=timeout)
    try:
        data = r.json()
    except Exception:
        data = {"reason": r.text[:200]}
    return r.status_code, data


def _cleanup_update_leftovers():
    """Delete the `<exe>.old` (and any stray `.new`) left by a self-update. Runs in the
    background on startup: by now the previous process has exited and released its lock,
    so the rename-swap's leftover gets removed reliably here instead of in the detached
    helper, which can lose the race against the lingering PyInstaller bootloader."""
    if not ost_setup._packaged():
        return
    _exe = ost_setup._self_exe()
    targets = [_exe + ".old", _exe + ".new"]

    def _worker():
        for _ in range(30):  # ~30s of attempts while the old process finishes exiting
            remaining = False
            for t in targets:
                if os.path.exists(t):
                    try:
                        os.remove(t)
                    except OSError:
                        remaining = True
            if not remaining:
                return
            time.sleep(1)

    threading.Thread(target=_worker, daemon=True).start()


def _push_progress(pct, msg):
    """Push a progress tick to the engine bar in the UI."""
    try:
        if WINDOW is not None:
            WINDOW.evaluate_js(
                f"window.__ostProgress && window.__ostProgress({int(pct)}, {json.dumps(msg)})")
    except Exception:
        pass


def _push_save(pct, msg, kind=None):
    """Save carry-over status for the Activate tab (pct None hides the bar)."""
    try:
        if WINDOW is not None:
            WINDOW.evaluate_js(
                "window.__saveProgress && window.__saveProgress(%s, %s, %s)"
                % (json.dumps(pct), json.dumps(msg), json.dumps(kind)))
    except Exception:
        pass


def _pump_engine_progress():
    """Relay the elevated helper's published progress to the UI bar (so it MOVES
    during a UAC-elevated install/uninstall instead of freezing)."""
    p = ost_setup.read_progress()
    if p:
        _push_progress(p[0], p[1])


# ---------------------------------------------------------------------------
# JS-facing API
# ---------------------------------------------------------------------------

class Api:
    def status(self) -> dict:
        """Server reachability — drives the live status pill."""
        try:
            r = _server_request("GET", "/health", timeout=6)
            ok = r.status_code == 200 and r.json().get("success")
            return {"online": bool(ok), "server": _good_server}
        except Exception:
            return {"online": False, "server": _good_server}

    # -- Force update --------------------------------------------------------
    def version_info(self) -> dict:
        """Compare this build with the latest GitHub release. update_required=True
        blocks the whole UI until the user updates."""
        info = {"current": APP_VERSION, "latest": APP_VERSION, "update_required": False,
                "url": UPDATE_DOWNLOAD}
        d = _latest_release(8)
        if d:
            tag = (d.get("tag_name") or "").lstrip("vV")
            if tag:
                info["latest"] = tag
                info["update_required"] = _version_gt(tag, APP_VERSION)
        return info

    def open_url(self, url: str) -> dict:
        try:
            import webbrowser
            webbrowser.open(url)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def update_now(self) -> dict:
        """In-app self-update: download the latest release .exe, swap it in place,
        and relaunch — no browser, no manual download. Only works in the packaged
        (frozen) build; from source there's nothing to swap."""
        if not ost_setup._packaged():
            return {"ok": False, "message": "In-app update only works in the packaged app — use the GitHub link."}

        def progress(pct, msg):
            try:
                if WINDOW is not None:
                    WINDOW.evaluate_js(
                        f"window.__updProgress && window.__updProgress({int(pct)}, {json.dumps(msg)})")
            except Exception:
                pass

        try:
            progress(5, "Finding the latest release…")
            rel = _latest_release(20)
            if not rel:
                return {"ok": False, "message": "Couldn't reach the update server. Check your connection and try again."}
            tag = rel.get("tag_name") or ""
            asset = next((a for a in rel.get("assets", [])
                          if a.get("name", "").lower().endswith(".exe")), None)
            if not asset:
                return {"ok": False, "message": "The latest release has no .exe to download."}

            cur_exe = ost_setup._self_exe()
            new_exe = cur_exe + ".new"
            # Mirror first (edge cached), then the release host itself.
            sources = [f"{UPDATE_MIRROR}/asset?tag={urllib.parse.quote(tag)}"
                       f"&name={urllib.parse.quote(asset['name'])}"]
            if asset.get("browser_download_url") and asset["browser_download_url"] not in sources:
                sources.append(asset["browser_download_url"])
            sources.append(f"https://git.lua.tools/{UPDATE_REPO}/releases/download/"
                           f"{urllib.parse.quote(tag)}/{urllib.parse.quote(asset['name'])}")
            want = int(asset.get("size") or 0)
            last_err = None
            for src in sources:
                progress(10, "Downloading update…")
                try:
                    with requests.get(src, headers={"User-Agent": "TokeerDRM"},
                                      timeout=(15, 120), stream=True) as dl:
                        dl.raise_for_status()
                        total = int(dl.headers.get("Content-Length") or 0) or want
                        got = 0
                        with open(new_exe, "wb") as f:
                            for chunk in dl.iter_content(65536):
                                if not chunk:
                                    continue
                                f.write(chunk)
                                got += len(chunk)
                                if total:
                                    progress(10 + int(got * 85 / total), "Downloading update…")
                    if want and got != want:
                        raise IOError(f"got {got} of {want} bytes")
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    try:
                        os.remove(new_exe)
                    except OSError:
                        pass
            if last_err is not None:
                raise last_err

            progress(98, "Installing update…")
            old_exe = cur_exe + ".old"

            def _quit_soon():
                def _q():
                    import time as _t
                    _t.sleep(1)  # let the UI show the message + the new process start
                    try:
                        if WINDOW is not None:
                            WINDOW.destroy()
                    except Exception:
                        pass
                    os._exit(0)
                threading.Thread(target=_q, daemon=True).start()

            # The new build must unpack its OWN files. Inheriting our PyInstaller env
            # makes it reuse this old build's _MEI folder (new code, old web/ + tools).
            relaunch_env = {k: v for k, v in os.environ.items()
                            if not k.startswith("_PYI") and k != "_MEIPASS2"}
            relaunch_env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"

            # Preferred swap: RENAME in place. Windows lets you rename a *running* exe
            # (the process keeps its handle to the renamed-away file), so there's no
            # waiting and no AV-hostile "background cmd copying an .exe" — that copy
            # approach was getting blocked/locked, leaving a stray <exe>.new the user
            # had to rename by hand. The relaunched build deletes <exe>.old on startup
            # via _cleanup_update_leftovers(), so no leftovers.
            try:
                if os.path.exists(old_exe):
                    try:
                        os.remove(old_exe)
                    except OSError:
                        pass
                os.replace(cur_exe, old_exe)          # move the running exe aside
                try:
                    os.replace(new_exe, cur_exe)      # drop the new build into place
                except OSError:
                    os.replace(old_exe, cur_exe)      # roll back so the app isn't bricked
                    raise
                # DETACHED_PROCESS so the new app isn't tied to this dying one.
                # --relaunched skips the single-instance guard during the handover.
                subprocess.Popen([cur_exe, "--relaunched"], creationflags=0x00000008, close_fds=True,
                                 env=relaunch_env)
                _quit_soon()
                return {"ok": True, "message": "Updating… TokeerDRM will restart automatically."}
            except OSError:
                pass  # rare (folder locked mid-sync/AV held the rename) — copy fallback below

            # Fallback: detached helper waits for full exit, then overwrites in place.
            pid = os.getpid()
            bat = os.path.join(tempfile.gettempdir(), f"tokeerdrm_update_{pid}.bat")
            lines = [
                "@echo off",
                f'set "CUR={cur_exe}"',
                f'set "NEW={new_exe}"',
                f'set "OLD={old_exe}"',
                "set /a n=0",
                ":waitloop",
                f'tasklist /fi "PID eq {pid}" 2>nul | find "{pid}" >nul',
                "if errorlevel 1 goto swap",
                "set /a n+=1",
                "if %n% geq 30 goto swap",
                "ping -n 2 127.0.0.1 >nul",
                "goto waitloop",
                ":swap",
                "set /a m=0",
                ":swaploop",
                'copy /y "%NEW%" "%CUR%" >nul 2>&1',
                'fc /b "%NEW%" "%CUR%" >nul 2>&1',
                "if not errorlevel 1 goto done",
                "set /a m+=1",
                "if %m% geq 40 goto done",
                "ping -n 2 127.0.0.1 >nul",
                "goto swaploop",
                ":done",
                'start "" "%CUR%" --relaunched',
                'del "%NEW%" >nul 2>&1',
                'del "%OLD%" >nul 2>&1',
                'del "%~f0"',
            ]
            with open(bat, "w", encoding="ascii") as f:
                f.write("\r\n".join(lines) + "\r\n")
            DETACHED = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
            subprocess.Popen(["cmd", "/c", bat], creationflags=DETACHED, close_fds=True,
                             env=relaunch_env)
            _quit_soon()
            return {"ok": True, "message": "Updating… TokeerDRM will restart automatically."}
        except Exception as e:
            return {"ok": False, "message": f"Update failed: {e}. Use the GitHub link to download manually."}

    # -- OpenSteamTool engine (required to apply Denuvo tickets) --------------
    def engine_status(self) -> dict:
        """Is the Denuvo-capable engine (OpenSteamTool) active?"""
        try:
            return ost_setup.engine_status()
        except Exception as e:
            return {"installed": False, "error": str(e)}

    def engine_check(self) -> dict:
        """What does the engine need (no elevation)? Drives auto-repair/update on
        launch: returns {action: none|install|repair|update, status, installed_tag,
        latest_tag}. The UI auto-runs install_engine() for repair/update."""
        try:
            return ost_setup.ensure_engine()
        except Exception as e:
            try:
                st = ost_setup.engine_status()
            except Exception:
                st = {"ready": False, "installed": False}
            return {"action": "none", "status": st, "error": str(e)}

    def install_engine(self) -> dict:
        """Install official OpenSteamTool. Needs admin (writes into Program Files +
        sets a Defender exclusion), so if we're not elevated, relaunch elevated via
        a UAC prompt and wait."""
        if not ost_setup.is_admin():
            try:
                ost_setup.clear_progress()
                ost_setup.clear_result()
                _push_progress(8, "Approve the Windows prompt to install…")
                ost_setup.relaunch_elevated("--install-engine", on_progress=_pump_engine_progress)
            except Exception as e:
                return {"ok": False, "message": f"Administrator approval is required: {e}"}
            _push_progress(100, "Done")
            res = ost_setup.read_result() or {}
            if res.get("defender"):  # Defender blocked it → route to LuaTools
                return res
            # Trust the helper on readiness (the toml can lag a Steam restart), but
            # not on the files: antivirus can delete them right after the helper
            # writes them, and then the app sat on "installed" with nothing there.
            if res.get("ok"):
                time.sleep(3)
                missing = ost_setup.missing_engine_files()
                if missing:
                    return {"ok": False, "message": (
                        f"Setup finished, but {', '.join(missing)} isn't in your Steam folder "
                        f"({ost_setup.steam_path()}). Your antivirus most likely removed it. Add an "
                        "exception for the Steam folder or pause real-time protection, then click "
                        "Install again.")}
                return {"ok": True, "message": res.get("message") or "OpenSteamTool ready. Sign in to Steam, then redeem."}
            st = ost_setup.engine_status()
            if st.get("ready"):
                return {"ok": True, "message": "OpenSteamTool ready. Sign in to Steam, then redeem."}
            return {"ok": False, "message": res.get("message") or "Setup didn't complete — was the prompt declined? Try again."}

        # Already elevated: install in-process with live progress.
        def progress(pct, msg):
            try:
                if WINDOW is not None:
                    WINDOW.evaluate_js(
                        f"window.__ostProgress && window.__ostProgress({int(pct)}, {json.dumps(msg)})")
            except Exception:
                pass
        try:
            return ost_setup.install_ost_custom(
                progress=progress,
                fallback_zip=resource_path("OpenSteamTool-Release.zip"),
            )
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def update_engine(self) -> dict:
        """Force-update OpenSteamTool to the latest release (re-downloads + replaces
        the engine even if it's already present). Elevates via UAC when needed."""
        if not ost_setup.is_admin():
            try:
                ost_setup.clear_progress()
                ost_setup.clear_result()
                _push_progress(8, "Approve the Windows prompt to update…")
                ost_setup.relaunch_elevated("--update-engine", on_progress=_pump_engine_progress)
            except Exception as e:
                return {"ok": False, "message": f"Administrator approval is required: {e}"}
            _push_progress(100, "Done")
            res = ost_setup.read_result() or {}
            if res.get("defender"):
                return res
            st = ost_setup.engine_status()
            if st.get("ready"):
                return {"ok": True, "message": "OpenSteamTool updated."}
            return {"ok": False, "message": res.get("message") or "Update didn't complete — was the prompt declined? Try again."}

        def progress(pct, msg):
            try:
                if WINDOW is not None:
                    WINDOW.evaluate_js(
                        f"window.__ostProgress && window.__ostProgress({int(pct)}, {json.dumps(msg)})")
            except Exception:
                pass
        try:
            return ost_setup.install_ost_custom(
                progress=progress,
                fallback_zip=resource_path("OpenSteamTool-Release.zip"),
                force=True,
            )
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def uninstall_engine(self) -> dict:
        """Remove OpenSteamTool. Needs admin → elevate via UAC when not already."""
        if not ost_setup.is_admin():
            try:
                ost_setup.clear_progress()
                _push_progress(8, "Approve the Windows prompt to remove…")
                ost_setup.relaunch_elevated("--uninstall-engine", on_progress=_pump_engine_progress)
            except Exception as e:
                return {"ok": False, "message": f"Administrator approval is required: {e}"}
            _push_progress(100, "Done")
            st = ost_setup.engine_status()
            if not st.get("installed"):
                return {"ok": True, "message": "OpenSteamTool removed."}
            return {"ok": False, "message": "Uninstall didn't complete — was the prompt declined?"}

        def progress(pct, msg):
            try:
                if WINDOW is not None:
                    WINDOW.evaluate_js(
                        f"window.__ostProgress && window.__ostProgress({int(pct)}, {json.dumps(msg)})")
            except Exception:
                pass
        try:
            return ost_setup.uninstall_ost(progress=progress)
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def dll_status(self) -> dict:
        """Check whether our custom OpenSteamTool.dll is installed (and up to date)."""
        try:
            st = ost_setup.engine_status()
            sp = st.get("steam_path")
            custom = ost_setup.custom_dll_installed(sp)
            needs_update = custom and ost_setup.custom_dll_needs_update(sp)
            return {
                "custom_installed": custom,
                "needs_fix":    st.get("installed") and not custom,
                "needs_update": needs_update,
            }
        except Exception as e:
            return {"custom_installed": False, "needs_fix": False, "needs_update": False, "error": str(e)}

    def fix_dll(self) -> dict:
        """Replace the original OpenSteamTool.dll with our enhanced fork build."""
        if not ost_setup.is_admin():
            try:
                ost_setup.clear_progress()
                ost_setup.clear_result()
                _push_progress(8, "Approve the Windows prompt to install the enhanced DLL…")
                ost_setup.relaunch_elevated("--fix-dll", on_progress=_pump_engine_progress)
            except Exception as e:
                return {"ok": False, "message": f"Administrator approval is required: {e}"}
            _push_progress(100, "Done")
            return ost_setup.read_result() or {"ok": False, "message": "No result from elevated helper."}

        def progress(pct, msg):
            try:
                if WINDOW is not None:
                    WINDOW.evaluate_js(
                        f"window.__dllProgress && window.__dllProgress({int(pct)}, {json.dumps(msg)})")
            except Exception:
                pass
        try:
            return ost_setup.install_custom_dll(progress=progress)
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def cloud_status(self) -> dict:
        """Whether Steam Cloud save redirection (CloudRedirect) is on. Drives the
        mandatory enable prompt shown before redeeming."""
        try:
            return ost_setup.cloud_status()
        except Exception as e:
            # Fail open: if we can't tell, don't block redeem behind a cloud prompt.
            return {"available": False, "enabled": False,
                    "supported": False, "error": str(e)}

    def enable_cloud(self) -> dict:
        """Turn on cloud saves. Writes into the Steam folder → needs admin, so
        elevate via UAC when we aren't already."""
        if not ost_setup.is_admin():
            try:
                ost_setup.clear_progress()
                ost_setup.clear_result()
                _push_progress(8, "Approve the Windows prompt to enable cloud saves…")
                ost_setup.relaunch_elevated("--enable-cloud", on_progress=_pump_engine_progress)
            except Exception as e:
                return {"ok": False, "message": f"Administrator approval is required: {e}"}
            _push_progress(100, "Done")
            return ost_setup.read_result() or {"ok": False, "message": "No result from elevated helper."}

        def progress(pct, msg):
            try:
                if WINDOW is not None:
                    WINDOW.evaluate_js(
                        f"window.__ostProgress && window.__ostProgress({int(pct)}, {json.dumps(msg)})")
            except Exception:
                pass
        try:
            return ost_setup.enable_cloud(progress=progress)
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def app_name(self, app_id: str) -> dict:
        """Best-effort game name for nicer display (Steam store API)."""
        app_id = "".join(c for c in str(app_id) if c.isdigit())
        if not app_id:
            return {"name": ""}
        try:
            r = requests.get(
                "https://store.steampowered.com/api/appdetails",
                params={"appids": app_id, "filters": "basic"}, timeout=8,
            )
            d = r.json().get(app_id, {})
            if d.get("success"):
                return {"name": d["data"].get("name", "")}
        except Exception:
            pass
        return {"name": ""}

    def generate(self, app_id: str, max_uses: int = 1) -> dict:
        """Extract the owner's tickets and mint a shareable code."""
        app_id = "".join(c for c in str(app_id) if c.isdigit())
        if not app_id:
            return {"ok": False, "error": "Enter a Steam AppID."}

        # Steam's RequestEncryptedAppTicket is async; the first call sometimes returns
        # before the response arrives (empty output). Retry up to 3× with a short pause.
        tickets = None
        last_err = None
        for attempt in range(3):
            try:
                tickets = _run_extract(app_id)
            except Exception as e:
                last_err = str(e)
                break  # hard error (exe missing etc.) — no point retrying
            if tickets:
                break
            if attempt < 2:
                time.sleep(1.5)

        if not tickets:
            return {"ok": False, "error": last_err or (
                f"This Steam account doesn't own app {app_id} (no ticket). "
                f"Make sure Steam is running and signed in."
            )}

        # Anti-resell guard: the extracted ticket must belong to the account
        # currently logged into Steam. After a redeem, the ORIGINAL owner's ticket
        # is cached in this PC's registry; re-extracting it here would let someone
        # who only redeemed a code mint fresh codes for a game they don't own.
        # Block that when the live account and the ticket's owner disagree.
        current_sid = _current_steam_id()
        owner_sid = _appticket_owner_steamid(tickets["appticket"])
        if current_sid and owner_sid and current_sid != owner_sid:
            return {"ok": False, "error": (
                "This game was activated with a redeemed code, your Steam account "
                "doesn't own it, so you can't generate codes for it."
            )}

        try:
            status, data = _server_post("/drm/generate", {
                "appticket": tickets["appticket"],
                "eticket": tickets["eticket"],
                "steam_id": tickets["steam_id"],
                "app_id": app_id,
                "max_uses": int(max_uses),
                "created_by_user": tickets["steam_id"],
                "current_steam_id": current_sid,
            })
        except Exception as e:
            return {"ok": False, "error": f"Server unreachable: {e}"}

        if status != 200 or not data.get("success"):
            return {"ok": False, "error": data.get("reason") or data.get("error") or f"Server error {status}"}
        return {
            "ok": True,
            "code": data.get("code", ""),
            "max_uses": data.get("max_uses", max_uses),
            "expires_in": data.get("expires_in", 86400),
            "app_id": app_id,
        }

    def redeem(self, code: str) -> dict:
        """Redeem a code and write both tickets to the registry for its game."""
        code = (code or "").strip().upper()
        if len(code) != 6:
            return {"ok": False, "error": "Enter the 6-character code."}

        # Gate on the engine: a Denuvo ticket only applies when OpenSteamTool is active
        # AND pointed at the library (toml → config\stplug-in). If it isn't, writing the
        # ticket is wasted — block redeem and tell the UI to surface repair/setup, WITHOUT
        # burning the one-use code on the server. (Fails open only if detection itself
        # throws, so a detection glitch can't lock the user out.)
        try:
            st = ost_setup.engine_status()
        except Exception:
            st = None
        if st is not None and not st.get("ready"):
            if st.get("foreign"):
                msg = (f"Another unlocker ({', '.join(st['foreign'])}) is hooking Steam and would "
                       "block this activation. Click Finish setup on the Engine tab to turn it off, then redeem.")
            elif st.get("installed"):
                msg = "OpenSteamTool isn't set up yet — finish setup/repair on the Engine tab, then redeem."
            else:
                msg = "OpenSteamTool isn't installed — install it on the Engine tab, then redeem."
            return {"ok": False, "error": msg, "engine_fix": True, "engine": st}

        # Gate on custom DLL — don't burn a one-use code on a session that may give
        # 005/012 because the wrong DLL is active. Block when the enhanced DLL is
        # missing/foreign (needs_fix) OR an OLD build of ours (needs_update): both must
        # be brought to the EXACT latest modified DLL before a ticket is worth writing.
        try:
            ds = self.dll_status()
        except Exception:
            ds = None
        if ds and ds.get("needs_fix"):
            return {"ok": False, "dll_fix": True,
                    "error": "Installing the enhanced DLL — Steam will restart. Once it's back, redeem your code again."}
        if ds and ds.get("needs_update"):
            return {"ok": False, "dll_fix": True,
                    "error": "Updating the enhanced DLL — Steam will restart. Once it's back, redeem your code again."}

        # Gate on cloud saves — the user MUST enable them before a code applies.
        # Only enforced where cloud saves can actually be delivered (the active engine
        # supports CloudRedirect and we have the DLL). If they aren't supported on this
        # machine we don't block, so the user is never permanently stuck. The UI shows
        # the enable prompt; this is the backend backstop so it can't be skipped.
        try:
            cs = ost_setup.cloud_status()
        except Exception:
            cs = None
        if cs and cs.get("supported") and cs.get("available") and not cs.get("enabled"):
            return {"ok": False, "cloud_fix": True,
                    "error": "Turn on cloud saves first so your game progress is protected — "
                             "click Apply, choose Enable, then redeem once Steam is back."}

        try:
            status, data = _server_post("/drm/redeem", {"code": code, "hwid": _machine_guid()})
        except Exception as e:
            return {"ok": False, "error": f"Server unreachable: {e}"}

        if status != 200 or not data.get("success"):
            return {"ok": False, "error": data.get("reason") or data.get("error") or f"Server error {status}"}

        app_id = str(data.get("app_id") or "")
        appticket = (data.get("appticket") or "").strip()
        eticket = (data.get("eticket") or "").strip()
        if not app_id or not appticket or not eticket:
            return {"ok": False, "error": "Server returned an incomplete ticket."}

        # The id the game ran under last time. Its saves may sit in a folder named
        # after it, which the vault carries over to the new id below.
        prev_sid = save_watchdog._registry_steam_id(app_id)

        try:
            _write_registry(app_id, appticket, eticket)
        except Exception as e:
            return {"ok": False, "error": f"Registry write failed: {e}"}

        # The ticket must also be in the registry of the Windows account Steam runs
        # as. On a standard account an admin password can put the app or Steam on
        # the admin account, and then the game finds no ticket (88500000).
        warning = ""
        try:
            if ost_setup.is_admin():
                _write_ticket_other_accounts(app_id, appticket, eticket)
            elif ost_setup.steam_on_other_account():
                if not _write_ticket_elevated(app_id, appticket, eticket):
                    warning = ("Steam is running under a different Windows account, so the game "
                               "can't see this activation. Exit Steam from the tray, open it again "
                               "normally (it shouldn't ask for a password), then launch the game.")
        except Exception:
            pass

        # Remember which account this activation runs under, so the save watchdog
        # knows the userdata folder the game will land in when it launches.
        owner = _appticket_owner_steamid(appticket)
        if owner:
            _LAST_ACTIVATION[app_id] = owner

        # Arm the save carry-over now, whether the user launches from the one-click
        # flow (run_game) or manually from Steam. It re-signs the save onto this
        # activation before the game opens, then launches the game itself.
        if owner and save_watchdog.is_supported(app_id):
            save_watchdog.start(app_id, owner, ui=_push_save)
        elif owner:
            # Everything else: saves kept per SteamID move to the new id. Done
            # before returning so the one-click launch can't race the copy.
            save_vault.carry_over(app_id, prev_sid, owner, ui=_push_save)

        return {
            "ok": True,
            "app_id": app_id,
            "uses_remaining": data.get("uses_remaining"),
            "warning": warning,
        }

    def run_game(self, app_id: str) -> dict:
        """Launch the game through Steam after a ticket is applied (one-click flow)."""
        app_id = "".join(c for c in str(app_id) if c.isdigit())
        if not app_id:
            return {"ok": False, "error": "No app id to launch."}
        # Capcom RE Engine games sign saves to the activation account, so a new
        # activation rejects the old save. The watchdog re-signs it first and then
        # launches the game itself, so hand off to it instead of launching here
        # (guarded against double-arming).
        if save_watchdog.is_supported(app_id):
            # Not the logged-in account: the game signs with the spoofed id, which
            # start() reads from the registry when this session didn't redeem it.
            save_watchdog.start(app_id, _LAST_ACTIVATION.get(app_id, ""), ui=_push_save)
            return {"ok": True, "app_id": app_id}

        try:
            url = f"steam://rungameid/{app_id}"
            if sys.platform == "win32":
                # As the desktop user even if we're elevated, so a Steam that isn't
                # running yet doesn't start on an admin account.
                ost_setup.launch_as_desktop_user(url)
            else:
                import webbrowser
                webbrowser.open(url)
        except Exception as e:
            return {"ok": False, "error": str(e)}

        return {"ok": True, "app_id": app_id}


# ---------------------------------------------------------------------------
# Startup robustness (WebView2 runtime + visible errors on other machines)
# ---------------------------------------------------------------------------

def _msgbox(title: str, text: str, style: int = 0x10) -> int:
    """Show a native message box (0x10 = error icon). Works with no console."""
    try:
        import ctypes
        return ctypes.windll.user32.MessageBoxW(0, text, title, style)
    except Exception:
        return 0


def _webview2_installed() -> bool:
    """True if the Edge WebView2 runtime is present (registry check)."""
    import winreg
    guid = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"  # Evergreen runtime client
    sep = "\\"
    locations = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients" + sep + guid),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients" + sep + guid),
        (winreg.HKEY_CURRENT_USER,  r"SOFTWARE\Microsoft\EdgeUpdate\Clients" + sep + guid),
    ]
    for root, path in locations:
        try:
            with winreg.OpenKey(root, path) as k:
                pv, _ = winreg.QueryValueEx(k, "pv")
                if pv and pv != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def _ensure_webview2() -> bool:
    """Make sure WebView2 is available, offering a one-click auto-install."""
    if _webview2_installed():
        return True
    # MB_OKCANCEL | MB_ICONINFORMATION
    if _msgbox(
        "TokeerDRM — one-time setup",
        "This app needs Microsoft Edge WebView2 (a small, free Microsoft "
        "component) which isn't installed on this PC.\n\n"
        "Click OK to download and install it now (~2 MB), or Cancel to install "
        "it yourself from the Microsoft website.",
        0x41,
    ) != 1:
        return False
    try:
        import urllib.request
        url = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"  # official bootstrapper
        dst = os.path.join(tempfile.gettempdir(), "MicrosoftEdgeWebView2Setup.exe")
        urllib.request.urlretrieve(url, dst)
        subprocess.run([dst, "/silent", "/install"], timeout=600,
                       creationflags=(0x08000000 if sys.platform == "win32" else 0))
    except Exception as e:
        _msgbox("TokeerDRM",
                f"Couldn't install WebView2 automatically:\n{e}\n\n"
                "Please install it manually from:\n"
                "https://developer.microsoft.com/microsoft-edge/webview2/")
        return False
    return _webview2_installed()


def main(initial_code=None) -> None:
    if sys.platform == "win32" and not _ensure_webview2():
        _msgbox("TokeerDRM",
                "WebView2 is required to run TokeerDRM.\nInstall it and reopen the app.")
        return

    global WINDOW
    _cleanup_update_leftovers()  # remove a previous update's <exe>.old / .new
    _register_uri_scheme()       # make tokeerdrm:// open this app (per-user, idempotent)
    api = Api()
    window = webview.create_window(
        APP_TITLE,
        WEB_INDEX,
        js_api=api,
        width=960,
        height=720,
        min_size=(820, 640),
        background_color="#0b0f17",
        frameless=False,
        easy_drag=False,
    )
    WINDOW = window

    # Blank-screen watchdog: if WebView2 is broken/blocked on this machine, the window
    # opens but the page never loads (just the dark background). Detect that and tell
    # the user how to fix it, instead of leaving them staring at a blank window.
    _loaded = {"ok": False}
    try:
        window.events.loaded += lambda *a: _loaded.__setitem__("ok", True)
        window.events.closed += lambda *a: _loaded.__setitem__("ok", True)  # closed early = don't nag
    except Exception:
        _loaded["ok"] = True  # older pywebview without events — don't nag

    # One-click flow: watch for codes forwarded by a second launch, and deliver a
    # code this instance was opened with (after the UI has had a moment to boot).
    _watch_pending_codes()
    if initial_code:
        def _deliver_initial(*_a):
            threading.Timer(1.6, lambda: _deliver_code_to_ui(initial_code)).start()
        try:
            window.events.loaded += _deliver_initial
        except Exception:
            threading.Timer(2.5, lambda: _deliver_code_to_ui(initial_code)).start()

    def _blank_watchdog():
        time.sleep(30)
        if not _loaded["ok"]:
            _msgbox(
                "TokeerDRM — display problem",
                "The window opened but the page never loaded (blank screen).\n\n"
                "This means Microsoft Edge WebView2 is broken or blocked on this PC.\n\n"
                "Fix:\n"
                "1) Repair/reinstall WebView2:\n"
                "   https://developer.microsoft.com/microsoft-edge/webview2/\n"
                "2) Allow TokeerDRM in your antivirus (it may be blocking it),\n"
                "then reopen TokeerDRM.",
            )
    threading.Thread(target=_blank_watchdog, daemon=True).start()

    # Force EdgeChromium (WebView2) so the modern CSS/animations render properly
    # (the legacy MSHTML fallback can't render this UI — it would just be blank too).
    try:
        webview.start(gui="edgechromium", debug=False)
    except Exception as e:
        _msgbox(
            "TokeerDRM — display problem",
            "The display engine (Microsoft Edge WebView2) failed to start.\n\n"
            "Repair/reinstall WebView2 from\n"
            "https://developer.microsoft.com/microsoft-edge/webview2/\n"
            "then reopen TokeerDRM.\n\n"
            f"(details: {e})",
        )


if __name__ == "__main__":
    # Elevated helper mode: relaunch_elevated_install() starts us with this flag,
    # already running as admin — do the install headless and exit (no window).
    if "--fix-dll" in sys.argv:
        try:
            ost_setup.write_result(ost_setup.install_custom_dll(progress=ost_setup.write_progress))
        except Exception as exc:
            ost_setup.write_result({"ok": False, "message": str(exc)})
        finally:
            ost_setup.write_progress(100, "Done")
        sys.exit(0)
    if "--install-engine" in sys.argv:
        try:
            ost_setup.write_result(ost_setup.install_ost_custom(progress=ost_setup.write_progress))
        except Exception as exc:
            ost_setup.write_result({"ok": False, "message": str(exc)})
        finally:
            ost_setup.write_progress(100, "Done")
        sys.exit(0)
    if "--update-engine" in sys.argv:
        try:
            ost_setup.write_result(ost_setup.install_ost_custom(progress=ost_setup.write_progress, force=True))
        except Exception as exc:
            ost_setup.write_result({"ok": False, "message": str(exc)})
        finally:
            ost_setup.write_progress(100, "Done")
        sys.exit(0)
    if "--enable-cloud" in sys.argv:
        try:
            ost_setup.write_result(ost_setup.enable_cloud(progress=ost_setup.write_progress))
        except Exception as exc:
            ost_setup.write_result({"ok": False, "message": str(exc)})
        finally:
            ost_setup.write_progress(100, "Done")
        sys.exit(0)
    if "--write-ticket" in sys.argv:
        try:
            ost_setup.write_result(_write_ticket_helper_main())
        except Exception as exc:
            ost_setup.write_result({"ok": False, "message": str(exc)})
        sys.exit(0)
    if "--uninstall-engine" in sys.argv:
        try:
            ost_setup.write_result(ost_setup.uninstall_ost(progress=ost_setup.write_progress))
        except Exception as exc:
            ost_setup.write_result({"ok": False, "message": str(exc)})
        finally:
            ost_setup.write_progress(100, "Done")
        sys.exit(0)

    # Older self-updaters started the new build with their PyInstaller env, so it
    # ran on the OLD build's unpacked files (old UI, tools that vanish once the old
    # process exits). Spot that by the bundled app.js not knowing our version, and
    # restart once with a clean env so this build unpacks its own files.
    if getattr(sys, "frozen", False) and not os.environ.get("TOKEERDRM_CLEAN_START"):
        try:
            with open(resource_path(os.path.join("web", "app.js")), encoding="utf-8") as _f:
                _stale = f'"{APP_VERSION}": [' not in _f.read()
        except Exception:
            _stale = False
        if _stale:
            _env = {k: v for k, v in os.environ.items()
                    if not k.startswith("_PYI") and k != "_MEIPASS2"}
            _env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
            _env["TOKEERDRM_CLEAN_START"] = "1"
            _args = [a for a in sys.argv[1:] if a != "--relaunched"] + ["--relaunched"]
            try:
                subprocess.Popen([sys.executable] + _args, creationflags=0x00000008,
                                 close_fds=True, env=_env)
                os._exit(0)
            except Exception:
                pass

    # Deep-link / single-instance handling for the one-click "Redeem in App" flow.
    # `--relaunched` is set by the self-updater when it starts the new build: during
    # the ~1s handover both processes are alive, so we must NOT let the guard treat
    # the fresh copy as a duplicate and exit it.
    _incoming_code = _code_from_argv(sys.argv[1:])
    _relaunched = "--relaunched" in sys.argv
    _is_primary = _acquire_single_instance()   # always grab/hold the mutex handle
    if not _relaunched and not _is_primary:
        # Another copy is already running. Hand it the code (if any) and bow out so
        # we don't open a duplicate window.
        if _incoming_code:
            try:
                with open(PENDING_CODE_FILE, "w", encoding="utf-8") as _f:
                    _f.write(_incoming_code)
            except Exception:
                pass
        sys.exit(0)

    try:
        main(initial_code=_incoming_code)
    except Exception as exc:
        log_path = os.path.join(tempfile.gettempdir(), "TokeerDRM_error.log")
        try:
            with open(log_path, "w", encoding="utf-8") as fh:
                fh.write(traceback.format_exc())
        except Exception:
            pass
        _msgbox(
            "TokeerDRM failed to start",
            f"{type(exc).__name__}: {exc}\n\nDetails saved to:\n{log_path}",
        )
