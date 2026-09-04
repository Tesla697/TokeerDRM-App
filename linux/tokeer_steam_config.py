#!/usr/bin/env python3
"""
Auto-configure Steam for a Tokeer Linux/Proton game - no manual Steam UI.

Given an appid + the launch-option string, this:
  1. sets the game's **Launch Options** in every user's localconfig.vdf,
  2. forces the game onto GE-Proton10-34 via config.vdf's CompatToolMapping,
     downloading it into compatibilitytools.d first if it isn't installed. It
     switches even when the game is already mapped to another Proton, because
     the Denuvo hook needs the modern Proton ABI and an old build fails in ways
     that look like a broken activation, and
  3. points Steam's **Default compatibility tool** (appid 0 in the same mapping)
     at that same Proton. If the default disagrees with the per-game override,
     Steam falls back to the default on the next launch / after a reboot and the
     activation looks like it vanished.

Pure stdlib so it runs on the Steam Deck's system python. Steam MUST be closed
while this runs (it rewrites these files on exit and would clobber our edits) -
the install script handles shutting it down and relaunching.

Usage:
    python3 tokeer_steam_config.py <appid> "<launch options>"
    python3 tokeer_steam_config.py 1570010 'WINEDLLOVERRIDES="dinput8=n,b" /home/u/.tokeer/ost-run.sh %command%'

Exit 0 on success (at least one file edited), 2 if nothing could be written.
"""

import glob
import os
import re
import shutil
import sys
import time


# --------------------------------------------------------------------------- #
# Steam discovery
# --------------------------------------------------------------------------- #
def steam_roots():
    cands = [
        "~/.steam/steam",
        "~/.local/share/Steam",
        "~/.steam/root",
        "~/.var/app/com.valvesoftware.Steam/data/Steam",  # flatpak
    ]
    out = []
    for c in cands:
        p = os.path.expanduser(c)
        if os.path.isdir(os.path.join(p, "steamapps")) or os.path.isdir(os.path.join(p, "userdata")):
            if p not in out:
                out.append(p)
    return out


# --------------------------------------------------------------------------- #
# Tiny VDF surgery (format-preserving, no external deps)
# --------------------------------------------------------------------------- #
def _match_brace(text, open_idx):
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\":
                    i += 1
                i += 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _find_key_block(text, key, lo=0, hi=None):
    """Find "key" (case-insensitive) followed by a { } block within [lo, hi).
    Returns (key_start, brace_open, brace_close) or None."""
    if hi is None:
        hi = len(text)
    pat = re.compile(r'"' + re.escape(key) + r'"', re.IGNORECASE)
    for m in pat.finditer(text, lo, hi):
        j = m.end()
        while j < hi and text[j] in " \t\r\n":
            j += 1
        if j < hi and text[j] == "{":
            close = _match_brace(text, j)
            if close != -1:
                return (m.start(), j, close)
    return None


def _navigate(text, path, lo=0, hi=None):
    """Walk a chain of nested keys, each a { } block. Returns the inner
    (open, close) of the last key, or None if any hop is missing."""
    if hi is None:
        hi = len(text)
    cur_lo, cur_hi = lo, hi
    open_i = close_i = None
    for key in path:
        found = _find_key_block(text, key, cur_lo, cur_hi)
        if not found:
            return None
        _, open_i, close_i = found
        cur_lo, cur_hi = open_i + 1, close_i
    return (open_i, close_i)


def _vdf_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _indent_at(text, idx):
    """Leading whitespace of the line containing idx (for tidy inserts)."""
    line_start = text.rfind("\n", 0, idx) + 1
    ws = []
    i = line_start
    while i < len(text) and text[i] in " \t":
        ws.append(text[i])
        i += 1
    return "".join(ws)


