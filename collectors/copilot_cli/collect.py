#!/usr/bin/env python3
"""GitHub Copilot CLI usage collector.

Reads every Copilot CLI session under ~/.copilot/session-state/<id>/events.jsonl and
emits a report in the same schema as the VS Code collector (session -> requests[] ->
result.metadata.toolCallRounds[]), plus per-session and grand totals and the latest
account quota snapshot. See README.md.

Usage:
    python3 collect.py                        # JSON report to stdout
    python3 collect.py --format tree          # human-readable tree
    python3 collect.py --out report.json      # write to file
    python3 collect.py --copilot-home PATH    # non-default ~/.copilot dir (repeatable)
    python3 collect.py --include-empty        # keep sessions with no requests
    python3 collect.py --ticket-prefix ABC    # detect ABC-123 instead of LEGO-123
    python3 collect.py --config team.json     # other config file (default: ./config.json)
    python3 collect.py --exclude-client ''    # also count IDE-driven sessions (default skips JetBrains ones)
    python3 collect.py --vscode-user-dir PATH # VS Code User dir whose agent-host.db tags VS Code-opened sessions

Precedence: command-line option > config file > built-in defaults.

Standard library only; works on Python 3.8+.
"""
import argparse
import fnmatch
import glob
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import (DEFAULT_TICKET_PREFIX, add_common_args, by_request_surface, credit_fields,  # noqa: E402
                    epoch_ms, find_ticket, fmt_credits, fmt_num, iso, load_config, now_iso, render_tree, round_credits,
                    summarise, ticket_pattern, totals_for, vscode_user_dirs, write_report)

COLLECTOR = "copilot-cli"
COLLECTOR_VERSION = "1.3.0"
NANO = 1e9  # 1 AI credit = 1e9 nano-AIU
DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
# Used when neither the command line nor the config file sets a value.
BUILTIN_DEFAULTS = {
    "copilotHomes": [],      # empty -> $COPILOT_HOME or ~/.copilot
    "format": "json",        # json | tree
    "out": None,             # None -> stdout
    "includeEmpty": False,
    "ticketPrefix": DEFAULT_TICKET_PREFIX,
    # workspace.yaml client_name globs to skip. JetBrains IDEs drive the CLI (AI Assistant over ACP:
    # "JetBrains.*"; the GitHub Copilot plugin: "copilot-intellij") and are reported by
    # collectors/intellij instead, so counting them here would double count.
    "excludeClients": ["JetBrains*", "copilot-intellij*"],
    # VS Code User dirs whose globalStorage/agent-host.db lists the CLI sessions VS Code opened.
    "vscodeUserDirs": [],    # empty -> auto-detect Code / Code - Insiders
}
SURFACE_CLI = "copilot-cli"
SURFACE_VSCODE = "vscode/agent-host"
CLI_INITIATORS = ("github/cli",)  # sessions started in the terminal (later requests may come from VS Code)


# --------------------------------------------------------------------------- #
# Locating storage
# --------------------------------------------------------------------------- #

def default_copilot_homes():
    home = os.environ.get("COPILOT_HOME") or os.path.join(os.path.expanduser("~"), ".copilot")
    return [home] if os.path.isdir(home) else []


def find_session_dirs(copilot_home):
    for events in sorted(glob.glob(os.path.join(copilot_home, "session-state", "*", "events.jsonl"))):
        yield os.path.dirname(events)


def read_workspace_yaml(session_dir):
    """workspace.yaml is flat `key: value`; parsed by hand to stay stdlib-only."""
    info = {}
    try:
        with open(os.path.join(session_dir, "workspace.yaml"), encoding="utf-8") as f:
            for line in f:
                if ":" not in line or line.startswith((" ", "\t", "#")):
                    continue
                key, _, value = line.partition(":")
                value = value.strip().strip("'\"")
                info[key.strip()] = None if value in ("", "null", "~") else value
    except OSError:
        pass
    return info


