#!/usr/bin/env python3
"""JetBrains (IntelliJ IDEA, PyCharm, …) Copilot usage collector.

Copilot reaches a JetBrains IDE in two ways, and both run the Copilot CLI runtime, which
writes the real usage to ~/.copilot/session-state/<sessionId>/events.jsonl:

1. "ai-assistant":   JetBrains AI Assistant runs Copilot as an ACP agent. The IDE keeps the chat
                     UI history in <config>/aia-task-history/<taskId>.events, linked to the CLI
                     session by <taskId>.agentsession.
2. "copilot-plugin": the GitHub Copilot plugin. Its CLI sessions have client_name
                     "copilot-intellij"; the plugin's own store (~/.config/github-copilot) adds
                     the IDE product code and plugin version.

This collector joins IDE context with credits and tokens from the CLI log, in the same
report schema as the other collectors. See README.md.

Usage:
    python3 collect.py                        # JSON report to stdout
    python3 collect.py --format tree          # human-readable tree
    python3 collect.py --out report.json      # write to file
    python3 collect.py --ide-dir PATH         # a JetBrains config dir, e.g. .../JetBrains/IntelliJIdea2026.2 (repeatable)
    python3 collect.py --copilot-home PATH    # where the linked Copilot CLI sessions live (repeatable)
    python3 collect.py --plugin-dir PATH      # GitHub Copilot plugin store, default ~/.config/github-copilot
    python3 collect.py --include-empty        # keep sessions with no requests
    python3 collect.py --ticket-prefix ABC    # detect ABC-123 instead of LEGO-123
    python3 collect.py --config team.json     # other config file (default: ./config.json)

Precedence: command-line option > config file > built-in defaults.

Standard library only; works on Python 3.8+.
"""
import argparse
import base64
import fnmatch
import glob
import importlib.util
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from common import (DEFAULT_TICKET_PREFIX, add_common_args, add_to_bucket, find_ticket, fmt_num,  # noqa: E402
                    load_config, now_iso, render_tree, round_credits, summarise, ticket_pattern, totals_for,
                    write_report)


