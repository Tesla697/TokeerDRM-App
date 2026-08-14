# TokeerDRM

Desktop app for redeeming and generating TokeerDRM codes.

Two things it does:

**Activate** - paste a 6 character code, it writes the AppTicket and ETicket to your
registry, then you launch the game from Steam.

**Generate** - type a game's Steam AppID and get a shareable code, as long as your
signed in account actually owns the game. The server checks the Steam signature on
the ownership ticket, so codes for games you don't own get rejected.

UI is HTML/CSS/JS in a WebView2 window via pywebview. The Python side runs
`extract_tickets.exe`, writes the registry and talks to the code store.

## Requirements

Windows 10 or 11 with the Edge WebView2 runtime. Win11 already has it, on older
systems grab it from Microsoft (it's free).

Steam has to be running and signed in for Generate, since the extractor pulls the
ticket out of the live session. Redeeming works without it.

`extract_tickets.exe` needs to sit next to `tokeer_drm.py`. It's bundled into the
built exe already.

## Running it

```bat
pip install -r requirements.txt
python tokeer_drm.py
```

## Building

```bat
pip install -r requirements.txt pyinstaller
build.bat
```

You get `dist\TokeerDRM.exe`, single file, no console window.

## Notes

Copy `server_config.example.py` to `server_config.py` and put your code store URL
in it.

Codes are single use and the Steam ticket behind them only lasts about half an
hour, so redeem and launch around the same time. If you leave it too long you'll
get 88500005 and need a fresh one.
