/*
 * Test 4: macro definitions and macro-expanded calls.
 *
 * Any call (or format argument) whose spelling lives inside a macro body, or
 * that is passed through a macro, cannot be rewritten textually without
 * risking the macro's other expansion sites. Every such case must appear in
 * the review list, pointing at the expansion site and naming the macro path.
 */
#include <stdio.h>
#include <syslog.h>

#define LITERAL_MACRO "macro literal"
#define RUNTIME_MACRO runtime_msg
#define WRAPPED_PRINTF(...) printf(__VA_ARGS__)
#define LOG(level, fmt) syslog(level, fmt)

int main(void) {
    const char *runtime_msg = "runtime";

    printf(LITERAL_MACRO);              /* format spelling is a macro body */
    printf(RUNTIME_MACRO);              /* argument expanded from a macro */
    WRAPPED_PRINTF(runtime_msg);        /* the whole call is a macro */
    LOG(LOG_ERR, runtime_msg);          /* syslog reached through a macro */
    return 0;
}