def read_events(path):
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError as exc:
                raise ValueError("%s:%d: %s" % (path, lineno, exc))


# --------------------------------------------------------------------------- #
# Building the session from the event stream
# --------------------------------------------------------------------------- #

def usage_nano_aiu(usage):
    cu = usage.get("copilotUsage") or {}
    value = cu.get("totalNanoAiu", cu.get("total_nano_aiu"))
    return value if isinstance(value, (int, float)) else None


def new_request(data, ts, selected_model, version):
    return {
        "requestId": data.get("interactionId") or data.get("messageId"),
        "timestamp": ts,
        "modelId": selected_model,
        "modeInfo": {"kind": "cli"},
        "agent": {"id": "copilot-cli", "name": "GitHub Copilot CLI", "extensionVersion": version},
        "message": {"text": data.get("content")},
        "promptTokenDetails": [],
        "outputBuffer": None,
        "modelState": None,
        "response": [],
        "_rounds": [],
    }


def new_round(data, ts):
    return {"turnId": data.get("turnId"), "subAgent": None, "modelId": None, "toolCalls": [],
            "thinking": {"tokens": None}, "timestamp": ts, "endedAt": None, "usage": None, "nanoAiu": None, "copilotCredits": None,
            "premiumRequestCost": None, "initiator": None, "finishReason": None, "durationMs": None}


def apply_usage(rnd, usage):
    rnd["modelId"] = rnd["modelId"] or usage.get("model")
    rnd["modelDisplayName"] = usage.get("modelDisplayName")
    rnd["usage"] = {k: usage.get(k) for k in ("inputTokens", "cacheReadTokens", "cacheWriteTokens",
                                              "outputTokens", "reasoningTokens")}
    rnd["thinking"] = {"tokens": usage.get("reasoningTokens")}
    nano = usage_nano_aiu(usage)
    rnd["nanoAiu"] = nano
    rnd["copilotCredits"] = None if nano is None else nano / NANO
    rnd["premiumRequestCost"] = usage.get("cost")
    rnd["initiator"] = usage.get("initiator")
    rnd["finishReason"] = usage.get("finishReason")
    rnd["durationMs"] = usage.get("duration")
    rnd["aiCreditsStatus"] = usage.get("aiCreditsStatus")


def finish_request(req):
    rounds = req.pop("_rounds")
    billed = [r for r in rounds if r["usage"]]
    credits = sum(r["copilotCredits"] or 0 for r in billed) if billed else None
    last = billed[-1] if billed else None
    sums = {k: sum((r["usage"] or {}).get(k) or 0 for r in billed)
            for k in ("inputTokens", "cacheReadTokens", "cacheWriteTokens", "outputTokens", "reasoningTokens")}
    ended = max([r["endedAt"] for r in rounds if r["endedAt"]] or [None]) if rounds else None
    req.update(credit_fields(None if credits is None else round(credits, 9)))
    req["promptTokens"] = last["usage"]["inputTokens"] if last else None   # last round, like VS Code
    req["completionTokens"] = sums["outputTokens"] if billed else None      # all rounds
    req["usage"] = sums if billed else None
    req["premiumRequestCost"] = sum(r["premiumRequestCost"] or 0 for r in billed if r["initiator"] == "user")
    req["elapsedMs"] = (ended - req["timestamp"]) if ended and req["timestamp"] else None
    req["modelState"] = {"value": 1 if last and last["finishReason"] == "stop" else 0, "completedAt": ended}
    display = last.get("modelDisplayName") if last else None
    req["result"] = {
        "timings": {"totalElapsed": req["elapsedMs"]},
        # synthesised in the VS Code UI format, e.g. "MAI-Code-1.1-Flash • 0.8 credits"
        "details": "%s • %.1f credits" % (display or last["modelId"], req["copilotCreditsRounded"]) if last else None,
        "metadata": {
            "resolvedModel": last["modelId"] if last else None,
            "promptTokens": req["promptTokens"],
            "outputTokens": req["completionTokens"],
            "toolCallRounds": [{k: v for k, v in r.items() if k != "modelDisplayName"} for r in rounds],
        },
    }
    return req


