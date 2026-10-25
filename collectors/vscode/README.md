# VS Code collector: GitHub Copilot Chat usage

Collects per-session and per-request Copilot Chat usage (credits, tokens, models,
tool-call rounds) from VS Code's local storage. No network calls, standard library only.

```bash
python3 collectors/vscode/collect.py --format tree                    # readable tree
python3 collectors/vscode/collect.py --out reports/vscode-copilot.json  # JSON report
python3 collectors/vscode/collect.py --user-dir "/path/to/Code/User"    # other machine / profile
python3 collectors/vscode/collect.py --include-empty                    # keep 0-request sessions
python3 collectors/vscode/collect.py --ticket-prefix ABC                # tickets are ABC-123 (default LEGO)
python3 collectors/vscode/collect.py --config team.json                 # other config file
```

### Config file

Defaults come from [`config.json`](config.json) next to the script, or another file via
`--config PATH`. **Precedence: command-line option > config file > built-in default.**

```jsonc
{
  "userDirs": [],                                   // [] = auto-detect Code / Code - Insiders
  "format": "json",                                 // json | tree
  "out": null,                                      // e.g. "../../reports/vscode-copilot.json"; null = stdout
  "includeEmpty": false,
  "ticketPrefix": "LEGO"                            // tickets look like LEGO-123
}
```

| Key | CLI override | Built-in default |
|---|---|---|
| `userDirs` | `--user-dir PATH` (repeatable; replaces the list) | `[]` → auto-detect |
| `format` | `--format json\|tree` | `json` |
| `out` | `--out FILE`, or `--out -` for stdout | `null` → stdout |
| `includeEmpty` | `--include-empty` / `--no-include-empty` | `false` |
| `ticketPrefix` | `--ticket-prefix KEY` | `LEGO` |

- Relative paths in the config resolve against the config file's folder, not the current
  directory. `~` and `$VARS` are expanded.
- Keys starting with `_` (e.g. `_comment`) are ignored. Unknown keys are an error.
- A missing default `config.json` is fine (built-ins apply). A missing `--config` file is an error.
- Missing folders for `out` are created.
- The real file can't hold `//` comments; they appear above for explanation only.

Verified against Copilot Chat 0.71.0 / VS Code 1.143.1 (macOS). The on-disk format is
internal to VS Code and can change between releases.

---

## 1. Where the data lives

`<User>` is the VS Code user-data dir:

| OS | `<User>` |
|---|---|
| macOS | `~/Library/Application Support/Code/User` |
| Windows | `%APPDATA%\Code\User` |
| Linux | `~/.config/Code/User` |

(`Code - Insiders` instead of `Code` for Insiders; the collector checks both.)

```
<User>/
├── workspaceStorage/<hash>/                 one dir per workspace/folder ever opened
│   ├── workspace.json                       {"folder": "file:///…"}  → which project this hash is
│   ├── state.vscdb                          SQLite, table ItemTable(key, value)
│   │     key 'chat.ChatSessionStore.index'  → session list: title, created / lastRequestStarted / lastRequestEnded
│   ├── chatSessions/<sessionId>.jsonl       ★ SOURCE: full chat session incl. usage (mutation log, §2)
│   ├── chatEditingSessions/<sessionId>/     edit checkpoints per request (state.json, contents/); no usage
│   └── GitHub.copilot-chat/
│       ├── transcripts/<sessionId>.jsonl    event stream: session.start, user.message, assistant.turn_*,
│       │                                    tool.execution_*; no token/credit data
│       └── debug-logs/<sessionId>/
│           ├── main.jsonl                   trace spans (often just session_start)
│           └── models.json                  model catalog incl. billing.token_prices (§4)
└── globalStorage/
    ├── emptyWindowChatSessions/<id>.jsonl   ★ SOURCE: chats opened with no folder (same format)
    └── github.copilot-chat/session-store.db SQLite: sessions, turns (text only), checkpoints; no usage
```

The collector reads only the two ★ sources plus `workspace.json`.

