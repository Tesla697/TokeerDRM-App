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
  if (kind === "ok") requestAnimationFrame(() => burstFrom(el, 14));
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
  const move = (tab) => { glider.style.translate = `${tab.offsetLeft - 7}px 0`; glider.style.width = `${tab.offsetWidth}px`; };
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      const was = $(".tab.active");
      tabs.forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      const name = tab.dataset.tab;
      if (was !== tab) {
        glider.classList.add("moving");
        setTimeout(() => glider.classList.remove("moving"), 260);
      }
      move(tab);
      document.body.dataset.tab = name;
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

// ── cloud saves ───────────────────────────────────────────────────────────────
// Unlocked games can't use Steam Cloud (Steam rejects Cloud RPCs for apps the
// account doesn't own), so their saves never persist. Enabling cloud saves is
// REQUIRED before a code applies: if the user declines, we don't let the redeem
// through. Doing it now is also much better than after the ticket's 30-min window
// has started, since enabling restarts Steam. Returns true only if redeem may go on.
async function maybeOfferCloud(out) {
  let s;
  // If we can't read the status, don't trap the user behind a prompt — let redeem
  // run (the backend applies the same fail-open rule).
  try { s = await call("cloud_status"); } catch { return true; }
  // Nothing to enforce when it isn't deliverable on this machine or already on.
  if (!s || !s.available || !s.supported || s.enabled) return true;

  const res = await showCloudGate();
  if (res.proceed) return true;          // enabled with no restart → go straight to redeem
  if (res.reason === "declined") {
    setResult(out, "err", "Cloud saves are required. Enable them to apply your code.");
    toast("Enable cloud saves to continue", "err");
  } else if (res.reason === "enabled") {
    // Steam is restarting — the engine check would fail right now, so make the user
    // click apply again once it's back rather than burning the code on a dead engine.
    setResult(out, "ok", res.message || "Cloud saves enabled. Apply your code once Steam is back.");
    toast("Cloud saves enabled", "ok");
  }
  return false;
}

// In-app cloud-saves modal (replaces the native confirm(), which leaked the local
// address in its title bar). Resolves to {proceed, reason, message, restarted}:
//   reason "declined" — user chose not to enable → redeem is blocked
//   reason "enabled"  — enabled OK; proceed is true only if Steam didn't need to bounce
function showCloudGate() {
  return new Promise((resolve) => {
    const gate = $("#cloudGate"), enableBtn = $("#cloudEnableBtn"),
          cancelBtn = $("#cloudCancelBtn"), note = $("#cloudNote"), prog = $("#cloudProgress");

    note.hidden = true; note.textContent = "";
    prog.hidden = true; { const b = $("#cloudBar"); if (b) b.style.width = "0%"; }
    loading(enableBtn, false);
    enableBtn.disabled = false; cancelBtn.disabled = false;
    gate.hidden = false;

    const close = (result) => {
      gate.hidden = true;
      enableBtn.onclick = null; cancelBtn.onclick = null;
      resolve(result);
    };

    cancelBtn.onclick = () => close({ proceed: false, reason: "declined" });

    enableBtn.onclick = async () => {
      loading(enableBtn, true); cancelBtn.disabled = true;
      note.hidden = true;
      prog.hidden = false;
      // Route the backend's progress ticks into the modal's bar for the duration.
      const prev = window.__ostProgress;
      window.__ostProgress = (pct) => {
        const bar = $("#cloudBar"), pctEl = $("#cloudPct");
        if (bar) bar.style.width = `${pct}%`;
        if (pctEl) pctEl.textContent = `${pct}%`;
      };
      let r;
      try { r = await call("enable_cloud"); }
      catch (e) { r = { ok: false, message: String(e) }; }
      window.__ostProgress = prev;
      loading(enableBtn, false);

      if (r && r.ok) {
        close({ proceed: !r.restarted, reason: "enabled", message: r.message, restarted: r.restarted });
      } else {
        // Keep the modal open so the user can retry without losing their place.
        prog.hidden = true;
        note.hidden = false;
        note.textContent = (r && r.message) || "Couldn't enable cloud saves. Try again.";
        cancelBtn.disabled = false;
      }
    };
  });
}