def build_session(session_dir, pattern, prefix):
    ws = read_workspace_yaml(session_dir)
    events_path = os.path.join(session_dir, "events.jsonl")
    session = {"sessionId": ws.get("id") or os.path.basename(session_dir), "creationDate": epoch_ms(ws.get("created_at")),
               "copilotVersion": None, "context": {}}
    selected = {"identifier": None, "metadata": {}, "modelConfiguration": {}}
    requests, by_interaction, rounds_by_event = [], {}, {}
    current_req = current_round = None
    pending_auto = None
    tool_parts = {}
    # Sub-agents (e.g. the search sub-agent) log their own user.message / turns with a top-level
    # "agentId" (= the parent's tool-call id). They belong to the parent request, not a new one.
    sub_rounds, sub_parts = {}, {}

    def round_for(ev):
        agent = ev.get("agentId")
        return sub_rounds.get(agent) if agent else current_round
    seen_usage = set()
    background, errors, quota = [], [], None
    checkpoint = None

    for ev in read_events(events_path):
        etype, data, ts = ev.get("type"), ev.get("data") or {}, epoch_ms(ev.get("timestamp"))
        agent = ev.get("agentId")
        if etype == "session.start":
            session["sessionId"] = data.get("sessionId") or session["sessionId"]
            session["creationDate"] = epoch_ms(data.get("startTime")) or session["creationDate"]
            session["copilotVersion"] = data.get("copilotVersion")
            session["context"] = data.get("context") or {}
        elif etype == "session.model_change" and not agent:
            selected = {"identifier": data.get("newModel"), "metadata": {},
                        "modelConfiguration": {"reasoningEffort": data.get("reasoningEffort"),
                                               "contextTier": data.get("contextTier"), "source": data.get("source")}}
        elif etype == "session.auto_mode_resolved":
            pending_auto = {"kind": "autoModeResolution", "resolved": {"id": data.get("chosenModel")}}
        elif etype == "subagent.started":
            if current_req is None:
                continue
            part = {"kind": "subAgent", "toolCallId": data.get("toolCallId"),
                    "name": data.get("agentDisplayName") or data.get("agentName"), "agentType": data.get("agentType"),
                    "model": data.get("model"), "prompt": None, "isComplete": False}
            sub_parts[agent or data.get("toolCallId")] = part
            current_req["response"].append(part)
        elif etype == "subagent.completed":
            part = sub_parts.get(agent or data.get("toolCallId"))
            if part:
                part["isComplete"] = True
        elif etype == "user.message" and agent:          # the sub-agent's task prompt
            if agent in sub_parts:
                sub_parts[agent]["prompt"] = data.get("content")
        elif etype == "user.message":
            key = data.get("interactionId")
            if key and key in by_interaction:          # steering message inside a running interaction
                current_req = by_interaction[key]
                current_req["message"]["text"] = "\n".join(
                    t for t in (current_req["message"]["text"], data.get("content")) if t)
                continue
            current_req = new_request(data, ts, selected["identifier"], session["copilotVersion"])
            if pending_auto:
                current_req["response"].append(pending_auto)
                pending_auto = None
            requests.append(current_req)
            if key:
                by_interaction[key] = current_req
            current_round = None
        elif etype == "assistant.turn_start":
            if current_req is None:
                continue
            rnd = new_round(data, ts)
            current_req["_rounds"].append(rnd)
            rounds_by_event[ev.get("id")] = rnd
            if agent:
                rnd["subAgent"] = {"id": agent, "name": (sub_parts.get(agent) or {}).get("name")}
                sub_rounds[agent] = rnd
            else:
                current_round = rnd
        elif etype == "assistant.turn_end":
            rnd = round_for(ev)
            if rnd is not None:
                rnd["endedAt"] = ts
        elif etype == "session.usage_record":
            usage = data.get("usage") or {}
            usage_id = (usage.get("accounting") or {}).get("usageId") or ev.get("id")
            if usage_id in seen_usage:                   # replayed after resume
                continue
            seen_usage.add(usage_id)
            rnd = rounds_by_event.get(ev.get("parentId")) or round_for(ev)
            if rnd is None or rnd["usage"] is not None:  # no turn to hang it on: give it its own round
                if current_req is None:
                    continue
                rnd = new_round({"turnId": None}, ts)
                if agent:
                    rnd["subAgent"] = {"id": agent, "name": (sub_parts.get(agent) or {}).get("name")}
                current_req["_rounds"].append(rnd)
            apply_usage(rnd, usage)
        elif etype == "assistant.message":
            if current_req is None:
                continue
            rnd = round_for(ev)
            if rnd is not None:
                rnd["modelId"] = rnd["modelId"] or data.get("model")
                rnd["toolCalls"].extend(
                    {"id": t.get("toolCallId"), "name": t.get("name")} for t in data.get("toolRequests") or [])
            if data.get("content") and not agent:       # sub-agent answers go to the parent, not the user
                current_req["response"].append({"kind": "markdown", "text": data.get("content")})
        elif etype == "tool.execution_start":
            if current_req is None:
                continue
            part = {"kind": "toolInvocation", "toolId": data.get("toolName"), "toolCallId": data.get("toolCallId"),
                    "message": data.get("toolTitle") or data.get("toolName"), "isComplete": False}
            if agent:
                part["subAgent"] = agent
            tool_parts[data.get("toolCallId")] = part
            current_req["response"].append(part)
        elif etype == "tool.execution_complete":
            part = tool_parts.get(data.get("toolCallId"))
            if part:
                part["isComplete"] = True
                part["success"] = data.get("success")
        elif etype == "model.model_call_success":
            if data.get("quotaSnapshots"):
                quota = {"asOf": ts, "snapshots": data["quotaSnapshots"]}
            cu = data.get("copilotUsage") or {}
            nano = cu.get("total_nano_aiu", cu.get("totalNanoAiu"))
            call = data.get("modelCall") or {}
            # Auxiliary calls (e.g. session naming) that never produce a usage_record.
            background.append({"model": call.get("model"), "modelDisplayName": data.get("modelDisplayName"),
                               "initiator": call.get("initiator"), "timestamp": ts,
                               "promptTokens": (data.get("responseUsage") or {}).get("prompt_tokens"),
                               "completionTokens": (data.get("responseUsage") or {}).get("completion_tokens"),
                               "nanoAiu": nano, "copilotCredits": None if nano is None else nano / NANO})
        elif etype == "session.usage_checkpoint":
            checkpoint = data
        elif etype == "session.error":
            errors.append({"timestamp": ts, "errorType": data.get("errorType"), "message": data.get("message")})

    requests = [finish_request(r) for r in requests]
    ticket_key, ticket_source = find_ticket(ws.get("name"), requests, pattern, prefix)
    totals = totals_for(requests)
    ctx = session["context"]
    snap = (checkpoint or {}).get("accountingSnapshot") or {}
    session_usage = None
    if checkpoint:
        nano = checkpoint.get("totalNanoAiu")
        session_usage = {
            "totalNanoAiu": nano,
            "copilotCredits": None if nano is None else nano / NANO,
            "totalPremiumRequests": checkpoint.get("totalPremiumRequests"),
            "totalUserRequests": snap.get("totalUserRequests"),
            "aiCreditsStatus": snap.get("aiCreditsStatus"),
            "codeChanges": snap.get("codeChanges"),
            # False if the event log and the CLI's own running total disagree (e.g. truncated log)
            "matchesRequests": None if nano is None else abs(nano / NANO - totals["copilotCredits"]) < 1e-6,
        }
    return {
        "sessionId": session["sessionId"],
        "ticketKey": ticket_key,
        "creationDate": session["creationDate"],
        "customTitle": ws.get("name"),
        "responderUsername": "GitHub Copilot CLI",
        "inputState": {"selectedModel": selected},
        "requests": requests,
        "sessionUsage": session_usage,
        "backgroundCalls": background,
        "quotaSnapshot": quota,
        "sessionErrors": errors,
        "_source": {
            "file": events_path,
            "workspace": {"id": None, "folder": ctx.get("cwd") or ws.get("cwd"), "kind": "cli",
                          "gitRoot": ctx.get("gitRoot") or ws.get("git_root"),
                          "repository": ctx.get("repository") or ws.get("repository"),
                          "branch": ctx.get("branch") or ws.get("branch"),
                          "client": ws.get("client_name"), "copilotVersion": session["copilotVersion"]},
            "ticket": ticket_source,
        },
        "_totals": totals,
    }


