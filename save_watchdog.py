"""Capcom RE Engine save carry-over for OST activations.

Every activation runs the game under a different pool account's SteamID, and RE
Engine saves are signed with that id. But Steam stores the save in the SIGNED-IN
account's userdata folder, which stays the same across activations. So the save
folder is stable while the file inside is signed for the previous activation, and
after a new activation the game rejects it and starts over.

This runs right after an activation, BEFORE the game opens. The save folder
already exists (the account has played before) and the new activation's id is
known from the appticket, so there is nothing the game needs to create first. It
makes sure the game isn't running, snapshots the signed-in account's save,
re-signs it onto the new id, writes it back, and only then launches the game,
once, so it loads the carried-over save. Older builds launched the game, killed
it and relaunched, and the kill didn't always land.

Each save file's current id is read off disk (brute-forced) rather than guessed,
so it never goes stale. MandarinJuice runs straight from here with its output
piped back, and progress shows in the app. Older builds went through a .bat and a
PowerShell script, which broke on some machines (non-English user names, script
policies). Every run is written to %LOCALAPPDATA%\\TokeerDRM\\save_carry.log.
Everything here is best effort: any failure is logged and leaves the account's
own saves alone; it never raises into the UI.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile

STEAMID64_BASE = 76561197960265728

# appid -> MandarinJuice profile filename. Only these four have a profile, which is
# the whole set MandarinJuice can re-sign.
SUPPORTED = {
    "3764200": "Resident Evil 9 Requiem v1.bin",
    "3357650": "PRAGMATA v1.bin",
    "2852190": "Monster Hunter Stories 3 Twisted Reflection v1.bin",
    "2638890": "Onimusha Way of the Sword v1.bin",
}

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# If the game is already open when a code is redeemed and we can't close it (it
# runs elevated, say), wait this long for the user to close it themselves.
_CLOSE_WAIT_SECONDS = 600
# A full brute-force sweep takes a couple of minutes; anything past this is hung.
_CLI_TIMEOUT = 900
_KEEP_BACKUPS = 5

_DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "TokeerDRM")
LOG_PATH = os.path.join(_DATA_DIR, "save_carry.log")
# MandarinJuice is framework-dependent on .NET 10. When the PC doesn't have it we
# fetch Microsoft's runtime zip into our own folder and point the tool at it, so
# there's no installer and no admin prompt.
_RUNTIME_DIR = os.path.join(_DATA_DIR, "dotnet")
_RUNTIME_URL = "https://aka.ms/dotnet/10.0/dotnet-runtime-win-x64.zip"


def is_supported(app_id: str) -> bool:
    return str(app_id) in SUPPORTED


def steam_path() -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            val, _ = winreg.QueryValueEx(k, "SteamPath")
        return os.path.normpath(str(val)) if val else ""
    except Exception:
        return ""


def _library_paths(steam: str) -> list:
    """Steam root plus every extra library folder from libraryfolders.vdf."""
    paths = [steam]
    vdf = os.path.join(steam, "steamapps", "libraryfolders.vdf")
    try:
        import re
        text = open(vdf, encoding="utf-8", errors="replace").read()
        for m in re.finditer(r'"path"\s+"([^"]+)"', text, re.IGNORECASE):
            p = m.group(1).replace("\\\\", "\\")
            if os.path.isdir(p):
                paths.append(p)
    except Exception:
        pass
    return paths


def _install_dir(steam: str, app_id: str) -> str:
    """Where the game's files live, from appmanifest_<appid>.acf across libraries."""
    import re
    for lib in _library_paths(steam):
        acf = os.path.join(lib, "steamapps", "appmanifest_%s.acf" % app_id)
        if not os.path.isfile(acf):
            continue
        try:
            text = open(acf, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        m = re.search(r'"installdir"\s+"([^"]+)"', text, re.IGNORECASE)
        if m:
            path = os.path.join(lib, "steamapps", "common", m.group(1))
            if os.path.isdir(path):
                return path
    return ""


def _is_cache_file(name: str) -> bool:
    """RE Engine drops small cache/index files next to the real save (e.g.
    data00-1.bin). They carry a hyphen in the name; the real save files never do.
    MandarinJuice must not brute-force or re-sign these, only the real save, so we
    keep them out of the work folder and out of the save-size detection."""
    return "-" in os.path.splitext(name)[0]


def _save_bytes(folder: str) -> int:
    """Direct save bytes in a folder, ignoring vdf bookkeeping and cache files."""
    total = 0
    try:
        for name in os.listdir(folder):
            p = os.path.join(folder, name)
            if (os.path.isfile(p) and not name.lower().endswith(".vdf")
                    and not _is_cache_file(name)):
                total += os.path.getsize(p)
    except Exception:
        pass
    return total


def _save_data_folder(remote: str):
    """The folder under a remote dir that directly holds the most save bytes.
    MandarinJuice does not recurse, so it has to be pointed at exactly that folder.
    Returns (path, bytes, rel) where rel is the path under remote (e.g. win64_save).
    """
    if not os.path.isdir(remote):
        return None
    best, best_bytes = None, 0
    for root, _dirs, _files in os.walk(remote):
        b = _save_bytes(root)
        if b > best_bytes:
            best_bytes, best = b, root
    if not best:
        return None
    rel = os.path.relpath(best, remote)
    if rel == ".":
        rel = ""
    return best, best_bytes, rel


def _acti_folders(steam: str, app_id: str) -> list:
    """Every userdata\\<id>\\<appid>\\remote that exists, as (steam32, remote)."""
    out = []
    userdata = os.path.join(steam, "userdata")
    try:
        for name in os.listdir(userdata):
            if not name.isdigit() or name == "0":
                continue
            remote = os.path.join(userdata, name, app_id, "remote")
            if os.path.isdir(remote):
                out.append((name, remote))
    except Exception:
        pass
    return out


def _tools():
    """Bundled MandarinJuice CLI and profiles dir. Raises if missing."""
    import tokeer_drm  # resource_path resolves frozen vs source layout
    base = tokeer_drm.resource_path("savetools")
    cli = os.path.join(base, "mandarin-juice-cli.exe")
    profiles = os.path.join(base, "profiles")
    if not os.path.isfile(cli):
        raise RuntimeError("save tools are missing from the app")
    return cli, profiles


_log_lock = threading.Lock()


def _file_log(msg: str) -> None:
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        with _log_lock:
            if os.path.isfile(LOG_PATH) and os.path.getsize(LOG_PATH) > 1_000_000:
                os.replace(LOG_PATH, LOG_PATH + ".old")
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


class _Report:
    """Sends status to the app (ui(pct, msg, kind)) and to the log file.

    Calling it shows a message and logs it, bar() only moves the progress bar,
    note() only goes in the log."""

    def __init__(self, ui):
        self._ui = ui or (lambda *_a: None)

    def _send(self, pct, msg, kind):
        try:
            self._ui(pct, msg, kind)
        except Exception:
            pass

    def __call__(self, msg, pct=None, kind=None):
        _file_log(msg)
        self._send(pct, msg, kind)

    def bar(self, pct, msg):
        self._send(pct, msg, None)

    def note(self, msg):
        _file_log(msg)


def _runtime_has_10(root: str) -> bool:
    try:
        names = os.listdir(os.path.join(root, "shared", "Microsoft.NETCore.App"))
    except OSError:
        return False
    return any(n.startswith("10.") for n in names)


def _system_dotnet_roots() -> list:
    """Where an installed .NET lives, the same places the tool's launcher looks."""
    roots = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\dotnet\Setup\InstalledVersions\x64", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_32KEY) as k:
            roots.append(str(winreg.QueryValueEx(k, "InstallLocation")[0]))
    except Exception:
        pass
    pf = os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles") or r"C:\Program Files"
    roots.append(os.path.join(pf, "dotnet"))
    return roots


def _dotnet_root(report) -> str:
    """A .NET folder that has the 10.x runtime, downloading our own copy if the PC
    has none. Only the base runtime is needed (the tool is a console app)."""
    for root in _system_dotnet_roots():
        if root and _runtime_has_10(root):
            return root
    if _runtime_has_10(_RUNTIME_DIR):
        return _RUNTIME_DIR

    report("Getting the .NET runtime the save tool needs (one time, about 37 MB)...", 0)
    part = _RUNTIME_DIR + ".zip.part"
    staging = _RUNTIME_DIR + ".new"
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        req = urllib.request.Request(_RUNTIME_URL, headers={"User-Agent": "TokeerDRM"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(part, "wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            got, last = 0, -1
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                pct = got * 100 // total if total else 0
                if pct != last:
                    last = pct
                    report.bar(pct, "Downloading the .NET runtime...")
        shutil.rmtree(staging, ignore_errors=True)
        with zipfile.ZipFile(part) as z:
            z.extractall(staging)
        shutil.rmtree(_RUNTIME_DIR, ignore_errors=True)
        os.replace(staging, _RUNTIME_DIR)
    except Exception as e:
        report.note("runtime download failed: %r" % e)
    finally:
        for p in (part, staging):
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                elif os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
    return _RUNTIME_DIR if _runtime_has_10(_RUNTIME_DIR) else ""


_PCT_RE = re.compile(r"^\[(\d+(?:\.\d+)?)%\]")
_FOUND_RE = re.compile(r"Found UserID:\s*(\d+)")
_CLEAN_RE = re.compile(r"Errors:\s*0\b")
_BANNER = ("~~*", "---", "Good morning", "Good afternoon", "Good evening", "Have a great")


def _run_cli(cli: str, args: list, env: dict, on_line=None) -> list:
    """Run MandarinJuice with its output piped back line by line. Its exit code is
    always 0, so callers judge success from the output."""
    lines = []
    p = subprocess.Popen([cli] + args, cwd=os.path.dirname(cli), env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, creationflags=_NO_WINDOW)
    killer = threading.Timer(_CLI_TIMEOUT, p.kill)
    killer.start()
    try:
        for raw in p.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            lines.append(line)
            if on_line:
                on_line(line)
        p.wait()
    finally:
        killer.cancel()
    return lines


def _log_line(report, line: str) -> None:
    if not line.startswith(_BANNER):
        report.note("  " + line)


def _single(run_dir: str, src: str) -> str:
    """A fresh folder holding only `src`. The tool works on whole folders, and the
    brute-force only ever looks at the first file in one."""
    folder = tempfile.mkdtemp(prefix="f_", dir=run_dir)
    shutil.copy2(src, os.path.join(folder, os.path.basename(src)))
    return folder


def _brute(cli, profile, src, env, run_dir, report) -> str:
    """The 32-bit id a save file is signed with, or "" if it isn't user-signed."""
    folder = _single(run_dir, src)
    name = os.path.basename(src)
    found = []

    def on_line(line):
        m = _PCT_RE.match(line)
        if m:
            report.bar(int(float(m.group(1))), "Reading your save's ID (%s)..." % name)
            return
        _log_line(report, line)
        f = _FOUND_RE.search(line)
        if f:
            found.append(f.group(1))

    try:
        _run_cli(cli, ["-m", "b", "-g", profile, "-p", folder, "-q"], env, on_line)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return found[-1] if found else ""


def _resign_one(cli, profile, src, old_uid, new_uid, env, run_dir, report) -> str:
    """Re-sign one file from old_uid to new_uid. Returns the re-signed copy's path,
    or "" if the file isn't signed with old_uid.

    A file MandarinJuice can't recognise as an encrypted save it doesn't refuse, it
    just encrypts it as if it were plain data, which wrecks it. So a run only
    counts when the log shows it decrypted this file first."""
    folder = _single(run_dir, src)
    name = os.path.basename(src)
    out_root = os.path.join(os.path.dirname(cli), "_OUTPUT")
    before = set(os.listdir(out_root)) if os.path.isdir(out_root) else set()
    try:
        lines = _run_cli(cli, ["-m", "r", "-g", profile, "-p", folder,
                               "-uI", old_uid, "-uO", new_uid, "-q"],
                         env, lambda line: _log_line(report, line))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    decrypted = "Decrypting the [%s]" % name
    if not (any(decrypted in line for line in lines)
            and any(_CLEAN_RE.search(line) for line in lines)):
        return ""
    after = set(os.listdir(out_root)) if os.path.isdir(out_root) else set()
    for d in sorted(after - before):
        for root, _dirs, files in os.walk(os.path.join(out_root, d)):
            if name in files:
                return os.path.join(root, name)
    return ""


def _backup(files: list, app_dir: str, report) -> None:
    """Keep a copy of the save as it was, outside the cloud-synced remote folder.
    One timestamped folder per carry-over, newest few kept."""
    root = os.path.join(app_dir, "ltsave_backup")
    dest = os.path.join(root, time.strftime("%Y%m%d-%H%M%S"))
    try:
        os.makedirs(dest, exist_ok=True)
        for f in files:
            shutil.copy2(f, os.path.join(dest, os.path.basename(f)))
        stamps = sorted(d for d in os.listdir(root)
                        if re.fullmatch(r"\d{8}-\d{6}", d) and os.path.isdir(os.path.join(root, d)))
        for old in stamps[:-_KEEP_BACKUPS]:
            shutil.rmtree(os.path.join(root, old), ignore_errors=True)
        report.note("backup: %s" % dest)
    except Exception as e:
        report.note("backup failed: %r" % e)


def _game_procs(install_dir: str) -> list:
    """PIDs of running processes whose exe lives under the game's install dir."""
    if not install_dir:
        return []
    ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.ExecutablePath -like '%s\\*' } | "
        "ForEach-Object { $_.ProcessId }" % install_dir.replace("'", "''")
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                            capture_output=True, text=True, timeout=30,
                            creationflags=_NO_WINDOW).stdout or ""
        return [int(x) for x in out.split() if x.strip().isdigit()]
    except Exception:
        return []


def _root_exes(install_dir: str) -> set:
    """Lower-case names of the .exe files at the top of the install dir. The main
    game exe always sits there for these games."""
    try:
        return {n.lower() for n in os.listdir(install_dir) if n.lower().endswith(".exe")}
    except Exception:
        return set()


def _running_images(install_dir: str) -> set:
    """Which of the game's exe names tasklist shows running. This still works when
    the game runs elevated, where Win32_Process hides ExecutablePath from us and
    _game_procs sees nothing."""
    names = _root_exes(install_dir)
    if not names:
        return set()
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                            text=True, timeout=30, creationflags=_NO_WINDOW).stdout or ""
    except Exception:
        return set()
    running = {line.split('","')[0].strip('"').lower() for line in out.splitlines() if line}
    return names & running


def _game_running(install_dir: str) -> bool:
    return bool(_game_procs(install_dir) or _running_images(install_dir))


def _kill_game(install_dir: str) -> None:
    for pid in _game_procs(install_dir):
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                          capture_output=True, creationflags=_NO_WINDOW)
        except Exception:
            pass
    for name in _running_images(install_dir):
        try:
            subprocess.run(["taskkill", "/F", "/IM", name],
                          capture_output=True, creationflags=_NO_WINDOW)
        except Exception:
            pass


