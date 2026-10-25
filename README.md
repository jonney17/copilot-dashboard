# Copilot usage dashboard: collectors

Collects GitHub Copilot AI-credit usage from the local logs of every Copilot surface and
merges it into one report, with usage per Jira ticket (`LEGO-<n>`), workspace, model and
surface. Python 3.8+, standard library only, no network calls.

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

## Layout

| Path | What |
|---|---|
| [`collect.py`](collect.py), [`collect.config.json`](collect.config.json) | Master collector and its config |
| [`collectors/vscode/`](collectors/vscode/README.md) | VS Code Copilot Chat panel (`chatSessions/*.jsonl`) |
| [`collectors/copilot_cli/`](collectors/copilot_cli/README.md) | Copilot CLI (`~/.copilot/session-state`), incl. sessions used from VS Code's agent host |
| [`collectors/intellij/`](collectors/intellij/README.md) | JetBrains: AI Assistant ACP agent + GitHub Copilot plugin |
| [`collectors/combine/`](collectors/combine/README.md) | Merges reports, de-duplicates sessions, final schema |
| [`collectors/common.py`](collectors/common.py) | Shared: tickets, credit rounding, totals, config, tree output |
| `reports/` | Generated reports (contain local prompt text) |

Each collector can still be run on its own (`python3 collectors/<name>/collect.py`) with its
own `config.json`; see its README for the source format and schema.
