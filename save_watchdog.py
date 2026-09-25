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
re-signs it onto the new id in a command prompt the user can watch, writes it
back, and only then launches the game, once, so it loads the carried-over save.
Older builds launched the game, killed it and relaunched, and the kill didn't
always land.

The save's current id is read off disk each time (brute-forced) rather than
guessed, so it never goes stale. Everything here is best effort: any failure is
logged and leaves the account's own saves alone; it never raises into the UI.
"""

import os
import shutil
import subprocess
import sys
import threading
import time

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
_NEW_CONSOLE = 0x00000010 if sys.platform == "win32" else 0

# If the game is already open when a code is redeemed and we can't close it (it
# runs elevated, say), wait this long for the user to close it themselves.
_CLOSE_WAIT_SECONDS = 600


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


def _has_dotnet10() -> bool:
    try:
        out = subprocess.run(["dotnet", "--list-runtimes"], capture_output=True,
                             text=True, timeout=15, creationflags=_NO_WINDOW).stdout or ""
        return any(line.startswith("Microsoft.WindowsDesktop.App 10.")
                   for line in out.splitlines())
    except Exception:
        return False


_BRUTE_PS = r"""$ErrorActionPreference = 'SilentlyContinue'
$cliexe = '@CLI@'
$prof   = '@PROF@'
$work   = '@WORK@'
$log    = '@LOG@'
# Real save files only (drop the hyphen cache files), slot files first: RE Engine
# signs the actual save in dataNNNSlot.bin, while data000.bin sits next to it and
# is not signed with the per-user id, so brute-forcing that one just fails.
$bins = Get-ChildItem -LiteralPath $work -File -Filter *.bin | Where-Object { $_.BaseName -notlike '*-*' }
$order = @()
$order += ($bins | Where-Object { $_.Name -match 'slot' })
$order += ($bins | Where-Object { $_.Name -notmatch 'slot' })
if (-not $order) { $order = $bins }
$found = $false
foreach ($f in $order) {
  if ($found) { break }
  $tmp = Join-Path $env:TEMP ('mjb_' + [guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Path $tmp | Out-Null
  Copy-Item -LiteralPath $f.FullName -Destination (Join-Path $tmp $f.Name)
  Write-Host ''
  Write-Host ('Checking ' + $f.Name + ' ...')
  & $cliexe -m b -g $prof -p $tmp -q 2>&1 | ForEach-Object {
    Write-Host $_
    $_ | Out-File -FilePath $log -Append -Encoding ascii
    if ($_ -match 'Found UserID') { $script:found = $true }
  }
  Remove-Item -LiteralPath $tmp -Recurse -Force
}
"""

_RESIGN_BAT = """@echo off
setlocal EnableDelayedExpansion
title Resigning your save
set "UID="
echo Detecting your save's ID. This can take a few minutes...
powershell -NoProfile -ExecutionPolicy Bypass -File "@PS@"
set "LINE="
for /f "tokens=*" %%A in ('findstr /c:"Found UserID" "@LOG@"') do set "LINE=%%A"
for %%B in (!LINE!) do set "UID=%%B"
set "UID=!UID: =!"
set "UID=!UID:.=!"
echo !UID!| findstr /r "^[0-9][0-9]*$" >nul || set "UID="
if not defined UID (
  echo.
  echo Could not detect the save ID. Nothing was changed.
  echo Press any key to close this window.
  pause >nul
  exit /b 1
)
echo.
echo Re-signing your save from !UID! to @NEWUID@ ...
"@CLI@" -m r -g "@PROF@" -p "@WORK@" -uI !UID! -uO @NEWUID@ -q
echo.
echo Done. Press any key to close this window.
pause >nul
endlocal
"""


def _resign_console(cli: str, profile: str, work: str, new_uid: str) -> None:
    """Re-sign the save in `work` onto new_uid in a command prompt the user watches.

    MandarinJuice works in the 32-bit account-id space (its "Found UserID" is a
    32-bit id), so new_uid is the new activation's 32-bit account id, not the
    SteamID64. The save's current id is read straight off disk each time by
    brute-forcing it, trying each save file on its own (slot files first) until one
    yields an id, so a data000.bin that is not user-signed can't stall it. Then the
    whole folder is re-signed onto new_uid. The window holds open on a keypress at
    the end; success is judged by the output files, not the exit code.
    """
    def pesc(s):
        return s.replace("'", "''")

    stamp = int(time.time() * 1000)
    log_file = os.path.join(os.environ.get("TEMP", "."), "ltbrute_%d.log" % stamp)
    ps = os.path.join(os.environ.get("TEMP", "."), "ltbrute_%d.ps1" % stamp)
    bat = os.path.join(os.environ.get("TEMP", "."), "ltresign_%d.bat" % stamp)

    ps_text = (_BRUTE_PS.replace("@CLI@", pesc(cli)).replace("@PROF@", pesc(profile))
               .replace("@WORK@", pesc(work)).replace("@LOG@", pesc(log_file)))
    bat_text = (_RESIGN_BAT.replace("@PS@", ps).replace("@LOG@", log_file)
                .replace("@CLI@", cli).replace("@PROF@", profile)
                .replace("@WORK@", work).replace("@NEWUID@", new_uid))
    try:
        with open(ps, "w", encoding="utf-8") as fh:
            fh.write(ps_text)
        with open(bat, "w") as fh:
            fh.write(bat_text.replace("\n", "\r\n"))
        try:
            subprocess.run(["cmd", "/c", bat], cwd=os.path.dirname(cli),
                          creationflags=_NEW_CONSOLE, timeout=3600)
        except subprocess.TimeoutExpired:
            pass
    except Exception:
        pass
    finally:
        for f in (bat, ps, log_file):
            try:
                os.remove(f)
            except Exception:
                pass


def _resigned_files(tools_dir: str, since: float):
    """The files MandarinJuice just wrote under _OUTPUT\\<ts>_resigned\\<newId>\\."""
    out_root = os.path.join(tools_dir, "_OUTPUT")
    if not os.path.isdir(out_root):
        return None
    newest, newest_t = None, 0
    for name in os.listdir(out_root):
        if not name.endswith("_resigned"):
            continue
        p = os.path.join(out_root, name)
        t = os.path.getmtime(p)
        if t >= since - 15 and t > newest_t:
            newest_t, newest = t, p
    if not newest:
        return None
    # Descend the single <newId> subfolder MandarinJuice nests the output in.
    kids = [os.path.join(newest, d) for d in os.listdir(newest)
            if os.path.isdir(os.path.join(newest, d))]
    content = kids[0] if len(kids) == 1 else newest
    files = [os.path.join(content, f) for f in os.listdir(content)]
    return files if files else None


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
            os.startfile("steam://rungameid/%s" % app_id)
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


def _restore(app_id: str, new_id: str, log) -> None:
    steam = steam_path()
    if not steam:
        return
    profile_name = SUPPORTED[app_id]

    # The id the game signs its save with = the activation account (what its
    # spoofed GetSteamID returns), as a 32-bit account id.
    try:
        n = int(new_id)
    except (TypeError, ValueError):
        return
    new_uid = str(n - STEAMID64_BASE if n > STEAMID64_BASE else n)
    active = _logged_in_account32(steam)
    install_dir = _install_dir(steam, app_id)

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
            log("No save on this Steam account yet; bringing over your save from another folder.")
            df = best[:3]
    if not df:
        log("No save found to carry; opening the game.")
        _launch(app_id)
        return
    old_data, _old_bytes, rel = df
    target = os.path.join(target_remote, rel) if rel else target_remote

    # The game must not be open while we swap the save. Usually it isn't yet: this
    # runs before we launch it.
    if not _ensure_closed(install_dir, log):
        log("The game is still open, so your save was not carried over this time.")
        return

    try:
        cli, profiles = _tools()
    except Exception as e:
        log("Save tools unavailable (%s); opening the game." % e)
        _launch(app_id)
        return
    profile = os.path.join(profiles, profile_name)
    if not os.path.isfile(profile):
        log("No save profile for this game; opening the game.")
        _launch(app_id)
        return
    if not _has_dotnet10():
        log("Carrying the save over needs the .NET 10 Desktop Runtime. Install it "
            "from https://dotnet.microsoft.com/download/dotnet/10.0 and redeem again.")
        _launch(app_id)
        return

    # Snapshot the real save, then re-sign the copy (MandarinJuice never touches it).
    work = os.path.join(os.environ.get("TEMP", "."),
                        "ltsave_%s_%d" % (app_id, int(time.time())))

    def _skip_cache(dirpath, names):
        return [n for n in names
                if os.path.isfile(os.path.join(dirpath, n)) and _is_cache_file(n)]
    try:
        shutil.copytree(old_data, work, ignore=_skip_cache)
    except Exception as e:
        log("Could not stage the save (%s); opening the game." % e)
        _launch(app_id)
        return

    log("Carrying your save over before the game starts...")
    since = time.time()
    _resign_console(cli, profile, work, new_uid)
    files = _resigned_files(os.path.dirname(cli), since)
    if not files:
        shutil.rmtree(work, ignore_errors=True)
        log("Re-signing failed; your save was not changed. Opening the game.")
        _launch(app_id)
        return

    # Back up the untouched save once, OUTSIDE the cloud-synced remote folder, from
    # the pristine snapshot (MandarinJuice reads `work`, it never modifies it).
    backup = os.path.join(os.path.dirname(target_remote), "ltsave_backup")
    if not os.path.exists(backup):
        try:
            shutil.copytree(work, backup)
        except Exception:
            pass
    shutil.rmtree(work, ignore_errors=True)

    os.makedirs(target, exist_ok=True)
    for f in files:
        try:
            shutil.copy2(f, os.path.join(target, os.path.basename(f)))
        except Exception as e:
            log("Could not write %s (%s)." % (os.path.basename(f), e))

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

    log("Carried your save over. Starting the game...")
    _launch(app_id)


_armed = set()
_armed_lock = threading.Lock()
# When each (game, activation) last finished a carry-over. redeem() and run_game()
# both call start() for the same activation; if the first run already launched the
# game, the second must not close it again to carry the same save twice.
_finished: dict = {}


def start(app_id: str, new_id: str, log=None) -> None:
    """Kick off the carry-over in the background. No-op for unsupported games or
    when the activation SteamID isn't known. Guarded so redeem() and run_game()
    firing in the same one-click flow arm it only once per game."""
    app_id = "".join(c for c in str(app_id) if c.isdigit())
    log = log or (lambda _m: None)
    if not is_supported(app_id) or not new_id:
        return

    with _armed_lock:
        if app_id in _armed or time.time() - _finished.get((app_id, str(new_id)), 0) < 60:
            return
        _armed.add(app_id)

    def worker():
        try:
            _restore(app_id, str(new_id), log)
        except Exception as e:
            log("Save carry-over skipped: %s" % e)
        finally:
            with _armed_lock:
                _armed.discard(app_id)
                _finished[(app_id, str(new_id))] = time.time()

    threading.Thread(target=worker, daemon=True).start()