def _ensure_closed(install_dir: str, log) -> bool:
    """Make sure the game isn't holding the save. Normally it isn't open at all.
    If it is, close it; if that fails, ask the user to close it and wait."""
    if not install_dir or not _game_running(install_dir):
        return True
    log("Closing the game so your save can be carried over...")
    _kill_game(install_dir)
    for _ in range(15):
        if not _game_running(install_dir):
            time.sleep(2)  # let it release the save files
            return True
        time.sleep(1)
    log("Please close the game. Your save will carry over as soon as it's closed.")
    deadline = time.time() + _CLOSE_WAIT_SECONDS
    while time.time() < deadline:
        if not _game_running(install_dir):
            time.sleep(2)
            return True
        time.sleep(2)
    return False


def _launch(app_id: str) -> None:
    try:
        if sys.platform == "win32":
            import ost_setup  # as the desktop user even when we're elevated
            ost_setup.launch_as_desktop_user("steam://rungameid/%s" % app_id)
        else:
            import webbrowser
            webbrowser.open("steam://rungameid/%s" % app_id)
    except Exception:
        pass


def _logged_in_account32(steam: str) -> str:
    """The 32-bit account id of the Steam account currently signed in. RE Engine
    cloud saves live under userdata\\<this id>\\<appid>, keyed by the SIGNED-IN
    account, NOT by the (spoofed) id the game signs the save with."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Valve\Steam\ActiveProcess") as k:
            v = winreg.QueryValueEx(k, "ActiveUser")[0]
        if v and int(v) != 0:
            return str(int(v))
    except Exception:
        pass
    try:  # Steam not running: fall back to the most recent login.
        import re
        text = open(os.path.join(steam, "config", "loginusers.vdf"),
                    encoding="utf-8", errors="replace").read()
        best = None
        for m in re.finditer(r'"(\d{17})"\s*\{([^}]*)\}', text):
            if re.search(r'"MostRecent"\s*"1"', m.group(2)):
                best = m.group(1)
        if best:
            return str(int(best) - STEAMID64_BASE)
    except Exception:
        pass
    return ""


def _biggest_save(steam: str, app_id: str):
    """The biggest save-data folder across every userdata folder for this game.
    That is the real progress. Returns (data, bytes, rel, remote) or None."""
    best = None
    for _sid32, remote in _acti_folders(steam, app_id):
        df = _save_data_folder(remote)
        if df and (best is None or df[1] > best[1]):
            best = (df[0], df[1], df[2], remote)   # data, bytes, rel, remote
    return best


def _save_files(folder: str) -> list:
    """The real save files directly in a folder, slot files first. RE Engine signs
    the actual progress in dataNNNSlot.bin; data000.bin next to it may not be
    signed with the per-user id at all."""
    try:
        names = [n for n in os.listdir(folder)
                 if os.path.isfile(os.path.join(folder, n))
                 and not n.lower().endswith(".vdf") and not _is_cache_file(n)]
    except OSError:
        return []
    names.sort(key=lambda n: ("slot" not in n.lower(), n.lower()))
    return [os.path.join(folder, n) for n in names]


def _registry_steam_id(app_id: str) -> str:
    """The SteamID the engine hands the game for this app (redeem writes it next to
    the tickets). The game signs its save with exactly this id."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Valve\Steam\Apps\%s" % app_id) as k:
            return str(winreg.QueryValueEx(k, "SteamID")[0]).strip()
    except Exception:
        return ""


