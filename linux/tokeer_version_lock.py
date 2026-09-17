#!/usr/bin/env python3
"""
Version lock for the Linux/Proton flow.

Some games' newest Steam build breaks the activation, so the supported build has
to be forced back on. The Windows validator does that with a pinned lua plus the
build's manifests dropped into Steam's depotcache. slsteam-moon (the loader
behind luatools-moon) works differently:

  * it imports only addappid() depot keys out of config/stplug-in/*.lua and
    ignores setManifestid, so a pinned lua on its own pins nothing;
  * the pin lives in ~/.config/SLSsteam/config.yaml under ManifestPins, where
    "locked: true" means "allow the one downgrade, then freeze on that build";
  * manifests live in ~/.config/SLSsteam/manifests, which Steam never purges,
    and the depot download hooks restore them into depotcache on demand, so no
    manifest request code is ever asked for.

So the lock is three writes: the locked lua (depot keys, and it is what makes
the app managed), the build's manifests into the store, and the pin itself.
Steam then shows an Update for the game which is really the downgrade.

Everything except the appid list comes out of the same <appid>.zip the Windows
script uses, so a newly locked game only needs its pack uploaded.

Standalone:  python3 tokeer_version_lock.py <APPID>
"""

import os
import re
import shutil
import sys
import urllib.request
import zipfile

PACK_URL = "https://github.com/madoiscool/lt_api_links/releases/download/versionlocks/{0}.zip"

# Games whose latest Steam build breaks activation. The pinned build, its lua
# and its manifests all come from the pack; only the name lives here.
VERSION_LOCKED = {
    "3751950": "Assassin's Creed Black Flag Resynced",
    "3405690": "EA SPORTS FC 26",
}

MANIFEST_MAGIC = b"\xd0\x17\xf6\x71"

TDIR = os.environ.get("TOKEER_HOME", os.path.expanduser("~/.tokeer"))


def is_locked(appid):
    return str(appid) in VERSION_LOCKED


def game_name(appid):
    return VERSION_LOCKED.get(str(appid), str(appid))


# --------------------------------------------------------------------------- #
# paths
# Same two helpers as the validator's, repeated so this module also runs alone.
def steam_roots():
    out = []
    for c in ("~/.steam/steam", "~/.local/share/Steam", "~/.steam/root",
              "~/.steam/debian-installation",
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


def steam_config_root():
    """The Steam root that holds config/stplug-in, where the luas go."""
    roots = steam_roots()
    for root in roots:
        if os.path.isdir(os.path.join(root, "config")):
            return root
    return roots[0] if roots else None


def config_dir():
    xdg = os.environ.get("XDG_CONFIG_HOME") or ""
    base = xdg if xdg else os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "SLSsteam")


def store_dir():
    return os.path.join(config_dir(), "manifests")


def config_file():
    return os.path.join(config_dir(), "config.yaml")


def imports_file():
    return os.path.join(config_dir(), "lumen_lua_imports.txt")


def sls_present():
    return (os.path.isdir(config_dir())
            or os.path.isdir(os.path.expanduser("~/.local/share/SLSsteam")))


