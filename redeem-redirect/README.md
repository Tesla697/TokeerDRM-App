# TokeerDRM one-click redeem redirect

Turns the Discord **🚀 Redeem in TokeerDRM App** button into a one-click activation.

```
Discord button
  → https://luastools.xyz/redeem?code=ABC123      (clean https, no server IP)
  → tokeerdrm://redeem?code=ABC123                 (custom scheme this page bounces to)
  → TokeerDRM opens, auto-applies the code, auto-fixes engine/DLL, launches the game
```

The bot already links to `https://luastools.xyz/redeem?code=<code>` — you just need this page live at that path.

## Option A — Cloudflare Worker (recommended, domain already on Cloudflare)

1. Cloudflare dashboard → **Workers & Pages** → **Create** → **Worker** → name it e.g. `tokeerdrm-redeem`.
2. **Edit code** → paste the contents of [`worker.js`](worker.js) → **Deploy**.
3. Back on the Worker → **Settings → Domains & Routes → Add → Route**:
   - Route: `luastools.xyz/redeem*`
   - Zone: `luastools.xyz`
4. Test: open `https://luastools.xyz/redeem?code=ABC123` in a browser — it should show the "Opening TokeerDRM…" card and (if the app is installed) prompt to open it.

## Option B — Cloudflare Pages (static)

1. Put [`index.html`](index.html) in a folder as `redeem/index.html`.
2. Deploy that folder as a Pages project, add `luastools.xyz` as a custom domain.
3. It serves at `https://luastools.xyz/redeem?code=…`.

## Don't-have-the-app flow (auto-download)

If the click doesn't open the app within ~2.5s (the browser tab never loses focus →
the `tokeerdrm://` scheme isn't registered → app not installed), the page:

1. Auto-starts the app download from `APP_EXE`.
2. Switches to an "install" card: **Run the file**, then click **Activate my game**
   (which re-fires the deep link — the code is preserved in the page URL).

> A web page **cannot** silently install an .exe (SmartScreen/UAC require the user to
> run it). So the non-installed path is: auto-download → user runs it once → one click
> to activate. That's the most seamless Windows allows.

**Requirement:** the release must attach the exe as **`TokeerDRM.exe`** so
`…/releases/latest/download/TokeerDRM.exe` resolves. If you rename it, update `APP_EXE`
in `worker.js` / `index.html`.

## Notes

- The custom scheme `tokeerdrm://` is registered per-user by the app itself on first launch
  (`_register_uri_scheme()` in `tokeer_drm.py`) — no admin, no installer step.
- A browser can't 302 straight to `tokeerdrm://`, so this page sets `location.href` in JS instead.
- Change the download links (`APP_DOWNLOAD` / `APP_EXE`) if the releases repo ever moves.

## luastools.xyz route map

The zone is Workers-only, and each route belongs to exactly one Worker. Adding a
route that overlaps another one silently breaks it, so keep these separate:

| Route | Worker | File |
|---|---|---|
| `luastools.xyz/redeem*` | redeem redirect (HTML bounce to `tokeerdrm://`) | `worker.js` |
| `luastools.xyz/drm/*` and `/quota*` | code-store reverse proxy | `tokeer-api-proxy.worker.js` |
| `luastools.xyz/privacy*` | public privacy policy | `privacy.worker.js` |

`/privacy` exists because Discord's privileged-intent review (Guild Members +
Message Content) requires a public policy URL that a reviewer can open while
signed out. Its text mirrors `danny/privacy.html` — edit there first, then copy
into the Worker so the two cannot drift.

## Enhanced-DLL mirror (`ost-release.worker.js`)

Fixes the "Couldn't reach GitHub: HTTP Error 403: rate limit exceeded" banner in
TokeerDRM. GitHub allows 60 unauthenticated API requests an hour **per IP**, so
users sharing an ISP/CGNAT address run out between them. This worker asks GitHub
once per cache window and serves every user from Cloudflare's edge, so no user
spends their own quota.

1. Workers & Pages -> Create -> Worker -> name it `ost-release` -> paste
   [`ost-release.worker.js`](ost-release.worker.js) -> Deploy.
2. Settings -> Domains & Routes -> Add -> Route: `luastools.xyz/ost/*`
   (zone `luastools.xyz`).
3. Settings -> Variables -> add a secret `GITHUB_TOKEN` (fine-grained PAT, public
   repo read). Cloudflare's egress IPs are shared, so without a token the worker
   itself can get rate limited. With one it gets 5000/hour and caching means it
   needs only a few.
4. Test:

```
curl https://luastools.xyz/ost/latest
curl -sI "https://luastools.xyz/ost/asset?tag=v1.0.0&name=OpenSteamTool-v1.0.0-Release.zip"
```

`/ost/latest` mirrors the GitHub release JSON (`tag_name` + `assets[]`) with each
asset URL rewritten to `/ost/asset?tag=&name=`, so the app's existing logic works
unchanged. This repo ships the DLL inside `OpenSteamTool-<tag>-Release.zip` rather
than as a bare `.dll`, which the app already knows how to unpack. The app prefers this mirror and falls back to GitHub automatically if
it's unreachable, so a worker outage degrades to the old behaviour instead of
breaking installs.
