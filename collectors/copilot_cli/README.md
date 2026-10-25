# Copilot CLI collector: GitHub Copilot CLI usage

Collects per-session, per-request and per-LLM-call usage (AI credits, tokens, models,
premium requests, account quota) from the Copilot CLI's local session logs. The report
uses the **same schema as the [VS Code collector](../vscode/README.md)**, so both can be
merged in the dashboard. No network calls, standard library only. Shared code is in
[`../common.py`](../common.py).

```bash
python3 collectors/copilot_cli/collect.py --format tree                 # readable tree
python3 collectors/copilot_cli/collect.py --out reports/copilot-cli.json  # JSON report
python3 collectors/copilot_cli/collect.py --copilot-home /path/.copilot   # other machine / user
python3 collectors/copilot_cli/collect.py --include-empty                 # keep 0-request sessions
python3 collectors/copilot_cli/collect.py --ticket-prefix ABC             # tickets are ABC-123 (default LEGO)
python3 collectors/copilot_cli/collect.py --config team.json              # other config file
python3 collectors/copilot_cli/collect.py --exclude-client ''             # also count IDE-driven sessions
python3 collectors/copilot_cli/collect.py --vscode-user-dir "/path/Code/User"   # VS Code whose agent host to check
```

### Config file

Same rules as the VS Code collector (command line > [`config.json`](config.json) >
built-in default; relative paths resolve against the config file; `_` keys ignored;
unknown keys are an error).

| Key | CLI override | Built-in default |
|---|---|---|
| `copilotHomes` | `--copilot-home PATH` (repeatable; replaces the list) | `[]` → `$COPILOT_HOME` or `~/.copilot` |
| `format` | `--format json\|tree` | `json` |
| `out` | `--out FILE`, or `--out -` for stdout | `null` → stdout |
| `includeEmpty` | `--include-empty` / `--no-include-empty` | `false` |
| `ticketPrefix` | `--ticket-prefix KEY` | `LEGO` |
| `vscodeUserDirs` | `--vscode-user-dir PATH` (repeatable) | `[]` → auto-detect `Code` / `Code - Insiders` |
| `excludeClients` | `--exclude-client GLOB` (repeatable; replaces the list; `''` = none) | `["JetBrains*", "copilot-intellij*"]` |

**Skipped clients.** JetBrains IDEs run Copilot on this same CLI runtime, both through
AI Assistant over ACP (`client_name: JetBrains.IntelliJ IDEA`) and through the GitHub
Copilot plugin (`client_name: copilot-intellij`). Their sessions land in `session-state/`
too. They are reported by the [IntelliJ collector](../intellij/README.md) with IDE context,
so this collector skips them by default to avoid double counting. Skipped sessions are listed in
`report.excludedSessions`, so nothing disappears silently.

Verified against Copilot CLI 1.0.103 (macOS). The on-disk format is internal to the CLI
and can change between releases.

---

## 1. Where the data lives

`<home>` = `~/.copilot` (`%USERPROFILE%\.copilot` on Windows). `$COPILOT_HOME` is honoured
if set; this is assumed, not verified.

```
<home>/
├── config.json                      login state, first launch (// comments, not strict JSON); no usage
├── session-state/<sessionId>/       one dir per CLI session
│   ├── events.jsonl                 ★ SOURCE: append-only event log incl. per-call usage (§2)
│   ├── workspace.yaml               ★ id, cwd, git_root, repository, branch, client_name,
│   │                                  name (session title), user_named, created_at, updated_at
│   ├── checkpoints/index.md         compaction checkpoints
│   ├── rewind-file-snapshots/       file snapshots for /rewind
│   └── files/, research/            session artefacts
├── session-store.db                 SQLite: sessions, turns, assistant_usage_events (per-call usage,
│                                    same numbers as events.jsonl), search index
├── open-sessions-state.json         currently open sessions
├── command-history-state.json       prompt history
└── logs/process-*.log               process logs; no usage
```

The collector reads `events.jsonl` + `workspace.yaml`. `session-store.db` →
`assistant_usage_events` is a second copy of the per-call usage, useful for cross-checking.

### Sessions used from VS Code's agent host

