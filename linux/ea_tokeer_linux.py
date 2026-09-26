#!/usr/bin/env python3
"""ea-tokeer: Linux / Proton activation helper for EA Denuvo (anadius) games.

The whole EA flow, from the user's side:

  1. verify  <APPID>   Check the game folder for the Not-a-Crack files.
                       - missing: tell them which files, and to download the
                         Not-a-Crack for this game from the EA resources channel
                         and extract it into the folder, then verify again.
                       - present: patch anadius64.dll so it stops refusing under
                         Wine, point Steam's launch option at the crack exe, build
                         the Proton prefix, and print a signed TEA1. code to paste
                         in the ticket.
  2. request <APPID>   After the bot OKs the code and you launch the game once,
                       this prints the token_req the game wrote, to paste back.
  3. apply   <APPID> <dbdata.json | TOKEN | @file>
                       Drop the dbdata.json the bot gives you into the game folder
                       (or build it from a raw token) and launch.
  4. verify is also the status check - run it any time.

The bot HMAC-verifies the TEA1 code with the same shared secret, reads the
checks, and only then guides you to the token_req step. The secret is not a
security boundary; a forged pass only wastes your own single-use slot.
"""
import base64
import glob
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# Shared with the danny EA cog. Same role as the Steam validator's SECRET.
EA_SECRET = b"tokeer_ea_setup_validate_2026_v1_shared"

# anadius64.dll Wine-detection bypass: is_wine() at RVA 0x45010 (file off 0x44410)
# returns 1 when ntdll!wine_get_version resolves. Overwrite its prologue with
# `xor eax,eax; ret; nop` so it always answers "not Wine". Fallback: break the
# probe string so GetProcAddress returns NULL (same not-Wine branch).
ISWINE_OFF = 0x44410
ISWINE_ORIG = bytes.fromhex("4883ec28")   # sub rsp,0x28
ISWINE_PATCH = bytes.fromhex("31c0c390")  # xor eax,eax; ret; nop

# Where users get the files. Shown in the "missing" message.
EA_RESOURCES = "the EA resources channel in the LuaTools Discord"


def log(m): print(m, flush=True)
def die(m, code=1):
    print(f"error: {m}", file=sys.stderr)
    sys.exit(code)


# --- Steam layout ----------------------------------------------------------

def steam_root():
    for c in (
        "~/.steam/steam", "~/.local/share/Steam",
        "~/.var/app/com.valvesoftware.Steam/.local/share/Steam", "~/.steam/root",
    ):
        p = os.path.expanduser(c)
        if os.path.isdir(os.path.join(p, "steamapps")):
            return p
    die("could not find your Steam directory")


def library_roots(root):
    roots = [root]
    try:
        txt = open(os.path.join(root, "steamapps", "libraryfolders.vdf"),
                   encoding="utf-8", errors="ignore").read()
        for m in re.finditer(r'"path"\s*"([^"]+)"', txt):
            p = m.group(1).replace("\\\\", "/")
            if os.path.isdir(p) and p not in roots:
                roots.append(p)
    except OSError:
        pass
    return roots


def install_dir(root, appid):
    for lib in library_roots(root):
        mani = os.path.join(lib, "steamapps", f"appmanifest_{appid}.acf")
        if not os.path.isfile(mani):
            continue
        m = re.search(r'"installdir"\s*"([^"]+)"',
                      open(mani, encoding="utf-8", errors="ignore").read())
        if m:
            d = os.path.join(lib, "steamapps", "common", m.group(1))
            if os.path.isdir(d):
                return d
    die(f"game for app {appid} isn't installed in Steam (or wrong AppID)")


def prefix_path(root, appid):
    for lib in library_roots(root):
        pfx = os.path.join(lib, "steamapps", "compatdata", appid, "pfx")
        if os.path.isdir(pfx):
            return pfx
    return None


# --- game-folder helpers ---------------------------------------------------

def crack_exe(folder):
    """The .exe that imports anadius64.dll = the Not-a-Crack replacement exe."""
    best = None
    for exe in glob.glob(os.path.join(folder, "*.exe")):
        try:
            if b"anadius64" in open(exe, "rb").read():
                sz = os.path.getsize(exe)
                if best is None or sz < best[1]:
                    best = (exe, sz)
        except OSError:
            pass
    return best[0] if best else None


