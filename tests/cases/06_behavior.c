/*
 * Runtime behavior fixture.
 *
 * Every call here is mechanically fixable: a runtime string occupies the
 * format slot and no arguments follow it, so the safe "%s" binding is exact
 * and preserves behavior (the string is printed verbatim, '%' bytes become
 * ordinary characters). Expected stdout is in tests/expected/behavior.txt.
 */
#include <stdio.h>
#include <syslog.h>

int main(void) {
    const char *user_controlled = "100% done: 50% of the 99%";
    const char *second = "second %s %d line";
    char stack_buf[32] = "buffer % contents";

    printf(user_controlled);
    putchar('\n');
    printf(second);
    putchar('\n');
    printf(stack_buf);
    putchar('\n');
    syslog(LOG_DEBUG, user_controlled);   /* exercised under the test shim */
    return 0;
}
