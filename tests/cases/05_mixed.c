/*
 * Test 5: mixed file exercising every decision path at once.
 *
 * One translation unit containing literal-safe calls, fixable calls, a macro
 * case and a same-named custom declaration.
 */
#include <stdarg.h>
#include <stdio.h>
#include <syslog.h>

#define STR_MACRO "from macro"

/* Compatible redeclaration in the main file: calls binding to it must be
 * treated as unsupported, even though the spelling is printf. */
extern int printf(const char *custom, ...);

int main(void) {
    const char *runtime = "dynamic";
    int number = 3;

    printf("literal %d\n", number);          /* safe: literal */
    printf(runtime);                         /* binds the main-file decl: review */
    syslog(LOG_ERR, runtime);                /* fixable: real library syslog */
    printf(STR_MACRO);                       /* review: macro expansion */
    (void)number;
    return 0;
}
