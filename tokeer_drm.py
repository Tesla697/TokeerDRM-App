"""
TokeerDRM — standalone desktop app.

Same engine as the Millennium plugin, for people who don't use Millennium:
  • Generate: run extract_tickets.exe → AppTicket + ETicket → server → 6-char code
  • Redeem:   code → server → write AppTicket + ETicket to the registry → play

UI is HTML/CSS/JS in a native webview (pywebview). The Python side below is the
privileged bridge: it runs the extractor, writes the registry, and talks to the
code-store server. Exposed to JS as `window.pywebview.api.*`.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback

import requests

import ost_setup

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
APP_TITLE = "TokeerDRM"
APP_VERSION = "1.0.11"                       # bump on every release
UPDATE_REPO = "Tesla697/TokeerDRM-App"      # GitHub repo whose latest release gates the app
WINDOW = None  # set in main(); lets the API push install progress to the UI


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
# Paths (work both from source and from a PyInstaller one-file build)
# ---------------------------------------------------------------------------

def resource_path(rel: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


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


def _write_registry(app_id: str, appticket_hex: str, eticket_hex: str) -> None:
    """Write AppTicket (ownership) + ETicket (encrypted) to Steam's credential store."""
    import winreg
    key_path = f"Software\\Valve\\Steam\\Apps\\{app_id}"
    key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
    try:
        winreg.SetValueEx(key, "AppTicket", 0, winreg.REG_BINARY, bytes.fromhex(appticket_hex))
        winreg.SetValueEx(key, "ETicket", 0, winreg.REG_BINARY, bytes.fromhex(eticket_hex))
    finally:
        winreg.CloseKey(key)


