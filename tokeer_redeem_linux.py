#!/usr/bin/env python3
"""
Tokeer DRM redeemer - Linux / Proton edition.

The Windows TokeerDRM app can't run on Linux, so this is the headless
equivalent of its ONE essential job: redeem a 6-digit code and apply the
Denuvo tickets so the game authorizes.

On Windows the app writes AppTicket + ETicket to HKCU\\Software\\Valve\\Steam\\
Apps\\<appid>. Under Proton, a game's HKCU IS its per-appid Wine prefix
(steamapps/compatdata/<appid>/pfx), so we write the exact same values there,
then launch the game through Steam.

Flow:
    code  ->  POST {SERVER}/drm/redeem  ->  {app_id, appticket, eticket}
          ->  wine reg import  into  compatdata/<app_id>/pfx
          ->  steam://rungameid/<app_id>

Pure stdlib (works with the Steam Deck's system python). No pip installs.

Usage:
    python3 tokeer_redeem_linux.py ABC123
    python3 tokeer_redeem_linux.py ABC123 --appid 1570010 --no-launch
    python3 tokeer_redeem_linux.py ABC123 --wine /path/to/proton/files/bin/wine

Server URL resolution (first hit wins):
    --server flag  >  $TOKEER_DRM_SERVER  >  server_config.py (SERVER_URL)
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def resolve_server(cli_value: str | None) -> str:
    if cli_value:
        return cli_value.rstrip("/")
    env = os.environ.get("TOKEER_DRM_SERVER")
    if env:
        return env.rstrip("/")
    # Same gitignored config the Windows app reads, if it's alongside us.
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        from server_config import SERVER_URL  # type: ignore
        return SERVER_URL.rstrip("/")
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str, code: int = 1):
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def server_redeem(server: str, code: str) -> dict:
    """POST /drm/redeem {code} -> parsed json (raises on transport error)."""
    body = json.dumps({"code": code}).encode("utf-8")
    req = urllib.request.Request(
        server + "/drm/redeem",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        status = e.code
    except Exception as e:
        die(f"server unreachable at {server}: {e}")
    try:
        data = json.loads(raw)
    except Exception:
        data = {"reason": raw[:300]}
    if status != 200 or not data.get("success"):
        die(data.get("reason") or data.get("error") or f"server error {status}")
    return data


# --------------------------------------------------------------------------- #
# Steam / Proton discovery
# --------------------------------------------------------------------------- #
def find_steam_root(override: str | None) -> str:
    if override:
        if os.path.isdir(os.path.join(override, "steamapps")):
            return override
        die(f"--steam-root has no steamapps/: {override}")
    candidates = [
        os.path.expanduser("~/.steam/steam"),
        os.path.expanduser("~/.local/share/Steam"),
        os.path.expanduser("~/.steam/root"),
        os.path.expanduser("~/.var/app/com.valvesoftware.Steam/data/Steam"),  # flatpak
    ]
    for c in candidates:
        if os.path.isdir(os.path.join(c, "steamapps")):
            return c
    die("could not find a Steam install. Pass --steam-root /path/to/Steam")


def _library_roots(steam_root: str) -> list[str]:
    """All Steam library roots (games often live on a second drive). Reads
    libraryfolders.vdf; always includes steam_root itself."""
    roots = [steam_root]
    for vdf in (
        os.path.join(steam_root, "steamapps", "libraryfolders.vdf"),
        os.path.join(steam_root, "config", "libraryfolders.vdf"),
    ):
        try:
            with open(vdf, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        # entries look like:  "path"   "/mnt/games/SteamLibrary"
        import re
        for m in re.finditer(r'"path"\s*"([^"]+)"', text):
            p = m.group(1).replace("\\\\", "/")
            if os.path.isdir(os.path.join(p, "steamapps")) and p not in roots:
                roots.append(p)
    return roots


def find_prefix(steam_root: str, app_id: str) -> str:
    tried = []
    for root in _library_roots(steam_root):
        pfx = os.path.join(root, "steamapps", "compatdata", app_id, "pfx")
        tried.append(pfx)
        if os.path.isdir(pfx):
            return pfx
    die(
        f"no Proton prefix for app {app_id}. Looked in:\n  " + "\n  ".join(tried) + "\n"
        "  -> Launch the game once under Proton first (it will fail Denuvo, but that\n"
        "     creates the prefix), then run this redeemer."
    )


def _version_key(path: str):
    # crude "newest GE/Proton" sort by digits in the folder name
    name = path.lower()
    nums = "".join(ch if ch.isdigit() else " " for ch in name).split()
    return (("ge-proton" in name), [int(n) for n in nums[:4]] if nums else [0])


def find_wine(steam_root: str, override: str | None) -> str:
    if override:
        if os.path.isfile(override) and os.access(override, os.X_OK):
            return override
        die(f"--wine is not an executable: {override}")
    # Prefer Proton (esp. GE) since that's what runs the games.
    patterns = [
        os.path.join(steam_root, "compatibilitytools.d", "*", "files", "bin", "wine"),
        os.path.expanduser("~/.steam/root/compatibilitytools.d/*/files/bin/wine"),
        os.path.join(steam_root, "steamapps", "common", "Proton*", "files", "bin", "wine"),
    ]
    found = []
    for p in patterns:
        found.extend(glob.glob(p))
    found = [f for f in found if os.access(f, os.X_OK)]
    if found:
        found.sort(key=_version_key, reverse=True)
        return found[0]
    sysw = shutil.which("wine")
    if sysw:
        return sysw
    die("no wine/proton found. Pass --wine /path/to/.../files/bin/wine")


# --------------------------------------------------------------------------- #
# Registry write via `wine reg import`
# --------------------------------------------------------------------------- #
def build_reg_file(app_id: str, appticket_hex: str, eticket_hex: str) -> str:
    """Return a .reg file body (UTF-8) writing both tickets as REG_BINARY."""
    def as_hex(h: str) -> str:
        h = h.strip().replace(" ", "").replace("\n", "")
        pairs = [h[i:i + 2].lower() for i in range(0, len(h), 2)]
        # wrap ~25 bytes/line with backslash continuations (standard .reg style)
        out, line = [], []
        for i, byte in enumerate(pairs):
            line.append(byte)
            if len(line) == 25:
                out.append(",".join(line))
                line = []
        if line:
            out.append(",".join(line))
        return ",\\\n  ".join(out)

    key = f"HKEY_CURRENT_USER\\Software\\Valve\\Steam\\Apps\\{app_id}"
    return (
        "Windows Registry Editor Version 5.00\n\n"
        f"[{key}]\n"
        f'"AppTicket"=hex:{as_hex(appticket_hex)}\n'
        f'"ETicket"=hex:{as_hex(eticket_hex)}\n'
    )


def write_prefix_registry(wine: str, prefix: str, reg_body: str, dry_run: bool) -> None:
    tmp = tempfile.NamedTemporaryFile("w", suffix=".reg", delete=False, encoding="utf-8")
    tmp.write(reg_body)
    tmp.close()
    env = dict(os.environ)
    env["WINEPREFIX"] = prefix
    env["WINEDEBUG"] = "-all"
    env["WINEDLLOVERRIDES"] = "mscoree,mshtml="  # no mono/gecko popups
    cmd = [wine, "reg", "import", tmp.name]
    log(f"  wine:   {wine}")
    log(f"  prefix: {prefix}")
    log(f"  cmd:    WINEPREFIX=<prefix> {os.path.basename(wine)} reg import <tmp.reg>")
    if dry_run:
        log("  (dry-run) not executing; .reg left at: " + tmp.name)
        return
    try:
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
    finally:
        pass
    if r.returncode != 0:
        # keep the temp file for debugging
        die(
            "wine reg import failed (rc={}).\n  stdout: {}\n  stderr: {}\n  .reg kept at: {}".format(
                r.returncode, r.stdout.strip(), r.stderr.strip(), tmp.name
            )
        )
    os.unlink(tmp.name)
    log("  registry written OK")


def launch_game(app_id: str) -> None:
    url = f"steam://rungameid/{app_id}"
    launcher = shutil.which("steam") or shutil.which("xdg-open")
    if not launcher:
        log(f"  (no `steam`/`xdg-open` found) launch manually: {url}")
        return
    try:
        subprocess.Popen([launcher, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log(f"  launching: {url}")
    except Exception as e:
        log(f"  could not launch ({e}); run manually: {url}")


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="Tokeer DRM redeemer (Linux/Proton).")
    ap.add_argument("code", help="6-character redeem code")
    ap.add_argument("--appid", help="override app id (else taken from server response)")
    ap.add_argument("--server", help="backend base URL (default: env or server_config.py)")
    ap.add_argument("--steam-root", help="Steam install dir (auto-detected if omitted)")
    ap.add_argument("--wine", help="path to a wine binary (auto-detects Proton if omitted)")
    ap.add_argument("--no-launch", action="store_true", help="write the ticket but don't start the game")
    ap.add_argument("--dry-run", action="store_true", help="fetch + build .reg but don't write or launch")
    args = ap.parse_args()

    code = args.code.strip().upper()
    if len(code) != 6:
        die("code must be exactly 6 characters")

    server = resolve_server(args.server)
    if not server or "your-server" in server:
        die("no server configured. Use --server, $TOKEER_DRM_SERVER, or server_config.py")

    log(f"[1/4] redeeming {code} at {server} ...")
    data = server_redeem(server, code)
    app_id = str(args.appid or data.get("app_id") or "").strip()
    appticket = (data.get("appticket") or "").strip()
    eticket = (data.get("eticket") or "").strip()
    if not (app_id and appticket and eticket):
        die("server returned an incomplete ticket (missing app_id/appticket/eticket)")
    uses = data.get("uses_remaining")
    log(f"      ok - app_id={app_id}, appticket={len(appticket)//2}B, eticket={len(eticket)//2}B"
        + (f", uses_remaining={uses}" if uses is not None else ""))

    log("[2/4] locating Steam + Proton prefix ...")
    steam_root = find_steam_root(args.steam_root)
    prefix = find_prefix(steam_root, app_id)
    wine = find_wine(steam_root, args.wine)

    log("[3/4] writing tickets into the game's Proton prefix ...")
    reg_body = build_reg_file(app_id, appticket, eticket)
    write_prefix_registry(wine, prefix, reg_body, args.dry_run)

    if args.no_launch or args.dry_run:
        log("[4/4] done (launch skipped).")
        log(f"      Start the game yourself: steam://rungameid/{app_id}")
        return
    log("[4/4] launching the game ...")
    launch_game(app_id)
    log("Done. If the game still shows a Denuvo/012/005 error, the game-side OST")
    log("hook likely isn't applying the ticket under Proton - tell me and we go deeper.")


if __name__ == "__main__":
    main()
