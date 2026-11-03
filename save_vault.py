"""Save carry-over for every Tokeer game MandarinJuice doesn't cover.

Each activation runs the game as a different pool account. A lot of games keep
their saves in a folder named after the SteamID (...\\SaveGames\\7656119...\\),
so after a new activation the game opens an empty folder and the progress looks
gone. It's still on disk, under the previous activation's id.

redeem() knows both ids: the SteamID the registry held for the app before the new
ticket went in, and the new ticket's owner. This looks for folders (and files)
named after any earlier activation id in the usual save spots, picks the real
progress among them and copies it into a folder for the new id, before the game
starts. Whatever the new id's folder held is backed up to SaveVault first, and so
is the save being carried, so a wrong pick can always be undone.

Games that save to a fixed folder don't need any of this. Games that sign the save
with the id (Capcom RE Engine) are save_watchdog's job and are skipped here.
Best effort throughout: failures go to save_carry.log and never reach the UI.
"""

import json
import os
import re
import shutil
import time

import save_watchdog as sw

BASE = sw.STEAMID64_BASE
VAULT_DIR = os.path.join(sw._DATA_DIR, "SaveVault")
_STATE_PATH = os.path.join(sw._DATA_DIR, "save_vault.json")

_KEEP_BACKUPS = 10
_SCAN_SECONDS = 20
_MAX_DEPTH = 7
_MAX_SIBLINGS = 50
_MAX_BACKUP_BYTES = 1 << 30

# Never worth walking into. Lower-case directory names, matched at any depth.
_SKIP_DIRS = {
    "microsoft", "packages", "windowsapps", "google", "mozilla", "temp", "tmp",
    "nvidia", "nvidia corporation", "amd", "intel", "crashdumps", "d3dscache",
    "pip", "npm-cache", "node_modules", ".git", "programs", "tokeerdrm",
    "discord", "spotify", "code", "squirrelsetup", "steam", "cache", "caches",
    "shadercache", "gpucache", "codecache", "webcache", "logs",
    "connecteddevicesplatform", "comms", "my music", "my pictures", "my videos",
    "pnpm", "npm", "yarn", "uv", "ccache", "pip-cache", "jetbrains", "python",
    "__pycache__", "site-packages", ".venv", "venv", "cef", "claude",
    "opera software", "bravesoftware", "chromium", "github", ".minecraft",
}

_ID64_RE = re.compile(r"7656119\d{10}")
_REPARSE_LINKS = (0xA0000003, 0xA000000C)  # junction, symlink

_DOCUMENTS = "FDD39AD0-238F-46AF-ADB4-6C85480369C7"
_SAVED_GAMES = "4C5C32FF-BB9D-43B0-B5B4-2D72E54EAAA4"
_LOCAL_LOW = "A520A1A4-1780-4FF6-BD18-167343C5AF16"


def _log(msg):
    sw._file_log("[vault] " + msg)


def _forms(sid):
    """Both spellings of an id a game might use in a path: SteamID64 and the
    32-bit account id."""
    try:
        n = int(str(sid).strip())
    except (TypeError, ValueError):
        return set()
    if n > BASE:
        return {str(n), str(n - BASE)}
    if n > 0:
        return {str(n), str(n + BASE)}
    return set()


def _looks_like_id(name):
    if not name.isdigit():
        return False
    if len(name) == 17:
        return name.startswith("7656119")
    return 6 <= len(name) <= 10


def _known_folder(guid):
    try:
        import ctypes
        import uuid

        class GUID(ctypes.Structure):
            _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16),
                        ("d3", ctypes.c_uint16), ("d4", ctypes.c_ubyte * 8)]

        u = uuid.UUID(guid)
        g = GUID(u.fields[0], u.fields[1], u.fields[2],
                 (ctypes.c_ubyte * 8)(*u.bytes[8:]))
        p = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None,
                                                       ctypes.byref(p)) != 0:
            return ""
        path = p.value or ""
        ctypes.windll.ole32.CoTaskMemFree(p)
        return path
    except Exception:
        return ""


def _norm(p):
    return os.path.normcase(os.path.abspath(p))


