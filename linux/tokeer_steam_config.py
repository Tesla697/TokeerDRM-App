#!/usr/bin/env python3
"""
Auto-configure Steam for a Tokeer Linux/Proton game - no manual Steam UI.

Given an appid + the launch-option string, this:
  1. sets the game's **Launch Options** in every user's localconfig.vdf,
  2. maps the game to the best Proton for it via config.vdf's CompatToolMapping -
     a per-game pick (see choose_proton), defaulting to GE-Proton 11-11
     (downloaded if missing). Steam installs the Valve Protons itself on first launch. It switches even
     when the game is already mapped to another Proton, because the Denuvo hook
     needs a modern Proton ABI and an old build fails in ways that look like a
     broken activation, and
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
import hashlib
import os
import re
import shutil
import sys
import tarfile
import time
import urllib.request


# --------------------------------------------------------------------------- #
# Steam discovery
# --------------------------------------------------------------------------- #
def steam_roots():
    cands = [
        "~/.steam/steam",
        "~/.local/share/Steam",
        "~/.steam/root",
        "~/.var/app/com.valvesoftware.Steam/.local/share/Steam",  # flatpak
        "~/.var/app/com.valvesoftware.Steam/data/Steam",  # older flatpak
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
# The problem this solves: one hardcoded Proton can't run every Denuvo title.
# Some launch on Experimental but not stable, some need a newer build than Valve
# ships, and the symptom is always the same from the user's side - the game just
# doesn't start, which reads as a broken activation.
#
# So instead of forcing one version we pick the best available per game:
#   1. a per-app override (PROTON_BY_APP) for titles we've pinned to a known-good
#      version - first entry that's actually installed wins,
#   2. otherwise GE-Proton 11-11, downloaded into compatibilitytools.d when
#      it isn't there yet,
#   3. and Proton Experimental if that download fails.
#
# Internal CompatToolMapping names: proton_experimental, proton_11, proton_9,
# proton_hotfix, ...; community tools use the key in their compatibilitytool.vdf
# (e.g. GE-Proton9-20). The Valve names below always "resolve" because Steam
# fetches them on first launch; community names must be detected on disk.
DEFAULT_PROTON = ["GE-Proton11-11-x86_64", "GE-Proton11-11"]
# Used when GE-Proton 11-11 isn't installed and the download fails.
FALLBACK_PROTON = "proton_experimental"

# Valve Protons Steam will install on demand, so they count as available without
# being on disk yet. Anything not here must be found in compatibilitytools.d.
_VALVE_PROTONS = {
    "proton_experimental", "proton_hotfix", "proton_11", "proton_10",
    "proton_9", "proton_8", "proton_7", "proton_63", "proton_513",
}

# Per-app Proton preference, highest first. Pin a game here when a specific
# version is the one that actually boots it. The confirmed-working titles are
# pinned to stable 11 (what they run on today) so smartening the default can't
# regress them; everything unlisted gets DEFAULT_PROTON (GE-Proton 11-11).
PROTON_BY_APP = {
    "1570010": ["proton_11"],   # FAR: Changing Tides (confirmed)
    "3321460": ["proton_11"],   # Crimson Desert (confirmed)
    # Experimental and 11.0-2 freeze the pre-rendered cutscenes (Proton #10327).
    # GE's own tarball registers as -x86_64; older installs may use the bare name.
    "4115450": ["GE-Proton11-11-x86_64", "GE-Proton11-11"],   # Phantom Blade Zero (confirmed)
}

GE_RELEASES = "https://github.com/GloriousEggroll/proton-ge-custom/releases/download"


def install_ge_proton(name, roots):
    """Download a GE-Proton build into compatibilitytools.d when a game is pinned
    to one that isn't there. Checked against the release's sha512 and unpacked
    beside the final folder first, so a cut-off download never leaves a half
    tool Steam would try to use. True once it's installed."""
    dest = os.path.join(roots[0], "compatibilitytools.d")
    tag = name[:-len("-x86_64")] if name.endswith("-x86_64") else name
    asset = "%s-x86_64.tar.gz" % tag
    base = "%s/%s/" % (GE_RELEASES, tag)
    tmp = os.path.join(dest, ".tokeer-" + asset)
    staging = os.path.join(dest, ".tokeer-" + name)
    try:
        os.makedirs(dest, exist_ok=True)
        req = urllib.request.Request(base + "%s-x86_64.sha512sum" % tag,
                                     headers={"User-Agent": "tokeer"})
        want = urllib.request.urlopen(req, timeout=30).read().decode().split()[0].lower()

        print("  compat tool: downloading %s (one time, a few hundred MB)..." % name)
        req = urllib.request.Request(base + asset, headers={"User-Agent": "tokeer"})
        h = hashlib.sha512()
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            got, shown = 0, -1
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
                got += len(chunk)
                pct = got * 100 // total if total else 0
                if total and pct // 10 != shown:
                    shown = pct // 10
                    print("    %d%%" % pct)
        if h.hexdigest() != want:
            print("  compat tool: %s download is corrupt (checksum mismatch)" % name)
            return False

        shutil.rmtree(staging, ignore_errors=True)
        with tarfile.open(tmp) as tar:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(staging, filter="data")
            else:
                tar.extractall(staging)
        inner = [os.path.join(staging, d) for d in os.listdir(staging)]
        if len(inner) != 1 or not os.path.isfile(os.path.join(inner[0], "compatibilitytool.vdf")):
            print("  compat tool: %s archive isn't laid out as expected" % name)
            return False
        final = os.path.join(dest, os.path.basename(inner[0]))
        shutil.rmtree(final, ignore_errors=True)
        os.replace(inner[0], final)
        print("  compat tool: installed %s -> %s" % (name, final))
        return name in community_tools(roots)
    except Exception as e:
        print("  compat tool: couldn't install %s: %s" % (name, e))
        return False
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        try:
            os.remove(tmp)
        except OSError:
            pass


