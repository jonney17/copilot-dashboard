# IntelliJ collector: Copilot in JetBrains IDEs

Collects GitHub Copilot usage from JetBrains IDEs (IntelliJ IDEA, PyCharm, WebStorm, …)
for both ways Copilot runs there: as an **ACP agent inside JetBrains AI Assistant**
(`integration: "ai-assistant"`) and through the **GitHub Copilot plugin**
(`integration: "copilot-plugin"`). The report uses the
same schema as the [VS Code](../vscode/README.md) and [Copilot CLI](../copilot_cli/README.md)
collectors. No network calls, standard library only. Shared code is in
[`../common.py`](../common.py); the Copilot CLI event-log parser is reused from
[`../copilot_cli/collect.py`](../copilot_cli/collect.py).

```bash
python3 collectors/intellij/collect.py --format tree               # readable tree
python3 collectors/intellij/collect.py --out reports/intellij.json   # JSON report
python3 collectors/intellij/collect.py --ide-dir ".../JetBrains/IntelliJIdea2026.2"   # one IDE/version
python3 collectors/intellij/collect.py --copilot-home /path/.copilot  # where linked CLI sessions live
python3 collectors/intellij/collect.py --plugin-dir ~/.config/github-copilot   # Copilot plugin store
python3 collectors/intellij/collect.py --include-empty               # keep 0-request sessions
python3 collectors/intellij/collect.py --ticket-prefix ABC           # tickets are ABC-123 (default LEGO)
python3 collectors/intellij/collect.py --config team.json            # other config file
```

### Config file

Same rules as the other collectors (command line > [`config.json`](config.json) > built-in
default; relative paths resolve against the config file; `_` keys ignored; unknown keys are an error).

| Key | CLI override | Built-in default |
|---|---|---|
| `ideDirs` | `--ide-dir PATH` (repeatable; replaces the list) | `[]` → every `JetBrains/*/` dir with `aia-task-history/` |
| `copilotHomes` | `--copilot-home PATH` (repeatable) | `[]` → `$COPILOT_HOME` or `~/.copilot` |
| `pluginDirs` | `--plugin-dir PATH` (repeatable) | `[]` → `~/.config/github-copilot` (`%LOCALAPPDATA%\github-copilot` on Windows) |
| `pluginClients` | (config only) | `["copilot-intellij*"]`: `client_name` globs of Copilot-plugin CLI sessions |
| `format` | `--format json\|tree` | `json` |
| `out` | `--out FILE`, or `--out -` for stdout | `null` → stdout |
| `includeEmpty` | `--include-empty` / `--no-include-empty` | `false` |
| `ticketPrefix` | `--ticket-prefix KEY` | `LEGO` |

Verified on macOS against IntelliJ IDEA 2026.2 (IU-262.10968.63) with:
- AI Assistant's `acp.registry.github-copilot` agent 1.561.0 (Copilot CLI 1.0.96)
- the GitHub Copilot plugin `copilot-intellij` 1.19.2-261 (Copilot CLI 1.0.94)

These are internal formats and can change between releases.

---

## 1. How Copilot runs in a JetBrains IDE

Both integrations run the **Copilot CLI runtime**, which logs the real usage:

```
(a) AI Assistant chat ──ACP──▶ github-copilot agent (= Copilot CLI) ─┐
      │                                                                ├──▶ ~/.copilot/session-state/<sessionId>/events.jsonl
      ▼                                                                │     ★ real usage: per-call tokens + nano-AIU credits
    <config>/aia-task-history/<taskId>.*  (UI history, NO usage)       │       (same format the Copilot CLI collector reads)
                                                                       │
(b) GitHub Copilot plugin chat ───────────▶ Copilot CLI runtime ──────┘
      │                                     client_name: copilot-intellij
      ▼
    ~/.config/github-copilot/<productCode>/chat-agent-sessions/<projectKey>/*.db  (plugin chat list, NO usage)
```

