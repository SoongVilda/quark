/* Shared by every probe: one verdict line written where the suite reads it.
 * The verdict lands in a file, not the exit code, because the game runs
 * under steam.exe and its exit status is not guaranteed to come back. */
#ifndef QUARK_PROBE_H
#define QUARK_PROBE_H

#include <windows.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>

#ifndef PROBE_NAME
#error "build with -DPROBE_NAME=\"name\""
#endif

#define PROBE_RESULT_DIR "Z:\\tmp\\quark-suite\\probe-results\\"

static void probe_verdict(int ok, const char *fmt, ...)
{
    char msg[512];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(msg, sizeof msg, fmt, ap);
    va_end(ap);

    FILE *f = fopen(PROBE_RESULT_DIR PROBE_NAME ".txt", "w");
    if (f) {
        fprintf(f, "%s %s\n", ok ? "PASS" : "FAIL", msg);
        fclose(f);
    }
    printf("%s %s: %s\n", ok ? "PASS" : "FAIL", PROBE_NAME, msg);
    exit(ok ? 0 : 1);
}

#define PROBE_REQUIRE(cond, ...) \
    do { if (!(cond)) probe_verdict(0, __VA_ARGS__); } while (0)

#endif