def _restore(app_id: str, new_id: str, report) -> None:
    steam = steam_path()
    if not steam:
        report.note("no Steam path in the registry")
        return
    profile_name = SUPPORTED[app_id]

    # The id the game signs its save with = the activation account (what its
    # spoofed GetSteamID returns), as a 32-bit account id. Read it live from the
    # credential store (what GetSpoofSteamID actually returns) rather than trusting
    # the value handed in: redeem writes it there, so a second redeem before this
    # runs leaves the passed value stale while the registry is current.
    live = _registry_steam_id(app_id)
    if live:
        if str(live) != str(new_id):
            report.note("using live credential SteamID %s (was handed %s)" % (live, new_id))
        new_id = live
    try:
        n = int(new_id)
    except (TypeError, ValueError):
        report.note("bad activation id %r" % new_id)
        return
    new_uid = str(n - STEAMID64_BASE if n > STEAMID64_BASE else n)

    # The save must go in the SIGNED-IN account's folder. Right after a redeem
    # Steam may still be starting up or logging in, so ActiveUser isn't set yet;
    # that's the run-1 case where we fell back to guessing the folder off disk
    # (wrong when more than one account has played). Wait briefly for the login.
    active = _logged_in_account32(steam)
    if not active:
        for _ in range(15):
            time.sleep(1)
            active = _logged_in_account32(steam)
            if active:
                break
        if not active:
            report.note("Steam account still unknown after waiting; using biggest save on disk")
    install_dir = _install_dir(steam, app_id)
    report.note("carry app=%s new=%s active=%s install=%s" % (app_id, new_uid, active, install_dir))

    # The game reads and writes its save ONLY in the signed-in account's folder, so
    # that folder holds the latest progress and is also where the carried save goes.
    # While that folder has a save, never source from other userdata folders: they
    # can be stale leftovers (an older activation's copy), and carrying one back
    # would revert progress.
    if active:
        target_remote = os.path.join(steam, "userdata", active, app_id, "remote")
    else:
        # Can't read the signed-in account (Steam not running?). Last resort only.
        best = _biggest_save(steam, app_id)
        target_remote = best[3] if best else ""

    df = _save_data_folder(target_remote) if target_remote else None
    if not df and active:
        # First time on this Steam account: its folder has no save yet, so there is
        # no newer progress a leftover could overwrite. Bring over the biggest save
        # from any other userdata folder and re-sign it into this account's folder.
        best = _biggest_save(steam, app_id)
        if best:
            report("No save on this Steam account yet; bringing over your save from another folder.")
            df = best[:3]
    if not df:
        report("No save found to carry over. Opening the game.", kind="ok")
        _launch(app_id)
        return
    old_data, _old_bytes, rel = df
    target = os.path.join(target_remote, rel) if rel else target_remote
    report.note("source=%s target=%s" % (old_data, target))

    # The game must not be open while we swap the save. Usually it isn't yet: this
    # runs before we launch it.
    if not _ensure_closed(install_dir, report):
        report("The game is still open, so your save was not carried over this time.", kind="err")
        return

    try:
        cli_src, profiles = _tools()
    except Exception as e:
        report("Save tools unavailable (%s). Opening the game." % e, kind="err")
        _launch(app_id)
        return
    profile = os.path.join(profiles, profile_name)
    if not os.path.isfile(profile):
        report("No save profile for this game. Opening the game.", kind="err")
        _launch(app_id)
        return

    dotnet = _dotnet_root(report)
    if not dotnet:
        report("Couldn't get the .NET runtime the save tool needs, so your save "
               "wasn't carried over. Check your internet and redeem again.", kind="err")
        _launch(app_id)
        return
    env = dict(os.environ)
    env["DOTNET_ROOT"] = dotnet
    env["DOTNET_ROOT_X64"] = dotnet
    env.pop("DOTNET_ROOT(x86)", None)
    report.note("dotnet=%s" % dotnet)

    # Private run folder: our own copy of the tool (so its _OUTPUT only ever holds
    # this run) and a snapshot of the save (MandarinJuice never touches the real one).
    run_dir = tempfile.mkdtemp(prefix="ltsave_%s_" % app_id)
    try:
        tool_dir = os.path.join(run_dir, "tool")
        snap_dir = os.path.join(run_dir, "snap")
        os.makedirs(tool_dir)
        os.makedirs(snap_dir)
        cli = os.path.join(tool_dir, os.path.basename(cli_src))
        shutil.copy2(cli_src, cli)
        for f in _save_files(old_data):
            shutil.copy2(f, os.path.join(snap_dir, os.path.basename(f)))
        snapshot = _save_files(snap_dir)
        if not snapshot:
            report("No save files found to carry over. Opening the game.", kind="ok")
            _launch(app_id)
            return
        report.note("files: %s" % ", ".join(os.path.basename(f) for f in snapshot))

        report("Carrying your save over before the game starts...", 0)
        # Each file is handled on its own. Files written in different sessions can
        # be signed with different ids, so an id found on one file is tried on the
        # rest first (instant), and only a file none of them fit gets brute-forced.
        known, resigned, current, unread = [], {}, [], []
        for src in snapshot:
            name = os.path.basename(src)
            out = ""
            for old in known:
                out = _resign_one(cli, profile, src, old, new_uid, env, run_dir, report)
                if out:
                    break
            if not out:
                old = _brute(cli, profile, src, env, run_dir, report)
                report.note("%s signed with %s" % (name, old or "?"))
                if not old:
                    unread.append(name)
                    continue
                known.append(old)
                if old == new_uid:
                    current.append(name)
                    continue
                out = _resign_one(cli, profile, src, old, new_uid, env, run_dir, report)
            if out:
                resigned[name] = out
            else:
                unread.append(name)

        report.note("resigned=%s current=%s unread=%s" % (sorted(resigned), current, unread))
        if not resigned:
            if current:
                report("Your save already matches this activation. Opening the game.", kind="ok")
            else:
                report("Couldn't read your save's ID, so it was left as it was. "
                       "Opening the game.", kind="err")
            _launch(app_id)
            return

        # The brute-force takes a while; make sure nobody opened the game meanwhile,
        # or it would overwrite what we're about to write (or hold the files open).
        if not _ensure_closed(install_dir, report):
            report("The game is still open, so your save was not carried over this time.", kind="err")
            return

        _backup(snapshot, os.path.dirname(target_remote), report)

        os.makedirs(target, exist_ok=True)
        failed = []
        for name, path in resigned.items():
            try:
                shutil.copy2(path, os.path.join(target, name))
            except Exception as e:
                failed.append(name)
                report.note("write %s failed: %r" % (name, e))
        if failed:
            report("Couldn't write %s. Close anything using the save folder and redeem again."
                   % ", ".join(failed), kind="err")
            return
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)

    # Drop leftover marker files older builds wrote into the save folder.
    for junk in (".ltsave_userid", ".ltsave_signed"):
        try:
            os.remove(os.path.join(target, junk))
        except Exception:
            pass

    # Drop the game's stale cache/index files (the hyphen ones) so it rebuilds them
    # against the freshly signed save instead of the previous activation's.
    try:
        for name in os.listdir(target):
            p = os.path.join(target, name)
            if _is_cache_file(name) and os.path.isfile(p):
                os.remove(p)
    except Exception:
        pass

    # Stale remotecache.vdf makes Steam re-pull the old cloud copy over ours.
    rc = os.path.join(os.path.dirname(target_remote), "remotecache.vdf")
    try:
        if os.path.isfile(rc):
            os.replace(rc, rc + "_org")
    except Exception:
        pass

    report("Your save was carried over. Starting the game...", kind="ok")
    _launch(app_id)