def set_launch_options(path, appid, opts):
    """Set "LaunchOptions" for appid inside Software/Valve/Steam/apps. Creates the
    apps block (and the app's own block) if Steam wrote the config but hasn't
    listed this app yet. Returns True if the file was changed, else a string
    reason for the diagnostics."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    esc = _vdf_escape(opts)
    apps = _navigate(text, ["Software", "Valve", "Steam", "apps"])
    if not apps:
        apps = _navigate(text, ["Software", "Valve", "Steam", "Apps"])
    if not apps:
        # Steam config exists but has no apps block yet - create one under the
        # Steam block so we can still write the launch option.
        steam = (_navigate(text, ["Software", "Valve", "Steam"])
                 or _navigate(text, ["Software", "Valve", "steam"]))
        if not steam:
            return "no Software/Valve/Steam block in this localconfig.vdf"
        s_open, _s_close = steam
        ind = _indent_at(text, s_open) + "\t"
        block = "\n" + ind + '"apps"' + "\n" + ind + "{" + "\n" + ind + "}"
        text = text[: s_open + 1] + block + text[s_open + 1:]
        apps = _navigate(text, ["Software", "Valve", "Steam", "apps"])
        if not apps:
            return "failed to create apps block"
    apps_open, apps_close = apps

    app = _find_key_block(text, str(appid), apps_open + 1, apps_close)
    if app:
        _, a_open, a_close = app
        lo = _find_key_block(text, "LaunchOptions", a_open + 1, a_close)
        m = re.search(r'"LaunchOptions"\s*"((?:[^"\\]|\\.)*)"',
                      text[a_open:a_close], re.IGNORECASE)
        if m:
            s = a_open + m.start()
            e = a_open + m.end()
            new = text[:s] + '"LaunchOptions"\t\t"' + esc + '"' + text[e:]
        else:
            ind = _indent_at(text, a_open) + "\t"
            ins = "\n" + ind + '"LaunchOptions"\t\t"' + esc + '"'
            new = text[: a_open + 1] + ins + text[a_open + 1:]
    else:
        ind = _indent_at(text, apps_open) + "\t"
        block = (
            "\n" + ind + '"' + str(appid) + '"'
            + "\n" + ind + "{"
            + "\n" + ind + "\t" + '"LaunchOptions"\t\t"' + esc + '"'
            + "\n" + ind + "}"
        )
        new = text[: apps_open + 1] + block + text[apps_open + 1:]

    if new == text:
        return "no change (launch option already set?)"
    shutil.copy2(path, path + ".tokeerbak")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new)
    return True


# --------------------------------------------------------------------------- #
# Proton (compat tool) auto-select
# --------------------------------------------------------------------------- #
TARGET_PROTON = "GE-Proton10-34"
_GE_URL = ("https://github.com/GloriousEggroll/proton-ge-custom/releases/download/"
           "{n}/{n}.tar.gz")
_GE_SUM = ("https://github.com/GloriousEggroll/proton-ge-custom/releases/download/"
           "{n}/{n}.sha512sum")


def _proton_version_key(name):
    n = name.lower()
    nums = [int(x) for x in re.findall(r"\d+", n)] or [0]
    return (("ge-proton" in n or "cachyos" in n), nums)


def compat_dirs(root):
    """Every compatibilitytools.d worth looking in, most canonical first."""
    out = []
    for d in (os.path.join(root, "compatibilitytools.d"),
              os.path.expanduser("~/.steam/root/compatibilitytools.d"),
              os.path.expanduser("~/.local/share/Steam/compatibilitytools.d")):
        if d not in out:
            out.append(d)
    return out


def find_proton(root, name):
    """Install path of a compat tool by folder name, or None. A folder only counts
    when it actually holds a build, so a half-extracted leftover from an
    interrupted download doesn't read as installed."""
    for d in compat_dirs(root):
        p = os.path.join(d, name)
        if os.path.isdir(os.path.join(p, "files")) or os.path.isdir(os.path.join(p, "dist")):
            return p
    return None


def newest_proton(root):
    """Best-guess newest installed Proton tool folder name, or None."""
    names = []
    for pat in (
        os.path.join(root, "compatibilitytools.d", "*"),
        os.path.expanduser("~/.steam/root/compatibilitytools.d/*"),
        os.path.join(root, "steamapps", "common", "Proton*"),
    ):
        for p in glob.glob(pat):
            if os.path.isdir(p):
                names.append(os.path.basename(p))
    if not names:
        return None
    names.sort(key=_proton_version_key, reverse=True)
    # Steam's internal key for bundled Protons is "proton_<ver>"; custom tools in
    # compatibilitytools.d are keyed by their folder name verbatim.
    return names[0]


