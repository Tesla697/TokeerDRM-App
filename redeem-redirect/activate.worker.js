/**
 * LuaTools "Activate Automatically" redirect - Cloudflare Worker.
 *
 * Deploy on luastools.xyz with a route like:  luastools.xyz/activate*
 *
 * The Discord "Activate Automatically" button links here:
 *     https://luastools.xyz/activate?appid=123&platform=windows&wh=<url-encoded webhook>
 * This page bounces the browser to the LuaTools Validator's custom scheme:
 *     luavalidator://activate?appid=123&wh=<url-encoded webhook>
 * (distinct from the LuaTools app's luatools:// so the two never collide)
 * which opens the validator, runs the validation for that AppID, and posts the
 * D-Report code back to the ticket through the per-ticket webhook. If the
 * validator isn't installed, the page shows the one-line PowerShell installer.
 *
 * We can't 302 to a custom scheme (browsers only follow http(s) on a redirect),
 * so we serve a tiny HTML page that sets location.href in JS. Mirrors worker.js.
 */

const PS1 = "https://luatools.vercel.app/LuaToolsValidator.ps1";

const htmlEscape = (s) => String(s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const appid = (url.searchParams.get("appid") || "").replace(/[^0-9]/g, "").slice(0, 12);
    const wh = url.searchParams.get("wh") || "";

    // Only bounce to the app when the webhook is a real Discord webhook URL, so
    // this page can't be used to point the validator at an arbitrary endpoint.
    const whOk = /^https:\/\/(discord|discordapp)\.com\/api\/webhooks\//i.test(wh);
    const deep = (appid && whOk)
      ? `luavalidator://activate?appid=${appid}&wh=${encodeURIComponent(wh)}`
      : "";

    // First-timers (no validator yet, so the deep link can't resolve) run this one
    // line: it installs the validator if missing, then launches it with the AppID +
    // ticket webhook so it validates and posts the D-Report back on its own. No
    // manual app opening. (Webhook tokens are URL-safe, so single quotes are fine.)
    const installCmd = (appid && whOk)
      ? `$AppID='${appid}'; $LuaValidatorWebhook='${wh}'; irm '${PS1}' | iex`
      : `irm '${PS1}' | iex`;

    const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Opening LuaTools Validator…</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
    background: radial-gradient(1200px 600px at 50% -10%, #16233b 0%, #0b0f17 60%); color: #e7ecf5;
    padding: 24px;
  }
  .card {
    width: 100%; max-width: 460px; background: #121826; border: 1px solid #223049;
    border-radius: 18px; padding: 30px 28px; text-align: center;
    box-shadow: 0 24px 60px rgba(0,0,0,.45);
  }
  .logo { font-size: 30px; margin-bottom: 6px; }
  h1 { font-size: 20px; margin: 6px 0 4px; }
  p { color: #9db0cc; font-size: 14px; line-height: 1.5; margin: 8px 0; }
  .code {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 13px; color: #fff; text-align: left;
    background: #0c1220; border: 1px dashed #2c3d5c; border-radius: 12px;
    padding: 14px 12px; margin: 16px 0; user-select: all; word-break: break-all;
  }
  .btn {
    display: inline-block; width: 100%; padding: 13px 16px; margin-top: 10px;
    border-radius: 12px; border: 0; cursor: pointer; font-size: 15px; font-weight: 600;
    text-decoration: none; transition: filter .15s ease;
  }
  .btn:hover { filter: brightness(1.08); }
  .btn.primary { background: linear-gradient(180deg, #4b7bec, #3f6fe0); color: #fff; }
  .muted { font-size: 12.5px; color: #6f819e; margin-top: 16px; }
  .err { color: #ff9c9c; }
</style>
</head>
<body>
  <div class="card">
    <div class="logo">⚡</div>
    ${deep ? `
      <div id="main">
        <h1>Opening LuaTools Validator…</h1>
        <p>If you have the app, it's opening now and will validate <b>AppID ${appid}</b>, then post your D-Report back to your ticket automatically. If Windows asks, choose <b>Open LuaTools Validator</b>.</p>
        <a class="btn primary" href="${deep}" id="openBtn">Open LuaTools Validator</a>
        <p class="muted">First time / don't have it? Open <b>PowerShell</b> and run this once. It installs the validator, then runs and submits your code automatically:</p>
        <div class="code">${htmlEscape(installCmd)}</div>
      </div>
      <div id="mobile" hidden>
        <h1>🖥️ Open this on your PC</h1>
        <p>LuaTools Validator runs on <b>Windows</b>. On the computer where you play, click <b>Activate Automatically</b> in your ticket again.</p>
      </div>
    ` : `
      <h1 class="err">Invalid link</h1>
      <p>This activation link is missing a valid AppID or ticket webhook. Head back to your ticket and click <b>Activate Automatically</b> again, or follow the manual steps there.</p>
    `}
  </div>
  ${deep ? `<script>
    (function () {
      var deep = ${JSON.stringify(deep)};
      var mainEl = document.getElementById("main");
      var mobileEl = document.getElementById("mobile");
      if (/android|iphone|ipad|ipod|iemobile|mobile/i.test(navigator.userAgent)) {
        if (mainEl) mainEl.hidden = true;
        if (mobileEl) mobileEl.hidden = false;
        return;
      }
      setTimeout(function () { try { window.location.href = deep; } catch (e) {} }, 400);
    })();
  </script>` : ""}
</body>
</html>`;

    return new Response(html, {
      headers: {
        "content-type": "text/html; charset=utf-8",
        "cache-control": "no-store, max-age=0",
      },
    });
  },
};