def _server_post(path: str, body: dict, timeout: int = 25) -> tuple[int, dict]:
    r = requests.post(SERVER_URL.rstrip("/") + path, json=body, timeout=timeout)
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
    if not getattr(sys, "frozen", False):
        return
    targets = [sys.executable + ".old", sys.executable + ".new"]

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
            r = requests.get(SERVER_URL.rstrip("/") + "/health", timeout=6)
            ok = r.status_code == 200 and r.json().get("success")
            return {"online": bool(ok), "server": SERVER_URL}
        except Exception:
            return {"online": False, "server": SERVER_URL}

    # -- Force update --------------------------------------------------------
    def version_info(self) -> dict:
        """Compare this build with the latest GitHub release. update_required=True
        blocks the whole UI until the user updates."""
        info = {"current": APP_VERSION, "latest": APP_VERSION, "update_required": False,
                "url": f"https://github.com/{UPDATE_REPO}/releases/latest"}
        try:
            r = requests.get(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
                             headers={"User-Agent": "TokeerDRM"}, timeout=8)
            if r.status_code == 200:
                d = r.json()
                tag = (d.get("tag_name") or "").lstrip("vV")
                if tag:
                    info["latest"] = tag
                    info["update_required"] = _version_gt(tag, APP_VERSION)
                info["url"] = d.get("html_url") or info["url"]
        except Exception:
            pass  # offline / API down → don't block
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
        if not getattr(sys, "frozen", False):
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
            r = requests.get(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
                             headers={"User-Agent": "TokeerDRM"}, timeout=20)
            r.raise_for_status()
            asset = next((a for a in r.json().get("assets", [])
                          if a.get("name", "").lower().endswith(".exe")), None)
            if not asset:
                return {"ok": False, "message": "The latest release has no .exe to download."}

            cur_exe = sys.executable
            new_exe = cur_exe + ".new"
            progress(10, "Downloading update…")
            with requests.get(asset["browser_download_url"], headers={"User-Agent": "TokeerDRM"},
                              timeout=600, stream=True) as dl:
                dl.raise_for_status()
                total = int(dl.headers.get("Content-Length") or 0)
                got = 0
                with open(new_exe, "wb") as f:
                    for chunk in dl.iter_content(65536):
                        if not chunk:
                            continue
                        f.write(chunk)
                        got += len(chunk)
                        if total:
                            progress(10 + int(got * 85 / total), "Downloading update…")

            progress(98, "Restarting…")
            # A detached helper swaps the new build in, relaunches it, and cleans up.
            #
            # Robust swap WITHOUT fighting the exe lock: NTFS lets you RENAME a running
            # .exe (you just can't overwrite its bytes). So we rename the running exe aside
            # (.old), drop the new build into its place, and launch it immediately — the
            # new app is in place before the old one even exits, so a ".new" is NEVER left
            # for the user to rename. After the old process exits we delete the .old (and,
            # belt-and-suspenders, finish the swap if the rename somehow didn't take). The
            # cleanup retries because a PyInstaller one-file exe is a parent bootloader +
            # child, and the parent can hold the old file a moment after the child exits.
            old_exe = cur_exe + ".old"
            pid = os.getpid()
            bat = os.path.join(tempfile.gettempdir(), f"tokeerdrm_update_{pid}.bat")
            with open(bat, "w", encoding="ascii") as f:
                f.write(
                    "@echo off\r\n"
                    # Rename the running exe aside (NTFS allows it), drop the new build in,
                    # launch it immediately. We try to delete .old here too, but if the
                    # PyInstaller bootloader still holds it, the NEW app finishes the job on
                    # startup (_cleanup_update_leftovers) so .old NEVER lingers.
                    f'del "{old_exe}" >nul 2>&1\r\n'
                    f'move /y "{cur_exe}" "{old_exe}" >nul 2>&1\r\n'
                    f'move /y "{new_exe}" "{cur_exe}" >nul 2>&1\r\n'
                    f'if not exist "{cur_exe}" move /y "{old_exe}" "{cur_exe}" >nul 2>&1\r\n'
                    f'start "" "{cur_exe}"\r\n'
                    f'del "{new_exe}" >nul 2>&1\r\n'
                    'del "%~f0"\r\n'
                )
            DETACHED = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
            subprocess.Popen(["cmd", "/c", bat], creationflags=DETACHED, close_fds=True)

            def _quit():
                import time as _t
                _t.sleep(1)  # let the UI show "Restarting…" and the helper start polling
                try:
                    if WINDOW is not None:
                        WINDOW.destroy()
                except Exception:
                    pass
                os._exit(0)
            threading.Thread(target=_quit, daemon=True).start()
            return {"ok": True, "message": "Updating… TokeerDRM will restart automatically."}
        except Exception as e:
            return {"ok": False, "message": f"Update failed: {e}. Use the GitHub link to download manually."}

    # -- OpenSteamTool engine (required to apply Denuvo tickets) --------------
    def engine_status(self) -> dict:
        """Is a Denuvo-capable engine (OpenSteamTool / mktl) active?"""
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
            return ost_setup.install_ost(
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
            return ost_setup.install_ost(
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

        try:
            tickets = _run_extract(app_id)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if not tickets:
            return {"ok": False, "error": f"This Steam account doesn't own app {app_id} (no ticket). "
                                          f"Make sure Steam is running and signed in."}

        try:
            status, data = _server_post("/drm/generate", {
                "appticket": tickets["appticket"],
                "eticket": tickets["eticket"],
                "steam_id": tickets["steam_id"],
                "app_id": app_id,
                "max_uses": int(max_uses),
                "created_by_user": tickets["steam_id"],
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
            msg = ("OpenSteamTool isn't set up yet — finish setup/repair on the Engine tab, then redeem."
                   if st.get("installed")
                   else "OpenSteamTool isn't installed — install it on the Engine tab, then redeem.")
            return {"ok": False, "error": msg, "engine_fix": True, "engine": st}

        try:
            status, data = _server_post("/drm/redeem", {"code": code})
        except Exception as e:
            return {"ok": False, "error": f"Server unreachable: {e}"}

        if status != 200 or not data.get("success"):
            return {"ok": False, "error": data.get("reason") or data.get("error") or f"Server error {status}"}

        app_id = str(data.get("app_id") or "")
        appticket = (data.get("appticket") or "").strip()
        eticket = (data.get("eticket") or "").strip()
        if not app_id or not appticket or not eticket:
            return {"ok": False, "error": "Server returned an incomplete ticket."}

        try:
            _write_registry(app_id, appticket, eticket)
        except Exception as e:
            return {"ok": False, "error": f"Registry write failed: {e}"}

        return {
            "ok": True,
            "app_id": app_id,
            "uses_remaining": data.get("uses_remaining"),
        }


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


def main() -> None:
    if sys.platform == "win32" and not _ensure_webview2():
        _msgbox("TokeerDRM",
                "WebView2 is required to run TokeerDRM.\nInstall it and reopen the app.")
        return

    global WINDOW
    _cleanup_update_leftovers()  # remove a previous update's <exe>.old / .new
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
    # Force EdgeChromium (WebView2) so the modern CSS/animations render properly
    # (the legacy MSHTML fallback can't). WebView2 ships with Windows 10/11.
    try:
        webview.start(gui="edgechromium", debug=False)
    except Exception:
        webview.start(debug=False)  # last-resort auto-select


if __name__ == "__main__":
    # Elevated helper mode: relaunch_elevated_install() starts us with this flag,
    # already running as admin — do the install headless and exit (no window).
    if "--install-engine" in sys.argv:
        try:
            ost_setup.write_result(ost_setup.install_ost(progress=ost_setup.write_progress))
        except Exception as exc:
            ost_setup.write_result({"ok": False, "message": str(exc)})
        finally:
            ost_setup.write_progress(100, "Done")
        sys.exit(0)
    if "--update-engine" in sys.argv:
        try:
            ost_setup.write_result(ost_setup.install_ost(progress=ost_setup.write_progress, force=True))
        except Exception as exc:
            ost_setup.write_result({"ok": False, "message": str(exc)})
        finally:
            ost_setup.write_progress(100, "Done")
        sys.exit(0)
    if "--uninstall-engine" in sys.argv:
        try:
            ost_setup.write_result(ost_setup.uninstall_ost(progress=ost_setup.write_progress))
        except Exception as exc:
            ost_setup.write_result({"ok": False, "message": str(exc)})
        finally:
            ost_setup.write_progress(100, "Done")
        sys.exit(0)

    try:
        main()
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
