// -----------------------------------------------------------------------------
// OST native-side probe - Stage N1.
//
// WHY THIS EXISTS
// Code-virtualized Denuvo (Monster Hunter Stories 3, appid 2852190) detects our
// Wine-side vtable patches and answers by corrupting its own control flow, so
// the game dies at a fixed address inside Denuvo's own code section. Proven by
// control runs: with our DLLs absent there is no fault anywhere.
//
// The fix is to intercept where Denuvo cannot look. Under Proton the game loads
// lsteamclient.dll (Valve's bridge), whose unix half dlopen()s Steam's real
// native steamclient.so. That is ELF memory in the same process, but Denuvo's
// integrity checks are PE-module based - it inspects steamclient64.dll, not a
// Linux shared object. Intercepting natively is the same architecture OST uses
// on Windows (patch the client, leave the game process pristine).
//
// THIS STAGE ONLY WATCHES. Nothing is hooked, nothing is modified. It answers:
//   1. does an LD_PRELOAD .so actually land in the Proton game process?
//   2. which native library does the bridge load, and from exactly which path?
//   3. does that library export CreateInterface, and at what address?
//   4. what else is loaded natively that we could hook instead?
//
// Build:  ./build.sh          Run:  set the wrapper script as the launch option.
// -----------------------------------------------------------------------------
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <link.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

// --------------------------------- logging ---------------------------------
static char g_logPath[512];
static char g_comm[64]  = "?";
static char g_cmd[256]  = "";
static int  g_interesting = 0;   // is this a wine/game process worth logging in detail?

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
    char body[1024];
    char line[1200];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(body, sizeof body, fmt, ap);
    va_end(ap);
    snprintf(line, sizeof line, "[%d %s] %s", (int)getpid(), g_comm, body);
    log_line(line);
}

// Read a /proc file into buf, turning NULs into spaces (for cmdline).
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

// ---------------------------- loaded-object dump ----------------------------
static int phdr_cb(struct dl_phdr_info *info, size_t size, void *data)
{
    (void)size;
    (void)data;
    if (info->dlpi_name && info->dlpi_name[0])
        plog("[obj]   %s @ %p", info->dlpi_name, (void *)info->dlpi_addr);
    return 0;
}

// Look for an already-loaded steamclient and report its CreateInterface. Uses
// RTLD_NOLOAD so we never cause a load ourselves - we only inspect.
static int steam_cb(struct dl_phdr_info *info, size_t size, void *data)
{
    (void)size;
    int *found = (int *)data;
    if (!info->dlpi_name || !info->dlpi_name[0]) return 0;
    if (!strstr(info->dlpi_name, "steamclient")) return 0;

    *found = 1;
    plog("[steam] native steamclient object: %s @ %p", info->dlpi_name, (void *)info->dlpi_addr);
    void *h = dlopen(info->dlpi_name, RTLD_LAZY | RTLD_NOLOAD);
    if (!h) { plog("[steam]   RTLD_NOLOAD failed: %s", dlerror()); return 0; }
    static const char *names[] = {
        "CreateInterface", "SteamClient", "Steam_GetAPICallResult",
        "Steam_BGetCallback", "Steam_FreeLastCallback", "SteamAPI_Init"
    };
    for (unsigned i = 0; i < sizeof names / sizeof names[0]; ++i) {
        void *p = dlsym(h, names[i]);
        plog("[steam]   %-24s = %p", names[i], p);
    }
    dlclose(h);   // NOLOAD handle, just drops our reference
    return 0;
}

static void dump_state(const char *why)
{
    plog("[obj] --- loaded ELF objects (%s) ---", why);
    dl_iterate_phdr(phdr_cb, NULL);
    int found = 0;
    dl_iterate_phdr(steam_cb, &found);
    if (!found) plog("[steam] no native steamclient object loaded yet");
}

// ------------------------------ dlopen watcher ------------------------------
// Interposing dlopen is enough to see how the bridge reaches native Steam. We
// deliberately do NOT interpose dlsym: resolving our own real dlopen needs it,
// and interposing both invites recursion for no extra information.
typedef void *(*fn_dlopen)(const char *, int);
static fn_dlopen g_real_dlopen;

void *dlopen(const char *file, int mode)
{
    if (!g_real_dlopen) {
        g_real_dlopen = (fn_dlopen)dlsym(RTLD_NEXT, "dlopen");
        if (!g_real_dlopen) return NULL;
    }
    void *h = g_real_dlopen(file, mode);
    if (g_interesting && file) {
        int steam = (strstr(file, "steamclient") != NULL) || (strstr(file, "steam_api") != NULL);
        plog("[dl] dlopen(\"%s\", 0x%x) -> %p%s", file, mode, h, steam ? "   <<< STEAM" : "");
        if (steam && h) {
            void *ci = dlsym(h, "CreateInterface");
            plog("[dl]   CreateInterface = %p", ci);
            dump_state("after steam dlopen");
        }
    }
    return h;
}

// -------------------------------- background --------------------------------
// The bridge loads native Steam a little after startup, so take one late
// snapshot as a safety net in case the dlopen interposer misses it (statically
// linked, preloaded, or loaded before we were).
static void *late_dump(void *arg)
{
    (void)arg;
    sleep(12);
    dump_state("T+12s");
    sleep(18);
    dump_state("T+30s");
    return NULL;
}

__attribute__((constructor)) static void ost_init(void)
{
    const char *env = getenv("OST_NATIVE_LOG");
    const char *home = getenv("HOME");
    if (env && env[0])       snprintf(g_logPath, sizeof g_logPath, "%s", env);
    else if (home && home[0]) snprintf(g_logPath, sizeof g_logPath, "%s/ost_native_probe.log", home);
    else                      snprintf(g_logPath, sizeof g_logPath, "/tmp/ost_native_probe.log");

    read_proc("/proc/self/comm", g_comm, sizeof g_comm, 0);
    read_proc("/proc/self/cmdline", g_cmd, sizeof g_cmd, 1);

    // Steam spawns a lot of helper processes. Only the ones running a Windows
    // exe are worth detailed logging, otherwise the log is unreadable.
    g_interesting = (strstr(g_cmd, ".exe") != NULL) || (strstr(g_cmd, ".EXE") != NULL);

    plog("=== OST native probe N1 loaded === interesting=%d cmd=%s", g_interesting, g_cmd);
    if (!g_interesting) return;

    dump_state("at load");
    pthread_t t;
    if (pthread_create(&t, NULL, late_dump, NULL) == 0) pthread_detach(t);
}
