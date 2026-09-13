#!/usr/bin/env python3
"""Capcom RE Engine save carry-over for Tokeer activations - Linux / Proton.

Linux port of the Windows save_watchdog. Same problem: every activation signs
the RE Engine save with a different pool account's spoofed id, but Steam keeps
the save in the SIGNED-IN account's userdata folder, which never changes across
activations. So the save folder is stable while the file inside is signed for
the previous activation, and after a new activation the game rejects it.

Right after a redeem this snapshots the signed-in account's save, launches the
game so Steam creates the folder, kills it once the folder is there, re-signs the
save onto the new activation's id with the official mandarin-juice-cli (linux),
writes it back, drops the stale cache, and relaunches so the game loads the
carried-over progress.

The save's current id is read off disk each time (brute-forced) rather than
guessed. The re-sign is adaptive: the CLI's "Found UserID" tells us whether this
build works in the 32-bit account id or the full SteamID64, and the new id is
handed back in the same shape, so it can't mismatch. Everything is best effort:
any failure is logged and leaves the account's own saves alone.
"""

import os
import re
import shutil
import signal
import subprocess
import time
import urllib.request

STEAMID64_BASE = 76561197960265728

# appid -> MandarinJuice profile filename. Only these have a profile, which is
# the whole set MandarinJuice can re-sign.
SUPPORTED = {
    "3764200": "Resident Evil 9 Requiem v1.bin",
    "3357650": "PRAGMATA v1.bin",
    "2852190": "Monster Hunter Stories 3 Twisted Reflection v1.bin",
    "2638890": "Onimusha Way of the Sword v1.bin",
}

_HERE = os.path.dirname(os.path.abspath(__file__))
_KILL_GRACE_SECONDS = 5
_FOLDER_CAP_SECONDS = 120


def is_supported(app_id: str) -> bool:
    return str(app_id) in SUPPORTED


def _log(log, msg: str) -> None:
    (log or (lambda _m: None))(msg)


# --------------------------------------------------------------------------- #
# Steam / save discovery
# --------------------------------------------------------------------------- #
def _steam_root() -> str:
    for c in ("~/.steam/steam", "~/.local/share/Steam", "~/.steam/root",
              "~/.var/app/com.valvesoftware.Steam/data/Steam"):
        p = os.path.expanduser(c)
        if os.path.isdir(os.path.join(p, "steamapps")):
            return p
    return ""


