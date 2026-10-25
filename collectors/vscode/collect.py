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
import glob
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import (DEFAULT_TICKET_PREFIX, add_common_args, credit_fields, find_ticket, load_config,  # noqa: E402
                    now_iso, pick, render_tree, summarise, ticket_pattern, totals_for, write_report)

COLLECTOR = "vscode-copilot-chat"
COLLECTOR_VERSION = "1.3.0"
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
        **credit_fields(req.get("copilotCredits")),
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

    return {
        "collector": COLLECTOR,
        "collectorVersion": COLLECTOR_VERSION,
        "generatedAt": now_iso(),
        "userDirs": user_dirs,
        "ticketPrefix": ticket_prefix.upper(),
        **summarise(sessions, lambda s: s["_source"]["workspace"]["folder"] or "(empty window)"),
        "sessions": sessions,
        "errors": errors,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Collect VS Code Copilot Chat usage into a report.",
        epilog="Options not given on the command line come from the config file, then built-in defaults.")
    ap.add_argument("--user-dir", action="append",
                    help="VS Code User directory (repeatable). Default: auto-detect Code / Code - Insiders.")
    add_common_args(ap)
    args = ap.parse_args(argv)

    config = load_config(args.config or DEFAULT_CONFIG_PATH, bool(args.config), BUILTIN_DEFAULTS,
                         path_keys=("userDirs", "out"))

    def opt(cli_value, key):
        return config[key] if cli_value is None else cli_value

    user_dirs = args.user_dir or config["userDirs"] or default_user_dirs()
    if not user_dirs:
        ap.error("no VS Code User directory found; pass --user-dir or set userDirs in the config")
    fmt = opt(args.format, "format")
    if fmt not in ("json", "tree"):
        ap.error("format must be json or tree, got %r" % fmt)
    report = collect(user_dirs, include_empty=bool(opt(args.include_empty, "includeEmpty")),
                     ticket_prefix=opt(args.ticket_prefix, "ticketPrefix"))
    write_report(report, fmt, opt(args.out, "out"), render_tree)


if __name__ == "__main__":
    main()
