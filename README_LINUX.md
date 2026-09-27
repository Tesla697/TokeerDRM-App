# Tokeer on Linux / Proton (Steam Deck too)

Activate Denuvo games on Linux with **two terminal commands** - no Steam launch
options to edit by hand, no manual setup. Same 6-char code as Windows; it gets
redeemed into the game's Proton prefix instead of the Windows registry, and the
native `lsteamclient.so` hook serves it so Denuvo passes.

## How it works

```
Step 1 (once per game): curl -fsSL .../install_linux.sh | bash -s -- <APPID>
Step 2 (verify):        tokeer verify <APPID>   # prints a TLX1. code -> paste it in the ticket
Step 3 (activate):      tokeer <CODE>           # the bot's code -> activates + launches on its own
```

- **`install_linux.sh <APPID>`** downloads + builds the native hook, auto-writes
  the game's Steam **launch option** (and picks a Proton if the game has none),
  installs the `tokeer` command, and creates the Proton prefix. Run it once per
  game.
- **`tokeer verify <APPID>`** checks your game files, prefix, hook and launch
  option, and prints a signed **`TLX1.` setup code**. Paste that in the ticket;
  the bot verifies it and only then issues your activation code (so a broken
  setup never burns a token).
- **`tokeer <CODE>`** (the activation code the bot sends) writes `AppTicket` +
  `ETicket` into the game's prefix and launches it through Steam. The launch
  option from step 1 makes the game load the Denuvo hook automatically.

Pure Python stdlib + a tiny C hook - runs on the Steam Deck's system python.

## One-time setup

```
curl -fsSL https://raw.githubusercontent.com/Tesla697/TokeerDRM-App/main/install_linux.sh | bash -s -- <APPID>
```

Replace `<APPID>` with the game's Steam AppID (the ticket gives it to you). Steam
restarts once during this so the launch option takes. On a normal desktop distro
it builds the hook with `gcc`; on the **Steam Deck** either run
`sudo steamos-readonly disable` first (to allow `gcc`) or use a build that ships
the prebuilt `ost_native_hook.so`.

## Activate (each time you get a code)

```
tokeer <CODE>
```

That's it - the game activates and launches. The code is single-use and expires
~30 minutes after it's issued, so run it promptly. If `~/.local/bin` isn't on your
PATH, use `~/.tokeer/tokeer <CODE>`.

## Version-locked games

A few games only activate on one older Steam build (right now Assassin's Creed
Black Flag Resynced and EA SPORTS FC 26). For those, `install_linux.sh` and
`tokeer verify` put the lock in place for you: the pinned script goes into
`config/stplug-in`, the build's manifests go into `~/.config/SLSsteam/manifests`,
and the pin itself into `ManifestPins` in `~/.config/SLSsteam/config.yaml`.

Restart Steam afterwards. The game then shows an **Update** that is really the
downgrade - run it, wait until Steam lists the game as fully installed, then run
`tokeer verify <APPID>` again. No setup code is printed while the game is still
on the broken build. Once the game is on the locked build, the pin freezes it
there, so Steam will not pull the broken build back.

You can run it on its own with `tokeer lock <APPID>`. This needs luatools-moon /
slsteam-moon installed, since the pin is its feature.

## RE Engine games (Monster Hunter Wilds / Stories 3, etc.)

These also need REFramework's `dinput8.dll` in the **game folder** (the ticket
attaches it). Drop it in before running `tokeer <CODE>`. The launch option already
sets `WINEDLLOVERRIDES="dinput8=n,b"` so Proton loads it.

## Handy flags (advanced)

`tokeer` forwards everything to the redeemer:

```
tokeer <CODE> --no-launch          # write the ticket, don't start the game
tokeer <CODE> --appid 1570010      # force the app id
tokeer <CODE> --dry-run            # fetch + show the .reg, write nothing
tokeer <CODE> --server https://... # override backend (else ~/.tokeer/server_config.py)
```

Backend URL resolution: `--server` > `$TOKEER_DRM_SERVER` > `~/.tokeer/server_config.py`
(the installer writes the last one for you).

## Black screen on DirectX 12 games

If the game activates and launches but you only get a **black screen** (sound may
still play), that's the DirectX-to-Vulkan layer, not the activation. DX12 games
render through VKD3D-Proton, and some Proton versions ship a build that breaks on
certain GPUs (Proton 10-34 is a known bad one for this).

Fix it by switching the game's Proton version: right-click the game in Steam ->
Properties -> Compatibility -> pick a different version. **Proton GE** or
**Proton-CachyOS** usually work, or just try an older stable Proton. Restart the
game after changing it. The activation still holds, so you don't need to re-run
`tokeer <CODE>`.

## If a game still errors after `tokeer <CODE>`

The eticket write only matters if the native hook actually loads in the Proton
game process. Confirm the launch option is set (`~/.tokeer/ost-run.sh %command%`)
and check `~/ost_native_hook.log` for `GetEncryptedAppTicket SERVED`. If it's not
there, the hook isn't attaching - report the log and we go from there.
