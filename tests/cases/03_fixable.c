/*
 * Test 3: mechanically fixable calls and review-routed edge cases.
 *
 * Runtime strings reach the format position of printf (index 0) and syslog
 * (index 1). Fixable calls receive a "%s" literal before the original
 * argument, preserving exact spelling, evaluation count, comments and order.
 * Calls with arguments after the format slot are routed to review because
 * those arguments may be consumed by runtime conversion specifiers.
 */
#include <stdio.h>
#include <syslog.h>

static const char *make_message(void);

int main(void) {
    const char *msg = "hello runtime";
    char buffer[64] = "stack buffer";
    int code = 9;

    printf(msg);                         /* simplest fixable printf */
    syslog(LOG_ERR, msg);                /* format is argument index 1 */
    printf(buffer);                      /* char array decays to pointer */
    printf((msg));                       /* parenthesised expression */
    printf(make_message() /* keep me */); /* comment inside the call stays */

    printf(msg, code);                   /* review: trailing varargs */
    syslog(LOG_INFO, buffer, code);      /* review: trailing varargs */
    return 0;
}

static const char *make_message(void) {
    return "from a function call";
}
