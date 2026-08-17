/**
 * OpenSteamTool release mirror - Cloudflare Worker.
 *
 * Why this exists: TokeerDRM used to ask api.github.com for the latest
 * BetterSteamTools release on every engine check. GitHub allows 60
 * unauthenticated requests an hour and counts them PER IP, so users sharing an
 * ISP/CGNAT address burn the quota between them and the app shows
 * "Couldn't reach GitHub: HTTP Error 403: rate limit exceeded".
 *
 * With this worker the app talks to luastools.xyz instead. The worker asks
 * GitHub once per cache window and every user is served from Cloudflare's edge,
 * so no user spends their own GitHub quota.
 *
 * /ost/latest is a drop-in mirror of the GitHub "latest release" JSON, with each
 * asset's browser_download_url rewritten to point back at /ost/asset. The app
 * therefore keeps using its existing logic (pick the Release zip, read the
 * sha256 digests, extract OpenSteamTool.dll) and simply never touches GitHub.
 *
 * Deploy:
 *   1. Cloudflare -> Workers & Pages -> Create -> Worker -> name it
 *      `ost-release` -> paste this file -> Deploy.
 *   2. Worker -> Settings -> Domains & Routes -> Add -> Route:
 *        luastools.xyz/ost/*     (zone luastools.xyz)
 *   3. Recommended: Settings -> Variables -> Add secret
 *        GITHUB_TOKEN = <classic PAT with NO scopes ticked>
 *      Cloudflare's egress IPs are shared, so an unauthenticated worker can
 *      itself get rate limited. A no-scope token lifts it to 5000/hour and can
 *      read nothing that isn't already public.
 *   4. Test:
 *        curl https://luastools.xyz/ost/latest
 *        curl -sI "https://luastools.xyz/ost/asset?tag=v1.0.0&name=OpenSteamTool-v1.0.0-Release.zip"
 *
 * Endpoints:
 *   GET /ost/latest                -> mirrored release JSON
 *   GET /ost/asset?tag=&name=      -> that release asset's bytes (tag-pinned)
 */

const REPO = "madoiscool/BetterSteamTools";
const UA = "TokeerDRM-Worker";

// GitHub is asked at most once per META_TTL per colo; everyone else is served
// from the edge. A new release reaches users within this window.
const META_TTL = 900;      // 15 min
const ASSET_TTL = 86400;   // assets are tag-pinned and never change, hold a day

// Asset names come from the URL, so keep them to the shape GitHub actually uses.
// This is what stops the endpoint being turned into an open proxy.
const SAFE_NAME = /^[A-Za-z0-9._-]{1,120}$/;
const SAFE_TAG = /^[A-Za-z0-9._-]{1,64}$/;

function json(body, status = 200, ttl = 0, extra = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": ttl ? `public, max-age=${ttl}` : "no-store",
      "access-control-allow-origin": "*",
      ...extra,
    },
  });
}

function ghHeaders(env, withToken = true) {
  const h = { "User-Agent": UA, Accept: "application/vnd.github+json" };
  if (withToken && env && env.GITHUB_TOKEN) {
    h.Authorization = `Bearer ${env.GITHUB_TOKEN}`;
  }
  return h;
}

/**
 * Ask GitHub, and if a token is configured but rejected (401, or a 403 that isn't
 * a plain rate limit) retry once anonymously. An expired or mistyped secret should
 * cost us the higher quota, not take the whole mirror down.
 */
async function askGitHub(url, env) {
  let res = await fetch(url, { headers: ghHeaders(env) });
  const used = Boolean(env && env.GITHUB_TOKEN);
  if (used && (res.status === 401 || res.status === 403)) {
    const retry = await fetch(url, { headers: ghHeaders(env, false) });
    if (retry.ok) return { res: retry, auth: "rejected" };
    return { res, auth: "rejected" };
  }
  return { res, auth: used ? "token" : "anonymous" };
}

