#!/usr/bin/env bash
#
# Tokeer Linux / Proton - one-time per-game setup.
#
#   curl -fsSL https://git.lua.tools/luatools-dedivision/TokeerDRM-App/raw/branch/main/install_linux.sh | bash -s -- <APPID>
#
# It downloads + builds the native Denuvo hook, auto-writes the game's Steam
# launch option (and points it at a Proton if it has none), installs the
# `tokeer` command, and creates the Proton prefix. Run once per game. After it,
# activation is a single command:   tokeer <CODE>
#
# Env overrides:
#   TOKEER_DRM_SERVER   backend base URL (default https://luastools.xyz)
#   TOKEER_BUNDLE_URL   bundle zip URL (default: latest GitHub release asset)
#   TOKEER_HOME         install dir (default ~/.tokeer)
#
set -euo pipefail

APPID="${1:-}"
if ! [[ "$APPID" =~ ^[0-9]+$ ]]; then
    echo "usage: install_linux.sh <APPID> [ubisoft|ea]   (numeric Steam AppID)" >&2
    exit 1
fi
# "ubisoft" mode installs the tokeer tools only: it does NOT build the native
# Denuvo hook and does NOT write a Steam launch option. Ubisoft games run
# folder-based under Proton (upc_r2 + dbdata), so the hook/launch option would
# break them. It still creates the Proton prefix so verify-ubi can pass.
# "ea" mode is the EA/anadius folder flow: like ubisoft it skips the native hook,
# but instead of the ost-run launch option it hands off to ea_tokeer_linux.py,
# which patches anadius64.dll for Wine and writes the crack-exe launch option.
MODE="${2:-steam}"

TDIR="${TOKEER_HOME:-$HOME/.tokeer}"
SERVER="${TOKEER_DRM_SERVER:-https://luastools.xyz}"
BUNDLE_URL="${TOKEER_BUNDLE_URL:-https://git.lua.tools/luatools-dedivision/TokeerDRM-App/releases/download/v1.0.29/tokeer-linux.zip}"

echo "== Tokeer Linux setup - app $APPID =="
mkdir -p "$TDIR"