def _cfg_val(folder, key):
    try:
        txt = open(os.path.join(folder, "anadius.cfg"),
                   encoding="utf-8", errors="ignore").read()
        m = re.search(r'"' + re.escape(key) + r'"\s*"([^"]+)"', txt)
        return m.group(1) if m else None
    except OSError:
        return None


def patch_anadius(folder):
    dll = os.path.join(folder, "anadius64.dll")
    d = bytearray(open(dll, "rb").read())
    cur = bytes(d[ISWINE_OFF:ISWINE_OFF + 4])
    if cur == ISWINE_PATCH:
        return "already patched"
    if cur == ISWINE_ORIG:
        if not os.path.exists(dll + ".orig"):
            shutil.copy2(dll, dll + ".orig")
        d[ISWINE_OFF:ISWINE_OFF + 4] = ISWINE_PATCH
        open(dll, "wb").write(d)
        return "patched (Wine check disabled)"
    i = d.find(b"wine_get_version")
    if i < 0:
        return "unrecognised build - left as-is"
    if not os.path.exists(dll + ".orig"):
        shutil.copy2(dll, dll + ".orig")
    d[i] = ord("X")
    open(dll, "wb").write(d)
    return "patched via probe-string fallback"


def anadius_patched(folder):
    try:
        d = open(os.path.join(folder, "anadius64.dll"), "rb").read()
    except OSError:
        return False
    return d[ISWINE_OFF:ISWINE_OFF + 4] == ISWINE_PATCH or b"Xine_get_version" in d


def find_token_req(folder):
    c = sorted(glob.glob(os.path.join(folder, "token_req*.txt")),
               key=os.path.getmtime, reverse=True)
    return c[0] if c else None


# --- Steam launch option ---------------------------------------------------

def launch_command(exe, folder):
    # swap the last arg of %command% (the exe Proton runs) for the crack exe.
    swap = ("bash -c 'a=(\"$@\"); a[${#a[@]}-1]=\"%s\"; exec \"${a[@]}\"' -- %%command%%"
            % exe)
    # If the crack uses a version.dll proxy (FC-style loaders), Wine ships its own
    # builtin version.dll and won't load the crack's copy unless we force it native.
    # dbdata.dll/anadius64.dll aren't system names, so Wine loads those on its own.
    if os.path.isfile(os.path.join(folder, "version.dll")):
        return 'WINEDLLOVERRIDES="version=n,b" ' + swap
    return swap


def write_launch_option(appid, launch):
    cfg = os.path.join(HERE, "tokeer_steam_config.py")
    if os.path.isfile(cfg):
        try:
            subprocess.run([sys.executable, cfg, appid, launch], check=True)
            return True
        except subprocess.CalledProcessError:
            pass
    return False


