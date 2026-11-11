# fmtrewrite

A C++17 rewriter that mechanically fixes C11 calls where a **runtime string**
is passed in the **format-argument position** of a formatting output function.
It is built to run before code review: everything that can be proven safe and
fixed textually is emitted as a unified diff; everything ambiguous is routed
to a review list with a precise location and reason.

The defect it targets is the classic `-Wformat-security` pattern:

```c
const char *msg = user_input;
printf(msg);          // msg may contain '%' specifiers interpreted at runtime
syslog(LOG_ERR, msg);
```

The mechanical fix binds the runtime string as an ordinary argument to a
literal `"%s"` format, preserving the expression's exact spelling, evaluation
count, order and surrounding comments:

```c
printf("%s", msg);
syslog(LOG_ERR, "%s", msg);
```

## Why Clang JSON AST

The tool drives the system `clang` as a subprocess with
`-Xclang -ast-dump=json` and analyzes the resulting AST plus its source
ranges. This requires no LLVM development headers or linked libraries — only a
working `clang` on `PATH` (override with `--clang`). A self-contained JSON
parser lives in `src/Json.*`; no third-party dependencies are pulled in.

## Supported functions (predeclared)

The two supported targets and their format-argument positions are declared up
front in `src/main.cpp`:

| Function | Canonical type                      | Format arg (0-based) |
|----------|-------------------------------------|----------------------|
| `printf` | `int (const char *, ...)`           | 0                    |
| `syslog` | `void (int, const char *, ...)`     | 1                    |

A call is only considered bound to a supported library declaration when the
named entity's type matches the predeclared signature **and** no
same-named declaration exists in the main translation unit. A user function
or shadow redeclaration that happens to be called `printf` therefore never
matches, even if the compiler merges it with the system-header declaration.

## Decision rules, in order

For every call whose spelling names a target function and lives in the main
file:

1. **Macro expansion** — the call or its format argument is produced by a
   macro (the AST location carries `spellingLoc`/`expansionLoc` or
   `isMacroArgExpansion`). Routed to review; rewriting one expansion site
   could change other sites of the same macro.
2. **String literal** — the format argument is a `StringLiteral` (adjacent
   literals like `"a" "b"` are already concatenated by Clang). Safe; left
   untouched regardless of trailing varargs.
3. **Unsupported callee** — a same-named declaration exists in the main file,
   so the call cannot be proven to bind to the library declaration.
4. **Non-string type** — the *semantic* (pre-implicit-cast) argument type is
   not a `char` pointer or `char[]` (e.g. `int`, `wchar_t *`). Routed to
   review.
5. **Runtime specifiers** — arguments follow the format slot. They may be
   consumed by conversion specifiers inside the runtime string; inserting
   `"%s"` would leave them unconsumed (`-Wformat-extra-args`) and change
   behavior. Routed to review.
6. **Mechanically fixable** — everything else. A literal `"%s", ` is inserted
   immediately before the original argument's first token.

## What is preserved

* **Evaluation count and order** — the original expression is not duplicated,
  moved or re-typed; only a literal is inserted before it.
* **Comments and whitespace** — insertion is token-offset based, so comments
  attached to the argument stay in place.
* **Compilability** — the generated source compiles under
  `-Werror -Wformat-security`; the test suite enforces this.
* **Idempotency** — after rewriting, the format slot is itself a literal, so a
  second run produces an empty diff and no file change.

## Build and usage

```sh
make                       # builds build/fmtrewrite (C++17, no extra deps)
make test                  # runs the full end-to-end suite
```

```text
usage: fmtrewrite [--apply] [--clang <path>] [--json] [--quiet]
                  <source.c> [-- <extra clang arguments>]

  --apply        rewrite the file in place instead of printing a diff
  --clang PATH   clang executable to use (default: clang)
  --json         print the machine-readable report to stdout
  --quiet        suppress the human-readable report
```

Default mode prints a standard unified diff (consumable by `patch -u`) and a
summary; `--json` emits counts and the review list; `--apply` writes the
rewritten file in place. Clang diagnostics belonging to the
`-Wformat-security` class are expected and filtered; other diagnostics are
surfaced on stderr but do not block analysis when Clang still emits an AST.

## Layout

```
src/
  Json.*       self-contained JSON parser (arena-owned DOM)
  Rewriter.*   AST walk, declaration binding, classification, edits
  Diff.*       LCS-based unified diff and insertion application
  main.cpp     CLI, clang invocation, diagnostic filtering, reports
tests/
  cases/       C11 fixtures for each decision path
  expected/    captured runtime output
  syslog_shim.h  test-only redirect of syslog to stdout
  run_tests.sh end-to-end verification
```

## Review-list reasons

| Reason              | Meaning                                                        |
|---------------------|----------------------------------------------------------------|
| `UnsupportedCallee` | does not bind to a supported library declaration               |
| `MacroExpansion`    | call or format argument comes from macro expansion              |
| `NonStringType`     | format argument is not a `char` pointer/array                   |
| `RuntimeSpecifiers` | trailing arguments imply runtime conversion specifiers          |