// ── save carry-over (Capcom RE Engine games) ──────────────────────────────────
// Python calls window.__saveProgress(pct, msg, kind) while it re-signs the save.
// pct null hides the bar; kind is "ok" / "err" for the final message.
window.__saveProgress = function (pct, msg, kind) {
  const prog = $("#saveProgress"), out = $("#saveResult");
  if (prog) {
    prog.hidden = pct === null || pct === undefined;
    if (!prog.hidden) {
      $("#saveBar").style.width = `${pct}%`;
      $("#savePct").textContent = `${pct}%`;
    }
  }
  if (out && msg) {
    const safe = String(msg).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    if (kind || out.dataset.msg !== safe) setResult(out, kind || "", safe);
    out.dataset.msg = safe;
  }
};
function clearSaveStatus() {
  const prog = $("#saveProgress"), out = $("#saveResult");
  if (prog) prog.hidden = true;
  if (out) { clearResult(out); out.dataset.msg = ""; }
}

// ── redeem ────────────────────────────────────────────────────────────────────
function initRedeem() {
  const btn = $("#applyBtn"), out = $("#redeemResult");
  btn.addEventListener("click", async () => {
    const code = getCode();
    if (code.length !== 6) { setResult(out, "err", "Enter all 6 characters."); return; }
    clearResult(out); clearSaveStatus(); loading(btn, true);
    try {
      if (!(await maybeOfferCloud(out))) { loading(btn, false); return; }
      const r = await call("redeem", code);
      if (r.ok && r.warning) {
        // Steam is on another Windows account and the ticket couldn't reach it.
        setResult(out, "err", r.warning);
        toast("Applied, but Steam needs a restart", "err");
        clearCode();
      } else if (r.ok) {
        setResult(out, "ok", `Ticket applied for app ${r.app_id}. Auto-launching the game…`);
        toast("Applied, launching game", "ok");
        clearCode();
        try { await call("run_game", r.app_id); } catch {}
      } else {
        setResult(out, "err", r.error || "Something went wrong.");
        toast(r.error || "Redeem failed", "err");
        // Engine isn't set up → surface the repair/setup banner + lock redeem, and
        // re-check the engine so the right action (install/finish/repair) is shown.
        if (r.engine_fix) {
          showEngineBanner(r.error);
          setRedeemEnabled(false);
          refreshEngine();
        }
        // DLL is missing/old → don't just show a banner the user has to hunt for;
        // actually kick off the install now (the message tells them Steam will restart).
        if (r.dll_fix) {
          showDllBanner(r.error);
          if (!window.__engineBusy) { const b = $("#dllFixBtn"); if (b) b.click(); }
        }
        // Backend backstop: reached redeem with cloud saves still off (e.g. the
        // prompt was bypassed). Re-run the same enable prompt now.
        if (r.cloud_fix) {
          await maybeOfferCloud(out);
        }
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
            <span class="code-val">${[...r.code].map((c, i) => `<span style="--i:${i}">${c}</span>`).join("")}</span>
            <button class="copy-btn" id="copyBtn">
              <svg viewBox="0 0 24 24"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>
              Copy
            </button>
          </div>
          <div class="code-meta">Single-use · expires in 30 min · share it now</div>`;
        const copyBtn = $("#copyBtn");
        copyBtn.addEventListener("click", async () => {
          try { await navigator.clipboard.writeText(r.code); } catch {}
          copyBtn.classList.add("done");
          copyBtn.innerHTML = `<svg viewBox="0 0 24 24"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg> Copied`;
          setTimeout(() => {
            copyBtn.classList.remove("done");
            copyBtn.innerHTML = `<svg viewBox="0 0 24 24"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg> Copy`;
          }, 1400);
        });
        toast("Code generated", "ok");
        requestAnimationFrame(() => burstFrom(reveal.firstElementChild, 18));
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
  btn.title = on ? "" : "Set up OpenSteamTool first. Denuvo codes can't apply without it.";
}

// Reflect status in the Engine tab (dot, label, button text/enabled).
function updateEnginePanel(s) {
  const dot = $("#esDot"), txt = $("#esText");
  const inst = $("#ostInstallBtn"), uninst = $("#ostUninstallBtn");
  if (!dot) return;
  const installed = !!(s && s.installed), ready = !!(s && s.ready);
  const state = ready ? "ok" : installed ? "warn" : "off";
  dot.className = "es-dot " + state;
  txt.textContent = ready ? `Active${s && s.engine ? " · " + s.engine : ""}`
                  : installed ? "Installed, needs setup"
                  : "Not installed";
  const sbDot = $("#sbEngineDot"), sbTxt = $("#sbEngineText");
  if (sbDot) sbDot.className = "es-dot " + state;
  if (sbTxt) sbTxt.textContent = ready ? "Active" : installed ? "Needs setup" : "Not installed";
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
        : "OpenSteamTool needs repair. Approve the Windows prompt…");
      await runEngineAction(action === "update" ? "update_engine" : "install_engine",
        $("#engineBtn"), action === "update" ? "OpenSteamTool updated" : "OpenSteamTool repaired");
      return;  // runEngineAction schedules a follow-up refreshEngine
    }
    const ok = !!(s && s.ready);
    if (ok) hideEngineBanner();
    else if (s && s.foreign && s.foreign.length)
      showEngineBanner(`Another unlocker (${s.foreign.join(", ")}) is hooking Steam and blocks activations. Click Finish setup to turn it off.`);
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
      // Tidy up once it's done so the bar doesn't linger at 100% (or a stuck value).
      if (p >= 100) setTimeout(() => { prog.hidden = true; if (bar) bar.style.width = "0%"; }, 900);
    });
  if (m) { const bm = $("#engineBannerMsg"); if (bm) bm.textContent = m; }
}

async function runEngineAction(method, btn, okMsg) {
  window.__engineBusy = true;   // pause the live monitor while we install/repair
  loading(btn, true);
  setEngineProgress(0, "Starting…");
  let r = null;
  try {
    r = await call(method);
    if (r && r.defender) {
      // Defender (Tamper Protection) blocked the official OST → route to LuaTools.
      handleDefenderBlock(r);
    } else if (r && r.ok) {
      toast(okMsg, "ok");
      if ($("#ostResult")) setResult($("#ostResult"), "ok", r.message || "");
    } else {
      toast((r && r.message) || "Failed", "err");
      if ($("#ostResult")) setResult($("#ostResult"), "err", (r && r.message) || "");
    }
  } catch (e) {
    if ($("#ostResult")) setResult($("#ostResult"), "err", String(e));
  }
  loading(btn, false);
  window.__engineBusy = false;
  setTimeout(refreshEngine, 1200);
  return r;
}

// Defender blocked the PUA-flagged official OST and Tamper Protection blocks the
// auto-exclusion. LuaTools ships an unflagged build, so guide the user there with a
// one-click link, then they redeem here.
function handleDefenderBlock(r) {
  const msg = (r && r.message) || "Windows Defender blocked OpenSteamTool. Install it via LuaTools.";
  const url = (r && r.url) || "https://lua.tools";
  toast("Defender blocked OST, use LuaTools", "err");
  if ($("#ostResult")) setResult($("#ostResult"), "err", msg);
  showEngineBanner(msg);
  // Turn the banner button into "Open LuaTools". Clone it first to drop the old
  // install-engine listener so it doesn't also try to reinstall.
  const bbtn = $("#engineBtn");
  if (bbtn && bbtn.parentNode) {
    const fresh = bbtn.cloneNode(true);
    bbtn.parentNode.replaceChild(fresh, bbtn);
    const label = fresh.querySelector(".btn-label") || fresh;
    label.textContent = "Open LuaTools";
    fresh.addEventListener("click", () => call("open_url", url));
  }
}

function initEngine() {
  window.__ostProgress = setEngineProgress;
  // Banner "Set it up"
  const bbtn = $("#engineBtn");
  if (bbtn) bbtn.addEventListener("click", () => runEngineAction("install_engine", bbtn, "OpenSteamTool ready"));
  // Engine tab buttons
  const ibtn = $("#ostInstallBtn"), ubtn = $("#ostUninstallBtn");
  if (ibtn) ibtn.addEventListener("click", () => runEngineAction("install_engine", ibtn, "OpenSteamTool ready"));
  if (ubtn) ubtn.addEventListener("click", async () => {
    if (!(await showConfirm())) return;
    runEngineAction("uninstall_engine", ubtn, "OpenSteamTool removed");
  });
}

// In-app yes/no sheet for uninstall (native confirm() shows the local address in its title).
function showConfirm() {
  return new Promise((resolve) => {
    const gate = $("#confirmGate"), ok = $("#cfOkBtn"), no = $("#cfCancelBtn");
    const close = (v) => { gate.hidden = true; ok.onclick = null; no.onclick = null; resolve(v); };
    ok.onclick = () => close(true);
    no.onclick = () => close(false);
    gate.hidden = false;
  });
}

// ── DLL fix banner ────────────────────────────────────────────────────────────
function showDllBanner(msg) {
  if (msg) $("#dllBannerMsg").textContent = msg;
  $("#dllBanner").hidden = false;
}
function hideDllBanner() { if ($("#dllBanner")) $("#dllBanner").hidden = true; }

function setDllProgress(p, m) {
  const prog = $("#dllProgress"); if (!prog) return;
  prog.hidden = false;
  const bar = $("#dllBar"), pct = $("#dllPct");
  if (bar) bar.style.width = `${p}%`;
  if (pct) pct.textContent = `${p}%`;
  if (p >= 100) setTimeout(() => { prog.hidden = true; if (bar) bar.style.width = "0%"; }, 900);
  if (m) { const bm = $("#dllBannerMsg"); if (bm) bm.textContent = m; }
}

async function checkDllStatus() {
  try {
    const s = await call("dll_status");
    if (s && s.needs_fix) {
      showDllBanner("Some games throw errors with the original OpenSteamTool. Click Fix to install the enhanced build.");
      setRedeemEnabled(false);   // wrong/foreign DLL — match the redeem gate
    } else if (s && s.needs_update) {
      showDllBanner("A newer version of the enhanced DLL is available. Click Update to upgrade.");
      const lbl = $("#dllFixBtn .btn-label");
      if (lbl) lbl.textContent = "Update DLL";
      setRedeemEnabled(false);   // out-of-date DLL — match the redeem gate
    } else {
      hideDllBanner();
    }
  } catch {}
}

function initDllFix() {
  window.__dllProgress = setDllProgress;
  const btn = $("#dllFixBtn");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    window.__engineBusy = true;   // pause the live monitor during the swap + Steam restart
    loading(btn, true);
    setDllProgress(0, "Starting…");
    try {
      const r = await call("fix_dll");
      if (r && r.defender) {
        handleDefenderBlock(r);
      } else if (r && r.ok) {
        toast("Enhanced DLL installed, Steam is restarting", "ok");
        hideDllBanner();
      } else {
        toast((r && r.message) || "Fix failed", "err");
        if (r && r.message) setDllProgress(0, r.message);
      }
    } catch (e) {
      toast(String(e), "err");
    }
    loading(btn, false);
    window.__engineBusy = false;
    setTimeout(() => { refreshEngine().then(checkDllStatus); }, 1200);
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

// ── patch notes: what's-new shown once after the app updates ────────────────────
// One entry per released version. When APP_VERSION goes up, the app shows the
// matching notes once, then remembers the version so it never nags again. Add a
// new entry here every release.
const PATCH_NOTES = {
  "1.0.34": [
    "Fixed a 00 error caused by another unlocker hiding in your Steam folder (like a DWrite.dll from Luma). The app now spots it, tells you, and turns it off when you click Finish setup.",
    "Installing OpenSteamTool no longer says it worked when it didn't. If your antivirus removes the files, or Windows ran the install on a different account, you now get told what happened and what to do.",
    "The engine download retries and switches servers when the connection drops, so the SSL \"bad record mac\" error doesn't stop the install anymore.",
    "Animations are smoother and use less of your PC.",
  ],
  "1.0.33": [
    "Fixed the 00 error on PCs with more than one Windows account. If Steam or the app ever asked for another account's password, the activation could land on that other account and the game couldn't find it.",
    "Your code now always goes to the Windows account Steam is actually running on, so this sorts itself out without you doing anything.",
    "When the app restarts Steam after a fix or an update, Steam now opens on your normal account instead of the admin one.",
  ],
  "1.0.32": [
    "Save carry-over now waits for Steam to finish signing in before it picks your save, so it no longer guesses the wrong one when you redeem right after launch.",
    "It also reads the account your game is actually signed in as and signs the save to that, which fixes the cases where a save came across but the game acted like it wasn't there.",
  ],
  "1.0.31": [
    "Save carry-over for RE9 Requiem, PRAGMATA, Monster Hunter Stories 3 and Onimusha is fixed for a lot more people. It now runs inside the app and shows its progress right under your code.",
    "If your PC doesn't have the .NET runtime the save tool needs, the app grabs it by itself. No installer, no admin prompt.",
    "Each save file is moved over on its own, and files the game doesn't sign are left alone. This stops the carry-over from damaging RE9 and PRAGMATA saves.",
    "A backup of your save is kept before every carry-over (the last 5), and a log is written to %LOCALAPPDATA%\\TokeerDRM\\save_carry.log if anything goes wrong.",
  ],
  "1.0.30": [
    "Brand new look: frosted glass, bright colors that change with each tab, and a dock at the bottom for Activate, Generate and Engine.",
    "Server status and the app version now sit in the pill at the top. Hover it to see the engine status.",
    "Buttons and code boxes react when you touch them, and you can pop the bubbles floating in the background.",
    "A little Halloween surprise is hiding in there for October.",
    "Uninstalling OpenSteamTool now asks inside the app instead of a Windows popup. Everything else works exactly like before.",
  ],
  "1.0.29": [
    "Save carry-over no longer opens and closes the game. Your save is moved to the new activation first (you'll see a command prompt for a couple of minutes), then the game opens once with your progress.",
    "If the game is already open when you redeem, the app closes it for you, or asks you to close it if it can't.",
  ],
  "1.0.28": [
    "Your existing save now carries over automatically when you activate on a new account, for Resident Evil Requiem, PRAGMATA, Monster Hunter Stories 3 and Onimusha. The first launch may briefly open and close the game and show a command prompt while the save transfers, then it opens on its own.",
    "After you paste a code, the app now launches the game for you instead of asking you to open it from Steam.",
    "Linux: games run on GE-Proton10-34 (installed automatically if missing), and the per-game Proton tool now survives a reboot.",
    "SteamID is written with the tickets on redeem, and update checks are faster.",
  ],
};
const PATCH_NOTES_SEEN_KEY = "tokeerdrm_seen_version";

function maybeShowPatchNotes(current) {
  if (!current) return;
  let seen = null;
  try { seen = localStorage.getItem(PATCH_NOTES_SEEN_KEY); } catch {}
  // Brand-new install (nothing seen yet): remember the version, don't greet a
  // first-time user with a changelog for a version they never ran before.
  if (!seen) { try { localStorage.setItem(PATCH_NOTES_SEEN_KEY, current); } catch {} return; }
  if (seen === current) return;                 // already showed this version
  const notes = PATCH_NOTES[current];
  try { localStorage.setItem(PATCH_NOTES_SEEN_KEY, current); } catch {}
  if (!notes || !notes.length) return;          // no notes for this version
  showPatchNotes(current, notes);
}

function showPatchNotes(version, notes) {
  const gate = $("#patchNotesGate");
  if (!gate) return;
  const titleEl = $("#pnTitle"), listEl = $("#pnList");
  if (titleEl) titleEl.textContent = `What's new in v${version}`;
  if (listEl) {
    listEl.innerHTML = "";
    for (const n of notes) { const li = document.createElement("li"); li.textContent = n; listEl.appendChild(li); }
  }
  const btn = $("#pnCloseBtn");
  if (btn) btn.onclick = () => { gate.hidden = true; };
  gate.hidden = false;
}

async function checkUpdate() {
  try {
    const v = await call("version_info");
    // Always surface the running version in the footer (offline-safe: version_info
    // returns `current` even when the GitHub check fails).
    const ver = $("#appVersion");
    if (ver && v && v.current) ver.textContent = "v" + v.current;
    if (v && v.update_required) {
      window.__updProgress = setUpdateProgress;
      $("#ugText").textContent =
        `Version ${v.latest} is out and you're on ${v.current}. Update to keep using TokeerDRM.`;
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
    } else {
      // On the latest version: show this version's patch notes once after an update.
      maybeShowPatchNotes(v && v.current);
    }
  } catch { /* offline → don't block */ }
}

// ── one-click auto-redeem (from the tokeerdrm:// deep link) ─────────────────────
// Python calls window.__ostIncomingCode(code) when the app is opened (or focused)
// via the Discord "Redeem in App" button. We fill the code, apply it, auto-run any
// required engine/DLL/cloud fix, retry the SAME code (it's single-use but never
// burned on a gated failure), and launch the game once the ticket actually applies.
window.__redeemReady = false;
window.__pendingCode = null;
window.__ostIncomingCode = function (code) {
  const clean = (code || "").toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 6);
  if (clean.length !== 6) return;
  if (window.__redeemReady) startAutoRedeem(clean);
  else window.__pendingCode = clean;   // arrived before the UI finished booting
};

function fillCodeBoxes(code) {
  const boxes = $$(".cbox", $("#codeBoxes"));
  const clean = (code || "").toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 6);
  boxes.forEach((b, i) => {
    b.value = clean[i] || "";
    b.classList.toggle("filled", !!b.value);
  });
  return clean;
}

function gotoActivateTab() {
  const tab = $('.tab[data-tab="activate"]');
  if (tab) tab.click();
}

// Poll until OpenSteamTool is ready AND the enhanced DLL is current, or time out.
// Used after an auto-install / DLL fix (which restarts Steam) before we retry.
async function waitForEngineReady(timeoutMs = 120000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const s = await call("engine_status");
      const ds = await call("dll_status");
      if (s && s.ready && !(ds && (ds.needs_fix || ds.needs_update))) return true;
    } catch {}
    await new Promise((r) => setTimeout(r, 3000));
  }
  return false;
}

