# Copilot usage dashboard: collectors

Collects GitHub Copilot AI-credit usage from the local logs of every Copilot surface and
merges it into one report, with usage per Jira ticket (`LEGO-<n>`), workspace, model and
surface. Members' reports are then consolidated into one team file (`ai-usage.json`) for the
team dashboard. Python 3.8+, standard library only, no network calls.

## Quick start

```bash
python3 collect.py                    # run all collectors → reports/*.json + reports/combined.json
python3 collect.py --format tree      # final report as a readable tree on stdout
```

`collect.py` is the **master collector**. It runs the three collectors with the options in
[`collect.config.json`](collect.config.json), writes each collector's report, and combines
them into the final report (`reports/combined.json`).

## Master config: `collect.config.json`

The one place to configure a run. `collect.py` does not read the per-collector
`collectors/*/config.json` files; those only apply when a collector is run on its own.

| Key | Meaning | Default |
|---|---|---|
| `member.id`, `member.name`, `member.email` | Who the report belongs to, used by `consolidate.py` | `id` = GitHub login from `~/.copilot/config.json`, else the OS user |
| `ticketPrefix` | Jira project key for ticket detection (all collectors) | `"LEGO"` |
| `includeEmpty` | Keep sessions without requests | `false` |
| `outputDir` | Base folder for report files (relative to the config file) | `"reports"` |
| `writeSourceReports` | Also write each collector's own report | `true` |
| `combinedReport` | Final report file (relative to `outputDir`); `null` = stdout | `"combined.json"` |
| `format` | Final report format: `json` \| `tree` | `"json"` |
| `collectors.vscode` | `enabled`, `userDirs` (VS Code User dirs), `report` | auto-detect |
| `collectors.copilotCli` | `enabled`, `copilotHomes`, `excludeClients`, `vscodeUserDirs`, `report` | auto-detect; skips JetBrains clients |
| `collectors.intellij` | `enabled`, `ideDirs`, `copilotHomes`, `pluginDirs`, `pluginClients`, `report` | auto-detect |

- An empty directory list `[]` means auto-detect. Directory paths resolve against the
  config file's folder; report names resolve against `outputDir`. `~` and `$VARS` are expanded.
- A collector's `report: null` skips writing its own report but still includes it in the final one.
- Unknown keys and wrong value types are errors (e.g. `collectors.vscode.enabld`), so typos
  don't pass silently. Keys starting with `_` are comments.

Command-line options override the config:

| Option | Effect |
|---|---|
| `--config FILE` | Use another master config |
| `--format json\|tree` | Final report format |
| `--out FILE` / `--out -` | Final report path / stdout (instead of `combinedReport`) |
| `--only vscode,copilotCli,intellij` | Run a subset |
| `--source-reports` / `--no-source-reports` | Override `writeSourceReports` |

**Run status.** The final report has a `run` section: the config file used and, per
collector, `status` (`ok` / `disabled` / `skipped` (nothing found to read) / `failed`),
sessions, credits, report path and error. A failing collector doesn't stop the others; the
final report is still written and `collect.py` exits with code 1.

## Team consolidation: `consolidate.py` → `ai-usage.json`

```
each member's machine                         team lead / shared folder
─────────────────────                         ─────────────────────────
python3 collect.py  ──▶ reports/combined.json ──copy──▶ team-reports/<member>.json ─┐
                                                                                   ├─▶ python3 consolidate.py ─▶ ai-usage.json ─▶ team dashboard
(another member)    ──▶ reports/combined.json ──copy──▶ team-reports/<member>.json ─┘
```

1. **Each member** sets `member.id` (and `name`, `email`) in `collect.config.json`, runs
   `python3 collect.py`, and hands in `reports/combined.json` (any file name, any subfolder
   of `team-reports/`). If `member.id` is left `null`, the GitHub login from
   `~/.copilot/config.json` is used, then the OS user name. Reports without a `member` block
   (made before collect.py 1.1.0) take the member id from the file name (`alice.json`,
   `alice.combined.json`).
2. **The team lead** runs `python3 consolidate.py`. By default it reads `team-reports/**/*.json`
   and writes `ai-usage.json`.
3. **The dashboard** reads `ai-usage.json` only.

Handing in a new report every week is safe: the same member's session is counted once, from
their newest report (`totals.supersededSessions` counts the older copies). Files that aren't
member reports (including a previous `ai-usage.json`) are skipped and listed in `errors`.

