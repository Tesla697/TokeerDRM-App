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
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import urllib.error


# RE Engine + Denuvo titles that crash on new-game without REFramework's
# dinput8.dll in the game folder. On Linux the redeemer drops it in for them (the
# install script's launch option already sets WINEDLLOVERRIDES="dinput8=n,b" so
# Proton loads it), so the user never has to place it by hand.
REFRAMEWORK_DINPUT8_GAMES = {"2246340", "2852190", "3764200", "3357650",
                             "2054970", "3500390", "2638890"}
REFRAMEWORK_DINPUT8_URL = "https://github.com/madoiscool/lt_api_links/releases/download/reframework/dinput8.dll"
REFRAMEWORK_DINPUT8_SHA256 = "1dfe37dede668b5a969833ddfb2bbc933c83074e86fb03d46015690adf8a29f3"


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


# Some machines (often ISP or router DNS that filters .xyz, or a stale negative
# cache) can't resolve our host even though the rest of the internet works. When
# that happens we fall back to DNS-over-HTTPS: we reach Cloudflare/Google by their
# literal IPs (no system DNS needed), look the host up ourselves, and pin the
# answer so the normal request goes through with the correct TLS name.
_DOH_ENDPOINTS = (
    "https://1.1.1.1/dns-query",
    "https://1.0.0.1/dns-query",
    "https://8.8.8.8/resolve",
)


def _is_name_resolution_error(exc: Exception) -> bool:
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, socket.gaierror):
        return True
    text = str(reason).lower()
    return "name or service not known" in text or "temporary failure in name" in text \
        or "nodename nor servname" in text or "getaddrinfo failed" in text


def _doh_resolve(host: str) -> list[str]:
    """Resolve host -> [ipv4...] via DoH, contacting the resolver by literal IP."""
    ips: list[str] = []
    for base in _DOH_ENDPOINTS:
        try:
            url = base + "?" + urllib.parse.urlencode({"name": host, "type": "A"})
            req = urllib.request.Request(url, headers={
                "Accept": "application/dns-json",
                "User-Agent": "TokeerRedeem",
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            for ans in data.get("Answer", []):
                if ans.get("type") == 1 and ans.get("data"):
                    ips.append(ans["data"])
            if ips:
                return ips
        except Exception:
            continue
    return ips


_dns_pins: dict[str, str] = {}
_orig_getaddrinfo = socket.getaddrinfo


def _pinned_getaddrinfo(host, *args, **kwargs):
    pinned = _dns_pins.get(host)
    if pinned:
        return _orig_getaddrinfo(pinned, *args, **kwargs)
    return _orig_getaddrinfo(host, *args, **kwargs)


def _install_dns_fallback(host: str) -> bool:
    """Pin host to a DoH-resolved IP so subsequent connections skip system DNS.
    TLS/SNI still uses the real host name, so certificate validation is unaffected."""
    ips = _doh_resolve(host)
    if not ips:
        return False
    _dns_pins[host] = ips[0]
    if socket.getaddrinfo is not _pinned_getaddrinfo:
        socket.getaddrinfo = _pinned_getaddrinfo
    return True


def server_redeem(server: str, code: str) -> dict:
    """POST /drm/redeem {code} -> parsed json (raises on transport error)."""
    body = json.dumps({"code": code}).encode("utf-8")
    # A real browser User-Agent: the urllib default ("Python-urllib/3.x") trips
    # Cloudflare's Browser Integrity Check (error 1010) before the request ever
    # reaches the backend. Any normal UA gets through.
    req = urllib.request.Request(
        server + "/drm/redeem",
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0 Safari/537.36 TokeerRedeem"
            ),
            "Accept": "application/json",
        },
        method="POST",
    )
    def _send():
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace"), resp.status

    try:
        raw, status = _send()
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        status = e.code
    except Exception as e:
        host = urllib.parse.urlparse(server).hostname or ""
        if host and _is_name_resolution_error(e) and _install_dns_fallback(host):
            log(f"      system DNS could not resolve {host}, using DNS-over-HTTPS ...")
            try:
                raw, status = _send()
            except urllib.error.HTTPError as e2:
                raw = e2.read().decode("utf-8", "replace")
                status = e2.code
            except Exception as e2:
                die(f"server unreachable at {server}: {e2}")
        else:
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
    """Return a .reg file body (UTF-8) writing both tickets as REG_BINARY, plus the
    owning SteamID as REG_SZ.

    The engine spoofs GetSteamID from the SteamID value FIRST and only reads the id
    out of AppTicket when that value is missing, so a leftover from an earlier redeem
    outranks the tickets written here — GetSteamID names one account, the ownership
    ticket another, and Denuvo refuses with 88500012. A "-" clears the value when the
    ticket has no readable owner, so the engine falls back to the ticket instead of
    keeping the stale id."""
    def owner_of(h: str) -> str:
        try:
            data = bytes.fromhex((h or "").strip())
            if len(data) < 16:
                return ""
            sid = int.from_bytes(data[8:16], "little")
            return str(sid) if sid >= 76561197960265728 else ""
        except Exception:
            return ""

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
    owner = owner_of(appticket_hex)
    steam_id_line = f'"SteamID"="{owner}"\n' if owner else '"SteamID"=-\n'
    return (
        "Windows Registry Editor Version 5.00\n\n"
        f"[{key}]\n"
        f'"AppTicket"=hex:{as_hex(appticket_hex)}\n'
        f'"ETicket"=hex:{as_hex(eticket_hex)}\n'
        f"{steam_id_line}"
    )