# 1) Get the bundle (hook source + scripts). If we're launched from inside an
#    already-unzipped bundle, use that instead of downloading.
need=(ost_native_hook.c ost-run.sh build.sh tokeer_redeem_linux.py tokeer tokeer_steam_config.py tokeer_validate_linux.py tokeer_version_lock.py ea_tokeer_linux.py)
have_local=1
for f in "${need[@]}"; do [ -f "./$f" ] || have_local=0; done
if [ "$have_local" = 1 ]; then
    echo "[1/7] using bundle from current directory"
    cp -f "${need[@]}" "$TDIR"/
    [ -f ./ost_native_hook.so ] && cp -f ./ost_native_hook.so "$TDIR"/ || true
    # Optional save-carry tools (RE Engine save re-sign). Absent = feature off.
    cp -f ./tokeer_save_carry.py "$TDIR"/ 2>/dev/null || true
    cp -f ./mandarin-juice-cli "$TDIR"/ 2>/dev/null || true
    for pf in ./*.bin; do [ -f "$pf" ] && cp -f "$pf" "$TDIR"/; done
else
    echo "[1/7] downloading bundle -> $BUNDLE_URL"
    tmp="$(mktemp -d)"
    curl -fsSL "$BUNDLE_URL" -o "$tmp/b.zip"
    unzip -oq "$tmp/b.zip" -d "$tmp/x"
    hookc="$(find "$tmp/x" -name ost_native_hook.c | head -1)"
    [ -n "$hookc" ] || { echo "ERROR: bundle has no ost_native_hook.c" >&2; exit 1; }
    cp -f "$(dirname "$hookc")"/* "$TDIR"/ 2>/dev/null || true
    rm -rf "$tmp"
fi

# 2) Backend URL for the redeemer (read via server_config.py next to it).
printf 'SERVER_URL = "%s"\n' "$SERVER" > "$TDIR/server_config.py"

# 3) Native hook: use the prebuilt .so if shipped, else compile it.
#    Ubisoft mode skips this entirely (the folder-based flow doesn't use it).
chmod +x "$TDIR/build.sh" "$TDIR/ost-run.sh" "$TDIR/tokeer" 2>/dev/null || true
chmod +x "$TDIR/mandarin-juice-cli" 2>/dev/null || true
if [ "$MODE" = "ubisoft" ] || [ "$MODE" = "ea" ]; then
    echo "[2/7] $MODE mode: skipping native hook (not needed for the Proton folder flow)"
elif [ -f "$TDIR/ost_native_hook.so" ]; then
    echo "[2/7] preparing native hook..."
    echo "  using prebuilt ost_native_hook.so"
elif command -v gcc >/dev/null 2>&1; then
    echo "[2/7] preparing native hook..."
    ( cd "$TDIR" && ./build.sh )
else
    echo "ERROR: no prebuilt ost_native_hook.so and gcc is not installed." >&2
    echo "  Steam Deck: 'sudo steamos-readonly disable' then install gcc, or ask staff" >&2
    echo "  for the prebuilt ost_native_hook.so and drop it in $TDIR/." >&2
    exit 1
fi

# 4) Install the `tokeer` command on PATH.
mkdir -p "$HOME/.local/bin"
ln -sf "$TDIR/tokeer" "$HOME/.local/bin/tokeer"
echo "[3/7] installed 'tokeer' -> ~/.local/bin/tokeer"
# Make `tokeer` runnable by name in future shells: append ~/.local/bin to PATH in
# whichever rc files exist (bash AND zsh - Arch/SteamOS often default to zsh, and
# a bash-only line wouldn't help there). Idempotent. Only affects NEW terminals;
# the full path ~/.tokeer/tokeer always works regardless of shell/PATH.
if ! case ":$PATH:" in *":$HOME/.local/bin:"*) true;; *) false;; esac; then
    PLINE='export PATH="$HOME/.local/bin:$PATH"'
    touched=""
    for rc in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile"; do
        [ -f "$rc" ] || continue
        if ! grep -Fqx "$PLINE" "$rc"; then
            printf '\n# added by Tokeer installer\n%s\n' "$PLINE" >> "$rc"
            touched="$touched ${rc##*/}"
        fi
    done
    # fish shell: different syntax + its own config file. fish_add_path is
    # idempotent (fish 3.2+), so it's safe to leave in config.fish.
    fish_cfg="$HOME/.config/fish/config.fish"
    if command -v fish >/dev/null 2>&1 || [ -f "$fish_cfg" ]; then
        FLINE='fish_add_path "$HOME/.local/bin"'
        if [ ! -f "$fish_cfg" ] || ! grep -Fq "$FLINE" "$fish_cfg"; then
            mkdir -p "$(dirname "$fish_cfg")"
            printf '\n# added by Tokeer installer\n%s\n' "$FLINE" >> "$fish_cfg"
            touched="$touched config.fish"
        fi
    fi
    # No rc file at all? Create ~/.bashrc so it's set for next login.
    if [ -z "$touched" ] && [ ! -f "$HOME/.zshrc" ] && [ ! -f "$fish_cfg" ]; then
        printf '\n# added by Tokeer installer\n%s\n' "$PLINE" >> "$HOME/.bashrc"
        touched=" .bashrc"
    fi
    [ -n "$touched" ] && echo "  added ~/.local/bin to PATH in$touched (open a NEW terminal for 'tokeer' to work)"
    echo "  for THIS terminal, use the full path:  $TDIR/tokeer <CODE>"
fi

