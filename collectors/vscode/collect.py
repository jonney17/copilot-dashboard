#!/usr/bin/env python3
"""VS Code Copilot Chat usage collector.

Scans VS Code's local chat storage, replays each chat-session mutation log and
emits a report that follows the schema in README.md (session -> requests[] ->
result.metadata.toolCallRounds[]), plus per-session and grand totals.

Usage:
    python3 collect.py                        # JSON report to stdout
    python3 collect.py --format tree          # human-readable tree
    python3 collect.py --out report.json      # write to file
    python3 collect.py --user-dir PATH        # non-default VS Code "User" dir (repeatable)
    python3 collect.py --include-empty        # keep sessions with no requests
    python3 collect.py --ticket-prefix ABC    # detect ABC-123 instead of LEGO-123
    python3 collect.py --config team.json     # other config file (default: ./config.json)

Precedence: command-line option > config file > built-in defaults.

Standard library only; works on Python 3.8+.
"""
import argparse
import datetime
import decimal
import glob
import json
import os
import re
import sys
import urllib.parse

COLLECTOR = "vscode-copilot-chat"
COLLECTOR_VERSION = "1.2.0"
DEFAULT_TICKET_PREFIX = "LEGO"  # Jira project key; tickets look like LEGO-123
NO_TICKET = "(no ticket)"
DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
# Used when neither the command line nor the config file sets a value.
BUILTIN_DEFAULTS = {
    "userDirs": [],          # empty -> auto-detect Code / Code - Insiders
    "format": "json",        # json | tree
    "out": None,             # None -> stdout
    "includeEmpty": False,
    "ticketPrefix": DEFAULT_TICKET_PREFIX,
}


# --------------------------------------------------------------------------- #
# Locating storage
# --------------------------------------------------------------------------- #

def default_user_dirs():
    """VS Code (stable + Insiders) "User" directories for the current OS."""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support")
    elif sys.platform.startswith("win"):
        base = os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming"))
    else:
        base = os.environ.get("XDG_CONFIG_HOME", os.path.join(home, ".config"))
    candidates = [os.path.join(base, name, "User") for name in ("Code", "Code - Insiders")]
    return [d for d in candidates if os.path.isdir(d)]


def find_session_files(user_dir):
    """Yield (path, workspace) for every chat-session JSONL under a User dir."""
    pattern = os.path.join(user_dir, "workspaceStorage", "*", "chatSessions", "*.jsonl")
    for path in sorted(glob.glob(pattern)):
        ws_dir = os.path.dirname(os.path.dirname(path))
        yield path, read_workspace(ws_dir)
    pattern = os.path.join(user_dir, "globalStorage", "emptyWindowChatSessions", "*.jsonl")
    for path in sorted(glob.glob(pattern)):
        yield path, {"id": None, "folder": None, "kind": "emptyWindow"}


