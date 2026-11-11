/*
 * Test-only syslog shim, force-included when compiling the runtime behavior
 * fixture. It redirects syslog() calls to fprintf so the output can be
 * captured without touching the system log. This file is never used by the
 * analyzer itself: fmtrewrite still resolves syslog against the real system
 * headers.
 *
 * Inclusion order matters: this header is force-included before the fixture,
 * which then includes <syslog.h>. The helper therefore uses a name that
 * cannot collide with the platform declaration, and the syslog macro is
 * installed only after the platform header has finished declaring syslog().
 */
#ifndef FMTREWRITE_TEST_SYSLOG_SHIM_H
#define FMTREWRITE_TEST_SYSLOG_SHIM_H

#include <stdarg.h>
#include <stdio.h>

#ifdef __cplusplus
extern "C" {
#endif

static inline int fmtrewrite_capture_log(int priority, const char *fmt, ...) {
    (void)priority;
    va_list ap;
    va_start(ap, fmt);
    int n = vfprintf(stdout, fmt, ap);
    va_end(ap);
    fputc('\n', stdout);
    return n;
}

#ifdef __cplusplus
}
#endif

/*
 * Pull in the real header ourselves while no overriding macro is active, so
 * its syslog() declaration is parsed cleanly. Then install the redirect.
 */
#ifndef FMTREWRITE_SYSLOG_HEADER_DONE
#define FMTREWRITE_SYSLOG_HEADER_DONE 1
#include <syslog.h>
#endif

#ifdef syslog
#undef syslog
#endif
#define syslog(fmtrewrite_pri, ...) \
    fmtrewrite_capture_log((fmtrewrite_pri), __VA_ARGS__)

#ifndef LOG_DEBUG
#define LOG_DEBUG 7
#endif

#endif /* FMTREWRITE_TEST_SYSLOG_SHIM_H */
