/**
 * LuaTools Validator download mirror - Cloudflare Worker.
 *
 * Deploy on luastools.xyz with a route like:  luastools.xyz/validator.exe*
 *
 * The installer (lt_api_links/LuaToolsValidator.ps1) tries this first:
 *     https://luastools.xyz/validator.exe
 * The worker fetches the latest release exe from GitHub server-side and streams
 * it back. Some networks mangle the direct github.com TLS handshake ("The
 * connection was closed unexpectedly"); fetching from Cloudflare's edge instead
 * of the user's ISP sidesteps that. Falls through to the github mirrors in the
 * installer if this is ever unavailable. Same idea as ost-release.worker.js.
 */

const EXE = "https://github.com/Tesla697/LuaToolsValidator/releases/latest/download/LuaToolsValidator.exe";

export default {
  async fetch(request) {
    let upstream;
    try {
      upstream = await fetch(EXE, { redirect: "follow", cf: { cacheEverything: true, cacheTtl: 300 } });
    } catch (e) {
      return new Response("validator mirror fetch failed: " + e, { status: 502 });
    }
    if (!upstream.ok || !upstream.body) {
      return new Response("validator mirror upstream error " + upstream.status, { status: 502 });
    }
    const headers = new Headers();
    headers.set("content-type", "application/octet-stream");
    headers.set("content-disposition", 'attachment; filename="LuaToolsValidator.exe"');
    headers.set("cache-control", "public, max-age=300");
    const len = upstream.headers.get("content-length");
    if (len) headers.set("content-length", len);
    return new Response(upstream.body, { status: 200, headers });
  },
};