let __autoRedeemRunning = false;
async function startAutoRedeem(code) {
  if (__autoRedeemRunning) return;
  __autoRedeemRunning = true;
  const out = $("#redeemResult");
  try {
    gotoActivateTab();
    const clean = fillCodeBoxes(code);
    if (clean.length !== 6) { toast("That link had a bad code.", "err"); return; }
    toast("Code received, applying…", "ok");
    clearSaveStatus();

    // Up to 4 passes; each may auto-run one fix (engine/DLL/cloud) then retry.
    for (let attempt = 0; attempt < 4; attempt++) {
      setResult(out, "", "Applying your code…");
      let r;
      try { r = await call("redeem", clean); }
      catch (e) { setResult(out, "err", String(e)); break; }

      if (r && r.ok && r.warning) {
        setResult(out, "err", r.warning);
        toast("Applied, but Steam needs a restart", "err");
        clearCode();
        break;
      }
      if (r && r.ok) {
        setResult(out, "ok", `Ticket applied for app ${r.app_id}. Launching the game…`);
        toast("Applied, launching game", "ok");
        clearCode();
        try { await call("run_game", r.app_id); } catch {}
        break;
      }

      // Auto-fix branches — run the fix, wait for Steam/engine to settle, then retry.
      if (r && r.engine_fix) {
        showEngineBanner(r.error || "Setting up OpenSteamTool. Approve the Windows prompt…");
        await runEngineAction("install_engine", $("#engineBtn") || $("#ostInstallBtn"), "OpenSteamTool ready");
        await waitForEngineReady();
        continue;
      }
      if (r && r.dll_fix) {
        showDllBanner(r.error || "Installing the enhanced DLL. Steam will restart…");
        const b = $("#dllFixBtn"); if (b) b.click();
        await new Promise((res) => setTimeout(res, 4000));  // let the swap + Steam restart begin
        await waitForEngineReady();
        continue;
      }
      if (r && r.cloud_fix) {
        await maybeOfferCloud(out);   // prompt + enable cloud saves, then retry
        continue;
      }

      // Any other error → surface it and stop (code stays valid to retry manually).
      setResult(out, "err", (r && r.error) || "Redeem failed.");
      toast((r && r.error) || "Redeem failed", "err");
      break;
    }
  } finally {
    __autoRedeemRunning = false;
  }
}

