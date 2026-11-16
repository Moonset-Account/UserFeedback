# V5s — Strict CPE 2.3 and PURL mapping validation

## CPE contract

`parse_cpe23()`:

- tokenizes unescaped component delimiters;
- accepts escaped delimiters such as `\\:` inside a component;
- decodes escaped component characters in the returned fields;
- rejects invalid escapes, empty components, whitespace, and unescaped wildcard characters;
- preserves `*` and `-` as complete wildcard/not-applicable component values;
- performs no CPE-to-PURL inference.

## PURL contract

Operator mappings require a syntactically valid `pkg:` URL with:

- a lowercase package type;
- a non-empty package path/name;
- no whitespace or empty binding;
- optional version, qualifiers, and subpath in the accepted PURL shape.

Operator approval, confidence, and rationale remain mandatory.
