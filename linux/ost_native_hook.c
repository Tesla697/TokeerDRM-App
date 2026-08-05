// -----------------------------------------------------------------------------
// OST native-side hook - Stage N2.
//
// The Wine-side activator works (it spoofs SteamID + serves the eticket and the
// game authenticates), but code-virtualized Denuvo (MHS3 2852190, MHW 2246340)
// runs a deferred integrity scan of the PE steamclient64.dll image and detects
// our in-process patches, so the game crashes in Denuvo's own code.
//
// N2 moves the exact same serves ONE LAYER DOWN, into Proton's lsteamclient.so.
// That is the winelib bridge every ISteamUser call funnels through on its way
// from the PE side to native Steam. It is a NATIVE ELF; Denuvo's PE-module
// integrity scan cannot see edits to it. We inline-hook three marshalling
// thunks and answer there, leaving steamclient64.dll byte-for-byte pristine:
//
//   ISteamUser_SteamUserNNN_GetSteamID               -> write pool SteamID
//   ISteamUser_SteamUserNNN_GetEncryptedAppTicket    -> serve prefix eticket
//   ISteamUser_SteamUserNNN_RequestEncryptedAppTicket-> return a fake call handle
//
// The thunk args-struct layouts below were recovered by disassembling Proton
// 11's lsteamclient.so (unixlib ABI: each thunk is NTSTATUS(void *params), the
// PE-side params packed struct is what we read/write). The tickets come from the
// SAME Wine prefix the Wine activator used: HKCU\Software\Valve\Steam\Apps\<id>,
// values ETicket / AppTicket (REG_BINARY), which live in <prefix>/user.reg.
//
// This stage does NOT yet fake the async EncryptedAppTicketResponse_t callback.
// The log will show whether Denuvo needs it (waits on the RequestEncrypted...
// call handle) or is happy with the synchronous GetEncryptedAppTicket serve.
//
// Build:  ./build.sh          Run:  set ost-run.sh as the launch option.
// -----------------------------------------------------------------------------
#define _GNU_SOURCE
#include <dlfcn.h>
#include <elf.h>
#include <execinfo.h>
#include <fcntl.h>
#include <link.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

// --------------------------------- logging ---------------------------------
static char g_logPath[512];
static char g_comm[64] = "?";
static char g_cmd[512] = "";
static int  g_interesting = 0;