// ── fx: bubbles, cursor glow, glass highlight, ripples ─────────────────────────
function burstAt(x, y, n = 10, spread = 90) {
  for (let k = 0; k < n; k++) {
    const b = document.createElement("span");
    const size = 6 + Math.random() * 16;
    const a = Math.random() * Math.PI * 2, d = spread * (0.4 + Math.random() * 0.8);
    b.className = "fx-bub";
    b.style.cssText = `left:${x - size / 2}px;top:${y - size / 2}px;width:${size}px;height:${size}px;` +
      `--dx:${Math.cos(a) * d}px;--dy:${Math.sin(a) * d - 30}px;--t:${0.6 + Math.random() * 0.6}s`;
    document.body.appendChild(b);
    setTimeout(() => b.remove(), 1300);
  }
}
function burstFrom(el, n) {
  if (!el) return;
  const r = el.getBoundingClientRect();
  burstAt(r.left + r.width / 2, r.top + r.height / 2, n, Math.max(80, r.width / 2));
}

// halloween bits show up for all of October and the first days of November
const SPOOKY = (() => { const d = new Date(); return d.getMonth() === 9 || (d.getMonth() === 10 && d.getDate() <= 2); })();
const GHOST_SVG = `<svg viewBox="0 0 40 44"><path class="g-body" d="M20 2C10.6 2 4 9.2 4 18.5V42l5.3-4.2 5.4 4.2 5.3-4.2 5.3 4.2 5.4-4.2L36 42V18.5C36 9.2 29.4 2 20 2Z"/><ellipse class="g-eye" cx="14.5" cy="18" rx="2.6" ry="3.6"/><ellipse class="g-eye" cx="25.5" cy="18" rx="2.6" ry="3.6"/><ellipse class="g-eye" cx="20" cy="27" rx="2.4" ry="3"/></svg>`;
const BAT_SVG = `<svg viewBox="0 0 64 28"><path class="wl" d="M28 12C24 6 17 3 9 4c3 2 4 5 3 8-3-2-8-2-12 0 4 2 7 6 7 10 4-3 9-3 13-1-1-4 2-7 8-9Z"/><path class="wr" d="M36 12c4-6 11-9 19-8-3 2-4 5-3 8 3-2 8-2 12 0-4 2-7 6-7 10-4-3-9-3-13-1 1-4-2-7-8-9Z"/><path d="M32 8c-2 0-3-3-3-5l2 2h2l2-2c0 2-1 5-3 5Zm-4 4c0-3 2-4 4-4s4 1 4 4c0 5-2 9-4 9s-4-4-4-9Z"/></svg>`;