def _fetch(url, dest=None, label=""):
    """Download to `dest`, or return the bytes when dest is None. Prints coarse
    progress so a 400 MB Proton doesn't look like a hang. None on any failure."""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Tokeer"})
        with urllib.request.urlopen(req, timeout=60) as r:
            if dest is None:
                return r.read()
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            step = 10
            with open(dest, "wb") as fh:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
                    got += len(chunk)
                    if total:
                        pct = got * 100 // total
                        if pct >= step:
                            print("    %s %d%% (%d/%d MiB)" % (label, pct, got >> 20, total >> 20))
                            step = pct - (pct % 10) + 10
            return True
    except Exception as e:
        print("    download failed: %s" % e, file=sys.stderr)
        return None


def _safe_extract(tar_path, into):
    """Unpack a Proton tarball, refusing any member that would escape `into`.
    tarfile will happily write ../ paths otherwise."""
    import tarfile
    into_abs = os.path.abspath(into)
    with tarfile.open(tar_path, "r:gz") as tf:
        for m in tf.getmembers():
            target = os.path.abspath(os.path.join(into_abs, m.name))
            if not (target == into_abs or target.startswith(into_abs + os.sep)):
                raise RuntimeError("refusing unsafe path in tarball: %s" % m.name)
            if m.issym() or m.islnk():
                link = os.path.abspath(os.path.join(os.path.dirname(target), m.linkname))
                if not (link == into_abs or link.startswith(into_abs + os.sep)):
                    raise RuntimeError("refusing unsafe link in tarball: %s" % m.name)
        tf.extractall(into)


def install_proton(root, name=TARGET_PROTON):
    """Download and unpack a GE-Proton build into compatibilitytools.d.

    Returns its path, or None if anything went wrong. Callers treat None as "keep
    whatever Proton the user already has" rather than an error, since a failed
    download shouldn't block the rest of setup."""
    dest_dir = compat_dirs(root)[0]
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as e:
        print("  could not create %s: %s" % (dest_dir, e), file=sys.stderr)
        return None

    tmp = os.path.join(dest_dir, "." + name + ".tar.gz")
    print("  downloading %s (about 400 MB, one time)..." % name)
    if not _fetch(_GE_URL.format(n=name), tmp, label=name):
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None

    # GE publishes a sha512sum beside the tarball. Verify whenever we can reach
    # it: this archive gets executed, so a corrupted or swapped download matters.
    want = _fetch(_GE_SUM.format(n=name))
    if want:
        import hashlib
        h = hashlib.sha512()
        with open(tmp, "rb") as fh:
            for blk in iter(lambda: fh.read(1 << 20), b""):
                h.update(blk)
        expect = want.decode("utf-8", "replace").split()[0].strip().lower()
        if h.hexdigest() != expect:
            print("  checksum mismatch, discarding the download", file=sys.stderr)
            try:
                os.remove(tmp)
            except OSError:
                pass
            return None
        print("  checksum ok")

    print("  unpacking...")
    try:
        _safe_extract(tmp, dest_dir)
    except Exception as e:
        print("  could not unpack %s: %s" % (name, e), file=sys.stderr)
        return None
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    got = find_proton(root, name)
    if got:
        print("  installed %s" % name)
    else:
        print("  %s did not unpack into the expected folder" % name, file=sys.stderr)
    return got


def ensure_proton_installed(root, name=TARGET_PROTON):
    """Path to `name`, installing it if it isn't there yet. None if unavailable."""
    have = find_proton(root, name)
    if have:
        print("  %s already installed" % name)
        return have
    return install_proton(root, name)


def _name_value_in(text, open_i, close_i):
    """The name/value match inside a CompatToolMapping entry, or None."""
    return re.search(r'("name"\s*")([^"]*)(")', text[open_i:close_i])