def read_workspace(ws_dir):
    """Map a workspaceStorage/<hash> dir to the folder/workspace it belongs to."""
    info = {"id": os.path.basename(ws_dir), "folder": None, "kind": "unknown"}
    try:
        with open(os.path.join(ws_dir, "workspace.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return info
    uri = data.get("folder") or data.get("workspace")
    if uri:
        info["kind"] = "folder" if "folder" in data else "workspace"
        parsed = urllib.parse.urlparse(uri)
        info["folder"] = urllib.parse.unquote(parsed.path) if parsed.scheme == "file" else uri
    return info


# --------------------------------------------------------------------------- #
# Replaying the mutation log
# --------------------------------------------------------------------------- #

def replay(path):
    """Rebuild the final session object from a chatSessions/*.jsonl mutation log.

    kind 0: initial snapshot       {"kind":0,"v":{...}}
    kind 1: set value at path      {"kind":1,"k":[...path],"v":value}
    kind 2: array append/splice    {"kind":2,"k":[...path],"v":[items],"i":truncateAt?}
    kind 3: delete at path         {"kind":3,"k":[...path]}
    """
    state = None
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            op = json.loads(line)
            kind, value = op.get("kind"), op.get("v")
            if kind == 0:
                state = value
                continue
            if state is None:
                raise ValueError("%s:%d: mutation before initial snapshot" % (path, lineno))
            *parents, last = op["k"]
            target = state
            for key in parents:
                target = target[key]
            if kind == 1:
                target[last] = value
            elif kind == 2:
                arr = target.setdefault(last, []) if isinstance(target, dict) else target[last]
                if op.get("i") is not None:
                    del arr[op["i"]:]
                arr.extend(value or [])
            elif kind == 3:
                if isinstance(target, list):
                    del target[last]
                else:
                    target.pop(last, None)
            else:
                raise ValueError("%s:%d: unknown mutation kind %r" % (path, lineno, kind))
    return state or {}


# --------------------------------------------------------------------------- #
# Normalising into the report schema
# --------------------------------------------------------------------------- #

def iso(ms):
    if not isinstance(ms, (int, float)):
        return None
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).isoformat()


def pick(obj, *keys):
    obj = obj or {}
    return {k: obj.get(k) for k in keys if obj.get(k) is not None}


def round_credits(value, rounding=decimal.ROUND_HALF_UP):
    """Round to one decimal (x.y). Half-up matches the UI's "GPT-6 Luna • 0.7 credits";
    pass decimal.ROUND_CEILING to always round up (0.71 -> 0.8)."""
    if value is None:
        return None
    return float(decimal.Decimal(str(value)).quantize(decimal.Decimal("0.1"), rounding=rounding))


def text_of(value):
    """Markdown-ish values are either a string or {"value": "..."}."""
    if isinstance(value, dict):
        return value.get("value")
    return value


def norm_selected_model(sel):
    if not sel:
        return None
    meta = sel.get("metadata") or {}
    return {
        "identifier": sel.get("identifier"),
        "metadata": pick(meta, "id", "vendor", "name", "family", "version",
                         "maxInputTokens", "maxOutputTokens", "isBYOK"),
        "modelConfiguration": sel.get("modelConfiguration"),
    }


def norm_response_part(part):
    kind = part.get("kind")
    if kind is None and "value" in part:
        return {"kind": "markdown", "text": text_of(part.get("value"))}
    if kind == "toolInvocationSerialized":
        return {
            "kind": "toolInvocation",
            "toolId": part.get("toolId"),
            "toolCallId": part.get("toolCallId"),
            "message": text_of(part.get("pastTenseMessage")) or text_of(part.get("invocationMessage")),
            "isComplete": part.get("isComplete"),
        }
    if kind == "autoModeResolution":
        return {"kind": kind, "resolved": part.get("resolved")}
    if kind == "thinking":
        return {"kind": kind}  # content is encrypted / empty locally
    return {"kind": kind}


def norm_tool_call_round(rnd):
    return {
        "modelId": rnd.get("modelId"),
        "toolCalls": [pick(tc, "id", "name") for tc in rnd.get("toolCalls") or []],
        "thinking": {"tokens": (rnd.get("thinking") or {}).get("tokens")},
        "timestamp": rnd.get("timestamp"),
    }


def norm_request(req):
    result = req.get("result") or {}
    meta = result.get("metadata") or {}
    agent = req.get("agent") or {}
    return {
        "requestId": req.get("requestId"),
        "timestamp": req.get("timestamp"),
        "modelId": req.get("modelId"),
        "modeInfo": pick(req.get("modeInfo"), "kind", "name", "isBuiltin", "permissionLevel"),
        "agent": {"id": agent.get("id"), "name": agent.get("name"),
                  "extensionVersion": agent.get("extensionVersion")},
        "message": {"text": (req.get("message") or {}).get("text")},
        "copilotCredits": req.get("copilotCredits"),
        "copilotCreditsRounded": round_credits(req.get("copilotCredits")),
        "copilotCreditsRoundedUp": round_credits(req.get("copilotCredits"), decimal.ROUND_CEILING),
        "promptTokens": req.get("promptTokens"),
        "completionTokens": req.get("completionTokens"),
        "promptTokenDetails": req.get("promptTokenDetails") or [],
        "outputBuffer": req.get("outputBuffer"),
        "elapsedMs": req.get("elapsedMs"),
        "modelState": req.get("modelState"),
        "response": [norm_response_part(p) for p in req.get("response") or []],
        "result": {
            "timings": result.get("timings"),
            "details": result.get("details"),
            "metadata": {
                "resolvedModel": meta.get("resolvedModel"),
                "promptTokens": meta.get("promptTokens"),
                "outputTokens": meta.get("outputTokens"),
                "toolCallRounds": [norm_tool_call_round(r) for r in meta.get("toolCallRounds") or []],
            },
        },
    }


def totals_for(requests):
    def total(key):
        return sum(r.get(key) or 0 for r in requests)
    models = sorted({r["result"]["metadata"]["resolvedModel"] for r in requests
                     if r["result"]["metadata"]["resolvedModel"]})
    return {
        "requests": len(requests),
        "billedRequests": sum(1 for r in requests if r.get("copilotCredits") is not None),
        "copilotCredits": round(total("copilotCredits"), 6),
        "copilotCreditsRounded": round(total("copilotCreditsRounded"), 1),  # sums of per-request rounded values
        "copilotCreditsRoundedUp": round(total("copilotCreditsRoundedUp"), 1),
        "promptTokens": total("promptTokens"),
        "completionTokens": total("completionTokens"),
        "toolCallRounds": sum(len(r["result"]["metadata"]["toolCallRounds"]) for r in requests),
        "resolvedModels": models,
    }


def ticket_pattern(prefix):
    # LEGO-123, lego-123, [LEGO-123]; not XLEGO-1 or LEGO-12a
    return re.compile(r"(?<![A-Za-z0-9])%s-(\d+)(?![A-Za-z0-9])" % re.escape(prefix), re.IGNORECASE)


def find_ticket(title, requests, pattern, prefix):
    """Session -> one Jira ticket: the title wins; otherwise the first match in the
    session content, in chat order (user message, then response text and tool messages)."""
    def first(text):
        m = pattern.search(text or "")
        return "%s-%s" % (prefix.upper(), m.group(1)) if m else None

    key = first(title)
    if key:
        return key, {"field": "customTitle", "requestIndex": None}
    for i, req in enumerate(requests):
        key = first(req["message"]["text"])
        if key:
            return key, {"field": "message", "requestIndex": i}
        for part in req["response"]:
            key = first(part.get("text") or part.get("message"))
            if key:
                return key, {"field": "response", "requestIndex": i}
    return None, None


def norm_session(raw, path, workspace, pattern, prefix):
    requests = [norm_request(r) for r in raw.get("requests") or []]
    ticket_key, ticket_source = find_ticket(raw.get("customTitle"), requests, pattern, prefix)
    return {
        "sessionId": raw.get("sessionId"),
        "ticketKey": ticket_key,
        "creationDate": raw.get("creationDate"),
        "customTitle": raw.get("customTitle"),
        "responderUsername": raw.get("responderUsername"),
        "inputState": {"selectedModel": norm_selected_model((raw.get("inputState") or {}).get("selectedModel"))},
        "requests": requests,
        "_source": {"file": path, "workspace": workspace, "ticket": ticket_source},
        "_totals": totals_for(requests),
    }


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

def add_to_bucket(buckets, key, session):
    b = buckets.setdefault(key, {"sessions": 0, "requests": 0, "copilotCredits": 0.0,
                                 "copilotCreditsRounded": 0.0, "copilotCreditsRoundedUp": 0.0})
    t = session["_totals"]
    b["sessions"] += 1
    b["requests"] += t["requests"]
    b["copilotCredits"] = round(b["copilotCredits"] + t["copilotCredits"], 6)
    b["copilotCreditsRounded"] = round(b["copilotCreditsRounded"] + t["copilotCreditsRounded"], 1)
    b["copilotCreditsRoundedUp"] = round(b["copilotCreditsRoundedUp"] + t["copilotCreditsRoundedUp"], 1)
    return b


def collect(user_dirs, include_empty=False, ticket_prefix=DEFAULT_TICKET_PREFIX):
    pattern = ticket_pattern(ticket_prefix)
    sessions, errors = [], []
    for user_dir in user_dirs:
        for path, workspace in find_session_files(user_dir):
            try:
                session = norm_session(replay(path), path, workspace, pattern, ticket_prefix)
            except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
                errors.append({"file": path, "error": "%s: %s" % (type(exc).__name__, exc)})
                continue
            if include_empty or session["requests"]:
                sessions.append(session)
    sessions.sort(key=lambda s: s.get("creationDate") or 0)

    all_requests = [r for s in sessions for r in s["requests"]]
    by_workspace, by_ticket = {}, {}
    for s in sessions:
        add_to_bucket(by_workspace, s["_source"]["workspace"]["folder"] or "(empty window)", s)
        add_to_bucket(by_ticket, s["ticketKey"] or NO_TICKET, s).setdefault("sessionIds", []).append(s["sessionId"])
    by_model = {}
    for r in all_requests:
        key = r["result"]["metadata"]["resolvedModel"] or r.get("modelId") or "(unknown)"
        m = by_model.setdefault(key, {"requests": 0, "copilotCredits": 0.0, "copilotCreditsRounded": 0.0,
                                      "copilotCreditsRoundedUp": 0.0})
        m["requests"] += 1
        m["copilotCredits"] = round(m["copilotCredits"] + (r.get("copilotCredits") or 0), 6)
        m["copilotCreditsRounded"] = round(m["copilotCreditsRounded"] + (r.get("copilotCreditsRounded") or 0), 1)
        m["copilotCreditsRoundedUp"] = round(m["copilotCreditsRoundedUp"] + (r.get("copilotCreditsRoundedUp") or 0), 1)

    totals = totals_for(all_requests)
    totals["sessions"] = len(sessions)
    return {
        "collector": COLLECTOR,
        "collectorVersion": COLLECTOR_VERSION,
        "generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "userDirs": user_dirs,
        "ticketPrefix": ticket_prefix.upper(),
        "totals": totals,
        "byTicket": by_ticket,
        "byWorkspace": by_workspace,
        "byModel": by_model,
        "sessions": sessions,
        "errors": errors,
    }


def fmt_num(n):
    return "-" if n is None else ("{:,}".format(n) if isinstance(n, int) else "{:.4f}".format(n))


def fmt_credits(obj, key="copilotCredits"):
    """Exact credits plus the x.y half-up and round-up values, e.g. "0.7193 (≈0.7 ↑0.8)"."""
    exact = obj.get(key)
    if exact is None:
        return "-"
    return "%.4f (≈%.1f ↑%.1f)" % (exact, obj.get(key + "Rounded") or 0, obj.get(key + "RoundedUp") or 0)


def render_tree(report):
    t = report["totals"]
    out = ["report  (%s v%s, %s)" % (report["collector"], report["collectorVersion"], report["generatedAt"]),
           "├── totals  sessions=%d requests=%d credits=%s prompt=%s completion=%s"
           % (t["sessions"], t["requests"], fmt_credits(t),
              fmt_num(t["promptTokens"]), fmt_num(t["completionTokens"]))]
    for name, tk in report["byTicket"].items():
        out.append("├── ticket %s  sessions=%d requests=%d credits=%s"
                   % (name, tk["sessions"], tk["requests"], fmt_credits(tk)))
    for name, ws in report["byWorkspace"].items():
        out.append("├── workspace %s  sessions=%d requests=%d credits=%s"
                   % (name, ws["sessions"], ws["requests"], fmt_credits(ws)))
    for name, m in report["byModel"].items():
        out.append("├── model %s  requests=%d credits=%s" % (name, m["requests"], fmt_credits(m)))
    for err in report["errors"]:
        out.append("├── ERROR %s  %s" % (err["file"], err["error"]))

    sessions = report["sessions"]
    for si, s in enumerate(sessions):
        last_s = si == len(sessions) - 1
        p = "    " if last_s else "│   "
        sel = s["inputState"]["selectedModel"] or {}
        out.append("%s session %s  \"%s\"" % ("└──" if last_s else "├──", s["sessionId"], s["customTitle"] or "-"))
        src = s["_source"]["ticket"]
        out.append(p + "├── ticketKey %s%s" % (s["ticketKey"] or "-", "  (from %s%s)" % (
            src["field"], "" if src["requestIndex"] is None else " of request [%d]" % src["requestIndex"]) if src else ""))
        out.append(p + "├── creationDate %s  responder=%s  workspace=%s"
                   % (iso(s["creationDate"]), s["responderUsername"] or "-",
                      s["_source"]["workspace"]["folder"] or "(empty window)"))
        out.append(p + "├── inputState.selectedModel %s  family=%s  config=%s"
                   % (sel.get("identifier"), (sel.get("metadata") or {}).get("family"),
                      json.dumps(sel.get("modelConfiguration"))))
        out.append(p + "├── ★ totals credits=%s requests=%d prompt=%s completion=%s"
                   % (fmt_credits(s["_totals"]), s["_totals"]["requests"],
                      fmt_num(s["_totals"]["promptTokens"]), fmt_num(s["_totals"]["completionTokens"])))
        out.append(p + "└── requests[%d]" % len(s["requests"]))
        for ri, r in enumerate(s["requests"]):
            last_r = ri == len(s["requests"]) - 1
            q = p + "    " + ("    " if last_r else "│   ")
            md = r["result"]["metadata"]
            msg = (r["message"]["text"] or "").replace("\n", " ")
            out.append(p + "    %s [%d] %s  %s" % ("└──" if last_r else "├──", ri, r["requestId"], iso(r["timestamp"])))
            out.append(q + "├── message \"%s\"" % (msg[:70] + ("…" if len(msg) > 70 else "")))
            out.append(q + "├── modelId=%s  mode=%s  agent=%s"
                       % (r["modelId"], r["modeInfo"].get("name") or r["modeInfo"].get("kind"), r["agent"]["id"]))
            out.append(q + "├── ★ copilotCredits=%s  promptTokens=%s  completionTokens=%s"
                       % (fmt_credits(r), fmt_num(r["promptTokens"]), fmt_num(r["completionTokens"])))
            details = ", ".join("%s %s%%" % (d.get("label"), d.get("percentageOfPrompt")) for d in r["promptTokenDetails"])
            out.append(q + "├── ★ promptTokenDetails  %s" % (details or "-"))
            out.append(q + "├── elapsedMs=%s  outputBuffer=%s  modelState=%s"
                       % (fmt_num(r["elapsedMs"]), fmt_num(r["outputBuffer"]), json.dumps(r["modelState"])))
            kinds = {}
            for part in r["response"]:
                kinds[part["kind"]] = kinds.get(part["kind"], 0) + 1
            out.append(q + "├── response[%d]  %s" % (len(r["response"]), ", ".join("%s×%d" % kv for kv in kinds.items())))
            out.append(q + "└── result  timings=%s" % json.dumps(r["result"]["timings"]))
            out.append(q + "    ├── ★ details \"%s\"" % (r["result"]["details"] or "-"))
            out.append(q + "    └── metadata  ★ resolvedModel=%s  promptTokens=%s  outputTokens=%s"
                       % (md["resolvedModel"], fmt_num(md["promptTokens"]), fmt_num(md["outputTokens"])))
            rounds = md["toolCallRounds"]
            out.append(q + "        └── toolCallRounds[%d]" % len(rounds))
            for xi, x in enumerate(rounds):
                tools = ", ".join(tc.get("name") or "?" for tc in x["toolCalls"]) or "(final answer)"
                out.append(q + "            %s %s  thinking=%s  %s"
                           % ("└──" if xi == len(rounds) - 1 else "├──", x["modelId"],
                              fmt_num(x["thinking"]["tokens"]), tools))
    return "\n".join(out)


def load_config(path, explicit):
    """Read config.json; keys missing from it fall back to BUILTIN_DEFAULTS.
    Relative paths in it are resolved against the config file's directory."""
    config = dict(BUILTIN_DEFAULTS)
    if not os.path.isfile(path):
        if explicit:
            raise SystemExit("config file not found: %s" % path)
        return config
    with open(path, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except ValueError as exc:
            raise SystemExit("invalid JSON in %s: %s" % (path, exc))
    data = {k: v for k, v in data.items() if not k.startswith("_")}  # "_comment" etc.
    unknown = sorted(set(data) - set(BUILTIN_DEFAULTS))
    if unknown:
        raise SystemExit("unknown key(s) in %s: %s (allowed: %s)"
                         % (path, ", ".join(unknown), ", ".join(BUILTIN_DEFAULTS)))
    base = os.path.dirname(os.path.abspath(path))

    def resolve(p):
        p = os.path.expanduser(os.path.expandvars(p))
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))

    if data.get("userDirs"):
        data["userDirs"] = [resolve(d) for d in data["userDirs"]]
    if data.get("out"):
        data["out"] = resolve(data["out"])
    config.update(data)
    return config


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Collect VS Code Copilot Chat usage into a report.",
        epilog="Options not given on the command line come from the config file, then built-in defaults.")
    ap.add_argument("--config", help="Config file (default: config.json next to this script).")
    ap.add_argument("--user-dir", action="append",
                    help="VS Code User directory (repeatable). Default: auto-detect Code / Code - Insiders.")
    ap.add_argument("--format", choices=("json", "tree"))
    ap.add_argument("--out", help="Write report to this file; '-' forces stdout.")
    ap.add_argument("--include-empty", dest="include_empty", action="store_const", const=True,
                    help="Keep sessions that have no requests.")
    ap.add_argument("--no-include-empty", dest="include_empty", action="store_const", const=False)
    ap.add_argument("--ticket-prefix", help="Jira project key used to detect tickets (built-in default: LEGO).")
    args = ap.parse_args(argv)

    config = load_config(args.config or DEFAULT_CONFIG_PATH, explicit=bool(args.config))

    def opt(cli_value, key):
        return config[key] if cli_value is None else cli_value

    user_dirs = args.user_dir or config["userDirs"] or default_user_dirs()
    if not user_dirs:
        ap.error("no VS Code User directory found; pass --user-dir or set userDirs in the config")
    fmt, out = opt(args.format, "format"), opt(args.out, "out")
    if fmt not in ("json", "tree"):
        ap.error("format must be json or tree, got %r" % fmt)
    report = collect(user_dirs, include_empty=bool(opt(args.include_empty, "includeEmpty")),
                     ticket_prefix=opt(args.ticket_prefix, "ticketPrefix"))
    text = render_tree(report) if fmt == "tree" else json.dumps(report, indent=2, ensure_ascii=False)
    if out and out != "-":
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print("wrote %s  (sessions=%d, credits=%s)" % (out, report["totals"]["sessions"],
                                                      fmt_credits(report["totals"])), file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