function spawnBat() {
  const box = $("#bats");
  if (!box) return;
  const bat = document.createElement("span");
  bat.className = "bat";
  bat.style.cssText = `--y:${6 + Math.random() * 30}vh;--s:${34 + Math.random() * 34}px;` +
    `--d:${8 + Math.random() * 6}s;--dy:${(Math.random() - 0.5) * 120}px`;
  bat.innerHTML = BAT_SVG;
  box.appendChild(bat);
  bat.addEventListener("animationend", (e) => { if (e.animationName === "bat-fly") bat.remove(); });
  setTimeout(spawnBat, 5000 + Math.random() * 7000);
}

function initSpooky() {
  if (!SPOOKY) return;
  document.body.classList.add("spooky");
  setTimeout(spawnBat, 1500);
  const spider = $("#spider");
  if (spider) spider.addEventListener("pointerenter", () => {
    if (spider.classList.contains("climb")) return;
    spider.classList.add("climb");
    setTimeout(() => spider.classList.remove("climb"), 3500);
  });
}

function spawnBubble(box, first) {
  const b = document.createElement("span");
  const ghost = SPOOKY && Math.random() < 0.3;
  const size = ghost ? 26 + Math.random() * 30 : 18 + Math.random() * 64;
  const dur = 14 + Math.random() * 16;
  b.className = ghost ? "bubble ghost" : "bubble";
  if (ghost) b.innerHTML = GHOST_SVG;
  b.style.cssText = `width:${size}px;height:${size}px;left:${Math.random() * 100}%;` +
    `--dur:${dur}s;--delay:${first ? -Math.random() * dur : 0}s;opacity:${0.5 + Math.random() * 0.5}`;
  b.addEventListener("pointerenter", () => {
    if (b.classList.contains("pop")) return;
    const r = b.getBoundingClientRect();
    const cs = getComputedStyle(b);
    b.style.translate = cs.translate; b.style.transform = cs.transform;
    b.classList.add("pop");
    burstAt(r.left + r.width / 2, r.top + r.height / 2, 6, size);
    setTimeout(() => { b.remove(); spawnBubble(box, false); }, 340);
  });
  b.addEventListener("animationiteration", (e) => {
    if (e.animationName === "rise") b.style.left = `${Math.random() * 100}%`;
  });
  box.appendChild(b);
}