def community_tools(roots):
    """Internal names of the GE-style compat tools installed under
    compatibilitytools.d (both the per-user dirs and each Steam root). The name
    Steam maps on is the first key inside compatibilitytool.vdf, which is also
    (for GE) the folder name, so we read the vdf and fall back to the folder."""
    names = set()
    dirs = [os.path.expanduser("~/.steam/root/compatibilitytools.d"),
            os.path.expanduser("~/.local/share/Steam/compatibilitytools.d")]
    dirs += [os.path.join(r, "compatibilitytools.d") for r in roots]
    seen = set()
    for d in dirs:
        if d in seen or not os.path.isdir(d):
            continue
        seen.add(d)
        for entry in os.listdir(d):
            sub = os.path.join(d, entry)
            if not os.path.isdir(sub):
                continue
            vdf = os.path.join(sub, "compatibilitytool.vdf")
            got = None
            try:
                txt = open(vdf, "r", encoding="utf-8", errors="replace").read()
                m = re.search(r'"compatibilitytools"\s*\{.*?"compat_tools"\s*\{\s*"([^"]+)"',
                              txt, re.DOTALL)
                if m:
                    got = m.group(1)
            except OSError:
                pass
            names.add(got or entry)
    return names


def choose_proton(appid, roots):
    """The compat-tool name to map this appid to. Never returns something that
    isn't (or won't be) installed: Valve Protons always qualify, community ones
    only when present (GE ones get downloaded). Falls back to FALLBACK_PROTON."""
    community = community_tools(roots)

    def usable(name):
        return name in _VALVE_PROTONS or name in community

    wants = PROTON_BY_APP.get(str(appid)) or DEFAULT_PROTON
    for want in wants:
        if usable(want):
            return want
    ge = next((w for w in wants if w.startswith("GE-Proton")), None)
    if ge and install_ge_proton(ge, roots):
        return ge
    print("  compat tool: app %s wants %s but none is installed, using %s"
          % (appid, " / ".join(wants), FALLBACK_PROTON))
    return FALLBACK_PROTON


def _name_value_in(text, open_i, close_i):
    """The name/value match inside a CompatToolMapping entry, or None."""
    return re.search(r'("name"\s*")([^"]*)(")', text[open_i:close_i])


def ensure_compat_tool(path, appid, want=None):
    """Point appid at stable Proton in config.vdf's CompatToolMapping, switching
    it even when it is already mapped to a different Proton. True if changed.

    Forcing the switch is deliberate: the Denuvo hook is built against the modern
    Proton unixlib ABI, and a game left on Proton 7 or an old GE fails in ways
    that look like a broken activation rather than a wrong Proton."""
    proton = want or FALLBACK_PROTON

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

    proton = choose_proton(appid, roots)
    print(f"  compat tool: app {appid} -> {proton}")

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
            # The per-game override AND the global default go on the SAME
            # Proton. Steam stores the "Default
            # compatibility tool" (Steam Play for all other titles) as appid 0
            # in the very same CompatToolMapping. If that default disagrees with
            # the per-game override, Steam quietly falls back to it on the next
            # launch and after a reboot, dropping the game onto the wrong Proton
            # so the activation looks like it vanished. Set both to match.
            try:
                ensure_compat_tool(cfg, appid, want=proton)
                ensure_compat_tool(cfg, "0", want=proton)
            except Exception as e:
                print(f"  WARN could not set compat tool in {cfg}: {e}", file=sys.stderr)
                print(f"  Set it by hand: Steam -> Settings -> Compatibility ->"
                      f" Default compatibility tool -> {proton}, and the same"
                      f" under the game's Properties -> Compatibility.", file=sys.stderr)

    if changed == 0:
        for r in reasons:
            print(f"  - {r}", file=sys.stderr)
        manual_fallback(f"no writable localconfig.vdf matched app {appid}")
    print(f"OK: launch options written for {changed} Steam user profile(s).")


if __name__ == "__main__":
    main()