# --------------------------------------------------------------------------- #
# VS Code agent host
# --------------------------------------------------------------------------- #

def read_vscode_agent_host(user_dirs):
    """CLI session id -> how VS Code's agent host knows it, from globalStorage/agent-host.db.

    VS Code's agent host runs Copilot on this same CLI runtime: sessions it opens or creates are
    catalogued in sessions_v2 as "copilotcli:/<sessionId>". Opened read-only; never written."""
    index = {}
    for user_dir in user_dirs:
        db = os.path.join(user_dir, "globalStorage", "agent-host.db")
        if not os.path.isfile(db):
            continue
        try:
            con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
            try:
                meta = dict(con.execute("SELECT key, value FROM metadata"))
                rows = con.execute("SELECT session_uri, registration_source, payload, modified_time "
                                   "FROM sessions_v2 WHERE provider = 'copilotcli'").fetchall()
                chats = con.execute("SELECT owner_session_uri, chat_uri FROM chats_v2").fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            continue
        for uri, registration, payload, modified in rows:
            if meta.get("sessionTombstone:" + uri) == "true":
                continue
            try:
                data = (json.loads(payload or "{}").get("data") or {})
            except ValueError:
                data = {}
            initiator = ((data.get("_meta") or {}).get("vscode.session.initiator") or {}).get("name")
            try:
                imported_at = json.loads(meta.get("sessionStateImported:" + uri) or "{}").get("at")
            except ValueError:
                imported_at = None
            own = [c for owner, c in chats if owner == uri]
            index[uri.split(":/", 1)[-1]] = {
                "sessionUri": uri, "initiator": initiator, "importedAt": imported_at,
                "registrationSource": registration, "modifiedTime": modified, "title": data.get("summary"),
                "chats": sum(1 for c in own if "://subagent/" not in c),
                "subAgentChats": sum(1 for c in own if "://subagent/" in c),
                "dbFile": db,
            }
    return index