VS Code's agent host (Copilot agent sessions in VS Code) runs on this same CLI runtime. It can
open an existing CLI session or create one, and every prompt it sends is appended to that
session's `events.jsonl`. `client_name` stays whatever started the session (e.g. `github/cli`),
so these sessions are reported here, not by the VS Code collector (they never appear in VS
Code's `chatSessions/`).

To show them as VS Code usage, the collector reads (read-only)
`<VS Code User>/globalStorage/agent-host.db`:

- `sessions_v2` rows with `provider = 'copilotcli'` and `session_uri = 'copilotcli:/<sessionId>'`
  are the CLI sessions VS Code knows. Rows marked `sessionTombstone:<uri>` in `metadata` are
  skipped (deleted in VS Code).
- `payload._meta["vscode.session.initiator"].name` says who started the session;
  `metadata["sessionStateImported:<uri>"].at` says when VS Code first imported it.
- `chats_v2` lists the session's chats, including `ahp-chat://subagent/…` ones for sub-agent runs.

Each request then gets a **`surface`**:

| Session | Request surface |
|---|---|
| not in `agent-host.db` | `copilot-cli` |
| started by VS Code (initiator ≠ `github/cli`) | `vscode/agent-host` |
| started in the terminal (initiator `github/cli`), opened in VS Code later | `vscode/agent-host` if sent at or after `importedAt`, else `copilot-cli` |

The last row is a best guess. The event log doesn't record which client sent each prompt (its
`session.resume` events carry no client name), so a prompt typed in the terminal *after* VS Code
opened the session is still labelled VS Code. Credits are unaffected; only the surface label is.

## 2. `events.jsonl`: event-log format

Unlike VS Code's mutation log, each line is a self-contained event:
`{"type", "id", "timestamp" (ISO), "parentId", "data"}`. Events are only ever appended.

| `type` | Used for | Key `data` fields |
|---|---|---|
| `session.start` | session id, start, version, git context | `sessionId, startTime, copilotVersion, context{cwd,gitRoot,repository,branch,headCommit}` |
| `session.model_change` | selected model | `newModel` ("auto"), `reasoningEffort`, `source` |
| `session.auto_mode_resolved` | what Auto picked | `chosenModel, candidateModels, routingMethod` |
| `user.message` | **starts a request** | `content, interactionId, messageId, turnId` |
| `assistant.turn_start` / `turn_end` | **one tool-call round** (one LLM call) | `turnId, interactionId` |
| `session.usage_record` | ★ **usage of one LLM call** (`parentId` = its `turn_start`) | `usage{model, inputTokens, cacheReadTokens, cacheWriteTokens, outputTokens, reasoningTokens, cost, initiator, finishReason, duration, copilotUsage{totalNanoAiu, tokenDetails[]}, accounting{usageId, sequence}}` |
| `assistant.message` | response text, tool requests | `model, content, toolRequests[{toolCallId,name}], phase` |
| `tool.execution_start` / `complete` | tool invocations | `toolName, toolCallId, toolTitle, success` |
| `model.model_call_success` | auxiliary calls (e.g. naming the session) + ★ **account quota** | `modelCall, copilotUsage, responseUsage, quotaSnapshots{chat, completions, premium_interactions}` |
| `subagent.started` / `completed` | a sub-agent run (e.g. "Search Subagent") | `toolCallId, agentName, agentDisplayName, agentType, model` |
| `session.usage_checkpoint` | ★ CLI's own running session total | `totalNanoAiu, totalPremiumRequests, accountingSnapshot{…}` |
| `session.error`, `session.info` | errors (e.g. not logged in), MCP / auth info | `errorType, message` |

How the collector maps events to the shared schema:

| Shared schema | Copilot CLI |
|---|---|
| session | `session-state/<id>/` |
| request | one `user.message` / `interactionId` (follow-up messages sent mid-run are merged in) |
| toolCallRound | one `assistant.turn_start … turn_end` with its `session.usage_record` |
| sub-agent | events with a top-level `agentId` (= the parent's tool-call id): their `user.message` is the sub-agent's task prompt (stored on a `subAgent` response part, **not** a new request); their turns are extra rounds of the parent request, marked `subAgent{id,name}`; usage records have `initiator: "sub-agent"` |
| `customTitle` | `workspace.yaml` → `name` |
| workspace | `cwd` (plus repository, branch) |

## 3. Report schema

Same as the VS Code report (see [its §3](../vscode/README.md#3-report-schema)). Fields
the CLI can't provide are `null`/`[]` (`promptTokenDetails`, `outputBuffer`). Additions:

```
report
├── … collector:"copilot-cli", copilotHomes[] (instead of userDirs[])
├── excludeClients[], excludedSessions[] {sessionId, client, excludedBy}
├── vscodeUserDirs[]
├── bySurface       per request: {"copilot-cli" | "vscode/agent-host": {sessions, requests, credits ×3}}
├── totals          + premiumRequests, backgroundCredits (NOT included in copilotCredits)
├── account.quota   latest quotaSnapshots across sessions:
│                   {asOf, quotas{chat|completions|premium_interactions: {entitlementRequests, usedRequests,
│                    remainingPercentage, resetDate, overage, …, usedEstimate}}}
└── sessions[]
    ├── … sessionId, ticketKey, creationDate, customTitle, responderUsername:"GitHub Copilot CLI",
    │     inputState.selectedModel {identifier:"auto", modelConfiguration{reasoningEffort, contextTier, source}}
    ├── sessionUsage    {totalNanoAiu, copilotCredits, totalPremiumRequests, totalUserRequests,
    │                    aiCreditsStatus, codeChanges, matchesRequests}  (from the last usage_checkpoint)
    ├── backgroundCalls[] {model, initiator, promptTokens, completionTokens, nanoAiu, copilotCredits}
    ├── quotaSnapshot   {asOf, snapshots}  (latest in this session)
    ├── sessionErrors[] {timestamp, errorType, message}
    ├── _source.workspace {folder(cwd), kind:"cli", gitRoot, repository, branch, client, copilotVersion}
    ├── _source.vscodeAgentHost {sessionUri, initiator, importedAt, registrationSource, modifiedTime, title,
    │                            chats, subAgentChats, dbFile} | null
    └── requests[]
        ├── requestId (= interactionId), timestamp, modelId (selected, e.g. "auto"), message.text
        ├── surface            "copilot-cli" | "vscode/agent-host" (see "Sessions used from VS Code's agent host")
        ├── modeInfo {kind:"cli"}, agent {id:"copilot-cli", name, extensionVersion}
        ├── ★ copilotCredits / copilotCreditsRounded / copilotCreditsRoundedUp
        ├── ★ promptTokens       last round's inputTokens (same meaning as VS Code)
        ├── ★ completionTokens   sum of outputTokens over all rounds
        ├── ★ usage              {inputTokens, cacheReadTokens, cacheWriteTokens, outputTokens, reasoningTokens} summed
        ├── ★ premiumRequestCost sum of usage.cost for user-initiated calls (premium-request multiplier)
        ├── elapsedMs, modelState {value: 1 if the last round finished with "stop", completedAt}
        ├── response[]           autoModeResolution / markdown / toolInvocation {toolId, toolCallId, message, isComplete, success, subAgent?}
        │                        / subAgent {toolCallId, name, agentType, model, prompt, isComplete}
        └── result
            ├── details          synthesised in the VS Code UI format: "MAI-Code-1.1-Flash • 0.8 credits"
            └── metadata
                ├── resolvedModel  model of the last billed round
                └── toolCallRounds[] {turnId, subAgent{id,name}|null, modelId, toolCalls[{id,name}], thinking{tokens}, timestamp, endedAt,
                                      ★ usage{…}, ★ nanoAiu, ★ copilotCredits, premiumRequestCost,
                                      initiator (user|agent|sub-agent), finishReason, durationMs, aiCreditsStatus}
```

Ticket assignment (`ticketKey`) works exactly as in the VS Code collector
([§3.1](../vscode/README.md#31-ticket-assignment-ticketkey)): the session title
(`workspace.yaml` `name`) first, then the first `LEGO-<n>` in chat order.

## 4. How credits are computed

The CLI records credits directly, so no estimating is needed:

```
copilotCredits (per call) = usage.copilotUsage.totalNanoAiu / 1e9      # 1 AI credit = 1e9 nano-AIU
request credits           = Σ calls in the request
session credits           = Σ requests          (checked against usage_checkpoint.totalNanoAiu → matchesRequests)
```

Per call, the CLI computes `totalNanoAiu` from `tokenDetails[]`:
`Σ tokenCount × costPerBatch / batchSize`. Here `input` = **uncached** input tokens
(`inputTokens − cacheReadTokens − cacheWriteTokens`). Prices match VS Code's `models.json`.
Verified on mai-code-1.1-flash (input 20, cache_read 2, output 120 per 1M tokens): 4 calls
add up to 0.793344 credits, equal to the checkpoint.

- Calls are de-duplicated by `accounting.usageId`, in case a resumed session replays records.
- `backgroundCalls` (e.g. gpt-4o-mini naming the session) are listed but not added to
  `copilotCredits`. So far they have cost 0.
- `premiumRequests` is the legacy premium-request count (`usage.cost` multiplier per user
  prompt). It is separate from AI credits.

## 5. Account quota: the "X of 200" number

`model.model_call_success.quotaSnapshots` carries the **account-wide** quota as of that
call, covering VS Code, CLI and every other surface:

```
chat: entitlementRequests 200, usedRequests 3, remainingPercentage 98.4  →  usedEstimate = 200 × 1.6% = 3.2
```

`account.quota` is the latest snapshot across all sessions. `usedEstimate` is calculated
from `remainingPercentage`, which is more precise than the integer `usedRequests`. The
snapshot is taken **before** the call it is attached to, so it trails by at most that one
request. It is the closest local match to the figure on github.com, which the per-session
sums can't give you: they miss other machines and surfaces.