def _library_roots(steam_root: str) -> list:
    roots = [steam_root]
    for vdf in (os.path.join(steam_root, "steamapps", "libraryfolders.vdf"),
                os.path.join(steam_root, "config", "libraryfolders.vdf")):
        try:
            text = open(vdf, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for m in re.finditer(r'"path"\s*"([^"]+)"', text):
            p = m.group(1).replace("\\\\", "/")
            if os.path.isdir(os.path.join(p, "steamapps")) and p not in roots:
                roots.append(p)
    return roots


def _install_dir(steam_root: str, app_id: str) -> str:
    """Where the game's files live, from appmanifest_<appid>.acf across libraries."""
    for lib in _library_roots(steam_root):
        acf = os.path.join(lib, "steamapps", "appmanifest_%s.acf" % app_id)
        if not os.path.isfile(acf):
            continue
        try:
            text = open(acf, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        m = re.search(r'"installdir"\s+"([^"]+)"', text, re.IGNORECASE)
        if m:
            path = os.path.join(lib, "steamapps", "common", m.group(1))
            if os.path.isdir(path):
                return path
    return ""


def _logged_in_account32(steam_root: str) -> str:
    """The 32-bit account id of the signed-in Steam account. RE Engine cloud saves
    live under userdata/<this id>/<appid>, keyed by the SIGNED-IN account, not by
    the spoofed id the game signs the save with."""
    # The running Steam records the active user in registry.vdf.
    for reg in (os.path.expanduser("~/.steam/registry.vdf"),
                os.path.join(steam_root, "registry.vdf")):
        try:
            text = open(reg, encoding="utf-8", errors="replace").read()
            m = re.search(r'"ActiveUser"\s*"(\d+)"', text)
            if m and int(m.group(1)) != 0:
                return str(int(m.group(1)))
        except OSError:
            pass
    # Steam not running, or no active user: fall back to the most recent login.
    try:
        text = open(os.path.join(steam_root, "config", "loginusers.vdf"),
                    encoding="utf-8", errors="replace").read()
        best = None
        for m in re.finditer(r'"(\d{17})"\s*\{(.*?)\}', text, re.DOTALL):
            if re.search(r'"MostRecent"\s*"1"', m.group(2)):
                best = m.group(1)
        if not best:
            m = re.search(r'"(\d{17})"', text)
            if m:
                best = m.group(1)
        if best:
            return str(int(best) - STEAMID64_BASE)
    except OSError:
        pass
    # Last resort: exactly one real userdata account is the signed-in one.
    try:
        ud = os.path.join(steam_root, "userdata")
        accts = [n for n in os.listdir(ud)
                 if n.isdigit() and n != "0" and os.path.isdir(os.path.join(ud, n))]
        if len(accts) == 1:
            return accts[0]
    except OSError:
        pass
    return ""


def _is_cache_file(name: str) -> bool:
    """RE Engine drops small cache/index files next to the real save (e.g.
    data00-1.bin). They carry a hyphen; the real save files never do. MandarinJuice
    must only touch the real save, so we keep these out of the work folder and out
    of the save-size detection."""
    return "-" in os.path.splitext(name)[0]


def _save_bytes(folder: str) -> int:
    total = 0
    try:
        for name in os.listdir(folder):
            p = os.path.join(folder, name)
            if (os.path.isfile(p) and not name.lower().endswith(".vdf")
                    and not _is_cache_file(name)):
                total += os.path.getsize(p)
    except OSError:
        pass
    return total


def _save_data_folder(remote: str):
    """The folder under a remote dir that directly holds the most save bytes.
    MandarinJuice does not recurse, so it has to be pointed at exactly that folder.
    Returns (path, bytes, rel) where rel is the path under remote (e.g. win64_save)."""
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


def _acti_folders(steam_root: str, app_id: str) -> list:
    """Every userdata/<id>/<appid>/remote that exists, as (id, remote)."""
    out = []
    userdata = os.path.join(steam_root, "userdata")
    try:
        for name in os.listdir(userdata):
            if not name.isdigit() or name == "0":
                continue
            remote = os.path.join(userdata, name, app_id, "remote")
            if os.path.isdir(remote):
                out.append((name, remote))
    except OSError:
        pass
    return out


def _biggest_save(steam_root: str, app_id: str):
    """The biggest save-data folder across every userdata folder for this game.
    That is the real progress. Returns (data, bytes, rel, remote) or None."""
    best = None
    for _id, remote in _acti_folders(steam_root, app_id):
        df = _save_data_folder(remote)
        if df and (best is None or df[1] > best[1]):
            best = (df[0], df[1], df[2], remote)
    return best


def _appid_folders(steam_root: str, app_id: str) -> set:
    ud = os.path.join(steam_root, "userdata")
    out = set()
    try:
        for name in os.listdir(ud):
            if os.path.isdir(os.path.join(ud, name, app_id)):
                out.add(name)
    except OSError:
        pass
    return out


# --------------------------------------------------------------------------- #
# Game process control (Proton-aware)
# --------------------------------------------------------------------------- #
def _game_procs(install_dir: str) -> list:
    """PIDs whose command line references the game's install dir. Under Proton the
    game exe runs via wine, so /proc/<pid>/exe points at the wine loader; the game
    path shows up in the command line instead, which is what we match on."""
    if not install_dir:
        return []
    out = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as fh:
                cmd = fh.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if install_dir in cmd:
            out.append(int(pid))
    return out


def _kill_game(install_dir: str) -> None:
    for pid in _game_procs(install_dir):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    time.sleep(2)
    for pid in _game_procs(install_dir):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _launch(app_id: str) -> None:
    url = "steam://rungameid/%s" % app_id
    launcher = shutil.which("steam") or shutil.which("xdg-open")
    if not launcher:
        return
    try:
        subprocess.Popen([launcher, url], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError:
        pass


def _wait_until_running(install_dir: str, deadline: float, log) -> bool:
    if not install_dir:
        _log(log, "Could not locate the game to watch; leaving saves alone.")
        return False
    _log(log, "Waiting for the game to start...")
    while time.time() < deadline:
        if _game_procs(install_dir):
            return True
        time.sleep(1)
    return False


def _settle_and_kill(steam_root, app_id, install_dir, target, base_folders, log) -> None:
    deadline = time.time() + _FOLDER_CAP_SECONDS
    while time.time() < deadline:
        if not _game_procs(install_dir):
            break
        if os.path.isdir(target) or (_appid_folders(steam_root, app_id) - base_folders):
            break
        time.sleep(1)
    time.sleep(_KILL_GRACE_SECONDS)
    _kill_game(install_dir)


# --------------------------------------------------------------------------- #
# MandarinJuice (linux CLI) + .NET runtime
# --------------------------------------------------------------------------- #
def _tools():
    """Bundled linux MandarinJuice CLI and profiles dir (both next to this script).
    Raises if the CLI is missing."""
    cli = os.path.join(_HERE, "mandarin-juice-cli")
    if not os.path.isfile(cli):
        raise RuntimeError("save tools are missing from the bundle")
    try:
        os.chmod(cli, 0o755)
    except OSError:
        pass
    return cli, _HERE


def _dotnet_env(log):
    """Return an environment that can run the framework-dependent CLI, installing
    the .NET 10 runtime into ~/.tokeer/dotnet (no root) if the system has none.
    Returns the env dict, or None if the runtime could not be made available."""
    env = dict(os.environ)

    def has10(root=None):
        exe = os.path.join(root, "dotnet") if root else (shutil.which("dotnet") or "dotnet")
        try:
            out = subprocess.run([exe, "--list-runtimes"], capture_output=True,
                                 text=True, timeout=20).stdout or ""
        except Exception:
            return False
        return any(l.startswith("Microsoft.NETCore.App 10.") for l in out.splitlines())

    if has10():
        return env

    local = os.path.join(os.path.expanduser("~/.tokeer"), "dotnet")
    if has10(local):
        env["DOTNET_ROOT"] = local
        env["PATH"] = local + os.pathsep + env.get("PATH", "")
        return env

    _log(log, "Fetching the .NET 10 runtime (one time, no admin needed)...")
    try:
        script = os.path.join(os.path.expanduser("~/.tokeer"), "dotnet-install.sh")
        os.makedirs(os.path.dirname(script), exist_ok=True)
        urllib.request.urlretrieve("https://dot.net/v1/dotnet-install.sh", script)
        os.chmod(script, 0o755)
        subprocess.run(["bash", script, "--channel", "10.0", "--runtime", "dotnet",
                        "--install-dir", local], timeout=600, check=True)
    except Exception as e:
        _log(log, "Could not install the .NET runtime (%s); skipping save carry-over." % e)
        return None
    if has10(local):
        env["DOTNET_ROOT"] = local
        env["PATH"] = local + os.pathsep + env.get("PATH", "")
        return env
    _log(log, "The .NET runtime did not install cleanly; skipping save carry-over.")
    return None


def _brute_old_id(cli, profile, work, env, log):
    """Run the CLI's brute-force on the real save files (slot files first) until one
    yields the id it was signed with. Returns (found_id_str, is_64bit) or (None, False)."""
    try:
        bins = [f for f in os.listdir(work)
                if f.lower().endswith(".bin") and not _is_cache_file(f)]
    except OSError:
        bins = []
    order = [f for f in bins if "slot" in f.lower()] + [f for f in bins if "slot" not in f.lower()]
    for name in order or bins:
        tmp = os.path.join(work, ".mjb_%d" % int(time.time() * 1000))
        try:
            os.makedirs(tmp, exist_ok=True)
            shutil.copy2(os.path.join(work, name), os.path.join(tmp, name))
            _log(log, "Checking %s ..." % name)
            r = subprocess.run([cli, "-m", "b", "-g", profile, "-p", tmp, "-q"],
                               capture_output=True, text=True, timeout=1800, env=env,
                               cwd=os.path.dirname(cli))
            m = re.search(r"Found UserID\D*(\d+)", (r.stdout or "") + (r.stderr or ""))
            if m:
                fid = m.group(1)
                return fid, int(fid) >= STEAMID64_BASE
        except Exception:
            pass
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return None, False


def _resigned_files(cli_dir: str, since: float):
    out_root = os.path.join(cli_dir, "_OUTPUT")
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
    kids = [os.path.join(newest, d) for d in os.listdir(newest)
            if os.path.isdir(os.path.join(newest, d))]
    content = kids[0] if len(kids) == 1 else newest
    files = [os.path.join(content, f) for f in os.listdir(content)]
    return files if files else None


# --------------------------------------------------------------------------- #
# Public
# --------------------------------------------------------------------------- #
def carry_over(app_id: str, new_owner_steamid: str, steam_root: str = "",
               install_dir: str = "", log=None) -> bool:
    """Carry the signed-in account's save onto this activation's id and relaunch.
    new_owner_steamid is the SteamID the ticket owner spoofs (a full SteamID64).
    Returns True if it launched the game itself (so the caller should not), False
    if it did nothing and the caller should launch normally."""
    app_id = "".join(c for c in str(app_id) if c.isdigit())
    if not is_supported(app_id) or not new_owner_steamid:
        return False
    try:
        n = int(new_owner_steamid)
    except (TypeError, ValueError):
        return False
    new64 = str(n if n > STEAMID64_BASE else n + STEAMID64_BASE)
    new32 = str(n - STEAMID64_BASE if n > STEAMID64_BASE else n)

    steam_root = steam_root or _steam_root()
    if not steam_root:
        return False
    install_dir = install_dir or _install_dir(steam_root, app_id)
    profile_name = SUPPORTED[app_id]

    # Prefer the signed-in account's folder (where the game reads and writes). If it
    # can't be read, fall back to the userdata folder that actually holds the biggest
    # save for this game, so a Deck that doesn't expose the active user still works.
    active = _logged_in_account32(steam_root)
    target_remote = ""
    if active:
        cand = os.path.join(steam_root, "userdata", active, app_id, "remote")
        if os.path.isdir(cand):
            target_remote = cand
    if not target_remote:
        best = _biggest_save(steam_root, app_id)
        if best:
            target_remote = best[3]

    df = _save_data_folder(target_remote) if target_remote else None
    if not df:
        _log(log, "No save found for this game to carry; opening the game.")
        _launch(app_id)
        return True
    old_data, _b, rel = df
    target = os.path.join(target_remote, rel) if rel else target_remote

    work = os.path.join("/tmp", "ltsave_%s_%d" % (app_id, int(time.time())))

    def _skip_cache(dirpath, names):
        return [x for x in names
                if os.path.isfile(os.path.join(dirpath, x)) and _is_cache_file(x)]
    try:
        shutil.copytree(old_data, work, ignore=_skip_cache)
    except Exception as e:
        _log(log, "Could not stage the save (%s); skipping." % e)
        return False

    base_folders = _appid_folders(steam_root, app_id)
    _log(log, "Launching the game...")
    _launch(app_id)
    if not _wait_until_running(install_dir, time.time() + 1800, log):
        _log(log, "Game did not start; leaving saves alone.")
        shutil.rmtree(work, ignore_errors=True)
        return True
    _log(log, "Closing the game to swap your save in...")
    _settle_and_kill(steam_root, app_id, install_dir, target, base_folders, log)
    time.sleep(3)

    try:
        cli, profiles = _tools()
    except Exception as e:
        _log(log, "Save tools unavailable (%s); skipping." % e)
        shutil.rmtree(work, ignore_errors=True)
        _launch(app_id)
        return True
    profile = os.path.join(profiles, profile_name)
    if not os.path.isfile(profile):
        _log(log, "No save profile for this game; skipping.")
        shutil.rmtree(work, ignore_errors=True)
        _launch(app_id)
        return True

    env = _dotnet_env(log)
    if not env:
        shutil.rmtree(work, ignore_errors=True)
        _launch(app_id)
        return True

    _log(log, "Detecting your save's id (this can take a few minutes)...")
    found, is64 = _brute_old_id(cli, profile, work, env, log)
    if not found:
        _log(log, "Could not detect the save id; your account was not changed.")
        shutil.rmtree(work, ignore_errors=True)
        _launch(app_id)
        return True

    new_out = new64 if is64 else new32
    _log(log, "Re-signing your save from %s to %s ..." % (found, new_out))
    since = time.time()
    try:
        subprocess.run([cli, "-m", "r", "-g", profile, "-p", work,
                        "-uI", found, "-uO", new_out, "-q"],
                       timeout=1800, env=env, cwd=os.path.dirname(cli))
    except Exception as e:
        _log(log, "Re-signing failed (%s); your account was not changed." % e)
        shutil.rmtree(work, ignore_errors=True)
        _launch(app_id)
        return True

    files = _resigned_files(os.path.dirname(cli), since)
    if not files:
        _log(log, "Re-signing produced no files; your account was not changed.")
        shutil.rmtree(work, ignore_errors=True)
        _launch(app_id)
        return True

    # Back up the untouched save once, outside the cloud-synced remote folder.
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
            _log(log, "Could not write %s (%s)." % (os.path.basename(f), e))

    # Drop the game's stale cache/index files so it rebuilds them against the
    # freshly signed save instead of the previous activation's.
    try:
        for name in os.listdir(target):
            p = os.path.join(target, name)
            if _is_cache_file(name) and os.path.isfile(p):
                os.remove(p)
    except OSError:
        pass

    # Stale remotecache.vdf makes Steam re-pull the old cloud copy over ours.
    rc = os.path.join(os.path.dirname(target_remote), "remotecache.vdf")
    try:
        if os.path.isfile(rc):
            os.replace(rc, rc + "_org")
    except OSError:
        pass

    _log(log, "Carried your save over. Restarting the game...")
    _launch(app_id)
    return True