def tag_surfaces(session, agent_host):
    """Set request["surface"]: "vscode/agent-host" for requests sent from VS Code, else "copilot-cli".

    A session VS Code created is VS Code throughout. A session started in the terminal
    (initiator github/cli) counts as VS Code from the moment VS Code imported it; the event log
    doesn't record which client sent each prompt, so this boundary is a best guess."""
    info = agent_host.get(session["sessionId"])
    session["_source"]["vscodeAgentHost"] = info
    for r in session["requests"]:
        if not info:
            r["surface"] = SURFACE_CLI
        elif info["initiator"] not in CLI_INITIATORS:
            r["surface"] = SURFACE_VSCODE
        else:
            after_import = info["importedAt"] and r["timestamp"] and r["timestamp"] >= info["importedAt"]
            r["surface"] = SURFACE_VSCODE if after_import else SURFACE_CLI


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

def account_quota(sessions):
    """Latest quota snapshot across sessions: the account-wide "X of N used" figure."""
    snaps = [s["quotaSnapshot"] for s in sessions if s.get("quotaSnapshot")]
    if not snaps:
        return None
    latest = max(snaps, key=lambda q: q["asOf"] or 0)
    out = {"asOf": latest["asOf"], "quotas": {}}
    for name, q in latest["snapshots"].items():
        entitlement, remaining = q.get("entitlementRequests"), q.get("remainingPercentage")
        used_est = None
        if isinstance(entitlement, (int, float)) and isinstance(remaining, (int, float)) and entitlement:
            used_est = round(entitlement * (100 - remaining) / 100, 2)  # finer than integer usedRequests
        out["quotas"][name] = dict(q, usedEstimate=used_est)
    return out