def _roots(install_dir):
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    public = os.environ.get("PUBLIC") or r"C:\Users\Public"
    cands = [
        _known_folder(_DOCUMENTS) or os.path.join(home, "Documents"),
        _known_folder(_SAVED_GAMES) or os.path.join(home, "Saved Games"),
        os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming"),
        local,
        _known_folder(_LOCAL_LOW) or os.path.join(os.path.dirname(local), "LocalLow"),
        os.path.join(public, "Documents"),
        os.environ.get("PROGRAMDATA") or r"C:\ProgramData",
        install_dir,
    ]
    out, seen = [], set()
    for c in cands:
        if c and os.path.isdir(c) and _norm(c) not in seen:
            seen.add(_norm(c))
            out.append(c)
    return out


def _is_link(entry):
    try:
        st = entry.stat(follow_symlinks=False)
        return getattr(st, "st_reparse_tag", 0) in _REPARSE_LINKS or entry.is_symlink()
    except OSError:
        return True


def _scan(roots, dir_names, id64s, skip, deadline):
    """Directories named exactly after an old id, and files whose name carries an
    old SteamID64. Hit directories aren't walked into."""
    dirs, files = set(), set()
    for root in roots:
        stack = [(root, 0)]
        while stack:
            if time.time() > deadline:
                _log("scan hit the %ss limit, using what it found" % _SCAN_SECONDS)
                return dirs, files
            path, depth = stack.pop()
            try:
                it = os.scandir(path)
            except OSError:
                continue
            with it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if _is_link(e):
                                continue
                            if e.name in dir_names:
                                dirs.add(e.path)
                            elif (depth < _MAX_DEPTH and e.name.lower() not in _SKIP_DIRS
                                  and _norm(e.path) not in skip):
                                stack.append((e.path, depth + 1))
                        elif any(i in e.name for i in id64s):
                            files.add(e.path)
                    except OSError:
                        continue
    return dirs, files


def _content(path):
    """(bytes, newest mtime) of a file or of everything under a folder."""
    if os.path.isfile(path):
        try:
            st = os.stat(path)
            return st.st_size, st.st_mtime
        except OSError:
            return 0, 0
    size, newest = 0, 0
    for root, _d, names in os.walk(path):
        for n in names:
            try:
                st = os.stat(os.path.join(root, n))
            except OSError:
                continue
            size += st.st_size
            newest = max(newest, st.st_mtime)
    return size, newest


def _pick(cands, target):
    """The real progress among a game's per-id saves. Newest alone loses when a
    launch under a new id wrote a fresh near-empty save; biggest alone loses when
    an old activation's save is larger. So: newest among those at least half the
    size of the biggest. A copy keeps its source's mtimes, so on a tie the target
    wins and an already carried save isn't copied again."""
    stats = {c: _content(c) for c in cands}
    real = [c for c in cands if stats[c][0] > 0]
    if not real:
        return None, stats
    top = max(stats[c][0] for c in real)
    tgt = _norm(target)
    best = max((c for c in real if stats[c][0] * 2 >= top),
               key=lambda c: (stats[c][1], _norm(c) == tgt))
    return best, stats


_STOPWORDS = {"the", "and", "edition", "game", "games", "deluxe", "definitive",
              "remastered", "ultimate", "complete", "standard", "digital", "steam"}


def _squash(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _game_tokens(steam, app_id, install_dir):
    """Name fragments of the game, from its appmanifest name and install folder."""
    names = [os.path.basename(install_dir)] if install_dir else []
    for lib in sw._library_paths(steam) if steam else []:
        acf = os.path.join(lib, "steamapps", "appmanifest_%s.acf" % app_id)
        try:
            m = re.search(r'"name"\s+"([^"]+)"', open(acf, encoding="utf-8",
                                                     errors="replace").read())
        except OSError:
            continue
        if m:
            names.append(m.group(1))
            break
    tokens = set()
    for n in names:
        if len(_squash(n)) >= 4:
            tokens.add(_squash(n))
        tokens |= {w for w in (_squash(x) for x in re.split(r"[\s:_\-.]+", n))
                   if len(w) >= 4 and w not in _STOPWORDS}
    return tokens


def _for_this_game(parents, tokens):
    """A pool account can own several games, so an old id can turn up in another
    game's save folder too. Prefer folders whose path names this game; if none do
    (studio codenames), keep them all. Carrying another game's save only copies it
    within that game's own folder, so a wrong guess costs nothing."""
    if not tokens:
        return parents
    named = {p for p in parents if any(t in _squash(p) for t in tokens)}
    return named or parents


def _load_state():
    try:
        with open(_STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state):
    try:
        os.makedirs(sw._DATA_DIR, exist_ok=True)
        tmp = _STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1)
        os.replace(tmp, _STATE_PATH)
    except Exception as e:
        _log("could not write state: %r" % e)