## 2. `chatSessions/*.jsonl`: mutation-log format

Each line is one operation. Replay them in order to get the final session object.

| `kind` | Shape | Effect |
|---|---|---|
| `0` | `{"kind":0,"v":{…}}` | Initial snapshot: `{version, sessionId, creationDate, requests:[], inputState, …}` |
| `1` | `{"kind":1,"k":[path…],"v":value}` | Set: `state[path] = value` |
| `2` | `{"kind":2,"k":[path…],"v":[items],"i"?:n}` | Append to array at path; if `i` is present, truncate to `i` first (retries / streamed parts) |
| `3` | `{"kind":3,"k":[path…]}` | Delete (assumed; not observed yet) |

Example path: `["requests", 2, "copilotCredits"]`.

**Fields are rewritten while a request runs.** `copilotCredits` is a running total that is
overwritten after each tool-call round (e.g. `0.378 → 0.457 → 0.719`). Always use the
final value after replay; never sum the log lines.

Requests replaced via `i` truncation (e.g. a first attempt routed to `setup.agent` before
Copilot was signed in) disappear from the final state. So far they have never carried credits.

## 3. Report schema

The report root:

```
report
├── collector, collectorVersion, generatedAt, userDirs[], ticketPrefix ("LEGO")
├── totals          {sessions, requests, billedRequests, copilotCredits, copilotCreditsRounded,
│                    copilotCreditsRoundedUp, promptTokens,
│                    completionTokens, toolCallRounds, resolvedModels[]}
├── byTicket        {"LEGO-123" | "(no ticket)": {sessions, sessionIds[], requests, copilotCredits,
│                    copilotCreditsRounded, copilotCreditsRoundedUp}}
├── byWorkspace     {"<folder>" | "(empty window)": {sessions, requests, copilotCredits, copilotCreditsRounded, copilotCreditsRoundedUp}}
├── byModel         {"<resolvedModel>": {requests, copilotCredits, copilotCreditsRounded, copilotCreditsRoundedUp}}
├── errors[]        {file, error}  (files that failed to parse)
└── sessions[]      ↓
```

Each session follows the VS Code session object (★ = usage fields, `_` = added by the collector):

```
session
├── sessionId, creationDate (epoch ms), customTitle, responderUsername
├── ticketKey                            _added_: Jira ticket, e.g. "LEGO-123", or null (see §3.1)
├── inputState.selectedModel             {identifier:"copilot/auto", metadata{id,vendor,name,family,version,
│                                         maxInputTokens,maxOutputTokens,isBYOK}, modelConfiguration{tier,tierSource}}
├── _source                              {file, workspace{id(hash), folder, kind: folder|workspace|emptyWindow},
│                                         ticket{field: customTitle|message|response, requestIndex} | null}
├── _totals                              same shape as report.totals (minus sessions)
└── requests[]
    ├── requestId, timestamp (epoch ms), modelId ("copilot/auto"; null on some first requests)
    ├── modeInfo                         {kind, name, isBuiltin, permissionLevel}
    ├── agent                            {id, name, extensionVersion}
    ├── message.text
    ├── ★ copilotCredits                 credits for this request, summed over all its tool-call rounds
    ├── ★ copilotCreditsRounded          _added_: copilotCredits rounded half-up to x.y (matches result.details)
    ├── ★ copilotCreditsRoundedUp        _added_: copilotCredits rounded up (ceiling) to x.y, e.g. 0.71 → 0.8
    ├── ★ promptTokens                   prompt size of the LAST round only (not a sum)
    ├── ★ completionTokens               output tokens (bigger than metadata.outputTokens on multi-round turns)
    ├── ★ promptTokenDetails[]           {category, label, percentageOfPrompt}: System Instructions /
    │                                    Tool Definitions / Messages / Files / Tool Results
    ├── outputBuffer, elapsedMs, modelState {value, completedAt}
    ├── response[]                       normalised parts:
    │                                      {kind:"markdown", text}
    │                                      {kind:"toolInvocation", toolId, toolCallId, message, isComplete}
    │                                      {kind:"autoModeResolution", resolved{id,name}}
    │                                      {kind:"thinking"}  (encrypted locally; content dropped)
    │                                      {kind:<other>}     (e.g. mcpServersStarting)
    └── result
        ├── timings {firstProgress, totalElapsed}
        ├── ★ details                    "GPT-6 Luna • 0.7 credits"  (what the UI shows, rounded)
        └── metadata
            ├── ★ resolvedModel          the real model Auto picked (gpt-6-luna)
            ├── promptTokens, outputTokens
            └── toolCallRounds[]         {modelId, toolCalls[{id,name}], thinking{tokens}, timestamp}; one per LLM call
```