- (a) is linked by `<taskId>.agentsession` = `acp.registry.github-copilot:<sessionId>`
  (`client_name: JetBrains.IntelliJ IDEA`).
- (b) is found by `client_name: copilot-intellij` in the CLI session's `workspace.yaml`. The
  plugin's own Nitrite DB (a Java object store) contains that session id, which tells us the
  IDE product code (`iu` = IntelliJ IDEA Ultimate) and the plugin's project key.

## 2. Where the data lives

`<config>` = `~/Library/Application Support/JetBrains/<Product><version>` on macOS,
`%APPDATA%\JetBrains\<Product><version>` on Windows, `~/.config/JetBrains/<Product><version>` on Linux.

```
<config>/
├── aia-task-history/                    one set of files per AI Assistant chat ("task")
│   ├── <taskId>.events                  ★ UI event log (§3)
│   ├── <taskId>.agentsession            ★ "<agentId>:<acpSessionId>": link to the agent's own session
│   ├── <taskId>.usage                   context window {"used": 45904, "size": 128000} (tokens, not credits)
│   └── <taskId>.lastid                  last event id
├── workspace/<projectWorkspaceId>.xml   ★ component ChatSessionStateTemp → SerializedChat:
│                                          uid (= taskId), title{text, custom}, chatModelId
│                                          ("agent_acp.registry.github-copilot"), modifiedAt,
│                                          statisticInformation{timestamp, sourceActionType}
├── options/recentProjects.xml           ★ project path ↔ projectWorkspaceId, IDE build
└── options/acpAgents.xml                ACP agent auth settings

~/Library/Caches/JetBrains/<Product><version>/acp-agents/github-copilot/<version>/   the ACP agent install
~/.copilot/session-state/<sessionId>/        ★ usage (client_name: JetBrains.IntelliJ IDEA | copilot-intellij)

~/.config/github-copilot/                    GitHub Copilot plugin store
├── versions.json                            {"copilot-intellij": "1.19.2-261-macos-arm64"}
├── <productCode>/chat-agent-sessions/<projectKey>/copilot-agent-sessions-nitrite.db   ★ plugin chat list (session ids, titles)
├── <productCode>/bg-agent-sessions/<projectKey>/*.db                                  background-agent sessions / snapshots
├── copilot-intellij.db                      SQLite key/value plugin state
└── auth.db, oauth.json                      sign-in (never read)
```

## 3. `<taskId>.events` format

The first line is `AUI_EVENTS_V1`. Every following line is **base64-encoded JSON**:

| `type` (short) | Content |
|---|---|
| `ChatSessionUserPromptEvent` | `prompt`, `attachments`, `agentId` → starts a request |
| `ChatSessionMessageBlockEvent` + `event.kind`: | |
| ・ `MarkdownBlockUpdatedEvent` | `stepId`, `textChunk` (streamed; chunks with the same `stepId` are joined) |
| ・ `AgentThoughtBlockUpdatedEvent` | reasoning summary text |
| ・ `ToolBlockUpdatedEvent` | `toolType` (Read, Authentication, …), `status`, `title`, `output` |
| ・ `ViewFilesBlockUpdatedEvent` | `files[]`, `missingFiles[]`, `status` |
| ・ `ResultBlockUpdatedEvent` | end of the agent's answer (`cancelled`, `changes[]`) |

There are no tokens, credits or model names in this file.

## 4. How a session is built

**(b) Copilot plugin.** For each `~/.copilot/session-state/*/` whose `client_name` matches
`pluginClients`: the whole session (requests, rounds, sub-agents, credits) comes from its
CLI log. `_source.ide` gets the product (from the plugin DB folder that mentions the session
id), plugin version and project key. The title is the CLI session `name` (the first prompt).

**(a) AI Assistant.** For each `aia-task-history/<taskId>.events`:

1. **Chat metadata.** Title, created/modified time and project come from
   `workspace/*.xml` + `recentProjects.xml`.
2. **Linked usage.** If `.agentsession` names an ACP session and
   `<copilotHome>/session-state/<acpSessionId>/events.jsonl` exists, requests, rounds,
   credits and tokens are parsed from it with the Copilot CLI collector's parser
   (`usageSource: "copilot-cli"`). Credits = Σ `totalNanoAiu / 1e9`, checked against the
   CLI checkpoint (`sessionUsage.matchesRequests`).
3. **Fallback.** With no linked log (another agent, e.g. AI Assistant's own models, or the
   CLI session was deleted), requests are rebuilt from the IDE events: prompts, markdown
   and tool blocks, with **no usage** (`usageSource: null`; counted in
   `totals.sessionsWithoutUsage`).
4. **Ticket** (`ticketKey`). The IDE chat title first (it's what the user sees and can
   rename), then the first `LEGO-<n>` in chat order. Same rule as the other collectors.

**Sub-agents.** The plugin delegates searches to a "Search Subagent" (model
`copilot-search-a`, 0 credits so far). Its calls show up as extra rounds of the parent
request marked `subAgent`, not as separate requests (see the CLI README).

**No double counting with the Copilot CLI collector.** Both kinds of IntelliJ session also
sit in `~/.copilot/session-state/`. The CLI collector skips `JetBrains*` and
`copilot-intellij*` clients by default (`excludeClients`), so adding the
VS Code + CLI + IntelliJ reports together counts every session once.

## 5. Report schema

Same as the Copilot CLI report (see its [§3](../copilot_cli/README.md#3-report-schema)),
including `account.quota` when a linked session has a quota snapshot. Differences:

```
report
├── … collector:"intellij", ideDirs[], copilotHomes[], pluginDirs[], pluginClients[]
├── totals          + premiumRequests, sessionsWithoutUsage
├── byIntegration   {"ai-assistant" | "copilot-plugin": {sessions, requests, copilotCredits, …Rounded, …RoundedUp}}
└── sessions[]
    ├── integration     "ai-assistant" | "copilot-plugin"
    ├── sessionId       ai-assistant: IntelliJ task/chat id (taskId); copilot-plugin: the CLI session id
    ├── acpSessionId    ai-assistant: linked Copilot CLI session id (use it to de-dupe against CLI reports); else null
    ├── customTitle     IDE chat title (ai-assistant) / CLI session name (copilot-plugin)
    ├── creationDate    chat creation time
    ├── responderUsername  "acp.registry.github-copilot" | "GitHub Copilot (JetBrains plugin)"
    ├── usageSource     "copilot-cli" | null (no usage data available)
    ├── … requests[] exactly as in the CLI report (or IDE-event fallback with null credits)
    └── _source
        ├── file, linkedFile (the CLI events.jsonl)
        ├── workspace {id: projectWorkspaceId, folder: project path, kind:"intellij", repository, branch, client}
        └── ide  ai-assistant:   {product, build, configDir, taskId, agentId, chatModelId, modifiedAt,
                                  contextWindow{used,size}, copilotVersion}
                 copilot-plugin: {product, productCode, projectKey, pluginVersion, agentId:"copilot-intellij",
                                  copilotVersion}
```

## 6. Caveats

- Verified on one real chat per integration. Multi-chat projects, several IDEs and
  versions, and non-Copilot agents follow the same files but haven't been seen in real data.
- The plugin's Nitrite DB is only scanned for session ids, not decoded, so the IDE
  product comes from its folder name. The project path comes from the CLI session's `cwd`.
- If the linked CLI session folder has been cleaned up, credits for that chat are gone;
  only the fallback remains.
- Ticket detection also scans the assistant's answers, so a ticket the assistant merely
  quotes (e.g. from a README example) can be picked up. Name the chat with the ticket to be safe.