class _Backup:
    """One timestamped folder in SaveVault\\<appid> per carry-over."""

    def __init__(self, app_id):
        self.root = os.path.join(VAULT_DIR, app_id)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.dir = os.path.join(self.root, stamp)
        n = 1
        while os.path.exists(self.dir):
            self.dir = os.path.join(self.root, "%s-%d" % (stamp, n))
            n += 1
        self.items = []

    def add(self, path, label):
        size = _content(path)[0]
        if size > _MAX_BACKUP_BYTES:
            _log("not backing up %s, %d bytes is over the limit" % (path, size))
            return False
        dest = os.path.join(self.dir, "%02d_%s_%s" % (len(self.items), label,
                                                    os.path.basename(path)))
        try:
            os.makedirs(self.dir, exist_ok=True)
            if os.path.isdir(path):
                shutil.copytree(path, dest)
            else:
                shutil.copy2(path, dest)
        except Exception as e:
            _log("backup of %s failed: %r" % (path, e))
            return False
        self.items.append({"what": label, "from": path, "saved_as": os.path.basename(dest)})
        return True

    def finish(self):
        if not self.items:
            return
        try:
            with open(os.path.join(self.dir, "info.json"), "w", encoding="utf-8") as fh:
                json.dump(self.items, fh, indent=1)
            stamps = sorted(d for d in os.listdir(self.root)
                            if re.fullmatch(r"\d{8}-\d{6}(-\d+)?", d))
            for old in stamps[:-_KEEP_BACKUPS]:
                shutil.rmtree(os.path.join(self.root, old), ignore_errors=True)
        except Exception as e:
            _log("backup bookkeeping failed: %r" % e)


def _place(src, target, backup):
    """Copy src over target. The old target is backed up first, or moved aside
    when it's too big for the vault, so nothing is ever just deleted."""
    if os.path.exists(target) and not backup.add(target, "replaced"):
        aside = "%s.tokeer-%s" % (target, time.strftime("%Y%m%d-%H%M%S"))
        os.replace(target, aside)
        _log("moved %s aside to %s" % (target, aside))
    backup.add(src, "carried")
    if os.path.isdir(src):
        tmp = target + ".tokeer-tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            shutil.copytree(src, tmp)
            if os.path.exists(target):
                shutil.rmtree(target)
            os.replace(tmp, target)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    else:
        shutil.copy2(src, target)


def _carry_dirs(parent, new_forms, backup):
    """Per-id save folders under one parent. Returns the carried folder or None."""
    try:
        sibs = [os.path.join(parent, n) for n in os.listdir(parent)
                if _looks_like_id(n) and os.path.isdir(os.path.join(parent, n))]
    except OSError:
        return None
    if not sibs or len(sibs) > _MAX_SIBLINGS:
        return None
    long_form = any(len(os.path.basename(s)) == 17 for s in sibs)
    new_name = next(f for f in new_forms if (len(f) == 17) == long_form)
    target = os.path.join(parent, new_name)
    src, stats = _pick(sibs, target)
    if not src:
        return None
    if _norm(src) == _norm(target):
        _log("%s already holds the newest save" % target)
        return None
    _log("carry %s (%d bytes) -> %s" % (src, stats[src][0], target))
    _place(src, target, backup)
    return src