/**
 * The latest release, trimmed to the fields the app reads and with download URLs
 * pointed back at this worker. Cached at the edge; if GitHub fails and we still
 * hold a cached copy we serve that, so an upstream rate limit never reaches a user.
 */
async function latestRelease(request, env, ctx) {
  const cache = caches.default;
  const key = new Request(new URL("/__ost/latest.json", request.url).toString());

  const hit = await cache.match(key);
  if (hit) {
    const h = new Response(hit.body, hit);
    h.headers.set("x-ost-cache", "HIT");
    return h;
  }

  let res, auth;
  try {
    ({ res, auth } = await askGitHub(
      `https://api.github.com/repos/${REPO}/releases/latest`,
      env
    ));
  } catch (e) {
    return json({ ok: false, error: `github unreachable: ${e}` }, 502);
  }
  if (!res.ok) {
    return json(
      { ok: false, error: `github returned ${res.status}`, status: res.status },
      502,
      0,
      { "x-ost-cache": "MISS", "x-ost-auth": auth }
    );
  }

  // Surfaced so the deployment can be checked from outside: x-gh-limit reads 60
  // when we're anonymous and 5000 when the token is accepted.
  const diag = {
    "x-ost-cache": "MISS",
    "x-ost-auth": auth,
    "x-gh-limit": res.headers.get("x-ratelimit-limit") || "?",
    "x-gh-remaining": res.headers.get("x-ratelimit-remaining") || "?",
  };

  const rel = await res.json();
  const tag = rel.tag_name || "";
  const base = new URL(request.url).origin;
  const assets = (rel.assets || []).map((a) => ({
    name: a.name,
    size: a.size,
    digest: a.digest || "",
    // Same shape the GitHub API uses, so the app reads it without changes.
    browser_download_url:
      `${base}/ost/asset?tag=${encodeURIComponent(tag)}` +
      `&name=${encodeURIComponent(a.name)}`,
  }));

  const out = json(
    { ok: true, tag_name: tag, published_at: rel.published_at || "", assets },
    200,
    META_TTL,
    diag
  );
  ctx.waitUntil(cache.put(key, out.clone()));
  return out;
}

/** One release asset, proxied so downloads never touch a per-user GitHub quota. */
async function asset(request) {
  const q = new URL(request.url).searchParams;
  const tag = q.get("tag") || "";
  const name = q.get("name") || "";
  if (!SAFE_TAG.test(tag) || !SAFE_NAME.test(name)) {
    return json({ ok: false, error: "bad tag or name" }, 400);
  }

  const src =
    `https://github.com/${REPO}/releases/download/` +
    `${encodeURIComponent(tag)}/${encodeURIComponent(name)}`;

  let res;
  try {
    res = await fetch(src, {
      headers: { "User-Agent": UA },
      cf: { cacheTtl: ASSET_TTL, cacheEverything: true },
    });
  } catch (e) {
    return json({ ok: false, error: `download failed: ${e}` }, 502);
  }
  if (!res.ok) {
    return json({ ok: false, error: `github returned ${res.status}` }, res.status);
  }

  const headers = new Headers();
  headers.set("content-type", "application/octet-stream");
  headers.set("cache-control", `public, max-age=${ASSET_TTL}`);
  headers.set("access-control-allow-origin", "*");
  headers.set("content-disposition", `attachment; filename="${name}"`);
  const len = res.headers.get("content-length");
  if (len) headers.set("content-length", len);
  return new Response(res.body, { status: 200, headers });
}

export default {
  async fetch(request, env, ctx) {
    const { pathname } = new URL(request.url);
    if (request.method !== "GET" && request.method !== "HEAD") {
      return json({ ok: false, error: "method not allowed" }, 405);
    }
    if (pathname === "/ost/latest") return latestRelease(request, env, ctx);
    if (pathname === "/ost/asset") return asset(request);
    return json({ ok: false, error: "not found" }, 404);
  },
};
