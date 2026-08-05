#!/bin/bash
# Steam launch-option wrapper for the OST native Denuvo hook.
#
# Set the game's launch options to:   /full/path/to/ost-run.sh %command%
#
# A wrapper rather than "LD_PRELOAD=... %command%" because Steam already sets
# LD_PRELOAD for its overlay. Overwriting it breaks the overlay and can break
# the launch; this APPENDS instead.
# Preloads the N2 hook (ost_native_hook.so). To go back to the watch-only N1
# probe instead, set OST_SO=ost_native.so in the launch options before this.
#
# EA AntiCheat titles (see OST_EAAC_EXE below) boot
# EAAntiCheat.GameServiceLauncher.exe, which refuses Proton with error
# E111000B ("Wine, Proton, and Steam Deck are not supported"). For those this
# wrapper SKIPS that launcher: it runs bypass.exe, then launches the real game
# exe directly, so no per-game launch option is needed. The Denuvo eticket is
# still served by the preloaded hook exactly as for any other title.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SO="$HERE/${OST_SO:-ost_native_hook.so}"

export OST_NATIVE_LOG="${OST_NATIVE_LOG:-$HOME/ost_native_hook.log}"
if [ -f "$SO" ]; then
    export LD_PRELOAD="${LD_PRELOAD:+$LD_PRELOAD:}$SO"
else
    echo "ost-run.sh: $SO missing, run build.sh first" >&2
fi

# --- EA AntiCheat table: appid -> real game exe name -----------------------
# Add new EAAC games here. The value is the game's actual executable (the one
# to run INSTEAD of EAAntiCheat.GameServiceLauncher.exe). You can also override
# for one launch without editing this file via OST_FORCE_EXE=Whatever.exe.
declare -A OST_EAAC_EXE=(
    [3059520]="F1_25.exe"     # F1 25
)

# Resolve the appid: prefer Steam's env, fall back to AppId=NNN in %command%.
appid="${SteamAppId:-$SteamGameId}"
if [ -z "$appid" ]; then
    for a in "$@"; do case "$a" in AppId=*) appid="${a#AppId=}";; esac; done
fi

exe="${OST_FORCE_EXE:-${OST_EAAC_EXE[$appid]}}"
if [ -n "$exe" ]; then
    # proton binary: from the compat-tool path env, else parse it out of %command%.
    proton="${STEAM_COMPAT_TOOL_PATHS%%:*}/proton"
    [ -x "$proton" ] || for a in "$@"; do case "$a" in */proton) proton="$a";; esac; done
    # game dir = folder of the .exe Steam was going to launch (the EAAC launcher).
    gamedir=""
    for a in "$@"; do case "$a" in *.exe|*.EXE) gamedir="$(dirname "$a")";; esac; done

    # The overtake.gg bypass is a LOADER: it launches $exe ITSELF and patches the
    # anti-cheat in memory (WriteProcessMemory), and it needs the companion file
    # it generates on first run sitting next to it. So we must:
    #   (1) use the bypass INSTALLED IN THE GAME FOLDER (with its companion), and
    #   (2) run ONLY the bypass - launching $exe ourselves makes an UNPATCHED
    #       second copy that throws E111000B.
    # Prefer the game-folder bypass; fall back to the bundled one (no companion,
    # last resort). LD_PRELOAD is already exported so the $exe the loader spawns
    # inherits the Denuvo hook.
    bypass="$gamedir/bypass.exe"
    [ -f "$bypass" ] || bypass="$HERE/bypass.exe"

    if [ -x "$proton" ] && [ -n "$gamedir" ] && [ -f "$bypass" ]; then
        echo "ost-run.sh: EAAC appid=$appid -> loader launches $exe (no separate launch)" >&2
        echo "ost-run.sh:   proton=$proton" >&2
        echo "ost-run.sh:   bypass=$bypass" >&2
        [ "$bypass" = "$HERE/bypass.exe" ] && echo "ost-run.sh:   WARNING using bundled bypass (no companion) - install it in $gamedir instead" >&2
        ( cd "$gamedir" && VKD3D_SHADER_CACHE_PATH=0 "$proton" run "$bypass" )
        # The loader may return right after spawning the game. Let the game show
        # up, then hold until it actually exits so Steam stays "running".
        sleep "${OST_EAAC_SLEEP:-8}"
        while pgrep -f "$exe" >/dev/null 2>&1; do sleep 2; done
        exit 0
    fi
    echo "ost-run.sh: EAAC appid=$appid but launch pieces missing - normal launch instead" >&2
    echo "ost-run.sh:   proton=$proton gamedir=$gamedir exe=$exe bypass=$bypass" >&2
fi

echo "ost-run.sh: LD_PRELOAD=$LD_PRELOAD" >&2
echo "ost-run.sh: log -> $OST_NATIVE_LOG" >&2
exec "$@"
