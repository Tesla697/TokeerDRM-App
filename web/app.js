"use strict";

// ── pywebview bridge ──────────────────────────────────────────────────────────
let API = null;
function whenReady(fn) {
  if (window.pywebview && window.pywebview.api) return fn();
  window.addEventListener("pywebviewready", fn, { once: true });
}
const call = async (method, ...args) => {
  if (!API) throw new Error("Bridge not ready");
  return API[method](...args);
};

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

// ── toast ─────────────────────────────────────────────────────────────────────
function toast(msg, kind = "ok") {
  const wrap = $("#toastWrap");
  const t = document.createElement("div");
  t.className = `toast ${kind}`;
  t.innerHTML = `<span class="ic"></span><span>${msg}</span>`;
  wrap.appendChild(t);
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 350); }, 2600);
}

const CHECK_SVG = `<svg class="check" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="m8 12 2.5 2.5L16 9"/></svg>`;

function setResult(el, kind, html) {
  el.className = `result ${kind}`;
  el.innerHTML = (kind === "ok" ? CHECK_SVG : "") + `<span>${html}</span>`;
}
function clearResult(el) { el.className = "result"; el.innerHTML = ""; }

function loading(btn, on) {
  btn.classList.toggle("loading", on);
  btn.disabled = on;
}

// ── tabs ────────────────────────────────────────────────────────────────────────
function initTabs() {
  const glider = $("#glider");
  const tabs = $$(".tab");
  const move = (tab) => { glider.style.left = `${tab.offsetLeft}px`; glider.style.width = `${tab.offsetWidth}px`; };
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      tabs.forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      const name = tab.dataset.tab;
      move(tab);
      $$(".panel").forEach((p) => p.classList.toggle("show", p.dataset.panel === name));
    });
  });
  const active = $(".tab.active") || tabs[0];
  if (active) requestAnimationFrame(() => move(active));
  window.addEventListener("resize", () => { const a = $(".tab.active"); if (a) move(a); });
}

// ── 6-box code input ──────────────────────────────────────────────────────────
function initCodeBoxes() {
  const boxes = $$(".cbox", $("#codeBoxes"));
  const sanitize = (v) => v.toUpperCase().replace(/[^A-Z0-9]/g, "");

  boxes.forEach((box, i) => {
    box.addEventListener("input", () => {
      box.value = sanitize(box.value).slice(0, 1);
      box.classList.toggle("filled", !!box.value);
      if (box.value && i < boxes.length - 1) boxes[i + 1].focus();
      if (getCode().length === 6) $("#applyBtn").focus();
    });
    box.addEventListener("keydown", (e) => {
      if (e.key === "Backspace" && !box.value && i > 0) { boxes[i - 1].focus(); boxes[i - 1].value = ""; boxes[i - 1].classList.remove("filled"); }
      if (e.key === "ArrowLeft" && i > 0) boxes[i - 1].focus();
      if (e.key === "ArrowRight" && i < boxes.length - 1) boxes[i + 1].focus();
      if (e.key === "Enter") $("#applyBtn").click();
    });
    box.addEventListener("paste", (e) => {
      e.preventDefault();
      const txt = sanitize((e.clipboardData || window.clipboardData).getData("text")).slice(0, 6);
      [...txt].forEach((ch, k) => { if (boxes[k]) { boxes[k].value = ch; boxes[k].classList.add("filled"); } });
      (boxes[txt.length] || boxes[5]).focus();
    });
  });
}
function getCode() { return $$(".cbox", $("#codeBoxes")).map((b) => b.value).join(""); }
function clearCode() { $$(".cbox", $("#codeBoxes")).forEach((b) => { b.value = ""; b.classList.remove("filled"); }); }

// ── status pill ─────────────────────────────────────────────────────────────────
async function refreshStatus() {
  const pill = $("#statusPill"), txt = $("#statusText");
  try {
    const s = await call("status");
    pill.classList.toggle("online", s.online);
    pill.classList.toggle("offline", !s.online);
    txt.textContent = s.online ? "online" : "offline";
  } catch {
    pill.classList.add("offline"); txt.textContent = "offline";
  }
}

// ── game-name lookup (debounced) ──────────────────────────────────────────────
let nameTimer = null;
function initGameName() {
  const input = $("#appIdInput"), label = $("#gameName");
  input.addEventListener("input", () => {
    input.value = input.value.replace(/[^0-9]/g, "");
    label.classList.remove("show"); label.textContent = "";
    clearTimeout(nameTimer);
    const id = input.value.trim();
    if (id.length < 3) return;
    nameTimer = setTimeout(async () => {
      try {
        const r = await call("app_name", id);
        if (r.name && input.value.trim() === id) { label.textContent = r.name; label.classList.add("show"); }
      } catch {}
    }, 450);
  });
}

