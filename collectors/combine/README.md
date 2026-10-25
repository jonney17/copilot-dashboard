# Combine: one report for all collectors

Merges the [VS Code](../vscode/README.md), [Copilot CLI](../copilot_cli/README.md) and
[IntelliJ](../intellij/README.md) reports into a single report with the same schema, so
the dashboard reads one file. Standard library only.

```bash
python3 collectors/combine/combine.py --format tree                    # readable tree
python3 collectors/combine/combine.py --out reports/combined.json        # JSON report
python3 collectors/combine/combine.py --run --out reports/combined.json  # collect fresh, then combine
python3 collectors/combine/combine.py --input a.json --input b.json      # explicit report files
```

By default it reads `reports/vscode-copilot.json`, `reports/copilot-cli.json` and
`reports/intellij.json`. With `--run` it runs the three collectors instead, each with its
own `config.json`, and combines their output; nothing is written in between.

### Config file

Same rules as the collectors (command line > [`config.json`](config.json) > built-in default;
relative paths resolve against the config file).

| Key | CLI override | Built-in default |
|---|---|---|
| `inputs` | `--input FILE` (repeatable; replaces the list) | the three files in `reports/` |
| `run` | `--run` / `--no-run` | `false` |
| `format` | `--format json\|tree` | `json` |
| `out` | `--out FILE`, or `--out -` for stdout | `null` → stdout |

## Combined report

```
report
├── collector:"combined", collectorVersion, generatedAt
├── sources[]           {collector, collectorVersion, generatedAt, file, sessions, totals}: one per input report
├── totals              over all sessions (same fields as each collector) + premiumRequests,
│                       backgroundCredits, sessionsWithoutUsage
├── byTicket            {"LEGO-123" | "(no ticket)": {sessions, sessionIds[], requests, credits ×3}}  ← per-ticket view
├── byWorkspace, byModel
├── bySource            {"vscode-copilot-chat" | "copilot-cli" | "intellij": {…}}
├── bySurface           {"vscode" | "copilot-cli" | "intellij/ai-assistant" | "intellij/copilot-plugin": {…}}
├── account.quota       latest quota snapshot from any report (account-wide "X of 200 used")
├── duplicatesDropped[] {sessionId, droppedFrom, keptFrom}
├── sessions[]          every session as its collector emitted it, plus:
│                         surface              (as in bySurface)
│                         _source.collector    which report it came from
└── errors[]            unreadable inputs + each report's own errors (tagged with collector)
```

"credits ×3" = `copilotCredits`, `copilotCreditsRounded`, `copilotCreditsRoundedUp`; rounded
totals are sums of per-request rounded values, as in every collector.

## De-duplication

With default settings the collectors don't overlap: the CLI collector skips JetBrains
sessions, which the IntelliJ collector reports. If a session still appears twice (e.g. the
CLI report was made with `--exclude-client ''`), it is counted once:

- **Key:** `acpSessionId` (IntelliJ AI Assistant chats) or `sessionId` (everything else).
  Both equal the Copilot CLI session id where one exists.
- **Kept copy:** intellij > vscode > copilot-cli. IntelliJ's copy carries the IDE context.
- Every dropped copy is listed in `duplicatesDropped`.

## Caveats

- The inputs can be generated at different times; check `sources[].generatedAt`, or use
  `--run` to collect everything at once.
- Reports from different machines can be combined with `--input`. VS Code session ids are
  unique, so they won't collide; a CLI session copied between machines would be de-duplicated.
- The combined report contains prompt text from every source.