_armed = set()
_armed_lock = threading.Lock()
# When each (game, activation) last finished a carry-over. redeem() and run_game()
# both call start() for the same activation; if the first run already launched the
# game, the second must not close it again to carry the same save twice.
_finished: dict = {}


def start(app_id: str, new_id: str = "", ui=None) -> None:
    """Kick off the carry-over in the background. ui(pct, msg, kind) gets status
    for the app. No-op for unsupported games or when the activation SteamID isn't
    known. Guarded so redeem() and run_game() firing in the same one-click flow arm
    it only once per game."""
    app_id = "".join(c for c in str(app_id) if c.isdigit())
    if not is_supported(app_id):
        return
    new_id = str(new_id or _registry_steam_id(app_id))
    if not new_id:
        _file_log("app=%s: no activation SteamID, nothing to carry" % app_id)
        return

    with _armed_lock:
        if app_id in _armed or time.time() - _finished.get((app_id, new_id), 0) < 60:
            return
        _armed.add(app_id)

    report = _Report(ui)

    def worker():
        try:
            _restore(app_id, new_id, report)
        except Exception as e:
            report.note("crashed: %r" % e)
            report("Save carry-over skipped: %s" % e, kind="err")
            _launch(app_id)
        finally:
            with _armed_lock:
                _armed.discard(app_id)
                _finished[(app_id, new_id)] = time.time()

    threading.Thread(target=worker, daemon=True).start()