// ── redeem ────────────────────────────────────────────────────────────────────
function initRedeem() {
  const btn = $("#applyBtn"), out = $("#redeemResult");
  btn.addEventListener("click", async () => {
    const code = getCode();
    if (code.length !== 6) { setResult(out, "err", "Enter all 6 characters."); return; }
    clearResult(out); loading(btn, true);
    try {
      const r = await call("redeem", code);
      if (r.ok) {
        setResult(out, "ok", `Ticket applied for app ${r.app_id}. Launch the game from Steam within 30 min.`);
        toast("Ticket applied — launch within 30 min", "ok");
        clearCode();
      } else {
        setResult(out, "err", r.error || "Something went wrong.");
        toast(r.error || "Redeem failed", "err");
      }
    } catch (e) {
      setResult(out, "err", String(e));
    }
    loading(btn, false);
  });
}

// ── generate ────────────────────────────────────────────────────────────────────
function initGenerate() {
  const btn = $("#genBtn"), reveal = $("#codeReveal"), out = $("#genResult");
  btn.addEventListener("click", async () => {
    const appId = $("#appIdInput").value.trim();
    const maxUses = 1; // codes are single-use (enforced server-side)
    if (!appId) { setResult(out, "err", "Enter a Steam AppID."); return; }
    clearResult(out); reveal.innerHTML = ""; loading(btn, true);
    try {
      const r = await call("generate", appId, maxUses);
      if (r.ok) {
        reveal.innerHTML = `
          <div class="code-card">
            <span class="code-val">${r.code}</span>
            <button class="copy-btn" id="copyBtn">
              <svg viewBox="0 0 24 24"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>
              Copy
            </button>
          </div>
          <div class="code-meta">Single-use · expires in 30 min · share it now</div>`;
        const copyBtn = $("#copyBtn");
        copyBtn.addEventListener("click", async () => {
          try { await navigator.clipboard.writeText(r.code); } catch {}
          copyBtn.innerHTML = "✓ Copied"; setTimeout(() => {
            copyBtn.innerHTML = `<svg viewBox="0 0 24 24"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg> Copy`;
          }, 1400);
        });
        toast("Code generated", "ok");
      } else {
        setResult(out, "err", r.error || "Couldn't generate a code.");
        toast(r.error && /own/i.test(r.error) ? "You don't own this game" : "Generate failed", "err");
      }
    } catch (e) {
      setResult(out, "err", String(e));
    }
    loading(btn, false);
  });
}

// ── engine (OpenSteamTool) — banner + management panel ────────────────────────
function showEngineBanner(msg) {
  if (msg) $("#engineBannerMsg").textContent = msg;
  $("#engineBanner").hidden = false;
}
function hideEngineBanner() { $("#engineBanner").hidden = true; }

function setRedeemEnabled(on) {
  const btn = $("#applyBtn");
  if (!btn) return;
  btn.disabled = !on;
  btn.title = on ? "" : "Set up OpenSteamTool first — Denuvo codes can't apply without it.";
}

// Reflect status in the Engine tab (dot, label, button text/enabled).
function updateEnginePanel(s) {
  const dot = $("#esDot"), txt = $("#esText");
  const inst = $("#ostInstallBtn"), uninst = $("#ostUninstallBtn");
  if (!dot) return;
  const installed = !!(s && s.installed), ready = !!(s && s.ready);
  dot.className = "es-dot " + (ready ? "ok" : installed ? "warn" : "off");
  txt.textContent = ready ? `Active${s && s.engine ? " · " + s.engine : ""}`
                  : installed ? "Installed — needs setup"
                  : "Not installed";
  if (inst) inst.querySelector(".btn-label").textContent =
    ready ? "Reinstall / Repair" : installed ? "Finish setup" : "Install OpenSteamTool";
  if (uninst) uninst.disabled = !installed;
}

async function refreshEngine() {
  let s = null;
  try {
    const chk = await call("engine_check");   // {action, status, installed_tag, latest_tag}
    s = (chk && chk.status) || null;
    const action = chk && chk.action;
    // Auto-heal: if WE set OST up here before and a Steam update clobbered it
    // ('repair'), or a newer OST release is out ('update'), fix it automatically —
    // once per launch so a declined UAC prompt can't loop. First-time installs
    // ('install') stay manual (the banner) so a brand-new user isn't surprised by UAC.
    if ((action === "repair" || action === "update") && !window.__engineAutoRan) {
      window.__engineAutoRan = true;
      showEngineBanner(action === "update"
        ? "Updating OpenSteamTool to the latest version…"
        : "OpenSteamTool needs repair — approve the Windows prompt…");
      await runEngineAction(action === "update" ? "update_engine" : "install_engine",
        $("#engineBtn"), action === "update" ? "OpenSteamTool updated" : "OpenSteamTool repaired");
      return;  // runEngineAction schedules a follow-up refreshEngine
    }
    const ok = !!(s && s.ready);
    if (ok) hideEngineBanner();
    else showEngineBanner(s && s.installed
      ? "Finish OpenSteamTool setup so it reads your library."
      : undefined);
    setRedeemEnabled(ok);
  } catch {
    setRedeemEnabled(true);  // detection failed → don't lock the user out
  }
  updateEnginePanel(s);
  return s;
}