def launch_game(appid):
    try:
        subprocess.Popen(["steam", f"steam://rungameid/{appid}"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True
    except OSError:
        return False


# --- the TEA1 verification code -------------------------------------------

def make_code(report):
    raw = json.dumps(report, separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(EA_SECRET, raw, hashlib.sha256).digest()
    b = lambda x: base64.urlsafe_b64encode(x).decode().rstrip("=")
    return "TEA1." + b(raw) + "." + b(sig)


# --- commands --------------------------------------------------------------

def cmd_verify(appid):
    root = steam_root()
    folder = install_dir(root, appid)
    game = os.path.basename(folder)
    log(f"Game: {game}")
    log(f"Folder: {folder}\n")

    an = os.path.isfile(os.path.join(folder, "anadius64.dll"))
    db = os.path.isfile(os.path.join(folder, "dbdata.dll"))
    exe = crack_exe(folder)

    missing = []
    if not an: missing.append("anadius64.dll")
    if not db: missing.append("dbdata.dll")
    if not exe: missing.append("the Not-a-Crack game .exe")

    if missing:
        log("Not-a-Crack files are NOT in the game folder yet:")
        for m in missing:
            log(f"   [missing] {m}")
        log("")
        log(f"Download the Not-a-Crack for '{game}' from {EA_RESOURCES},")
        log("extract ALL of it into the game folder (overwrite when asked):")
        log(f"   {folder}")
        log(f"\nThen run again:  tokeer ea-verify {appid}")
        sys.exit(2)

    # all present -> prepare the game
    log("Not-a-Crack files found. Preparing the game:")
    log("  [1/3] anadius64.dll: " + patch_anadius(folder))

    launch = launch_command(exe, folder)
    if write_launch_option(appid, launch):
        log("  [2/3] Steam launch option set (Steam may restart)")
        lopt = True
    else:
        log("  [2/3] set this launch option on the game manually:")
        log("        " + launch)
        lopt = False

    if prefix_path(root, appid):
        log("  [3/3] Proton prefix already built")
        pfx = True
    else:
        log("  [3/3] building the Proton prefix (launch once; it fails Denuvo - normal)")
        launch_game(appid)
        pfx = bool(prefix_path(root, appid))

    report = {
        "v": 1, "platform": "linux", "appid": appid, "game": game,
        "content_id": _cfg_val(folder, "ContentId"),
        "exe": os.path.basename(exe),
        "denuvo_exe_hash": _cfg_val(folder, "DenuvoExeHash"),
        "denuvo_dll_hash": _cfg_val(folder, "DenuvoDllHash"),
        "anadius": True, "dbdata": True, "patched": True,
        "launch_opt": lopt, "prefix": pfx,
    }
    log("\nPaste this whole code into your ticket:\n")
    log(make_code(report))
    log("\nAfter the bot OKs it: launch the game once from Steam, then run")
    log(f"  tokeer ea-request {appid}")


def cmd_request(appid):
    folder = install_dir(steam_root(), appid)
    req = find_token_req(folder)
    if not req:
        die("no token_req yet - launch the game once from Steam so dbdata.dll writes it, then re-run")
    body = open(req, encoding="utf-8", errors="ignore").read().strip()
    log(f"# {os.path.basename(req)} ({len(body)} chars) - paste this into your ticket:")
    log(body)


def cmd_apply(appid, arg, do_launch=True):
    folder = install_dir(steam_root(), appid)
    out = os.path.join(folder, "dbdata.json")

    src = arg[1:] if arg.startswith("@") else arg
    data = None
    # a path to the dbdata.json the bot gave -> copy it in verbatim
    if os.path.isfile(src):
        raw = open(src, encoding="utf-8", errors="ignore").read()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and data.get("DenuvoToken"):
            json.dump(data, open(out, "w", encoding="utf-8"), indent=2)
            log(f"placed dbdata.json in the game folder ({len(data['DenuvoToken'])}-char token)")
        else:
            # file holds a raw token blob
            data = None
            arg = raw.strip()
    if data is None and not os.path.isfile(src):
        arg = arg.strip()
    if data is None:
        token = (open(src).read() if os.path.isfile(src) else arg).strip().split("|")[0].strip()
        if len(token) < 200 or not re.fullmatch(r"[A-Za-z0-9_\-=]+", token):
            die("that isn't a dbdata.json or a Denuvo token")
        doc = {"DenuvoToken": token}
        cid = _cfg_val(folder, "ContentId")
        if cid:
            doc["ContentId"] = cid
        json.dump(doc, open(out, "w", encoding="utf-8"), indent=2)
        log(f"wrote dbdata.json ({len(token)}-char token" + (f", ContentId {cid}" if cid else "") + ")")

    for r in glob.glob(os.path.join(folder, "token_req*.txt")):
        try: os.remove(r)
        except OSError: pass

    if do_launch:
        log("launching ...")
        if not launch_game(appid):
            log("  couldn't reach steam; start the game yourself")


def main():
    a = sys.argv[1:]
    if not a:
        print(__doc__); sys.exit(1)
    cmd = a[0]
    # `setup` kept as an alias for `verify` (install_linux.sh ea calls it)
    if cmd == "setup":
        cmd = "verify"
    if len(a) < 2 or not re.fullmatch(r"\d+", a[1]):
        die("usage: ea_tokeer_linux.py <verify|request|apply> <APPID> [...]")
    appid = a[1]
    if cmd == "verify":
        cmd_verify(appid)
    elif cmd == "request":
        cmd_request(appid)
    elif cmd == "apply":
        if len(a) < 3:
            die("usage: apply <APPID> <dbdata.json | TOKEN | @file> [--no-launch]")
        cmd_apply(appid, a[2], do_launch=("--no-launch" not in a))
    else:
        die(f"unknown command '{cmd}' (verify|request|apply)")


if __name__ == "__main__":
    main()