**Credit values.** Every request and every total has three credit values:

| Field | Meaning | 0.7193 → |
|---|---|---|
| `copilotCredits` | exact value | 0.7193 |
| `copilotCreditsRounded` | rounded half-up to x.y (what the UI shows in `result.details`) | 0.7 |
| `copilotCreditsRoundedUp` | rounded up (ceiling) to x.y | 0.8 |

The rounded totals (session `_totals`, `byWorkspace`, `byModel`, `totals`) are **sums of
the per-request rounded values**, not the exact total rounded. The tree output shows all
three values, e.g. `credits=0.7193 (≈0.7 ↑0.8)`.

Requests without `copilotCredits` (`billedRequests` < `requests`) made no model call,
e.g. `setup.agent`.

Dropped from the raw data on purpose: full `agent` metadata, mode instruction bodies,
tool-call arguments and results, rendered prompts, encrypted thinking, and editor
selection state. Read the raw JSONL if you need these.

### 3.1 Ticket assignment (`ticketKey`)

Tracks AI usage per Jira ticket for team LEGO. A ticket can have many sessions; each
session belongs to at most one ticket.

1. **Session title** (`customTitle`): if it contains a ticket, use it.
2. **Otherwise, session content in chat order.** For each request: the user's
   `message.text`, then its response parts (markdown text and tool-invocation messages).
   The **first** ticket found wins.
3. No match: `ticketKey` is `null`, and the session counts under `"(no ticket)"` in `byTicket`.

Matching is case-insensitive and normalised to upper case (`lego-42` → `LEGO-42`). The key
must stand alone: `XLEGO-9` and `LEGO-12a` don't match. `_source.ticket` records where the
key was found, so wrong assignments can be traced. Change the project key with
`--ticket-prefix`.

The title is usually generated by Copilot from the first message. To pin a session to a
ticket reliably, start the first message with the key (e.g. `LEGO-123: add login form`),
or rename the chat to include it.

## 4. How credits are computed

`copilotCredits` is authoritative: sum it per request → session → workspace.

It can be reproduced from `debug-logs/<id>/models.json` → `billing.token_prices`
(prices per `batch_size` = 1,000,000 tokens). For each LLM call (tool-call round):

```
credits = ( cache_read  × cache_read_price
          + cache_write × cache_write_price      # new prompt tokens appear to bill at this rate
          + output      × output_price ) / 1,000,000
cache_read + cache_write = promptTokens
```

The `long_context` price row applies when the prompt is longer than `max_prompt_tokens`.
The cache split is not stored locally. Estimate it as `cache_read ≈ previous call's
promptTokens`, `cache_write = the rest`. Checked on gpt-6-luna (1 / 12.5 / 50): this
matches stored values to 4 decimals for single-round requests.

## 5. Known gaps vs. GitHub's billing dashboard

The local total can be lower than github.com → Settings → Billing → Usage. Observed so far:
2.66 locally vs 3.1 on GitHub. The difference can come from:

- Other machines (e.g. Windows `%APPDATA%`), github.com chat, the CLI, code review and
  the coding agent. Collect each machine with `--user-dir` and merge the reports.
- Background model calls that are not attached to a request (title generation, commit
  messages, history summarisation).
- Deleted chats (their JSONL is removed).
- Rounding or reporting lag on the billing side.