# --------------------------------------------------------------------------- #
# small file helpers
def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _read_bytes(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _write_atomic(path, data):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = "%s.tokeer_tmp.%d" % (path, os.getpid())
    if isinstance(data, bytes):
        with open(tmp, "wb") as fh:
            fh.write(data)
    else:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(data)
    os.replace(tmp, path)


def _valid_manifest(path):
    data = None
    try:
        with open(path, "rb") as fh:
            data = fh.read(4)
    except OSError:
        return False
    return data == MANIFEST_MAGIC


# --------------------------------------------------------------------------- #
# the pack
def parse_pins(lua):
    pins = {}
    for m in re.finditer(r'(?im)^[ \t]*setManifestid\s*\(\s*(\d+)\s*,\s*"(\d+)"', lua or ""):
        pins[m.group(1)] = m.group(2)
    return pins


def parse_build_id(lua):
    m = re.search(r"(?im)^--\s*Version-locked to Build\s+(\d+)", lua or "")
    return m.group(1) if m else None


def open_pack(path, appid):
    """Return (lua_text, {filename: bytes}) from a manifest pack, or None."""
    if not path or not os.path.isfile(path):
        return None
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            lua = None
            for name in names:
                if os.path.basename(name) == "%s.lua" % appid:
                    lua = zf.read(name).decode("utf-8", "replace")
                    break
            if not lua:
                return None
            manifests = {}
            for name in names:
                base = os.path.basename(name)
                if not re.match(r"^\d+_\d+\.manifest$", base):
                    continue
                data = zf.read(name)
                if data[:4] == MANIFEST_MAGIC:
                    manifests[base] = data
            return lua, manifests
    except (OSError, zipfile.BadZipFile, KeyError):
        return None


def pack_candidates(appid):
    here = os.path.dirname(os.path.abspath(__file__))
    return [
        os.path.join(TDIR, "versionlocks", "%s.zip" % appid),
        os.path.join(here, "%s.zip" % appid),
        os.path.expanduser("~/Downloads/%s.zip" % appid),
    ]


def download_pack(appid, dest):
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = dest + ".part"
    req = urllib.request.Request(PACK_URL.format(appid),
                                 headers={"User-Agent": "tokeer-linux"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        with open(tmp, "wb") as fh:
            shutil.copyfileobj(resp, fh)
    os.replace(tmp, dest)
    return dest


# --------------------------------------------------------------------------- #
# the three writes
def place_manifests(pins, manifests, steam_root):
    """Put every pinned manifest in the slsteam store, and in depotcache too
    when it exists (older loaders read only from there). Only the PINNED
    depots: an unpinned depot's old manifest would join the store's fallback
    pool, and those depots should keep tracking the public build.

    Returns (placed, missing)."""
    store = store_dir()
    depotcache = os.path.join(steam_root, "depotcache") if steam_root else None
    placed = 0
    missing = []
    for depot in sorted(pins, key=int):
        name = "%s_%s.manifest" % (depot, pins[depot])
        target = os.path.join(store, name)
        if not _valid_manifest(target):
            data = manifests.get(name)
            if not data:
                missing.append(name)
                continue
            _write_atomic(target, data)
            placed += 1
        if depotcache and os.path.isdir(depotcache):
            cached = os.path.join(depotcache, name)
            if not _valid_manifest(cached):
                data = manifests.get(name) or _read_bytes(target)
                if data:
                    try:
                        _write_atomic(cached, data)
                    except OSError:
                        pass
    return placed, missing


def write_lua(steam_root, appid, lua_text):
    """Write the locked lua, keeping any depot key the previous script had that
    the locked one does not list. Returns (path, changed).

    Nothing patches the other luas here: setManifestid is ignored on Linux, so
    a competing pin in another script cannot cancel this lock the way it does
    on Windows."""
    stplug = os.path.join(steam_root, "config", "stplug-in")
    os.makedirs(stplug, exist_ok=True)
    path = os.path.join(stplug, "%s.lua" % appid)
    old = (_read_text(path) or "").replace("\r\n", "\n")
    text = lua_text.replace("\r\n", "\n")

    have = set(re.findall(r"(?m)^[ \t]*addappid\s*\(\s*(\d+)", text))
    extra = []
    for m in re.finditer(r'(?m)^[ \t]*addappid\s*\(\s*(\d+)\s*,\s*\d+\s*,\s*"[0-9A-Fa-f]{64}"\s*\)', old):
        if m.group(1) not in have:
            extra.append(m.group(0).strip())
    if extra:
        text = text.rstrip("\n") + "\n\n-- kept from the previous script\n" + "\n".join(extra) + "\n"

    if old == text:
        return path, False
    if old and not os.path.exists(path + ".bak"):
        try:
            shutil.copyfile(path, path + ".bak")
        except OSError:
            pass
    _write_atomic(path, text)
    return path, True


def _block_end(lines, header):
    """Index of the last line of the block opened at `header` (a line starting
    in column 0 ends it; blank lines stay inside)."""
    for i in range(header + 1, len(lines)):
        if lines[i][:1] and not lines[i][:1].isspace():
            return i - 1
    return len(lines) - 1


def sync_manifest_pins(text, appid, pins, build_id):
    """Return config.yaml with this app's ManifestPins block replaced, or
    (None, error). Comments, ordering and every other key are preserved."""
    lines = text.split("\n") if text else []
    if lines and lines[-1] == "":
        lines.pop()

    header = None
    for i, line in enumerate(lines):
        m = re.match(r"^ManifestPins\s*:\s*(.*?)\s*$", line)
        if m:
            if re.sub(r"#.*$", "", m.group(1)).strip():
                return None, "ManifestPins already has an inline value in config.yaml"
            header = i
            break

    if header is not None:
        block_end = _block_end(lines, header)
        app_start = None
        app_end = None
        for i in range(header + 1, block_end + 1):
            m = re.match(r"^  (\d+)\s*:\s*$", lines[i])
            if m and int(m.group(1)) == int(appid):
                app_start = i
                app_end = block_end
                for j in range(i + 1, block_end + 1):
                    if re.match(r"^  \S", lines[j]):
                        app_end = j - 1
                        break
                break
        if app_start is not None:
            if not build_id:
                # An offline run reads the pins off the installed lua and has no
                # pack to read the build number from. Keep the one already here.
                for line in lines[app_start:app_end + 1]:
                    m = re.match(r"^\s+build_id\s*:\s*(\d+)\s*$", line)
                    if m:
                        build_id = m.group(1)
                        break
            del lines[app_start:app_end + 1]

    if header is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("ManifestPins:")
        header = len(lines) - 1

    block = ["  %s:" % appid, "    locked: true"]
    if build_id:
        block.append("    build_id: %s" % build_id)
    block.append("    depots:")
    for depot in sorted(pins, key=int):
        block.append('      %s: "%s"' % (depot, pins[depot]))

    at = _block_end(lines, header) + 1
    lines[at:at] = block
    return "\n".join(lines) + "\n", None


def sync_pins(appid, pins, build_id):
    """Write the pin into config.yaml. Returns (ok, error)."""
    path = config_file()
    current = _read_text(path)
    if current is None:
        # No config yet: slsteam fills every missing key from its own defaults,
        # so a file holding just the pin is enough to get started.
        current = ""
    updated, err = sync_manifest_pins(current, appid, pins, build_id)
    if err:
        return False, err
    if updated == current:
        return True, None
    if current and not os.path.exists(path + ".bak"):
        try:
            shutil.copyfile(path, path + ".bak")
        except OSError:
            pass
    try:
        _write_atomic(path, updated)
    except OSError as exc:
        return False, str(exc)
    return True, None


def mark_import(appid):
    """List the app in lumen_lua_imports.txt, the way the plugin does, so it
    treats the script as published rather than something to clean up."""
    path = imports_file()
    ids = set()
    for line in (_read_text(path) or "").splitlines():
        line = line.strip()
        if line.isdigit():
            ids.add(line)
    if str(appid) in ids:
        return False
    ids.add(str(appid))
    try:
        _write_atomic(path, "\n".join(sorted(ids, key=int)) + "\n")
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------- #
# state
def installed_gids(appid):
    """{depot: manifest gid} out of the app's appmanifest InstalledDepots."""
    for root in steam_roots():
        for lib in library_roots(root):
            text = _read_text(os.path.join(lib, "steamapps", "appmanifest_%s.acf" % appid))
            if not text:
                continue
            m = re.search(r'"InstalledDepots"\s*\{', text)
            if not m:
                return {}
            depth = 1
            i = m.end()
            while i < len(text) and depth:
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                i += 1
            out = {}
            for dm in re.finditer(r'"(\d+)"\s*\{([^{}]*)\}', text[m.end():i - 1]):
                gid = re.search(r'"manifest"\s*"(\d+)"', dm.group(2))
                if gid:
                    out[dm.group(1)] = gid.group(1)
            return out
    return {}


def at_pinned(pins, installed):
    """Every pinned depot Steam actually installed is on its pinned gid, and at
    least one is. Same rule as the loader's own appAtPinnedGids, so this agrees
    with when it freezes updates."""
    if not pins or not installed:
        return False
    matched = False
    for depot, gid in pins.items():
        have = installed.get(depot)
        if have is None:
            continue
        if have != gid:
            return False
        matched = True
    return matched


def apply(appid):
    """Put the lock in place and report where the game stands."""
    appid = str(appid)
    st = {
        "appid": appid,
        "name": game_name(appid),
        "build_id": None,
        "sls": sls_present(),
        "steam_root": None,
        "pins": {},
        "lua_path": None,
        "lua_written": False,
        "placed": 0,
        "missing": [],
        "pin_synced": False,
        "pin_error": None,
        "installed": {},
        "at_pinned": False,
        "ready": False,
        "notes": [],
    }

    root = steam_config_root()
    st["steam_root"] = root
    if not root:
        st["notes"].append("Steam folder not found, so the lock cannot be written.")
        return st
    if not st["sls"]:
        st["notes"].append("slsteam-moon not found (no ~/.config/SLSsteam). "
                           "Install luatools-moon first, then run this again.")
        return st

    lua_path = os.path.join(root, "config", "stplug-in", "%s.lua" % appid)
    pack = None
    for cand in pack_candidates(appid):
        pack = open_pack(cand, appid)
        if pack:
            break
    if not pack:
        dest = os.path.join(TDIR, "versionlocks", "%s.zip" % appid)
        try:
            download_pack(appid, dest)
            pack = open_pack(dest, appid)
            if not pack:
                st["notes"].append("The downloaded pack for this app is unreadable.")
        except Exception as exc:
            st["notes"].append("Could not fetch the locked build's pack: %s" % exc)

    if pack:
        lua_text, manifests = pack
        pins = parse_pins(lua_text)
        build_id = parse_build_id(lua_text)
    else:
        # Offline re-run: the lua already on disk still says what to pin, so the
        # pin and the checks below keep working without the pack.
        lua_text, manifests = None, {}
        pins = parse_pins(_read_text(lua_path) or "")
        build_id = None

    st["pins"] = pins
    st["build_id"] = build_id
    if not pins:
        st["notes"].append("No pinned depots for this app, so there is nothing to lock.")
        return st

    if lua_text:
        st["lua_path"], st["lua_written"] = write_lua(root, appid, lua_text)
    else:
        st["lua_path"] = lua_path

    st["placed"], st["missing"] = place_manifests(pins, manifests, root)
    st["pin_synced"], st["pin_error"] = sync_pins(appid, pins, build_id)
    mark_import(appid)

    st["installed"] = installed_gids(appid)
    st["at_pinned"] = at_pinned(pins, st["installed"])
    st["ready"] = bool(st["pin_synced"] and not st["missing"] and st["at_pinned"])
    return st


def summary(st):
    """Compact form for the signed setup report."""
    return {
        "build": st.get("build_id"),
        "pins": len(st.get("pins") or {}),
        "pinned_ok": bool(st.get("pin_synced")),
        "manifests_missing": len(st.get("missing") or []),
        "at_pinned": bool(st.get("at_pinned")),
    }


def print_status(st):
    def mark(ok):
        return "OK " if ok else "-- "

    print()
    print("[*] %s is version-locked%s (its newest Steam build breaks the activation)."
          % (st["name"], " to build %s" % st["build_id"] if st["build_id"] else ""))
    print("  %s locked script  (%s)" % (mark(bool(st["lua_path"])), st["lua_path"] or "not written"))
    print("  %s build manifests in the slsteam store (%d of %d)"
          % (mark(not st["missing"]), len(st["pins"]) - len(st["missing"]), len(st["pins"])))
    print("  %s pin in config.yaml%s"
          % (mark(st["pin_synced"]), "" if st["pin_synced"] else ": %s" % (st["pin_error"] or "not written")))
    print("  %s game is on the locked build" % mark(st["at_pinned"]))
    for note in st["notes"]:
        print("  !! %s" % note)


def print_next_steps(st):
    print()
    print("=" * 62)
    print(" Let Steam update this game, then verify again")
    print("=" * 62)
    if st["missing"]:
        print("  Some of the locked build's manifests are missing:")
        for name in st["missing"][:6]:
            print("    %s" % name)
        print("  Re-run this with a working connection so they can be fetched.")
        print()
    if not st["pin_synced"]:
        print("  The pin could not be written to %s" % config_file())
        print("  %s" % (st["pin_error"] or "unknown reason"))
        print()
    if st["pin_synced"] and not st["missing"]:
        print("  The lock is in place. The update Steam now offers for this game")
        print("  is the downgrade, so running it is what puts you on the build")
        print("  that works.")
        print()
        print("  Do this now:")
        print("    1. Close Steam completely and open it again (the pin is read at")
        print("       startup).")
        if st["installed"]:
            print("    2. The game shows an Update in your library. Start it and let it")
            print("       finish.")
        else:
            print("    2. Install the game from your library. It installs the locked")
            print("       build straight away, so there is no second download.")
        print("    3. Wait until Steam lists it as fully installed, not Queued,")
        print("       Downloading or Updating.")
        print("    4. Run the verify command again.")
        print()
        print("  Your files are not deleted and this is not a reinstall, so only the")
        print("  parts that differ between the two builds are downloaded. The build's")
        print("  manifests are already on your PC, so nothing has to be requested")
        print("  from Steam for it.")
    print("=" * 62)


def main():
    if len(sys.argv) < 2 or not re.fullmatch(r"\d+", sys.argv[1] or ""):
        print("usage: tokeer lock <APPID>", file=sys.stderr)
        return 2
    appid = sys.argv[1]
    if not is_locked(appid):
        print("App %s is not version-locked, nothing to do." % appid)
        return 0
    st = apply(appid)
    print_status(st)
    if st["ready"]:
        print()
        print("Version lock is set and the game is on build %s." % (st["build_id"] or "?"))
        return 0
    print_next_steps(st)
    return 1


if __name__ == "__main__":
    sys.exit(main())