# 5) Auto-write the Steam launch option (Steam must be closed while we edit).
#    Ubisoft mode SKIPS this: the game must launch normally under Proton, not
#    through the Denuvo hook, so writing ost-run.sh here would break it.
if [ "$MODE" = "ea" ]; then
    echo "[4/7] EA mode: patching anadius64 + writing the crack-exe launch option..."
    # ea_tokeer setup patches anadius64.dll for Wine, finds the crack exe, writes
    # the Steam launch option that swaps %command%'s exe for it, and (step 7 below
    # is skipped for ea) builds the prefix. Steam is already shut down here.
    python3 "$TDIR/ea_tokeer_linux.py" setup "$APPID" || true
elif [ "$MODE" = "ubisoft" ]; then
    echo "[4/7] Ubisoft mode: skipping Steam launch option (game runs folder-based under Proton)"
else
    LAUNCH="WINEDLLOVERRIDES=\"dinput8=n,b\" $TDIR/ost-run.sh %command%"
    echo "[4/7] writing Steam launch options (Steam will restart)..."
    if pgrep -x steam >/dev/null 2>&1; then
        steam -shutdown >/dev/null 2>&1 || true
        for _ in $(seq 1 20); do pgrep -x steam >/dev/null 2>&1 || break; sleep 1; done
    fi
    # Non-fatal: if auto-setting fails it prints the manual launch option and exits 0;
    # `|| true` guards against any unexpected error aborting the rest of setup.
    python3 "$TDIR/tokeer_steam_config.py" "$APPID" "$LAUNCH" || true
fi

# 6) Version lock, for a game whose newest Steam build breaks the activation.
#    Writes the pinned script, the build's manifests and the slsteam pin, so the
#    update Steam offers next is the downgrade. Steam is already shut down here
#    (step 5), which is when the pin is cheapest to write. Non-fatal: verify
#    runs the same thing and reports what is missing.
if [ "$MODE" = "ea" ]; then
    echo "[5/7] EA mode: skipping version lock (n/a)"
elif [ -f "$TDIR/tokeer_version_lock.py" ]; then
    echo "[5/7] checking for a version lock..."
    python3 "$TDIR/tokeer_version_lock.py" "$APPID" || true
else
    echo "[5/7] no version lock tool in this bundle, skipping"
fi

# 7) Create the Proton prefix by launching once (it fails Denuvo - expected -
#    but that first run is what builds compatdata/<appid>/pfx). EA mode already
#    did this inside `ea_tokeer setup`, so it is skipped here to avoid a double
#    launch.
if [ "$MODE" = "ea" ]; then
    echo "[6/7] EA mode: prefix handled by ea setup"
else
    echo "[6/7] launching once to create the Proton prefix (first launch fails Denuvo - that's normal)..."
    ( setsid steam "steam://rungameid/$APPID" >/dev/null 2>&1 & ) || \
        ( setsid xdg-open "steam://rungameid/$APPID" >/dev/null 2>&1 & ) || true
fi

echo "[7/7] done."
echo
echo "Setup complete for app $APPID."
if [ "$MODE" = "ea" ]; then
    echo "  1) Launch the game once so it writes a token_req, then:"
    echo "       ~/.tokeer/tokeer ea-request $APPID"
    echo "  2) Paste that token_req into your ticket."
    echo "  3) When you get the Denuvo token back:"
    echo "       ~/.tokeer/tokeer ea-apply $APPID @token.txt   (or paste the token inline)"
    echo "  Check state any time:  ~/.tokeer/tokeer ea-verify $APPID"
elif [ "$MODE" = "ubisoft" ]; then
    echo "  Verify your game (prints a code to paste in the ticket):"
    echo "    ~/.tokeer/tokeer verify-ubi $APPID"
    echo "  Then paste the care-package files into the game folder and send your"
    echo "  token_req in the ticket to get your dbdata.json."
else
    echo "  Verify your setup (prints a code to paste in the ticket):"
    echo "    ~/.tokeer/tokeer verify $APPID"
    echo "  Then, when your activation code arrives:"
    echo "    ~/.tokeer/tokeer <CODE>"
fi
echo
echo "(Full paths shown in case ~/.local/bin isn't on your PATH. If the game is"
echo " already open from this setup run, close it first before activating.)"
