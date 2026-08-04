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
 *   a. DNS -> add an A record  api  ->  31.57.38.79  (Proxied / orange cloud).
 *   b. Rules -> Origin Rules -> Create: if hostname eq `api.luastools.xyz`,
 *      set Origin Port = 8091  (the box listens on 8091).
 *   c. SSL/TLS -> Overview -> set to "Flexible" (the box speaks plain HTTP on
 *      8091, so Cloudflare must connect to the origin over HTTP). luastools.xyz
 *      is Workers-only otherwise, so Flexible is safe zone-wide here.
 * Then this Worker fetches https://api.luastools.xyz on 443 (allowed) and
 * Cloudflare reaches the box on :8091. The real IP stays hidden behind the proxy.
 */

const ORIGIN = "https://api.luastools.xyz";

// Only these path prefixes are allowed through, so the proxy can never be used
// as an open relay to arbitrary origin paths.
const ALLOW = ["/drm/", "/quota"];

export default {
  async fetch(request) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors() });
    }
    if (!ALLOW.some((p) => url.pathname === p || url.pathname.startsWith(p))) {
      return json({ success: false, error: "not found" }, 404);
    }

    const target = ORIGIN + url.pathname + url.search;
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
      return json({ success: false, error: "code store unreachable" }, 502);
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
