# secscan Claude Code plugin

Adds the `/secscan` skill and the `secscan-triager` agent.

```
claude plugin marketplace add zapai-inc/secscan
claude plugin install secscan@zapai
```

The skill runs the CLI from `SECSCAN_HOME` when that variable points at a checkout of this
repo, otherwise via `npx --yes github:zapai-inc/secscan`. Set `SECSCAN_TOOLS_DIR` to choose
where pinned scanner binaries are cached (default `~/.secscan/bin`).

Without the plugin system, copy `skills/secscan` into `~/.claude/skills/` and
`agents/secscan-triager.md` into `~/.claude/agents/`.
