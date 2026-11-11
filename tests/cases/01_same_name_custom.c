/*
 * Test 1: same-named custom function.
 *
 * A user-declared function that happens to be called printf, with a
 * signature the compiler accepts as a redeclaration, must never be mistaken
 * for the supported library declaration. Because its declaration lives in the
 * main file (not in a system header), calls binding to it are routed to the
 * review list with an unsupported-callee reason.
 */
#include <stdarg.h>
#include <stdio.h>

/*
 * A compatible redeclaration in the main translation unit. Calls below bind
 * to this declaration rather than to the one coming from <stdio.h>.
 */
extern int printf(const char *custom, ...);

int main(void) {
    const char *runtime = "runtime text";
    printf(runtime);          /* custom printf: review, never rewritten */
    printf(runtime, 1, 2);    /* same declaration, extra varargs */
    return 0;
}