static void log_line(const char *s)
{
    int fd = open(g_logPath, O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (fd < 0) return;
    ssize_t n = write(fd, s, strlen(s));
    n = write(fd, "\n", 1);
    (void)n;
    close(fd);
}
static void plog(const char *fmt, ...)
{
    char body[1024], line[1200];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(body, sizeof body, fmt, ap);
    va_end(ap);
    snprintf(line, sizeof line, "[%d %s] %s", (int)getpid(), g_comm, body);
    log_line(line);
}
static void read_proc(const char *path, char *buf, size_t cap, int nul_to_space)
{
    buf[0] = 0;
    int fd = open(path, O_RDONLY);
    if (fd < 0) return;
    ssize_t n = read(fd, buf, cap - 1);
    close(fd);
    if (n <= 0) { buf[0] = 0; return; }
    buf[n] = 0;
    for (ssize_t i = 0; i < n; ++i) {
        if (buf[i] == '\n') buf[i] = 0;
        else if (nul_to_space && buf[i] == 0) buf[i] = ' ';
    }
    buf[cap - 1] = 0;
}

// ------------------------------ ticket state -------------------------------
static uint8_t  *g_eticket = NULL;   static uint32_t g_eticketN = 0;
static uint8_t  *g_appticket = NULL; static uint32_t g_appticketN = 0;
static uint64_t  g_spoofId = 0;
static uint32_t  g_appId = 0;
static int       g_ticketsReady = 0;
static int       g_fakeRequest = 0;   // OST_FAKE_REQUEST=1 -> hook RequestEncryptedAppTicket
static volatile int g_injectPending = 0;  // a fabricated eticket completion is owed
#define OST_FAKE_APICALL 0x00000000CAFEF00Dull

static uint32_t resolve_appid(void)
{
    const char *e = getenv("SteamAppId");
    if (!e || !e[0]) e = getenv("SteamGameId");
    if (!e || !e[0]) e = getenv("SteamOverlayGameId");
    return e ? (uint32_t)strtoul(e, NULL, 10) : 0;
}

// Read <prefix>/user.reg, find [Software\\Valve\\Steam\\Apps\\<appid>], pull the
// "<value>"=hex:.. blob (handles backslash line-continuations). Returns malloc'd
// bytes + count, or NULL. Wine writes REG_BINARY as lowercase comma hex.
static uint8_t *read_prefix_binary(uint32_t appid, const char *value, uint32_t *out_len)
{
    *out_len = 0;
    char reg[600];
    const char *cdp = getenv("STEAM_COMPAT_DATA_PATH");
    const char *wp  = getenv("WINEPREFIX");
    if (cdp && cdp[0])      snprintf(reg, sizeof reg, "%s/pfx/user.reg", cdp);
    else if (wp && wp[0])   snprintf(reg, sizeof reg, "%s/user.reg", wp);
    else                    return NULL;

    int fd = open(reg, O_RDONLY);
    if (fd < 0) { plog("[cfg] user.reg not found at %s", reg); return NULL; }
    // Slurp the file (user.reg is typically a few hundred KB at most).
    size_t cap = 1 << 20, len = 0;
    char *buf = (char *)malloc(cap);
    if (!buf) { close(fd); return NULL; }
    for (;;) {
        if (len + 65536 > cap) { cap *= 2; char *nb = realloc(buf, cap); if (!nb) { free(buf); close(fd); return NULL; } buf = nb; }
        ssize_t n = read(fd, buf + len, 65536);
        if (n <= 0) break;
        len += (size_t)n;
    }
    close(fd);
    buf[len < cap ? len : cap - 1] = 0;

    char section[128];
    snprintf(section, sizeof section, "[Software\\\\Valve\\\\Steam\\\\Apps\\\\%u]", appid);
    char *s = strstr(buf, section);
    if (!s) { free(buf); return NULL; }

    // Within this section (until the next "[" at column 0), find "value"=hex:
    char needle[64];
    snprintf(needle, sizeof needle, "\"%s\"=hex", value);
    char *v = NULL;
    for (char *p = s + strlen(section); *p; ++p) {
        if (p[0] == '\n' && p[1] == '[') break;             // next section
        if (!strncmp(p, needle, strlen(needle))) { v = p; break; }
    }
    if (!v) { free(buf); return NULL; }
    char *colon = strchr(v, ':');
    if (!colon) { free(buf); return NULL; }

    // Parse hex bytes from colon+1, across '\' continuations, until a newline
    // that is not preceded by a continuation backslash.
    uint8_t *out = (uint8_t *)malloc(len / 2 + 16);
    if (!out) { free(buf); return NULL; }
    uint32_t cnt = 0;
    char *p = colon + 1;
    while (*p) {
        if (*p == ',' || *p == ' ' || *p == '\t' || *p == '\\' || *p == '\r') { ++p; continue; }
        if (*p == '\n') {
            // Continue only if the previous non-space char was a backslash.
            char *q = p - 1;
            while (q > colon && (*q == ' ' || *q == '\t' || *q == '\r')) --q;
            if (*q == '\\') { ++p; continue; }
            break;
        }
        int hi = -1, lo = -1;
        char c = *p;
        if      (c >= '0' && c <= '9') hi = c - '0';
        else if (c >= 'a' && c <= 'f') hi = c - 'a' + 10;
        else if (c >= 'A' && c <= 'F') hi = c - 'A' + 10;
        else break;
        c = p[1];
        if      (c >= '0' && c <= '9') lo = c - '0';
        else if (c >= 'a' && c <= 'f') lo = c - 'a' + 10;
        else if (c >= 'A' && c <= 'F') lo = c - 'A' + 10;
        if (lo < 0) break;
        out[cnt++] = (uint8_t)((hi << 4) | lo);
        p += 2;
    }
    free(buf);
    if (!cnt) { free(out); return NULL; }
    *out_len = cnt;
    return out;
}

static void load_tickets(void)
{
    if (g_ticketsReady) return;
    if (!g_appId) g_appId = resolve_appid();
    if (!g_appId) { plog("[cfg] no SteamAppId in env yet"); return; }

    if (!g_eticket)   g_eticket   = read_prefix_binary(g_appId, "ETicket",   &g_eticketN);
    if (!g_appticket) g_appticket = read_prefix_binary(g_appId, "AppTicket", &g_appticketN);
    if (g_appticket && g_appticketN >= 16 && !g_spoofId)
        memcpy(&g_spoofId, g_appticket + 8, 8);   // [u32 size][u32 ver][u64 steamid]

    if (g_eticket && g_eticketN && g_spoofId) {
        g_ticketsReady = 1;
        plog("[cfg] tickets ready: appid=%u eticket=%u appticket=%u spoof=0x%016llx",
             g_appId, g_eticketN, g_appticketN, (unsigned long long)g_spoofId);
    } else {
        plog("[cfg] not ready yet: eticket=%u appticket=%u spoof=0x%llx (retrying)",
             g_eticketN, g_appticketN, (unsigned long long)g_spoofId);
    }
}

// -------------------------- hook handlers (unixlib) -------------------------
// Each is NTSTATUS handler(void *params); params is the packed PE-side struct.
static long ost_GetSteamID(void *args)
{
    if (!g_ticketsReady) load_tickets();
    void **a = (void **)args;
    uint64_t *dst = (uint64_t *)a[1];   // params+0x8 = pointer to CSteamID out
    if (g_spoofId && dst && (uintptr_t)dst > 0x1000) *dst = g_spoofId;
    return 0;
}
static long ost_GetEncryptedAppTicket(void *args)
{
    if (!g_ticketsReady) load_tickets();
    uint8_t *p = (uint8_t *)args;
    void     *pTicket; uint32_t cbMax; uint32_t *pcb;
    memcpy(&pTicket, p + 0x9,  8);
    memcpy(&cbMax,   p + 0x11, 4);
    memcpy(&pcb,     p + 0x15, 8);
    if (g_eticket && g_eticketN) {
        if (pcb) *pcb = g_eticketN;
        if (!pTicket || cbMax == 0)     { p[8] = 1; plog("[su] GetEncryptedAppTicket sizing -> %u", g_eticketN); return 0; }
        if (cbMax < g_eticketN)         { p[8] = 0; plog("[su] GetEncryptedAppTicket buf too small %u<%u", cbMax, g_eticketN); return 0; }
        memcpy(pTicket, g_eticket, g_eticketN);
        p[8] = 1;
        plog("[su] *** GetEncryptedAppTicket SERVED %u bytes ***", g_eticketN);
        return 0;
    }
    p[8] = 0;
    plog("[su] GetEncryptedAppTicket asked but no eticket loaded");
    return 0;
}
// ISteamApps::BIsSubscribed / BIsSubscribedApp: {+0 linux_side; +8 bool ret}.
// The Steam DRM (SteamStub) wrapper's ownership check reads these; forcing true
// clears "Application load error 6". Full replace (always owned).
static long ost_force_subscribed(void *args)
{
    ((uint8_t *)args)[8] = 1;
    return 0;
}

// ISteamAppTicket::GetAppOwnershipTicketData - the SteamStub wrapper validates
// ownership from this signed blob. Serve the pool's AppTicket (same as the Wine
// hook_core error-54 fix). AppTicket layout: [u32 sigOffset][... steamid@8 ...
// appid@16 ...][128-byte sig]. Out-params piAppId/piSteamId/piSig are the byte
// OFFSETS within the ticket; pcbSig is the sig length.
// Thunk params (packed): {+0 linux_side; +8 u32 ret; +0xc appId; +0x10 void* buf;
// +0x18 u32 cbBuf; +0x1c u32* piAppId; +0x24 u32* piSteamId; +0x2c u32* piSig;
// +0x34 u32* pcbSig}.
static long ost_GetAppOwnershipTicketData(void *args)
{
    if (!g_ticketsReady) load_tickets();
    uint8_t *p = (uint8_t *)args;
    void *buf; uint32_t cbBuf; uint32_t *piAppId, *piSteamId, *piSig, *pcbSig;
    memcpy(&buf,       p + 0x10, 8);
    memcpy(&cbBuf,     p + 0x18, 4);
    memcpy(&piAppId,   p + 0x1c, 8);
    memcpy(&piSteamId, p + 0x24, 8);
    memcpy(&piSig,     p + 0x2c, 8);
    memcpy(&pcbSig,    p + 0x34, 8);
    if (g_appticket && g_appticketN >= 20 && buf && cbBuf >= g_appticketN) {
        uint32_t sigOff = *(uint32_t *)g_appticket;
        if (sigOff + 128 > g_appticketN) sigOff = (g_appticketN >= 128) ? g_appticketN - 128 : 0;
        memcpy(buf, g_appticket, g_appticketN);
        if (piAppId)   *piAppId   = 16;
        if (piSteamId) *piSteamId = 8;
        if (piSig)     *piSig     = sigOff;
        if (pcbSig)    *pcbSig    = 128;
        *(uint32_t *)(p + 8) = g_appticketN;
        plog("[apps] *** GetAppOwnershipTicketData -> SERVED %u bytes ***", g_appticketN);
        return 0;
    }
    *(uint32_t *)(p + 8) = 0;
    plog("[apps] GetAppOwnershipTicketData asked but no appticket (n=%u cbBuf=%u)", g_appticketN, cbBuf);
    return 0;
}

static long ost_RequestEncryptedAppTicket(void *args)
{
    if (!g_ticketsReady) load_tickets();
    uint8_t *p = (uint8_t *)args;
    uint64_t fake = OST_FAKE_APICALL;
    memcpy(p + 8, &fake, 8);           // params+0x8 = SteamAPICall_t return
    g_injectPending = 1;               // owe a SteamAPICallCompleted_t for this
    plog("[su] RequestEncryptedAppTicket -> fake handle 0x%llx", (unsigned long long)fake);
    return 0;
}

// ------------------------------ inline hooking ------------------------------
static int g_installed = 0;
static int g_hookCount = 0;

static int patch_jmp(void *target, void *handler)
{
    uint8_t patch[14] = { 0xFF, 0x25, 0, 0, 0, 0 };   // jmp qword [rip+0]
    memcpy(patch + 6, &handler, 8);
    long pg = sysconf(_SC_PAGESIZE);
    uintptr_t start = (uintptr_t)target & ~((uintptr_t)pg - 1);
    uintptr_t end   = ((uintptr_t)target + sizeof patch + pg - 1) & ~((uintptr_t)pg - 1);
    if (mprotect((void *)start, end - start, PROT_READ | PROT_WRITE | PROT_EXEC) != 0) {
        plog("[hook] mprotect(RWX) failed @ %p", target);
        return -1;
    }
    memcpy(target, patch, sizeof patch);
    __builtin___clear_cache((char *)target, (char *)target + sizeof patch);
    mprotect((void *)start, end - start, PROT_READ | PROT_EXEC);
    return 0;
}

// ---- trampoline hooking (call-original-then-patch) for GetAPICallResult ----
// A minimal length decoder, just enough for the fixed marshalling-thunk
// prologues. Returns total bytes of whole instructions covering >= need, or 0
// if it meets anything it cannot safely relocate (then we skip that hook).
static int insn_len(uint8_t *p)
{
    int i = 0, rex = 0;
    while (p[i] >= 0x40 && p[i] <= 0x4f) { rex = 1; i++; }
    uint8_t op = p[i++];
    if (op >= 0x50 && op <= 0x5f) return i;            // push/pop r64
    if (op == 0x90) return i;                          // nop
    if (op >= 0xb8 && op <= 0xbf) return i + (rex ? 8 : 4);   // mov reg, imm
    int has_modrm = (op == 0x31 || op == 0x01 || op == 0x29 || op == 0x89 ||
                     op == 0x8b || op == 0x8d || op == 0x85 || op == 0x09 ||
                     op == 0x21 || op == 0x03 || op == 0x2b || op == 0x33 ||
                     op == 0x39 || op == 0x3b || op == 0x83);
    if (!has_modrm) return 0;
    uint8_t modrm = p[i++];
    int mod = modrm >> 6, rm = modrm & 7;
    if (mod != 3 && rm == 4) i++;                      // SIB
    if (mod == 0 && rm == 5) return 0;                 // rip-relative: do not relocate
    if (mod == 1) i++;                                 // disp8
    else if (mod == 2) i += 4;                         // disp32
    if (op == 0x83) i++;                               // grp1 imm8
    return i;
}
static int decode_len(uint8_t *p, int need)
{
    int total = 0;
    while (total < need) { int l = insn_len(p + total); if (!l) return 0; total += l; }
    return total;
}

// EncryptedAppTicketResponse_t: iCallback = k_iSteamUserCallbacks(100) + 54.
#define OST_ETICKET_CB 154
#define OST_ERESULT_OK 1

// Common body for every hooked GetAPICallResult version. rdi=params, rsi=tramp
// (the per-hook stub loads rsi). Runs the real thunk, then if the caller was
// fetching an EncryptedAppTicketResponse_t, forces it to success.
// params: +0x08 bool ret; +0x11 void* pCallback(out buf); +0x1d u32 iExpected;
//         +0x21 bool* pbFailed.
// All three below filter on OUR fake call handle so real async ops are
// untouched (we call the original thunk for any handle that isn't ours).
//
// ISteamUtils::IsAPICallCompleted thunk: {+8 ret; +9 hCall; +0x11 pbFailed}.
__attribute__((used))
static long ost_iscompleted_common(void *args, void *tramp)
{
    uint8_t *p = (uint8_t *)args;
    uint64_t h; memcpy(&h, p + 9, 8);
    if (h == OST_FAKE_APICALL) {
        void *pFail; memcpy(&pFail, p + 0x11, 8);
        if (pFail) *(uint8_t *)pFail = 0;   // not failed
        p[8] = 1;                           // completed = true
        plog("[su] IsAPICallCompleted(eticket handle) -> forced complete");
        return 0;
    }
    return ((long (*)(void *))tramp)(args);
}

// Denuvo registers a CCallResult on the RequestEncryptedAppTicket handle and
// waits for a SteamAPICallCompleted_t (iCallback 703) to be delivered through
// the pump. Real Steam never delivers one for our fabricated handle, so we
// inject it: when the pump has drained the real callbacks (BGetCallback would
// return false) and our request is outstanding, hand back a 703 pointing at a
// SteamAPICallCompleted_t{m_hAsyncCall=magic, m_iCallback=154, m_cubParam=4}.
// The dispatch then calls GetAPICallResult(magic,154), which our hook serves OK.
//
// BGetCallback params: {+0 ret; +0x5 void* unixMsg(out); +0xd CallbackMsg_t*
// (m_hSteamUser@0, m_iCallback@4, m_cubParam@0x10)}.
// Native CallbackMsg_t (the +0x5 alloc): {m_hSteamUser@0; m_iCallback@4;
// m_pubParam@8; m_cubParam@0x10}. We malloc alloc+param per injection: the
// bridge leaks the alloc (FreeLastCallback doesn't free it), and a heap ptr is
// safe whether it is leaked OR freed - a static would crash if it were freed.
#define OST_STEAMAPICALLCOMPLETED 703

__attribute__((used))
static long ost_bgetcallback(void *args, void *tramp)
{
    long ret = ((long (*)(void *))tramp)(args);
    uint8_t *p = (uint8_t *)args;
    if (p[0]) {                              // a real callback was delivered
        void *msg; memcpy(&msg, p + 0xd, 8);
        if (msg) {
            int32_t icb; memcpy(&icb, (uint8_t *)msg + 4, 4);
            plog("[cb] BGetCallback -> iCallback=%d", icb);
        }
        return ret;
    }
    if (g_injectPending) {                   // real queue drained: inject ours
        g_injectPending = 0;
        uint8_t *param = (uint8_t *)malloc(16);      // SteamAPICallCompleted_t
        uint8_t *alloc = (uint8_t *)malloc(0x18);    // native CallbackMsg_t
        if (!param || !alloc) { free(param); free(alloc); return ret; }
        *(uint64_t *)(param + 0)  = OST_FAKE_APICALL;   // m_hAsyncCall
        *(int32_t  *)(param + 8)  = OST_ETICKET_CB;     // m_iCallback = 154
        *(uint32_t *)(param + 12) = 4;                  // m_cubParam (result size)
        *(int32_t  *)(alloc + 0)    = 1;                        // m_hSteamUser
        *(int32_t  *)(alloc + 4)    = OST_STEAMAPICALLCOMPLETED;// m_iCallback = 703
        void *pp = param;
        memcpy(alloc + 8, &pp, 8);                             // m_pubParam
        *(int32_t  *)(alloc + 0x10) = 16;                      // m_cubParam
        void *peMsg; memcpy(&peMsg, p + 0xd, 8);
        if (peMsg) {
            *(int32_t *)((uint8_t *)peMsg + 0)    = 1;
            *(int32_t *)((uint8_t *)peMsg + 4)    = OST_STEAMAPICALLCOMPLETED;
            *(int32_t *)((uint8_t *)peMsg + 0x10) = 16;
        }
        memcpy(p + 0x5, &alloc, 8);
        p[0] = 1;
        plog("[cb] *** injected SteamAPICallCompleted(703) for eticket handle ***");
        return 0;
    }
    return ret;
}

// ISteamUtils::GetAPICallResult thunk: {+8 ret; +9 hCall; +0x11 pCallback;
// +0x1d iCallbackExpected; +0x21 pbFailed}.
__attribute__((used))
static long ost_apicall_common(void *args, void *tramp)
{
    uint8_t *p = (uint8_t *)args;
    uint64_t h; memcpy(&h, p + 9, 8);
    if (h == OST_FAKE_APICALL) {
        void *pCb;   memcpy(&pCb,   p + 0x11, 8);
        void *pFail; memcpy(&pFail, p + 0x21, 8);
        if (pCb)   *(int32_t *)pCb = OST_ERESULT_OK;
        if (pFail) *(uint8_t *)pFail = 0;
        p[8] = 1;                 // API bool return (in params) = true
        plog("[su] ISteamUtils::GetAPICallResult(eticket) -> forced OK");
        return 0;                 // NTSTATUS must be 0 (PE side asserts !status)
    }
    return ((long (*)(void *))tramp)(args);
}

// Flat steamclient_Steam_GetAPICallResult (the one the CCallResult dispatch
// uses): {+0 ret; +5 hCall; +0xd pCallback; +0x15 iCallbackExpected;
// +0x1d pbFailed}.
__attribute__((used))
static long ost_flat_apicall_common(void *args, void *tramp)
{
    uint8_t *p = (uint8_t *)args;
    uint64_t h; memcpy(&h, p + 5, 8);
    if (h == OST_FAKE_APICALL) {
        void *pCb;   memcpy(&pCb,   p + 0xd,  8);
        void *pFail; memcpy(&pFail, p + 0x1d, 8);
        if (pCb)   *(int32_t *)pCb = OST_ERESULT_OK;
        if (pFail) *(uint8_t *)pFail = 0;
        p[0] = 1;                 // API bool return (in params) = true
        plog("[su] *** Steam_GetAPICallResult(eticket) -> forced OK ***");
        return 0;                 // NTSTATUS must be 0 (PE side asserts !status)
    }
    return ((long (*)(void *))tramp)(args);
}

// Build [trampoline | stub] in one RWX page and point target at the stub.
// `common` is the (args, tramp) body to run after the real thunk.
static int hook_apicall(void *target, void *common)
{
    int len = decode_len((uint8_t *)target, 14);
    if (!len) { plog("[hook] GetAPICallResult prologue unrecognised @ %p (skipped)", target); return -1; }
    uint8_t *mem = mmap(NULL, 128, PROT_READ | PROT_WRITE | PROT_EXEC, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (mem == MAP_FAILED) return -1;
    // trampoline: original prologue + jmp to (target+len)
    memcpy(mem, target, len);
    mem[len] = 0xFF; mem[len + 1] = 0x25; *(uint32_t *)(mem + len + 2) = 0;
    void *cont = (uint8_t *)target + len;
    memcpy(mem + len + 6, &cont, 8);
    void *tramp = mem;
    // stub: mov rsi, tramp ; mov rax, common ; jmp rax   (rdi=args preserved)
    uint8_t *stub = mem + len + 14;
    stub[0] = 0x48; stub[1] = 0xBE; memcpy(stub + 2,  &tramp,  8);
    stub[10] = 0x48; stub[11] = 0xB8; memcpy(stub + 12, &common, 8);
    stub[20] = 0xFF; stub[21] = 0xE0;
    return patch_jmp(target, stub);
}

// Locate lsteamclient.so (base + on-disk path) in this process.
static uintptr_t g_lsc_base = 0;
static char      g_lsc_path[512];
static int lsc_cb(struct dl_phdr_info *info, size_t size, void *data)
{
    (void)size; (void)data;
    if (info->dlpi_name && strstr(info->dlpi_name, "lsteamclient.so")) {
        g_lsc_base = (uintptr_t)info->dlpi_addr;
        snprintf(g_lsc_path, sizeof g_lsc_path, "%s", info->dlpi_name);
        return 1;
    }
    return 0;
}

// Parse lsteamclient.so's .symtab from disk and hook every matching thunk.
// Robust across Proton builds: we match by symbol NAME, not a baked offset.
static void install_hooks(void)
{
    if (g_installed) return;
    g_lsc_base = 0; g_lsc_path[0] = 0;
    dl_iterate_phdr(lsc_cb, NULL);
    if (!g_lsc_base || !g_lsc_path[0]) return;   // not mapped yet

    int fd = open(g_lsc_path, O_RDONLY);
    if (fd < 0) { plog("[hook] cannot open %s", g_lsc_path); return; }
    off_t fsz = lseek(fd, 0, SEEK_END); lseek(fd, 0, SEEK_SET);
    uint8_t *f = mmap(NULL, fsz, PROT_READ, MAP_PRIVATE, fd, 0);
    close(fd);
    if (f == MAP_FAILED) { plog("[hook] mmap %s failed", g_lsc_path); return; }

    Elf64_Ehdr *eh = (Elf64_Ehdr *)f;
    if (memcmp(eh->e_ident, ELFMAG, SELFMAG) != 0) { munmap(f, fsz); return; }
    Elf64_Shdr *sh = (Elf64_Shdr *)(f + eh->e_shoff);
    Elf64_Sym  *syms = NULL; const char *strs = NULL; size_t nsyms = 0;
    for (int i = 0; i < eh->e_shnum; ++i) {
        if (sh[i].sh_type == SHT_SYMTAB) {
            syms  = (Elf64_Sym *)(f + sh[i].sh_offset);
            nsyms = sh[i].sh_size / sizeof(Elf64_Sym);
            strs  = (const char *)(f + sh[sh[i].sh_link].sh_offset);
        }
    }
    if (!syms || !strs) { plog("[hook] no .symtab in %s (stripped?)", g_lsc_path); munmap(f, fsz); return; }

    g_installed = 1;   // set before patching so re-entrancy can't double-install
    for (size_t i = 0; i < nsyms; ++i) {
        const char *nm = strs + syms[i].st_name;
        if (!nm[0] || syms[i].st_value == 0) continue;

        // ISteamAppTicket::GetAppOwnershipTicketData -> serve the pool appticket.
        if (!strncmp(nm, "ISteamAppTicket_", 16) && strstr(nm, "_GetAppOwnershipTicketData")) {
            void *target = (void *)(g_lsc_base + syms[i].st_value);
            if (patch_jmp(target, (void *)ost_GetAppOwnershipTicketData) == 0) {
                g_hookCount++; plog("[hook] %-52s @ %p", nm, target);
            }
            continue;
        }

        // ISteamApps ownership check -> force owned (clears load error 6).
        if (!strncmp(nm, "ISteamApps_", 11)) {   // excludes wow64_ISteamApps_
            size_t l = strlen(nm);
            int is_sub    = (l >= 14 && !strcmp(nm + l - 14, "_BIsSubscribed"));
            int is_subapp = (l >= 17 && !strcmp(nm + l - 17, "_BIsSubscribedApp"));
            if (is_sub || is_subapp) {
                void *target = (void *)(g_lsc_base + syms[i].st_value);
                if (patch_jmp(target, (void *)ost_force_subscribed) == 0) {
                    g_hookCount++; plog("[hook] %-52s @ %p", nm, target);
                }
                continue;
            }
        }

        // The callback dispatch fetches results through the FLAT
        // steamclient_Steam_GetAPICallResult - THIS is the one that matters.
        // Also hook the ISteamUtils vtable version for coverage. Both trampoline
        // to force the EncryptedAppTicketResponse_t to success. Skip wow64_.
        if (!strcmp(nm, "steamclient_Steam_GetAPICallResult")) {
            void *target = (void *)(g_lsc_base + syms[i].st_value);
            if (hook_apicall(target, (void *)ost_flat_apicall_common) == 0) {
                g_hookCount++; plog("[hook] %-52s @ %p", nm, target);
            }
            continue;
        }
        if (!strcmp(nm, "steamclient_Steam_BGetCallback")) {
            void *target = (void *)(g_lsc_base + syms[i].st_value);
            if (hook_apicall(target, (void *)ost_bgetcallback) == 0) {
                g_hookCount++; plog("[hook] %-52s @ %p", nm, target);
            }
            continue;
        }
        if (!strncmp(nm, "ISteamUtils_SteamUtils", 22) && strstr(nm, "_GetAPICallResult")) {
            void *target = (void *)(g_lsc_base + syms[i].st_value);
            if (hook_apicall(target, (void *)ost_apicall_common) == 0) {
                g_hookCount++; plog("[hook] %-52s @ %p", nm, target);
            }
            continue;
        }
        if (!strncmp(nm, "ISteamUtils_SteamUtils", 22) && strstr(nm, "_IsAPICallCompleted")) {
            void *target = (void *)(g_lsc_base + syms[i].st_value);
            if (hook_apicall(target, (void *)ost_iscompleted_common) == 0) {
                g_hookCount++; plog("[hook] %-52s @ %p", nm, target);
            }
            continue;
        }

        if (strncmp(nm, "ISteamUser_SteamUser", 20) != 0) continue;   // skips wow64_ too
        void *handler = NULL;
        if      (strstr(nm, "_GetSteamID"))                 handler = (void *)ost_GetSteamID;
        else if (strstr(nm, "_GetEncryptedAppTicket"))      handler = (void *)ost_GetEncryptedAppTicket;
        // RequestEncryptedAppTicket -> fake handle (default). The completion is
        // then made to look successful via the IsAPICallCompleted /
        // GetAPICallResult hooks above, which filter on that fake handle.
        else if (strstr(nm, "_RequestEncryptedAppTicket") && g_fakeRequest)
            handler = (void *)ost_RequestEncryptedAppTicket;
        else continue;
        void *target = (void *)(g_lsc_base + syms[i].st_value);
        if (patch_jmp(target, handler) == 0) {
            g_hookCount++;
            plog("[hook] %-52s @ %p", nm, target);
        }
    }
    munmap(f, fsz);
    plog("[hook] === installed %d thunk hooks in lsteamclient.so (base %p) ===",
         g_hookCount, (void *)g_lsc_base);
    load_tickets();
}

// ------------------------------ dlopen watcher ------------------------------
typedef void *(*fn_dlopen)(const char *, int);
static fn_dlopen g_real_dlopen;
void *dlopen(const char *file, int mode)
{
    if (!g_real_dlopen) {
        g_real_dlopen = (fn_dlopen)dlsym(RTLD_NEXT, "dlopen");
        if (!g_real_dlopen) return NULL;
    }
    void *h = g_real_dlopen(file, mode);
    if (g_interesting && !g_installed && file && strstr(file, "lsteamclient.so") && h) {
        plog("[dl] lsteamclient.so loaded -> installing hooks");
        install_hooks();
    }
    return h;
}

// --------------------------------- startup ---------------------------------
static void *late_worker(void *arg)
{
    (void)arg;
    for (int i = 0; i < 40 && !g_installed; ++i) { usleep(500000); install_hooks(); }
    for (int i = 0; i < 30 && !g_ticketsReady; ++i) { usleep(1000000); load_tickets(); }
    return NULL;
}

// Crash handler: turn a silent death into a logged fault address + backtrace,
// with each frame's offset into lsteamclient.so so we can locate it in the .so.
static void ost_crash(int sig, siginfo_t *si, void *uc)
{
    (void)uc;
    int fd = open(g_logPath, O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (fd >= 0) {
        char b[256];
        int n = snprintf(b, sizeof b, "[%d %s] [CRASH] sig=%d addr=%p lsc_base=%p\n",
                         (int)getpid(), g_comm, sig, si ? si->si_addr : 0, (void *)g_lsc_base);
        if (write(fd, b, n) < 0) { /* ignore */ }
        void *bt[40];
        int m = backtrace(bt, 40);
        for (int i = 0; i < m; ++i) {
            uintptr_t off = g_lsc_base ? ((uintptr_t)bt[i] - g_lsc_base) : 0;
            n = snprintf(b, sizeof b, "[%d %s] [CRASH]  #%02d %p  lsc+0x%lx\n",
                         (int)getpid(), g_comm, i, bt[i], (unsigned long)off);
            if (write(fd, b, n) < 0) { /* ignore */ }
        }
        close(fd);
    }
    _exit(139);
}
static void install_crash_handler(void)
{
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_sigaction = ost_crash;
    sa.sa_flags = SA_SIGINFO;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGSEGV, &sa, NULL);
    sigaction(SIGABRT, &sa, NULL);
    sigaction(SIGBUS,  &sa, NULL);
    sigaction(SIGILL,  &sa, NULL);
}

__attribute__((constructor)) static void ost_init(void)
{
    const char *env = getenv("OST_NATIVE_LOG");
    const char *home = getenv("HOME");
    if (env && env[0])        snprintf(g_logPath, sizeof g_logPath, "%s", env);
    else if (home && home[0]) snprintf(g_logPath, sizeof g_logPath, "%s/ost_native_hook.log", home);
    else                      snprintf(g_logPath, sizeof g_logPath, "/tmp/ost_native_hook.log");

    read_proc("/proc/self/comm", g_comm, sizeof g_comm, 0);
    read_proc("/proc/self/cmdline", g_cmd, sizeof g_cmd, 1);
    g_interesting = (strstr(g_cmd, ".exe") != NULL) || (strstr(g_cmd, ".EXE") != NULL);

    // Fake the RequestEncryptedAppTicket handle by default: passing it through
    // to real Steam fails at the API-call level (unowned app) and Denuvo aborts
    // before fetching the ticket. Faking the handle + forcing IsAPICallCompleted
    // / GetAPICallResult for that handle makes the async look successful.
    // OST_PASSTHROUGH=1 reverts to letting real Steam handle the request.
    const char *pt = getenv("OST_PASSTHROUGH");
    g_fakeRequest = !(pt && pt[0] && pt[0] != '0');

    plog("=== OST native hook N2 loaded === interesting=%d fake_request=%d cmd=%s",
         g_interesting, g_fakeRequest, g_cmd);
    if (!g_interesting) return;

    install_crash_handler();
    g_appId = resolve_appid();
    load_tickets();          // prefix is usually already written by the redeemer
    install_hooks();         // in case lsteamclient.so is already mapped
    pthread_t t;
    if (pthread_create(&t, NULL, late_worker, NULL) == 0) pthread_detach(t);
}