def _carry_file(path, new64, backup, done):
    """Saves named after the id (save_7656119....sav) instead of living in a
    per-id folder. Same pick, among every file in that folder with the same name
    apart from the id."""
    folder, name = os.path.split(path)
    m = _ID64_RE.search(name)
    if not m:
        return None
    pattern = re.compile(re.escape(name[:m.start()]) + r"7656119\d{10}"
                         + re.escape(name[m.end():]) + r"\Z")
    key = (_norm(folder), pattern.pattern)
    if key in done:
        return None
    done.add(key)
    try:
        cands = [os.path.join(folder, n) for n in os.listdir(folder)
                 if pattern.match(n) and os.path.isfile(os.path.join(folder, n))]
    except OSError:
        return None
    target = os.path.join(folder, name[:m.start()] + new64 + name[m.end():])
    src, stats = _pick(cands, target)
    if not src:
        return None
    if _norm(src) == _norm(target):
        return None
    _log("carry %s (%d bytes) -> %s" % (src, stats[src][0], target))
    _place(src, target, backup)
    return src


def carry_over(app_id, old_id, new_id, ui=None):
    """Bring the save from earlier activations over to new_id. Runs before the
    game launches. Returns True when something was carried."""
    app_id = "".join(c for c in str(app_id) if c.isdigit())
    new_forms = _forms(new_id)
    if not app_id or not new_forms or sw.is_supported(app_id):
        return False
    report = sw._Report(ui)
    try:
        return _carry_over(app_id, str(old_id or ""), new_forms, report)
    except Exception as e:
        _log("app=%s crashed: %r" % (app_id, e))
        return False


def _carry_over(app_id, old_id, new_forms, report):
    new64 = next(f for f in new_forms if len(f) == 17)
    state = _load_state()
    app = state.setdefault("apps", {}).setdefault(app_id, {"ids": [], "parents": []})

    old_ids = [i for i in app.get("ids", []) + [old_id] if i and _forms(i) != new_forms]
    old_forms = set()
    for i in old_ids:
        old_forms |= _forms(i)

    def remember(parents=()):
        ids = [i for i in app.get("ids", []) if i != new64] + [i for i in [old_id] if i]
        app["ids"] = list(dict.fromkeys(ids + [new64]))[-50:]
        app["parents"] = list(dict.fromkeys(app.get("parents", []) + list(parents)))[-10:]
        _save_state(state)

    if not old_forms:
        _log("app=%s: first activation on this PC, nothing to carry" % app_id)
        remember()
        return False

    steam = sw.steam_path()
    install_dir = sw._install_dir(steam, app_id) if steam else ""
    if install_dir and sw._game_running(install_dir):
        _log("app=%s: game is running, not touching its saves" % app_id)
        remember()
        return False

    skip = {_norm(VAULT_DIR)}
    if steam:
        skip |= {_norm(p) for p in sw._library_paths(steam)}
    roots = _roots(install_dir)
    id64s = {f for f in old_forms if len(f) == 17}
    dirs, files = _scan(roots, old_forms, id64s, skip, time.time() + _SCAN_SECONDS)
    tokens = _game_tokens(steam, app_id, install_dir)
    parents = _for_this_game({os.path.dirname(d) for d in dirs}, tokens)
    parents |= {p for p in app.get("parents", []) if os.path.isdir(p)}
    file_dirs = _for_this_game({os.path.dirname(f) for f in files}, tokens)
    files = {f for f in files if os.path.dirname(f) in file_dirs}
    _log("app=%s old=%s new=%s: %d id folders, %d id files, parents=%s"
         % (app_id, sorted(old_ids), new64, len(dirs), len(files), sorted(parents)))
    if not parents and not files:
        remember()
        return False

    backup = _Backup(app_id)
    carried, used = [], []
    for p in sorted(parents):
        try:
            got = _carry_dirs(p, new_forms, backup)
        except Exception as e:
            _log("carry under %s failed: %r" % (p, e))
            got = None
        if got:
            carried.append(got)
        used.append(p)
    done = set()
    for f in sorted(files):
        try:
            got = _carry_file(f, new64, backup, done)
        except Exception as e:
            _log("carry of %s failed: %r" % (f, e))
            got = None
        if got:
            carried.append(got)
    backup.finish()
    remember(used)

    if carried:
        newest = max(_content(c)[1] for c in carried)
        report("Your save from %s was carried over to this activation."
               % time.strftime("%d %b %Y %H:%M", time.localtime(newest)), kind="ok")
        return True
    _log("app=%s: newest save already in place" % app_id)
    return False