def excluded_client(session_dir, exclude_clients):
    client = read_workspace_yaml(session_dir).get("client_name") or ""
    return next((pat for pat in exclude_clients if pat and fnmatch.fnmatch(client, pat)), None)


def collect(copilot_homes, include_empty=False, ticket_prefix=DEFAULT_TICKET_PREFIX, exclude_clients=(),
            vscode_dirs=()):
    pattern = ticket_pattern(ticket_prefix)
    agent_host = read_vscode_agent_host(vscode_dirs)
    sessions, errors, excluded = [], [], []
    for home in copilot_homes:
        for session_dir in find_session_dirs(home):
            matched = excluded_client(session_dir, exclude_clients)
            if matched:
                excluded.append({"sessionId": os.path.basename(session_dir), "excludedBy": matched,
                                 "client": read_workspace_yaml(session_dir).get("client_name")})
                continue
            try:
                session = build_session(session_dir, pattern, ticket_prefix)
            except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
                errors.append({"file": session_dir, "error": "%s: %s" % (type(exc).__name__, exc)})
                continue
            tag_surfaces(session, agent_host)
            if include_empty or session["requests"]:
                sessions.append(session)
    sessions.sort(key=lambda s: s.get("creationDate") or 0)
    report = {
        "collector": COLLECTOR,
        "collectorVersion": COLLECTOR_VERSION,
        "generatedAt": now_iso(),
        "copilotHomes": copilot_homes,
        "ticketPrefix": ticket_prefix.upper(),
        "excludeClients": list(exclude_clients),
        "excludedSessions": excluded,
        "vscodeUserDirs": list(vscode_dirs),
        **summarise(sessions, lambda s: s["_source"]["workspace"]["folder"] or "(unknown cwd)"),
        "bySurface": by_request_surface(sessions, lambda s: SURFACE_CLI),
        "account": {"quota": account_quota(sessions)},
        "sessions": sessions,
        "errors": errors,
    }
    background = sum(c["copilotCredits"] or 0 for s in sessions for c in s["backgroundCalls"])
    report["totals"]["backgroundCredits"] = round(background, 6)  # not included in copilotCredits
    report["totals"]["premiumRequests"] = sum(r["premiumRequestCost"] or 0 for s in sessions for r in s["requests"])
    return report


