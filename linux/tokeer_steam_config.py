#!/usr/bin/env python3
"""
Auto-configure Steam for a Tokeer Linux/Proton game - no manual Steam UI.

Given an appid + the launch-option string, this:
  1. sets the game's **Launch Options** in every user's localconfig.vdf, and
  2. if the game has no compatibility tool yet, forces the newest installed
     Proton via config.vdf's CompatToolMapping (so a Windows-only Denuvo game
     actually runs under Proton).

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
    """Set "LaunchOptions" for appid inside Software/Valve/Steam/apps. Returns
    True if the file was changed."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    apps = _navigate(text, ["Software", "Valve", "Steam", "apps"])
    if not apps:
        apps = _navigate(text, ["Software", "Valve", "Steam", "Apps"])
    if not apps:
        return False
    apps_open, apps_close = apps

    esc = _vdf_escape(opts)
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
        return False
    shutil.copy2(path, path + ".tokeerbak")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new)
    return True


# --------------------------------------------------------------------------- #
# Proton (compat tool) auto-select
# --------------------------------------------------------------------------- #
def _proton_version_key(name):
    n = name.lower()
    nums = [int(x) for x in re.findall(r"\d+", n)] or [0]
    return (("ge-proton" in n or "cachyos" in n), nums)


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


def ensure_compat_tool(path, appid):
    """If config.vdf has no CompatToolMapping for appid, point it at the newest
    installed Proton. Returns True if changed."""
    root = os.path.dirname(os.path.dirname(path))  # <root>/config/config.vdf -> <root>
    proton = newest_proton(root)
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
    if _find_key_block(text, str(appid), c_open + 1, c_close):
        return False  # already mapped - respect the user's choice

    ind = _indent_at(text, c_open) + "\t"
    block = (
        "\n" + ind + '"' + str(appid) + '"'
        + "\n" + ind + "{"
        + "\n" + ind + "\t" + '"name"\t\t"' + _vdf_escape(proton) + '"'
        + "\n" + ind + "\t" + '"config"\t\t""'
        + "\n" + ind + "\t" + '"priority"\t\t"250"'
        + "\n" + ind + "}"
    )
    new = text[: c_open + 1] + block + text[c_open + 1:]
    if new == text:
        return False
    shutil.copy2(path, path + ".tokeerbak")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new)
    print(f"  compat tool: mapped app {appid} -> {proton}")
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

    roots = steam_roots()
    if not roots:
        print("ERROR: no Steam install found", file=sys.stderr)
        sys.exit(2)

    changed = 0
    for root in roots:
        for lc in glob.glob(os.path.join(root, "userdata", "*", "config", "localconfig.vdf")):
            try:
                if set_launch_options(lc, appid, opts):
                    changed += 1
                    print(f"  launch options set in {lc}")
            except Exception as e:
                print(f"  WARN could not edit {lc}: {e}", file=sys.stderr)
        cfg = os.path.join(root, "config", "config.vdf")
        if os.path.isfile(cfg):
            try:
                ensure_compat_tool(cfg, appid)
            except Exception as e:
                print(f"  WARN could not set compat tool in {cfg}: {e}", file=sys.stderr)

    if changed == 0:
        print(
            "ERROR: could not write launch options for app "
            f"{appid} (no user localconfig.vdf matched). Is this the right appid?",
            file=sys.stderr,
        )
        sys.exit(2)
    print(f"OK: launch options written for {changed} Steam user profile(s).")


if __name__ == "__main__":
    main()
