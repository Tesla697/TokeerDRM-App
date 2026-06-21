# TokeerDRM — Standalone App

A native desktop version of the TokeerDRM Millennium plugin, for people who don't
use Millennium. Same engine, same server, same codes — just outside Steam.

- **Activate** — paste a 6-character code → the AppTicket + ETicket are written to
  your registry → launch the game from Steam.
- **Generate** — enter a game's Steam AppID → if your signed-in account owns it, you
  get a shareable code (no install or launch needed). Codes you don't own are
  refused by the server (real Steam-signature check).

The UI is HTML/CSS/JS in a native WebView2 window (pywebview); the Python side runs
`extract_tickets.exe`, writes the registry, and talks to the code-store server.

## Requirements
- **Windows 10/11** with the **Edge WebView2 runtime** (preinstalled on Win11; on
  older systems install it free from Microsoft).
- **Steam running and signed in** — required for **Generate** (the extractor reads
  your account's ticket from the live Steam session). Redeeming doesn't need it.
- `extract_tickets.exe` must sit next to `tokeer_drm.py` (and is bundled into the
  built `.exe`).

## Run from source
```bat
pip install -r requirements.txt
python tokeer_drm.py
```

## Build a single .exe
```bat
pip install -r requirements.txt pyinstaller
build.bat
```
Output: `dist\TokeerDRM.exe` — a single, no-console file you can share.

## Notes
- Server: set your code-store URL in `server_config.py` (copy `server_config.example.py`).
- Generate only works for games your account **genuinely owns** — the server
  verifies the App Ownership Ticket's Steam signature, so spoofed/unlocker games
  are rejected with "you don't own this game."
- Whoever redeems a code should apply it and launch reasonably soon (the underlying
  Steam ticket is short-lived).