function initFx() {
  initSpooky();
  const box = $("#bubbles");
  if (box) for (let i = 0; i < 14; i++) spawnBubble(box, true);

  // the glow trails the cursor like it's floating in water
  const glow = $("#cursorGlow");
  let tx = innerWidth / 2, ty = innerHeight * 0.4, gx = tx, gy = ty;
  let hover = null, hx = 0, hy = 0, frame = 0, last = performance.now();
  // one frame per batch of pointer moves; the glow loop stops once it catches up
  const step = () => {
    frame = 0;
    if (hover) {
      const r = hover.getBoundingClientRect();
      hover.style.setProperty("--mx", `${hx - r.left}px`);
      hover.style.setProperty("--my", `${hy - r.top}px`);
      hover = null;
    }
    if (!glow) return;
    const dt = Math.min((performance.now() - last) / 16.7, 4);
    last = performance.now();
    const k = 1 - Math.pow(1 - 0.07, dt);
    gx += (tx - gx) * k; gy += (ty - gy) * k;
    glow.style.transform = `translate3d(${gx}px, ${gy}px, 0)`;
    if (Math.abs(tx - gx) > 0.3 || Math.abs(ty - gy) > 0.3) frame = requestAnimationFrame(step);
  };
  const kick = () => { if (!frame) { last = performance.now(); frame = requestAnimationFrame(step); } };
  document.addEventListener("pointermove", (e) => {
    tx = e.clientX; ty = e.clientY;
    hover = e.target.closest && e.target.closest(".glass, .btn");
    hx = e.clientX; hy = e.clientY;
    kick();
  }, { passive: true });
  kick();

  // ripple + a few bubbles on every press
  document.addEventListener("pointerdown", (e) => {
    const t = e.target.closest && e.target.closest(".btn, .tab, .copy-btn");
    if (!t || t.disabled) return;
    const r = t.getBoundingClientRect();
    const rp = document.createElement("span");
    rp.className = "ripple";
    rp.style.left = `${e.clientX - r.left}px`;
    rp.style.top = `${e.clientY - r.top}px`;
    t.appendChild(rp);
    setTimeout(() => rp.remove(), 750);
    burstAt(e.clientX, e.clientY, 7, 60);
  });

  // wobble once when the pointer lands on a button
  document.addEventListener("pointerover", (e) => {
    const t = e.target.closest && e.target.closest(".btn");
    if (!t || t.disabled || t.contains(e.relatedTarget)) return;
    t.classList.remove("wobble"); void t.offsetWidth; t.classList.add("wobble");
    setTimeout(() => t.classList.remove("wobble"), 720);
  });
}
initFx();

// ── boot ────────────────────────────────────────────────────────────────────────
whenReady(() => {
  API = window.pywebview.api;
  initTabs();
  initCodeBoxes();
  initGameName();
  initRedeem();
  initGenerate();
  initEngine();
  initDllFix();
  setRedeemEnabled(false);  // lock redeem until the engine check confirms (refreshEngine flips it)
  checkUpdate();            // force-update gate
  refreshStatus();
  refreshEngine().then(checkDllStatus);
  // The UI is ready — run any code that arrived via deep link before boot finished.
  window.__redeemReady = true;
  if (window.__pendingCode) { const c = window.__pendingCode; window.__pendingCode = null; startAutoRedeem(c); }
  setInterval(refreshStatus, 15000);
  // Live re-check: a user can swap in a different/old OpenSteamTool.dll or remove the
  // hijack proxies AFTER the app is open. Poll engine + DLL state so the banners and
  // the redeem gate stay honest without needing a restart. Skips while an install/fix
  // is running so it never fights an action in progress.
  setInterval(() => {
    if (window.__engineBusy) return;
    refreshEngine().then(checkDllStatus);
  }, 12000);
});