def ensure_compat_tool(path, appid, want=None):
    """Point appid at GE-Proton10-34 in config.vdf's CompatToolMapping, switching
    it even when it is already mapped to a different Proton. True if changed.

    Forcing the switch is deliberate: the Denuvo hook is built against the modern
    Proton unixlib ABI, and a game left on Proton 7 or an old GE fails in ways
    that look like a broken activation rather than a wrong Proton."""
    root = os.path.dirname(os.path.dirname(path))  # <root>/config/config.vdf -> <root>
    if want:
        proton = want
    else:
        got = ensure_proton_installed(root)
        proton = os.path.basename(got) if got else newest_proton(root)
    if not proton:
        return False

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    ctm = _navigate(text, ["InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping"])
    if not ctm:
        # Older layouts omit CompatToolMapping entirely; leave it to Steam's UI
        # rather than risk corrupting config.vdf by fabricating the whole path.
        return False
    c_open, c_close = ctm

    existing = _find_key_block(text, str(appid), c_open + 1, c_close)
    if existing:
        _, e_open, e_close = existing
        m = _name_value_in(text, e_open, e_close)
        if not m:
            return False
        current = m.group(2)
        if current == proton:
            print("  compat tool: app %s already on %s" % (appid, proton))
            return False
        lo, hi = e_open + m.start(2), e_open + m.end(2)
        new = text[:lo] + _vdf_escape(proton) + text[hi:]
        shutil.copy2(path, path + ".tokeerbak")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new)
        print("  compat tool: app %s switched %s -> %s" % (appid, current or "(none)", proton))
        return True

    ind = _indent_at(text, c_open) + "\t"
    q = '"'
    block = (
        "\n" + ind + q + str(appid) + q
        + "\n" + ind + "{"
        + "\n" + ind + "\t" + q + "name" + q + "\t\t" + q + _vdf_escape(proton) + q
        + "\n" + ind + "\t" + q + "config" + q + "\t\t" + q + q
        + "\n" + ind + "\t" + q + "priority" + q + "\t\t" + q + "250" + q
        + "\n" + ind + "}"
    )
    new = text[: c_open + 1] + block + text[c_open + 1:]
    if new == text:
        return False
    shutil.copy2(path, path + ".tokeerbak")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new)
    print("  compat tool: mapped app %s -> %s" % (appid, proton))
    return True


# --------------------------------------------------------------------------- #
def main():
    if len(sys.argv) < 3:
        print("usage: tokeer_steam_config.py <appid> \"<launch options>\"", file=sys.stderr)
        sys.exit(2)
    appid = re.sub(r"\D", "", sys.argv[1])
    opts = sys.argv[2]
    if not appid:
        print("ERROR: appid must be numeric", file=sys.stderr)
        sys.exit(2)

    def manual_fallback(reason):
        # Never abort the installer over this - print exactly what to set by hand
        # and exit 0 so the rest of setup (prefix creation etc.) still runs.
        print(f"  WARN: couldn't auto-set the launch option ({reason}).", file=sys.stderr)
        print("  Set it yourself: Steam -> right-click the game -> Properties ->", file=sys.stderr)
        print("  Launch Options, and paste EXACTLY:", file=sys.stderr)
        print(f"\n    {opts}\n", file=sys.stderr)
        sys.exit(0)

    roots = steam_roots()
    if not roots:
        manual_fallback("no Steam install auto-detected")

    lc_files = []
    for root in roots:
        lc_files += glob.glob(os.path.join(root, "userdata", "*", "config", "localconfig.vdf"))
    print(f"  steam roots: {roots}")
    print(f"  localconfig.vdf found: {len(lc_files)}")

    changed = 0
    reasons = []
    for lc in lc_files:
        try:
            res = set_launch_options(lc, appid, opts)
            if res is True:
                changed += 1
                print(f"  launch options set in {lc}")
            else:
                reasons.append(f"{lc}: {res}")
        except Exception as e:
            reasons.append(f"{lc}: {e}")
    for root in roots:
        cfg = os.path.join(root, "config", "config.vdf")
        if os.path.isfile(cfg):
            # Resolve the Proton once so the per-game override AND the global
            # default land on the SAME build. Steam stores the "Default
            # compatibility tool" (Steam Play for all other titles) as appid 0
            # in the very same CompatToolMapping. If that default disagrees with
            # the per-game override, Steam quietly falls back to it on the next
            # launch and after a reboot, dropping the game onto the wrong Proton
            # so the activation looks like it vanished. Set both to match.
            got = ensure_proton_installed(root)
            proton = os.path.basename(got) if got else newest_proton(root)
            try:
                ensure_compat_tool(cfg, appid, want=proton)
                ensure_compat_tool(cfg, "0", want=proton)
            except Exception as e:
                print(f"  WARN could not set compat tool in {cfg}: {e}", file=sys.stderr)
                print(f"  Set it by hand: Steam -> Settings -> Compatibility ->"
                      f" Default compatibility tool -> {TARGET_PROTON}, and the same"
                      f" under the game's Properties -> Compatibility.", file=sys.stderr)

    if changed == 0:
        for r in reasons:
            print(f"  - {r}", file=sys.stderr)
        manual_fallback(f"no writable localconfig.vdf matched app {appid}")
    print(f"OK: launch options written for {changed} Steam user profile(s).")


if __name__ == "__main__":
    main()