def _load_cli_collector():
    # Reuse the Copilot CLI event-log parser; both modules are named collect.py, so load by path.
    spec = importlib.util.spec_from_file_location(
        "copilot_cli_collect", os.path.join(os.path.dirname(HERE), "copilot_cli", "collect.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load_cli_collector()

COLLECTOR = "intellij"
COLLECTOR_VERSION = "1.1.0"
EVENTS_HEADER = "AUI_EVENTS_V1"
UUID_RE = re.compile(rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# github-copilot/<code>/ product folders used by the Copilot plugin
PRODUCT_CODES = {"iu": "IntelliJ IDEA Ultimate", "ic": "IntelliJ IDEA Community", "py": "PyCharm Professional",
                 "pc": "PyCharm Community", "ws": "WebStorm", "go": "GoLand", "rd": "Rider", "cl": "CLion",
                 "ps": "PhpStorm", "rm": "RubyMine", "db": "DataGrip", "ai": "Android Studio", "rr": "RustRover"}
DEFAULT_CONFIG_PATH = os.path.join(HERE, "config.json")
# Used when neither the command line nor the config file sets a value.
BUILTIN_DEFAULTS = {
    "ideDirs": [],           # empty -> every JetBrains config dir that has aia-task-history
    "copilotHomes": [],      # empty -> $COPILOT_HOME or ~/.copilot
    "pluginDirs": [],        # empty -> ~/.config/github-copilot (%LOCALAPPDATA%\github-copilot on Windows)
    "pluginClients": ["copilot-intellij*"],  # client_name globs of Copilot-plugin CLI sessions
    "format": "json",        # json | tree
    "out": None,             # None -> stdout
    "includeEmpty": False,
    "ticketPrefix": DEFAULT_TICKET_PREFIX,
}


# --------------------------------------------------------------------------- #
# Locating storage
# --------------------------------------------------------------------------- #

def jetbrains_base():
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "JetBrains")
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming")), "JetBrains")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.join(home, ".config")), "JetBrains")


def default_ide_dirs():
    """All product/version config dirs (IntelliJIdea2026.2, PyCharm2026.1, …) with AI task history."""
    return sorted(os.path.dirname(d) for d in glob.glob(os.path.join(jetbrains_base(), "*", "aia-task-history")))


def default_plugin_dirs():
    home = os.path.expanduser("~")
    candidates = [os.path.join(home, ".config", "github-copilot")]
    if sys.platform.startswith("win"):
        candidates.insert(0, os.path.join(os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local")),
                                          "github-copilot"))
    return [d for d in candidates if os.path.isdir(d)]


# --------------------------------------------------------------------------- #
# IDE metadata
# --------------------------------------------------------------------------- #

def read_plugin_index(plugin_dirs):
    """CLI session id -> {productCode, projectKey, pluginVersion} for the Copilot plugin.

    The plugin keeps its chat list in <dir>/<productCode>/chat-agent-sessions/<projectKey>/*.db
    (Nitrite, a Java object store). Rather than decode it, scan it for the session ids it holds."""
    index = {}
    for plugin_dir in plugin_dirs:
        try:
            with open(os.path.join(plugin_dir, "versions.json"), encoding="utf-8") as f:
                version = json.load(f).get("copilot-intellij")
        except (OSError, ValueError):
            version = None
        for db in glob.glob(os.path.join(plugin_dir, "*", "chat-agent-sessions", "*", "*.db")):
            project_dir = os.path.dirname(db)
            product = os.path.basename(os.path.dirname(os.path.dirname(project_dir)))
            try:
                with open(db, "rb") as f:
                    ids = set(UUID_RE.findall(f.read()))
            except OSError:
                continue
            for sid in ids:
                index.setdefault(sid.decode(), {"productCode": product, "projectKey": os.path.basename(project_dir),
                                                "pluginVersion": version, "pluginDir": plugin_dir})
    return index


def _option(elem, name):
    for opt in elem.findall("option"):
        if opt.get("name") == name:
            return opt
    return None


def _option_value(elem, name):
    opt = _option(elem, name)
    return opt.get("value") if opt is not None else None


def _int(text):
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def read_recent_projects(ide_dir):
    """projectWorkspaceId -> {path, build} from options/recentProjects.xml."""
    projects = {}
    try:
        root = ET.parse(os.path.join(ide_dir, "options", "recentProjects.xml")).getroot()
    except (OSError, ET.ParseError):
        return projects
    for entry in root.iter("entry"):
        meta = entry.find("value/RecentProjectMetaInfo")
        if meta is not None and meta.get("projectWorkspaceId"):
            projects[meta.get("projectWorkspaceId")] = {"path": entry.get("key"),
                                                        "build": _option_value(meta, "build")}
    return projects


def read_chat_index(ide_dir):
    """taskId -> chat metadata from workspace/<projectWorkspaceId>.xml (ChatSessionStateTemp)."""
    projects = read_recent_projects(ide_dir)
    chats = {}
    for path in glob.glob(os.path.join(ide_dir, "workspace", "*.xml")):
        workspace_id = os.path.splitext(os.path.basename(path))[0]
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError):
            continue
        for comp in root.iter("component"):
            if not comp.get("name", "").startswith("ChatSessionState"):
                continue
            for chat in comp.iter("SerializedChat"):
                uid = _option_value(chat, "uid")
                if not uid:
                    continue
                title_opt = _option(chat, "title")
                title_elem = title_opt.find("SerializedChatTitle") if title_opt is not None else None
                stats_opt = _option(chat, "statisticInformation")
                stats = stats_opt.find("ChatStatisticInformation") if stats_opt is not None else None
                project = projects.get(workspace_id) or {}
                chats[uid] = {
                    "title": _option_value(title_elem, "text") if title_elem is not None else None,
                    "titleIsCustom": _option_value(title_elem, "custom") == "true" if title_elem is not None else None,
                    "chatModelId": _option_value(chat, "chatModelId"),
                    "createdAt": _int(_option_value(stats, "timestamp")) if stats is not None else None,
                    "modifiedAt": _int(_option_value(chat, "modifiedAt")),
                    "sourceActionType": _option_value(stats, "sourceActionType") if stats is not None else None,
                    "projectWorkspaceId": workspace_id,
                    "project": project.get("path"),
                    "build": project.get("build"),
                }
    return chats


def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def read_task_events(path):
    """<taskId>.events: header line, then one base64-encoded JSON event per line."""
    with open(path, encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]
    if not lines or lines[0] != EVENTS_HEADER:
        raise ValueError("%s: unexpected header %r" % (path, lines[0] if lines else None))
    events = []
    for lineno, line in enumerate(lines[1:], 2):
        try:
            events.append(json.loads(base64.b64decode(line)))
        except ValueError as exc:
            raise ValueError("%s:%d: %s" % (path, lineno, exc))
    return events


def short_type(name):
    return (name or "").rsplit(".", 1)[-1]


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #

def requests_from_ide_events(events, agent_id):
    """Fallback when no linked Copilot CLI log exists: prompts and answers, but no usage."""
    requests, markdown = [], {}
    for ev in events:
        etype = short_type(ev.get("type"))
        if etype == "ChatSessionUserPromptEvent":
            requests.append({
                "requestId": "ide-event-%s" % (ev.get("id") or {}).get("id"),
                "timestamp": None, "modelId": agent_id, "modeInfo": {"kind": "ide"},
                "agent": {"id": agent_id, "name": None, "extensionVersion": None},
                "message": {"text": ev.get("prompt")},
                "copilotCredits": None, "copilotCreditsRounded": None, "copilotCreditsRoundedUp": None,
                "promptTokens": None, "completionTokens": None, "promptTokenDetails": [],
                "outputBuffer": None, "elapsedMs": None, "modelState": None, "response": [],
                "result": {"timings": None, "details": None,
                           "metadata": {"resolvedModel": None, "promptTokens": None, "outputTokens": None,
                                        "toolCallRounds": []}},
            })
            markdown = {}
            continue
        if etype != "ChatSessionMessageBlockEvent" or not requests:
            continue
        block = ev.get("event") or {}
        kind, step = short_type(block.get("kind")), block.get("stepId")
        response = requests[-1]["response"]
        if kind == "MarkdownBlockUpdatedEvent":
            if step in markdown:                          # streamed in chunks per step
                markdown[step]["text"] += block.get("textChunk") or ""
            else:
                markdown[step] = {"kind": "markdown", "text": block.get("textChunk") or ""}
                response.append(markdown[step])
        elif kind in ("ToolBlockUpdatedEvent", "ViewFilesBlockUpdatedEvent"):
            files = ", ".join(f.get("path", "") for f in block.get("files") or block.get("missingFiles") or [])
            response.append({"kind": "toolInvocation", "toolId": block.get("toolType") or "ViewFiles",
                             "toolCallId": step, "message": block.get("title") or files or None,
                             "isComplete": block.get("status") in ("COMPLETED", "FAILED"),
                             "success": block.get("status") == "COMPLETED"})
        elif kind == "AgentThoughtBlockUpdatedEvent":
            response.append({"kind": "thinking"})
    return requests


def linked_cli_dir(acp_session_id, copilot_homes):
    if not acp_session_id:
        return None
    for home in copilot_homes:
        d = os.path.join(home, "session-state", acp_session_id)
        if os.path.isfile(os.path.join(d, "events.jsonl")):
            return d
    return None


def build_session(ide_dir, task_id, chat, copilot_homes, pattern, prefix):
    base = os.path.join(ide_dir, "aia-task-history", task_id)
    events_path = base + ".events"
    events = read_task_events(events_path)
    agent_session = read_text(base + ".agentsession") or ""
    agent_id, _, acp_session_id = agent_session.rpartition(":")
    if not agent_id:                                  # no ":" -> whole value is the agent id
        agent_id, acp_session_id = acp_session_id or None, None
    if not agent_id:
        agent_id = next(((ev.get("agentId") or {}).get("id") for ev in events if ev.get("agentId")), None)
    context_window = None
    try:
        context_window = json.loads(read_text(base + ".usage") or "null")
    except ValueError:
        pass

    cli_dir = linked_cli_dir(acp_session_id, copilot_homes)
    if cli_dir:
        session = cli.build_session(cli_dir, pattern, prefix)
        usage_source = "copilot-cli"
    else:
        requests = requests_from_ide_events(events, agent_id)
        session = {"sessionId": task_id, "creationDate": None, "customTitle": None,
                   "inputState": {"selectedModel": {"identifier": agent_id, "metadata": {}, "modelConfiguration": {}}},
                   "requests": requests, "sessionUsage": None, "backgroundCalls": [], "quotaSnapshot": None,
                   "sessionErrors": [], "_source": {"workspace": {}}, "_totals": totals_for(requests)}
        usage_source = None

    # The IDE's chat title is what the user sees (and can rename), so it wins for tickets.
    title = chat.get("title") or session.get("customTitle")
    ticket_key, ticket_source = find_ticket(title, session["requests"], pattern, prefix)
    cli_workspace = session["_source"].get("workspace") or {}
    product = os.path.basename(ide_dir.rstrip(os.sep))
    session.update({
        "sessionId": task_id,
        "acpSessionId": acp_session_id,
        "ticketKey": ticket_key,
        "creationDate": chat.get("createdAt") or session.get("creationDate"),
        "customTitle": title,
        "responderUsername": agent_id,
        "usageSource": usage_source,   # None -> the IDE log has no token/credit data for this session
        "integration": "ai-assistant",
    })
    session["_source"] = {
        "file": events_path,
        "linkedFile": os.path.join(cli_dir, "events.jsonl") if cli_dir else None,
        "workspace": {"id": chat.get("projectWorkspaceId"), "folder": chat.get("project") or cli_workspace.get("folder"),
                      "kind": "intellij", "repository": cli_workspace.get("repository"),
                      "branch": cli_workspace.get("branch"), "client": cli_workspace.get("client")},
        "ide": {"product": product, "build": chat.get("build"), "configDir": ide_dir, "taskId": task_id,
                "agentId": agent_id, "chatModelId": chat.get("chatModelId"), "modifiedAt": chat.get("modifiedAt"),
                "contextWindow": context_window, "copilotVersion": cli_workspace.get("copilotVersion")},
        "ticket": ticket_source,
    }
    return session


def plugin_session_dirs(copilot_homes, plugin_clients):
    for home in copilot_homes:
        for session_dir in cli.find_session_dirs(home):
            client = cli.read_workspace_yaml(session_dir).get("client_name") or ""
            if any(fnmatch.fnmatch(client, pat) for pat in plugin_clients):
                yield session_dir


def build_plugin_session(cli_dir, plugin_info, pattern, prefix):
    """A GitHub Copilot plugin chat: everything comes from its Copilot CLI session."""
    session = cli.build_session(cli_dir, pattern, prefix)
    ws = session["_source"]["workspace"]
    code = plugin_info.get("productCode")
    session.update({"acpSessionId": None, "usageSource": "copilot-cli", "integration": "copilot-plugin",
                    "responderUsername": "GitHub Copilot (JetBrains plugin)"})
    session["_source"] = {
        "file": session["_source"]["file"],
        "linkedFile": None,
        "workspace": dict(ws, kind="intellij"),
        "ide": {"product": PRODUCT_CODES.get(code, code), "productCode": code, "build": None,
                "projectKey": plugin_info.get("projectKey"), "pluginVersion": plugin_info.get("pluginVersion"),
                "agentId": ws.get("client"), "copilotVersion": ws.get("copilotVersion"), "contextWindow": None},
        "ticket": session["_source"]["ticket"],
    }
    return session


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

def collect(ide_dirs, copilot_homes, include_empty=False, ticket_prefix=DEFAULT_TICKET_PREFIX,
            plugin_dirs=(), plugin_clients=("copilot-intellij*",)):
    pattern = ticket_pattern(ticket_prefix)
    sessions, errors = [], []
    plugin_index = read_plugin_index(plugin_dirs)
    for cli_dir in plugin_session_dirs(copilot_homes, plugin_clients):
        try:
            session = build_plugin_session(cli_dir, plugin_index.get(os.path.basename(cli_dir), {}),
                                           pattern, ticket_prefix)
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            errors.append({"file": cli_dir, "error": "%s: %s" % (type(exc).__name__, exc)})
            continue
        if include_empty or session["requests"]:
            sessions.append(session)
    for ide_dir in ide_dirs:
        chats = read_chat_index(ide_dir)
        for events_path in sorted(glob.glob(os.path.join(ide_dir, "aia-task-history", "*.events"))):
            task_id = os.path.splitext(os.path.basename(events_path))[0]
            try:
                session = build_session(ide_dir, task_id, chats.get(task_id, {}), copilot_homes, pattern, ticket_prefix)
            except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
                errors.append({"file": events_path, "error": "%s: %s" % (type(exc).__name__, exc)})
                continue
            if include_empty or session["requests"]:
                sessions.append(session)
    sessions.sort(key=lambda s: s.get("creationDate") or 0)
    report = {
        "collector": COLLECTOR,
        "collectorVersion": COLLECTOR_VERSION,
        "generatedAt": now_iso(),
        "ideDirs": ide_dirs,
        "copilotHomes": copilot_homes,
        "pluginDirs": list(plugin_dirs),
        "pluginClients": list(plugin_clients),
        "ticketPrefix": ticket_prefix.upper(),
        **summarise(sessions, lambda s: s["_source"]["workspace"]["folder"] or "(unknown project)"),
        "account": {"quota": cli.account_quota(sessions)},
        "sessions": sessions,
        "errors": errors,
    }
    by_integration = {}
    for s in sessions:
        add_to_bucket(by_integration, s["integration"], s)
    report["byIntegration"] = by_integration
    report["totals"]["sessionsWithoutUsage"] = sum(1 for s in sessions if not s["usageSource"])
    report["totals"]["premiumRequests"] = sum(r.get("premiumRequestCost") or 0
                                              for s in sessions for r in s["requests"])
    return report


def render(report):
    t = report["totals"]
    header = ["├── premiumRequests=%s  sessionsWithoutUsage=%s" % (t["premiumRequests"], t["sessionsWithoutUsage"])]
    for name, b in report["byIntegration"].items():
        header.append("├── integration %s  sessions=%d requests=%d credits=%.4f (≈%.1f ↑%.1f)"
                      % (name, b["sessions"], b["requests"], b["copilotCredits"], b["copilotCreditsRounded"],
                         b["copilotCreditsRoundedUp"]))
    quota = (report["account"] or {}).get("quota")
    for name, q in ((quota or {}).get("quotas") or {}).items():
        if q.get("entitlementRequests"):
            header.append("├── account quota %s  used≈%s of %s  (resets %s)"
                          % (name, q.get("usedEstimate"), q.get("entitlementRequests"), q.get("resetDate")))

    def session_lines(s):
        ide = s["_source"]["ide"]
        cw = ide.get("contextWindow") or {}
        lines = ["├── integration=%s  ide %s (%s)  agent=%s  acpSession=%s  plugin=%s"
                 % (s["integration"], ide["product"], ide.get("build") or "-", ide["agentId"],
                    s.get("acpSessionId") or "-", ide.get("pluginVersion") or "-"),
                 "├── usageSource=%s  contextWindow=%s/%s  repository=%s  branch=%s"
                 % (s["usageSource"] or "NONE (no linked Copilot CLI log)", fmt_num(cw.get("used")),
                    fmt_num(cw.get("size")), s["_source"]["workspace"].get("repository"),
                    s["_source"]["workspace"].get("branch"))]
        u = s.get("sessionUsage")
        if u:
            lines.append("├── sessionUsage (CLI running total) credits=%s premiumRequests=%s matchesRequests=%s"
                         % (fmt_num(u["copilotCredits"]), u["totalPremiumRequests"], u["matchesRequests"]))
        return lines

    def round_suffix(x):
        if x.get("copilotCredits") is None:
            return ""
        u = x["usage"]
        return "%s  credits=%.6f (≈%.1f) in=%s cached=%s out=%s" % (
            "  [sub-agent: %s]" % x["subAgent"]["name"] if x.get("subAgent") else "",
            x["copilotCredits"], round_credits(x["copilotCredits"]), fmt_num(u["inputTokens"]),
            fmt_num(u["cacheReadTokens"]), fmt_num(u["outputTokens"]))

    return render_tree(report, header_lines=header, session_lines=session_lines, round_suffix=round_suffix)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Collect JetBrains AI Assistant / Copilot usage into a report.",
        epilog="Options not given on the command line come from the config file, then built-in defaults.")
    ap.add_argument("--ide-dir", action="append",
                    help="JetBrains config dir, e.g. .../JetBrains/IntelliJIdea2026.2 (repeatable). "
                         "Default: every product dir that has aia-task-history.")
    ap.add_argument("--copilot-home", action="append",
                    help="Copilot CLI home holding the linked sessions (repeatable). Default: $COPILOT_HOME or ~/.copilot.")
    ap.add_argument("--plugin-dir", action="append",
                    help="GitHub Copilot plugin store (repeatable). Default: ~/.config/github-copilot.")
    add_common_args(ap)
    args = ap.parse_args(argv)

    config = load_config(args.config or DEFAULT_CONFIG_PATH, bool(args.config), BUILTIN_DEFAULTS,
                         path_keys=("ideDirs", "copilotHomes", "pluginDirs", "out"))

    def opt(cli_value, key):
        return config[key] if cli_value is None else cli_value

    ide_dirs = args.ide_dir or config["ideDirs"] or default_ide_dirs()
    homes = args.copilot_home or config["copilotHomes"] or cli.default_copilot_homes()
    plugin_dirs = args.plugin_dir or config["pluginDirs"] or default_plugin_dirs()
    if not ide_dirs and not homes:
        ap.error("no JetBrains config dir or Copilot home found; pass --ide-dir / --copilot-home")
    fmt = opt(args.format, "format")
    if fmt not in ("json", "tree"):
        ap.error("format must be json or tree, got %r" % fmt)
    report = collect(ide_dirs, homes, include_empty=bool(opt(args.include_empty, "includeEmpty")),
                     ticket_prefix=opt(args.ticket_prefix, "ticketPrefix"), plugin_dirs=plugin_dirs,
                     plugin_clients=config["pluginClients"])
    write_report(report, fmt, opt(args.out, "out"), render)


if __name__ == "__main__":
    main()