// Shared progress sink: Python pushes here; update whichever bar is on screen.
function setEngineProgress(p, m) {
  [["#engineProgress", "#engineBar", "#enginePct"], ["#ostProgress", "#ostBar", "#ostPct"]]
    .forEach(([pSel, bSel, tSel]) => {
      const prog = $(pSel); if (!prog) return;
      prog.hidden = false;
      const bar = $(bSel), pct = $(tSel);
      if (bar) bar.style.width = `${p}%`;
      if (pct) pct.textContent = `${p}%`;
    });
  if (m) { const bm = $("#engineBannerMsg"); if (bm) bm.textContent = m; }
}

async function runEngineAction(method, btn, okMsg) {
  loading(btn, true);
  setEngineProgress(0, "Starting…");
  let r = null;
  try {
    r = await call(method);
    if (r && r.ok) toast(okMsg, "ok");
    else toast((r && r.message) || "Failed", "err");
    if ($("#ostResult")) setResult($("#ostResult"), r && r.ok ? "ok" : "err", (r && r.message) || "");
  } catch (e) {
    if ($("#ostResult")) setResult($("#ostResult"), "err", String(e));
  }
  loading(btn, false);
  setTimeout(refreshEngine, 1200);
  return r;
}

function initEngine() {
  window.__ostProgress = setEngineProgress;
  // Banner "Set it up"
  const bbtn = $("#engineBtn");
  if (bbtn) bbtn.addEventListener("click", () => runEngineAction("install_engine", bbtn, "OpenSteamTool ready"));
  // Engine tab buttons
  const ibtn = $("#ostInstallBtn"), ubtn = $("#ostUninstallBtn");
  if (ibtn) ibtn.addEventListener("click", () => runEngineAction("install_engine", ibtn, "OpenSteamTool ready"));
  if (ubtn) ubtn.addEventListener("click", () => {
    if (!confirm("Remove OpenSteamTool? Your game library (luas) stays; your previous setup is restored if there was one.")) return;
    runEngineAction("uninstall_engine", ubtn, "OpenSteamTool removed");
  });
}

// ── force update ──────────────────────────────────────────────────────────────
// Progress sink for the in-app updater (Python pushes here via __updProgress).
function setUpdateProgress(p, m) {
  const prog = $("#ugProgress"); if (prog) prog.hidden = false;
  const bar = $("#ugBar"), pct = $("#ugPct");
  if (bar) bar.style.width = `${p}%`;
  if (pct) pct.textContent = `${p}%`;
  if (m) { const t = $("#ugText"); if (t) t.textContent = m; }
}

async function checkUpdate() {
  try {
    const v = await call("version_info");
    if (v && v.update_required) {
      window.__updProgress = setUpdateProgress;
      $("#ugText").textContent =
        `Version ${v.latest} is out — you're on ${v.current}. Update to keep using TokeerDRM.`;
      // In-app: download + swap + relaunch, no browser needed.
      const upd = $("#ugUpdateBtn");
      if (upd) upd.onclick = async () => {
        loading(upd, true);
        setUpdateProgress(0, "Starting update…");
        const r = await call("update_now");
        if (r && !r.ok) { toast((r && r.message) || "Update failed", "err"); loading(upd, false); }
        // on success the app exits and relaunches itself — nothing more to do
      };
      // Fallback: open the GitHub release to download manually.
      $("#ugBtn").onclick = () => call("open_url", v.url);
      $("#updateGate").hidden = false;   // blocks the whole UI
    }
  } catch { /* offline → don't block */ }
}

// ── boot ────────────────────────────────────────────────────────────────────────
whenReady(() => {
  API = window.pywebview.api;
  initTabs();
  initCodeBoxes();
  initGameName();
  initRedeem();
  initGenerate();
  initEngine();
  setRedeemEnabled(false);  // lock redeem until the engine check confirms (refreshEngine flips it)
  checkUpdate();            // force-update gate
  refreshStatus();
  refreshEngine();
  setInterval(refreshStatus, 15000);
});