def render(report):
    quota = (report["account"] or {}).get("quota")
    header = []
    if quota:
        for name, q in quota["quotas"].items():
            if q.get("entitlementRequests"):
                header.append("├── account quota %s  used≈%s of %s  (usedRequests=%s, remaining=%s%%, resets %s)"
                              % (name, q.get("usedEstimate"), q.get("entitlementRequests"), q.get("usedRequests"),
                                 q.get("remainingPercentage"), q.get("resetDate")))
    header.append("├── premiumRequests=%s  backgroundCredits=%s"
                  % (report["totals"]["premiumRequests"], fmt_num(report["totals"]["backgroundCredits"])))
    for name, b in report["bySurface"].items():
        header.append("├── surface %s  sessions=%d requests=%d credits=%s"
                      % (name, b["sessions"], b["requests"], fmt_credits(b)))
    for x in report["excludedSessions"]:
        header.append("├── skipped %s  client=%s  (excludeClients %s)" % (x["sessionId"], x["client"], x["excludedBy"]))

    def session_lines(s):
        lines = ["├── repository=%s  branch=%s  copilotVersion=%s"
                 % (s["_source"]["workspace"]["repository"], s["_source"]["workspace"]["branch"],
                    s["_source"]["workspace"]["copilotVersion"])]
        ah = s["_source"].get("vscodeAgentHost")
        if ah:
            lines.append("├── opened in VS Code agent host  initiator=%s  importedAt=%s  chats=%s subAgentChats=%s"
                         % (ah["initiator"], iso(ah["importedAt"]), ah["chats"], ah["subAgentChats"]))
        u = s["sessionUsage"]
        if u:
            lines.append("├── sessionUsage (CLI running total) credits=%s premiumRequests=%s matchesRequests=%s"
                         % (fmt_num(u["copilotCredits"]), u["totalPremiumRequests"], u["matchesRequests"]))
        for c in s["backgroundCalls"]:
            lines.append("├── backgroundCall %s  credits=%s  prompt=%s" % (c["model"], fmt_num(c["copilotCredits"]),
                                                                           fmt_num(c["promptTokens"])))
        for e in s["sessionErrors"]:
            lines.append("├── error %s: %s" % (e["errorType"], e["message"]))
        surfaces = ["[%d] %s" % (i, r["surface"]) for i, r in enumerate(s["requests"])]
        lines.append("├── request surfaces  %s" % ", ".join(surfaces))
        return lines

    def round_suffix(x):
        if x["copilotCredits"] is None:
            return ""
        u = x["usage"]
        return "%s  credits=%.6f (≈%.1f) in=%s cached=%s out=%s" % (
            "  [sub-agent: %s]" % x["subAgent"]["name"] if x.get("subAgent") else "",
            x["copilotCredits"], round_credits(x["copilotCredits"]), fmt_num(u["inputTokens"]),
            fmt_num(u["cacheReadTokens"]), fmt_num(u["outputTokens"]))

    return render_tree(report, header_lines=header, session_lines=session_lines, round_suffix=round_suffix)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Collect GitHub Copilot CLI usage into a report.",
        epilog="Options not given on the command line come from the config file, then built-in defaults.")
    ap.add_argument("--copilot-home", action="append",
                    help="Copilot CLI home directory (repeatable). Default: $COPILOT_HOME or ~/.copilot.")
    ap.add_argument("--exclude-client", action="append",
                    help="Skip sessions whose client_name matches this glob (repeatable; replaces the config "
                         "list; '' = skip none). Built-in default: JetBrains*, copilot-intellij*.")
    ap.add_argument("--vscode-user-dir", action="append",
                    help="VS Code User dir whose globalStorage/agent-host.db marks sessions opened from VS Code "
                         "(repeatable). Default: auto-detect Code / Code - Insiders.")
    add_common_args(ap)
    args = ap.parse_args(argv)

    config = load_config(args.config or DEFAULT_CONFIG_PATH, bool(args.config), BUILTIN_DEFAULTS,
                         path_keys=("copilotHomes", "vscodeUserDirs", "out"))

    def opt(cli_value, key):
        return config[key] if cli_value is None else cli_value

    homes = args.copilot_home or config["copilotHomes"] or default_copilot_homes()
    if not homes:
        ap.error("no Copilot CLI home found; pass --copilot-home or set copilotHomes in the config")
    fmt = opt(args.format, "format")
    if fmt not in ("json", "tree"):
        ap.error("format must be json or tree, got %r" % fmt)
    report = collect(homes, include_empty=bool(opt(args.include_empty, "includeEmpty")),
                     ticket_prefix=opt(args.ticket_prefix, "ticketPrefix"),
                     exclude_clients=[c for c in opt(args.exclude_client, "excludeClients") if c],
                     vscode_dirs=args.vscode_user_dir or config["vscodeUserDirs"] or vscode_user_dirs())
    write_report(report, fmt, opt(args.out, "out"), render)


if __name__ == "__main__":
    main()