### `consolidate.config.json`

| Key | Meaning | Default |
|---|---|---|
| `team` | Team name stored in the file | `"LEGO"` |
| `inputDir`, `pattern` | Folder of member reports, searched recursively | `"team-reports"`, `"*.json"` |
| `inputs` | Explicit report files; used instead of `inputDir` when non-empty | `[]` |
| `output` | ai-usage file; `null` = stdout | `"ai-usage.json"` |
| `format` | `json` \| `tree` (readable summary) | `"json"` |
| `content` | Text copied into the team file: `none` (usage only), `titles`, `full` (prompts + answers) | `"none"` |

Command-line overrides: `--config`, `--input FILE` (repeatable), `--input-dir DIR`, `--out FILE|-`,
`--format`, `--content`, `--team`.

**Privacy.** Member reports contain prompts, answers and local paths. With `content: "none"`
the team file holds only usage data: member, ticket, project, surface, model, credits, tokens
and timestamps. No prompt or answer text, no titles and no local file paths. Projects are
named by repository (`owner/repo`) or folder name, never the full path.

### `ai-usage.json` schema

```
ai-usage
├── schema:"ai-usage", schemaVersion, team, generatedAt, content
├── period          {from, to}               epoch ms of the first/last request
├── inputs[]        {file, member, memberIdSource, machine, generatedAt, sessions, copilotCredits,
│                    collectors[{collector, status}]}
├── totals          {members, sessions, requests, billedRequests, premiumRequests, promptTokens,
│                    completionTokens, copilotCredits, copilotCreditsRounded, copilotCreditsRoundedUp,
│                    supersededSessions}
├── byMember        {"<id>": {…totals fields, name, email, machines[], firstActivity, lastActivity,
│                    latestReportAt, accountQuota{asOf, quotas{chat:{entitlementRequests, usedEstimate, …}}}}}
├── byTicket        {"LEGO-123" | "(no ticket)": {…totals fields, members{"<id>": credits}}}
├── byProject       {"owner/repo" | "<folder name>": {…}}
├── bySurface       {"vscode/chat" | "vscode/agent-host" | "copilot-cli" | "intellij/ai-assistant" |
│                    "intellij/copilot-plugin": {…}}
├── byModel         {"<model>": {…}}         per request, by its resolved model
├── byDay           {"YYYY-MM-DD" (UTC): {…totals fields, members{"<id>": credits}}}
├── sessions[]      one slim record per session:
│     member, sessionId, acpSessionId, surface, collector, ticketKey, ticketFrom, project,
│     repository, branch, creationDate, lastActivity, reportFile, reportGeneratedAt,
│     title (content ≥ titles),
│     requests[] {requestId, timestamp, surface, model, copilotCredits, copilotCreditsRounded,
│                 copilotCreditsRoundedUp, promptTokens, completionTokens, premiumRequestCost,
│                 toolCallRounds, subAgentRounds, subAgentCredits, message/response (content = full)}
└── errors[]        {file, error}: inputs that were skipped
```

All breakdowns count requests and sum per-request credits, so a session that spans two
surfaces or days is split correctly. Rounded credit totals are sums of per-request rounded values.

## Layout

| Path | What |
|---|---|
| [`collect.py`](collect.py), [`collect.config.json`](collect.config.json) | Master collector and its config |
| [`consolidate.py`](consolidate.py), [`consolidate.config.json`](consolidate.config.json) | Team consolidation → `ai-usage.json` |
| [`collectors/vscode/`](collectors/vscode/README.md) | VS Code Copilot Chat panel (`chatSessions/*.jsonl`) |
| [`collectors/copilot_cli/`](collectors/copilot_cli/README.md) | Copilot CLI (`~/.copilot/session-state`), incl. sessions used from VS Code's agent host |
| [`collectors/intellij/`](collectors/intellij/README.md) | JetBrains: AI Assistant ACP agent + GitHub Copilot plugin |
| [`collectors/combine/`](collectors/combine/README.md) | Merges reports, de-duplicates sessions, final schema |
| [`collectors/common.py`](collectors/common.py) | Shared: tickets, credit rounding, totals, config, tree output |
| `reports/` | Generated reports (contain local prompt text) |
| `team-reports/`, `ai-usage.json` | Members' reports and the team file (git-ignored) |

Each collector can still be run on its own (`python3 collectors/<name>/collect.py`) with its
own `config.json`; see its README for the source format and schema.
