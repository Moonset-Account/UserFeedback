/*
 * Test 2: legitimate literal format strings.
 *
 * Calls already passing a string literal (including adjacent concatenated
 * literals and literals with conversion specifiers) require no change; the
 * tool must produce an empty diff and no review entries for them.
 */
#include <stdio.h>
#include <syslog.h>

int main(int argc, char **argv) {
    (void)argc;
    (void)argv;
    printf("plain literal\n");
    printf("with specifiers: %d %s\n", 7, "ok");
    printf("adjacent " "literal %s\n", "x");
    syslog(LOG_ERR, "syslog literal %d", 1);
    syslog(LOG_WARNING,
           "multi-line adjacent "
           "literal %s", "y");
    return 0;
}