def _hex_csv(h: str) -> str:
    """Hex string -> 'aa,bb,cc' (lowercase byte pairs), the REG_BINARY form wine
    stores in user.reg."""
    h = (h or "").strip().replace(" ", "").replace("\n", "").replace(",", "")
    return ",".join(h[i:i + 2].lower() for i in range(0, len(h), 2))


def write_prefix_registry_direct(prefix: str, app_id: str, appticket_hex: str,
                                 eticket_hex: str, dry_run: bool) -> bool:
    """Write the tickets straight into the prefix's user.reg, no wine needed.

    Proton's bundled wine can't always be exec'd on its own - some builds ship it as
    a wrapper, or it only runs inside the Steam Linux Runtime container, which is why
    shelling out to it threw 'Exec format error'. HKCU lives in user.reg and that one
    key is all we touch, so we edit the file directly. Works on any distro and on the
    Steam Deck with nothing installed. Returns True on success, False to let the
    caller fall back to the wine method."""
    import re
    import time as _time

    reg = os.path.join(prefix, "user.reg")
    if not os.path.isfile(reg):
        log("  user.reg not found; falling back to wine")
        return False

    owner = ticket_owner_steamid(appticket_hex)
    key_hdr = "[Software\\\\Valve\\\\Steam\\\\Apps\\\\%s]" % app_id
    block = (
        f"{key_hdr} {int(_time.time())}\n"
        f'"AppTicket"=hex:{_hex_csv(appticket_hex)}\n'
        f'"ETicket"=hex:{_hex_csv(eticket_hex)}\n'
        + (f'"SteamID"="{owner}"\n' if owner else '"SteamID"=-\n')
    )

    log(f"  prefix: {prefix}")
    log(f"  writing HKCU\\Software\\Valve\\Steam\\Apps\\{app_id} directly into user.reg")
    if dry_run:
        log("  (dry-run) not writing. Block would be:\n" + block)
        return True

    # Read byte-preserving so any non-ASCII keys elsewhere in the file survive
    # untouched (surrogateescape round-trips arbitrary bytes).
    try:
        with open(reg, encoding="utf-8", errors="surrogateescape") as f:
            text = f.read()
    except OSError as e:
        log(f"  couldn't read user.reg ({e}); falling back to wine")
        return False
    if "WINE REGISTRY" not in text and "[" not in text:
        log("  user.reg doesn't look like a wine registry; falling back to wine")
        return False

    # Drop any existing block for this exact app key (header line through the last
    # of its values), then append a fresh one.
    pat = re.compile(
        r"(?ms)^\[Software\\\\Valve\\\\Steam\\\\Apps\\\\" + re.escape(app_id)
        + r"\].*?(?=^\[|\Z)"
    )
    new_text = pat.sub("", text)
    if not new_text.endswith("\n"):
        new_text += "\n"
    new_text += "\n" + block

    # Atomic write with a one-time backup so a bad edit can never brick the prefix.
    bak = reg + ".tokeerbak"
    try:
        if not os.path.exists(bak):
            shutil.copy2(reg, bak)
        tmp = reg + ".tokeer.tmp"
        with open(tmp, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(new_text)
        os.replace(tmp, reg)
    except OSError as e:
        log(f"  couldn't write user.reg ({e}); falling back to wine")
        return False
    log("  registry written OK (direct, no wine)")
    return True


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
    except OSError as e:
        # ENOEXEC etc: this Proton build's wine can't be launched directly.
        die(
            "couldn't run the Proton wine binary ({}).\n"
            "  wine: {}\n"
            "  This Proton build's wine can't be executed on its own. Update the app so it\n"
            "  writes the ticket directly, or pass --wine to a working wine binary.".format(e, wine)
        )
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


def ticket_owner_steamid(appticket_hex: str) -> str:
    """The owning SteamID64 encoded in an AppTicket, or "" if it can't be read."""
    try:
        data = bytes.fromhex((appticket_hex or "").strip())
        if len(data) < 16:
            return ""
        sid = int.from_bytes(data[8:16], "little")
        return str(sid) if sid >= 76561197960265728 else ""
    except Exception:
        return ""


def find_install_dir(steam_root: str, app_id: str) -> str:
    """The game's install folder, from appmanifest_<appid>.acf across libraries."""
    import re
    for root in _library_roots(steam_root):
        acf = os.path.join(root, "steamapps", f"appmanifest_{app_id}.acf")
        if not os.path.isfile(acf):
            continue
        try:
            text = open(acf, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        m = re.search(r'"installdir"\s+"([^"]+)"', text, re.IGNORECASE)
        if m:
            p = os.path.join(root, "steamapps", "common", m.group(1))
            if os.path.isdir(p):
                return p
    return ""


# --------------------------------------------------------------------------- #
# REFramework dinput8.dll
# --------------------------------------------------------------------------- #
def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def _fetch_reframework_dll() -> str:
    """A verified local dinput8.dll, downloaded once into ~/.tokeer/reframework and
    re-used after. Returns "" on any failure."""
    cache_dir = os.path.expanduser("~/.tokeer/reframework")
    cached = os.path.join(cache_dir, "dinput8.dll")
    try:
        if os.path.isfile(cached) and _sha256(cached) == REFRAMEWORK_DINPUT8_SHA256:
            return cached
    except OSError:
        pass
    os.makedirs(cache_dir, exist_ok=True)
    tmp = cached + ".part"

    def _download():
        req = urllib.request.Request(REFRAMEWORK_DINPUT8_URL, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) TokeerRedeem"})
        with urllib.request.urlopen(req, timeout=180) as resp, open(tmp, "wb") as out:
            shutil.copyfileobj(resp, out)

    try:
        try:
            _download()
        except Exception as e:
            # Same broken-DNS fallback as the redeem call. GitHub release URLs 302
            # to a CDN, so pin both the front host and the asset host.
            if _is_name_resolution_error(e):
                ok = False
                for h in ("github.com", "objects.githubusercontent.com",
                          "release-assets.githubusercontent.com"):
                    ok = _install_dns_fallback(h) or ok
                if not ok:
                    raise
                _download()
            else:
                raise
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return ""
    if _sha256(tmp) != REFRAMEWORK_DINPUT8_SHA256:
        os.remove(tmp)
        return ""
    os.replace(tmp, cached)
    return cached


def place_reframework_dll(install_dir: str) -> None:
    """Drop dinput8.dll into the game folder for RE Engine + Denuvo titles. Presence
    check first: if it's already there, leave it. Best effort; never raises."""
    if not install_dir:
        log("  could not locate the game folder; skipping dinput8 placement")
        return
    target = os.path.join(install_dir, "dinput8.dll")
    if os.path.isfile(target):
        log("  dinput8.dll already present; leaving it")
        return
    src = _fetch_reframework_dll()
    if not src:
        log("  could not fetch dinput8.dll (the game may crash on new-game until it is placed)")
        return
    try:
        shutil.copy2(src, target)
        log("  placed dinput8.dll in the game folder")
    except Exception as e:
        log(f"  could not place dinput8.dll ({e})")


# --------------------------------------------------------------------------- #
# Standalone save re-sign (no code needed)
# --------------------------------------------------------------------------- #
def read_prefix_owner(prefix: str, app_id: str) -> str:
    """The owning SteamID of the ticket already applied to this Proton prefix, read
    from user.reg (the redeemer wrote it there at activation). Prefers the SteamID
    value; falls back to the owner encoded in the AppTicket. "" if none found."""
    import re
    reg = os.path.join(prefix, "user.reg")
    try:
        text = open(reg, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""
    # Grab just this app's key block (up to the next section header).
    key = r"[Software\\Valve\\Steam\\Apps\\%s]" % app_id
    start = text.find(key)
    if start == -1:
        return ""
    end = text.find("\n[", start + len(key))
    block = text[start:] if end == -1 else text[start:end]

    m = re.search(r'"SteamID"="(\d+)"', block)
    if m and int(m.group(1)) >= 76561197960265728:
        return m.group(1)
    # AppTicket=hex:xx,xx,\   with 2-space continuation lines.
    m = re.search(r'"AppTicket"=hex:([0-9a-fA-F,\\\s]+)', block)
    if m:
        hx = re.sub(r'[^0-9a-fA-F]', '', m.group(1))
        owner = ticket_owner_steamid(hx)
        if owner:
            return owner
    return ""


def resign_main(argv) -> None:
    """`tokeer resign [APPID]` - re-sign the existing save onto the account the
    game is currently activated with, without spending a code. Only the RE Engine
    titles that sign saves per-account are supported."""
    try:
        import tokeer_save_carry
    except Exception as e:
        die(f"save tools are missing from this install ({e}); re-run install_linux.sh")

    steam_root = find_steam_root(None)

    app_id = (argv[0].strip() if argv and argv[0].strip().isdigit() else "")
    if not app_id:
        # No appid given: pick the supported game that has a save on disk.
        active = None
        try:
            for name in os.listdir(os.path.join(steam_root, "userdata")):
                for a in tokeer_save_carry.SUPPORTED:
                    if os.path.isdir(os.path.join(steam_root, "userdata", name, a, "remote")):
                        active = a
        except OSError:
            pass
        if not active:
            names = ", ".join(sorted(tokeer_save_carry.SUPPORTED))
            die(f"no supported save found. Pass the app id: tokeer resign <APPID>  (one of: {names})")
        app_id = active
        log(f"resigning the save for app {app_id} (auto-detected)")

    if not tokeer_save_carry.is_supported(app_id):
        die(f"save re-signing isn't available for app {app_id} (no per-account save profile)")

    prefix = find_prefix(steam_root, app_id)
    owner = read_prefix_owner(prefix, app_id)
    if not owner:
        die("this game isn't activated yet on this account. Run `tokeer <CODE>` first, "
            "then use `tokeer resign` only if the save still won't load.")

    install_dir = find_install_dir(steam_root, app_id)
    # Make sure the game folder is fully fixed, not just the save: ensure the
    # REFramework dinput8.dll is in place too (no-op if it already is).
    if app_id in REFRAMEWORK_DINPUT8_GAMES:
        place_reframework_dll(install_dir)
    log(f"re-signing your save onto the current activation ({owner}) ...")
    tokeer_save_carry.carry_over(app_id, owner, steam_root=steam_root,
                                 install_dir=install_dir, log=log)
    log("Done.")


# --------------------------------------------------------------------------- #
def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "resign":
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        resign_main(sys.argv[2:])
        return

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

    log("[3/5] writing tickets into the game's Proton prefix ...")
    # Edit user.reg directly first - it needs no wine and can't hit the Proton
    # 'Exec format error'. Only fall back to importing via wine if that can't apply
    # (missing/odd user.reg), and locate wine lazily so a broken Proton binary never
    # blocks the direct path.
    if not write_prefix_registry_direct(prefix, app_id, appticket, eticket, args.dry_run):
        wine = find_wine(steam_root, args.wine)
        reg_body = build_reg_file(app_id, appticket, eticket)
        write_prefix_registry(wine, prefix, reg_body, args.dry_run)

    install_dir = find_install_dir(steam_root, app_id)

    # RE Engine + Denuvo titles need REFramework's dinput8.dll in the game folder
    # or they crash on new-game. Place it before launch (the install script's
    # launch option already sets WINEDLLOVERRIDES so Proton loads it).
    log("[4/5] checking REFramework dinput8.dll ...")
    if app_id in REFRAMEWORK_DINPUT8_GAMES and not args.dry_run:
        place_reframework_dll(install_dir)
    else:
        log("  not needed for this game")

    if args.no_launch or args.dry_run:
        log("[5/5] done (launch skipped).")
        log(f"      Start the game yourself: steam://rungameid/{app_id}")
        return

    # Capcom RE Engine saves are signed per-account, so a new activation would make
    # the game reject the previous save. For those games the carry-over re-signs the
    # existing save onto this activation's id and launches the game itself.
    log("[5/5] launching the game ...")
    launched = False
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import tokeer_save_carry
        if tokeer_save_carry.is_supported(app_id):
            owner = ticket_owner_steamid(appticket)
            if owner:
                launched = tokeer_save_carry.carry_over(
                    app_id, owner, steam_root=steam_root,
                    install_dir=install_dir, log=log)
    except Exception as e:
        log(f"  save carry-over skipped ({e})")
    if not launched:
        launch_game(app_id)
    log("Done. If the game still shows a Denuvo/012/005 error, the game-side OST")
    log("hook likely isn't applying the ticket under Proton - tell me and we go deeper.")


if __name__ == "__main__":
    main()
