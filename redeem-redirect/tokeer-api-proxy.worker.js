/**
 * Tokeer code-store reverse proxy - Cloudflare Worker.
 *
 * Purpose: let the Linux bundle (and anything else) reach the DRM code store
 * over a clean HTTPS domain instead of the raw origin IP. The redeemer calls
 *     https://luastools.xyz/drm/redeem
 * and this worker forwards it to the code-store box, hiding the IP + adding TLS.
 *
 * Deploy:
 *   1. Cloudflare -> Workers & Pages -> Create -> Worker -> name it e.g.
 *      `tokeer-api-proxy` -> paste this file -> Deploy.
 *   2. Worker -> Settings -> Domains & Routes -> Add -> Route, one row each:
 *        luastools.xyz/drm/*     (zone luastools.xyz)
 *        luastools.xyz/quota*    (zone luastools.xyz)
 *      Do NOT add /redeem* here - that stays on the existing redeem-redirect
 *      worker (the HTML bounce page). This proxy only owns /drm/* and /quota*.
 *   3. Test:  curl -X POST https://luastools.xyz/drm/redeem \
 *               -H 'content-type: application/json' -d '{"code":"ABCDEF"}'
 *      A JSON error like {"success":false,...} means the proxy reached the
 *      origin (good). A Cloudflare 5xx HTML page means the route/origin is off.
 *
 * ORIGIN must be a HOSTNAME, not a raw IP: a Worker fetch() to http://<ip>:port
 * is rejected by Cloudflare with "error code: 1003". So front the code-store box
 * with a Cloudflare hostname and point ORIGIN at that:
 *   a. DNS -> add an A record  codestore  ->  31.57.38.79 (Proxied / orange cloud).
 *   b. Rules -> Origin Rules -> Create: if hostname eq `codestore.luastools.xyz`,
 *      set Origin Port = 8091  (the box listens on 8091).
 *   c. SSL/TLS -> Overview -> set to "Flexible" (the box speaks plain HTTP on
 *      8091, so Cloudflare must connect to the origin over HTTP). luastools.xyz
 *      is Workers-only otherwise, so Flexible is safe zone-wide here.
 * Then this Worker fetches https://codestore.luastools.xyz on 443 (allowed) and
 * Cloudflare reaches the box on :8091. The real IP stays hidden behind the proxy.
 * (`api` already had a CNAME, so we use `codestore`; any free name works as long
 *  as ORIGIN, the DNS record, and the Origin Rule all use the same one.)
 */

// Path prefix -> origin. Both origins are the SAME box (31.57.38.79) on
// DIFFERENT ports, and a Cloudflare Origin Rule can only set the port per
// hostname, so each port needs its own hostname:
//
//   codestore.luastools.xyz -> :8091  code store  (/drm/*, /quota*)
//   backend.luastools.xyz   -> :8080  Tokeer API  (/eticket)
//
// backend goes over http: Cloudflare talks https to that origin and the box only
// speaks plain http on 8080, so https here fails with a 525 every time.
//
// Anything not matching a prefix here is refused, so the proxy can never be
// used as an open relay to arbitrary origin paths.
const ROUTES = [
  { prefixes: ["/drm/", "/quota"], origin: "https://codestore.luastools.xyz" },
  { prefixes: ["/eticket"],        origin: "http://backend.luastools.xyz" },
];

function resolveOrigin(pathname) {
  for (const r of ROUTES) {
    if (r.prefixes.some((p) => pathname === p || pathname.startsWith(p))) {
      return r.origin;
    }
  }
  return null;
}

export default {
  async fetch(request) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors() });
    }
    const origin = resolveOrigin(url.pathname);
    if (!origin) {
      return json({ success: false, error: "not found" }, 404);
    }

    const target = origin + url.pathname + url.search;
    const init = {
      method: request.method,
      headers: passHeaders(request.headers),
      body: ["GET", "HEAD"].includes(request.method) ? undefined : await request.arrayBuffer(),
      redirect: "manual",
    };

    let resp;
    try {
      resp = await fetch(target, init);
    } catch (e) {
      return json({ success: false, error: "origin unreachable" }, 502);
    }

    // Re-emit with permissive CORS so the redeemer (and a browser, if ever) can read it.
    const out = new Headers(resp.headers);
    for (const [k, v] of Object.entries(cors())) out.set(k, v);
    out.delete("content-encoding"); // body is already decoded by fetch()
    return new Response(resp.body, { status: resp.status, headers: out });
  },
};

function passHeaders(h) {
  const out = new Headers();
  for (const k of ["content-type", "accept", "user-agent"]) {
    const v = h.get(k);
    if (v) out.set(k, v);
  }
  // Forward the real visitor IP so the origin can rate-limit per user. A Worker
  // subrequest to a proxied origin otherwise arrives from a Cloudflare edge IP,
  // making every visitor look like one client. CF-Connecting-IP is the true
  // visitor; pass it as X-Forwarded-For (fall back to any inbound XFF).
  const ip = h.get("CF-Connecting-IP") || h.get("X-Forwarded-For");
  if (ip) out.set("X-Forwarded-For", ip);
  return out;
}

function cors() {
  return {
    "access-control-allow-origin": "*",
    "access-control-allow-methods": "GET,POST,OPTIONS",
    "access-control-allow-headers": "content-type",
  };
}

function json(obj, status) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json", ...cors() },
  });
}
