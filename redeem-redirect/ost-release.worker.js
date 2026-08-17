/**
 * OpenSteamTool release proxy - Cloudflare Worker.
 *
 * Why this exists: TokeerDRM used to ask api.github.com for the latest
 * BetterSteamTools release on every engine check. GitHub allows 60
 * unauthenticated requests an hour and counts them PER IP, so users sharing an
 * ISP/CGNAT address burn the quota between them and the app shows
 * "Couldn't reach GitHub: HTTP Error 403: rate limit exceeded".
 *
 * With this worker the app talks to luastools.xyz instead. The worker asks
 * GitHub once per cache window and every user is served from Cloudflare's edge,
 * so no user is ever rate limited by GitHub again.
 *
 * Deploy:
 *   1. Cloudflare -> Workers & Pages -> Create -> Worker -> name it
 *      `ost-release` -> paste this file -> Deploy.
 *   2. Worker -> Settings -> Domains & Routes -> Add -> Route:
 *        luastools.xyz/ost/*     (zone luastools.xyz)
 *   3. STRONGLY recommended: Settings -> Variables -> Add secret
 *        GITHUB_TOKEN = <a fine-grained PAT with public repo read access>
 *      Cloudflare's egress IPs are shared, so an unauthenticated worker can
 *      itself get rate limited. A token raises the worker's own ceiling to
 *      5000/hour, and caching means it needs only a handful of those.
 *   4. Test:
 *        curl https://luastools.xyz/ost/latest
 *        curl -sI https://luastools.xyz/ost/dll | head -3
 *
 * Endpoints:
 *   GET /ost/latest       -> {ok, tag, hashes[], size, published_at}
 *   GET /ost/dll[?tag=v1] -> the OpenSteamTool.dll bytes (tag-pinned when given)
 */

const REPO = "madoiscool/BetterSteamTools";
const ASSET = "OpenSteamTool.dll";
const UA = "TokeerDRM-Worker";

// GitHub is asked at most once per META_TTL per colo; everyone else is served
// from the edge. A new release reaches users within this window.
const META_TTL = 900;      // 15 min
const DLL_TTL = 86400;     // a tag-pinned asset never changes, so hold it a day
const LATEST_DLL_TTL = 900; // the unpinned "latest" link has to track releases

function json(body, status = 200, ttl = 0) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": ttl ? `public, max-age=${ttl}` : "no-store",
      "access-control-allow-origin": "*",
    },
  });
}

function ghHeaders(env) {
  const h = {
    "User-Agent": UA,
    Accept: "application/vnd.github+json",
  };
  if (env && env.GITHUB_TOKEN) h.Authorization = `Bearer ${env.GITHUB_TOKEN}`;
  return h;
}

/**
 * Latest release metadata, trimmed to what the app actually needs.
 * Served from the edge cache; on a GitHub failure we fall back to the last good
 * copy so a rate limit or outage upstream never reaches the user.
 */
async function latestMeta(request, env, ctx) {
  const cache = caches.default;
  const key = new Request(new URL("/__ost/meta.json", request.url).toString(), {
    method: "GET",
  });

  const hit = await cache.match(key);
  if (hit) return hit;

  let res;
  try {
    res = await fetch(`https://api.github.com/repos/${REPO}/releases/latest`, {
      headers: ghHeaders(env),
    });
  } catch (e) {
    return json({ ok: false, error: `github unreachable: ${e}` }, 502);
  }

  if (!res.ok) {
    // Rate limited or upstream error and nothing cached: say so plainly, and
    // let the app fall back to talking to GitHub directly.
    const stale = await cache.match(key, { ignoreMethod: true });
    if (stale) return stale;
    return json(
      { ok: false, error: `github returned ${res.status}`, status: res.status },
      502
    );
  }

  const rel = await res.json();
  const hashes = [];
  let size = 0;
  for (const a of rel.assets || []) {
    if (!(a.name || "").toLowerCase().endsWith(".dll")) continue;
    if ((a.name || "").toLowerCase() === ASSET.toLowerCase()) size = a.size || 0;
    const dg = a.digest || "";
    if (dg.toLowerCase().startsWith("sha256:")) {
      hashes.push(dg.split(":")[1].trim().toLowerCase());
    }
  }

  const body = {
    ok: true,
    tag: rel.tag_name || "",
    hashes,
    size,
    published_at: rel.published_at || "",
    dll: new URL(`/ost/dll?tag=${encodeURIComponent(rel.tag_name || "")}`, request.url)
      .toString(),
  };

  const out = json(body, 200, META_TTL);
  ctx.waitUntil(cache.put(key, out.clone()));
  return out;
}

/** The DLL itself, proxied so the download never touches a per-user GitHub quota. */
async function dll(request, env) {
  const tag = new URL(request.url).searchParams.get("tag") || "";
  const pinned = Boolean(tag);
  const src = pinned
    ? `https://github.com/${REPO}/releases/download/${encodeURIComponent(tag)}/${ASSET}`
    : `https://github.com/${REPO}/releases/latest/download/${ASSET}`;

  const ttl = pinned ? DLL_TTL : LATEST_DLL_TTL;
  let res;
  try {
    res = await fetch(src, {
      headers: { "User-Agent": UA },
      cf: { cacheTtl: ttl, cacheEverything: true },
    });
  } catch (e) {
    return json({ ok: false, error: `download failed: ${e}` }, 502);
  }
  if (!res.ok) {
    return json({ ok: false, error: `github returned ${res.status}` }, res.status);
  }

  const headers = new Headers();
  headers.set("content-type", "application/octet-stream");
  headers.set("cache-control", `public, max-age=${ttl}`);
  headers.set("access-control-allow-origin", "*");
  const len = res.headers.get("content-length");
  if (len) headers.set("content-length", len);
  headers.set("content-disposition", `attachment; filename="${ASSET}"`);
  return new Response(res.body, { status: 200, headers });
}

export default {
  async fetch(request, env, ctx) {
    const { pathname } = new URL(request.url);
    if (request.method !== "GET" && request.method !== "HEAD") {
      return json({ ok: false, error: "method not allowed" }, 405);
    }
    if (pathname === "/ost/latest") return latestMeta(request, env, ctx);
    if (pathname === "/ost/dll") return dll(request, env);
    return json({ ok: false, error: "not found" }, 404);
  },
};
