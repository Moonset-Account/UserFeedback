# Offline Scanner agent-hook input

The open-source Scanner accepts a bounded JSON envelope on **stdin** and scans only the supplied unified diff. It does not execute agent output or target-project code.

Supported labels for generic adapters, Codex and Claude Code:

- `generic` — schema `coderisktools.agent-hook`, event `post-change`;
- `codex` — schema `coderisktools.codex-hook`, event `post-change`;
- `claude-code` — schema `coderisktools.claude-hook`, `hook_event_name` set to `Stop`.

Examples:

```bash
python -m src hook --agent generic < hook-envelope.json
python -m src hook --agent codex < hook-envelope.json
python -m src hook --agent claude-code < hook-envelope.json
```

The parser is strict and bounded. Duplicate keys, unknown fields, malformed JSON, oversized input and unsupported labels fail closed. Reports redact matched values and line content.

## Exit codes

- `0`: clean or warning-only;
- `1`: secret finding at the failure threshold;
- `2`: config-only failure;
- `3`: malformed or unsafe input.

This integration is **offline**. It does not claim native compatibility with undocumented vendor payloads; an adapter must construct the documented CodeRiskTools envelope.
