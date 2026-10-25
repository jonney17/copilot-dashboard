"""Shared helpers for the Copilot usage collectors (vscode/, copilot_cli/).

Every collector normalises its source into the same report schema
(session -> requests[] -> result.metadata.toolCallRounds[]), so ticket detection,
credit rounding, aggregation, config loading and tree rendering live here.

Standard library only; works on Python 3.8+.
"""
import datetime
import decimal
import json
import os
import re
import sys

DEFAULT_TICKET_PREFIX = "LEGO"  # Jira project key; tickets look like LEGO-123
NO_TICKET = "(no ticket)"


# --------------------------------------------------------------------------- #
# Small value helpers
# --------------------------------------------------------------------------- #

def iso(ms):
    if not isinstance(ms, (int, float)):
        return None
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).isoformat()


def epoch_ms(text):
    """ISO-8601 string ("2026-10-25T03:47:45.603Z") -> epoch milliseconds."""
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(dt.timestamp() * 1000)


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def pick(obj, *keys):
    obj = obj or {}
    return {k: obj.get(k) for k in keys if obj.get(k) is not None}


def round_credits(value, rounding=decimal.ROUND_HALF_UP):
    """Round to one decimal (x.y). Half-up matches the UI's "GPT-6 Luna • 0.7 credits";
    pass decimal.ROUND_CEILING to always round up (0.71 -> 0.8)."""
    if value is None:
        return None
    return float(decimal.Decimal(str(value)).quantize(decimal.Decimal("0.1"), rounding=rounding))


def credit_fields(value):
    """The three credit values every request carries."""
    return {
        "copilotCredits": value,
        "copilotCreditsRounded": round_credits(value),
        "copilotCreditsRoundedUp": round_credits(value, decimal.ROUND_CEILING),
    }


# --------------------------------------------------------------------------- #
# Tickets
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #

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


def _add_credits(bucket, src):
    bucket["copilotCredits"] = round(bucket["copilotCredits"] + (src.get("copilotCredits") or 0), 6)
    bucket["copilotCreditsRounded"] = round(bucket["copilotCreditsRounded"] + (src.get("copilotCreditsRounded") or 0), 1)
    bucket["copilotCreditsRoundedUp"] = round(bucket["copilotCreditsRoundedUp"] + (src.get("copilotCreditsRoundedUp") or 0), 1)


def add_to_bucket(buckets, key, session):
    b = buckets.setdefault(key, {"sessions": 0, "requests": 0, "copilotCredits": 0.0,
                                 "copilotCreditsRounded": 0.0, "copilotCreditsRoundedUp": 0.0})
    b["sessions"] += 1
    b["requests"] += session["_totals"]["requests"]
    _add_credits(b, session["_totals"])
    return b


def summarise(sessions, workspace_key):
    """Report-level totals, byTicket, byWorkspace and byModel for normalised sessions."""
    all_requests = [r for s in sessions for r in s["requests"]]
    by_workspace, by_ticket = {}, {}
    for s in sessions:
        add_to_bucket(by_workspace, workspace_key(s), s)
        add_to_bucket(by_ticket, s["ticketKey"] or NO_TICKET, s).setdefault("sessionIds", []).append(s["sessionId"])
    by_model = {}
    for r in all_requests:
        key = r["result"]["metadata"]["resolvedModel"] or r.get("modelId") or "(unknown)"
        m = by_model.setdefault(key, {"requests": 0, "copilotCredits": 0.0, "copilotCreditsRounded": 0.0,
                                      "copilotCreditsRoundedUp": 0.0})
        m["requests"] += 1
        _add_credits(m, r)
    totals = totals_for(all_requests)
    totals["sessions"] = len(sessions)
    return {"totals": totals, "byTicket": by_ticket, "byWorkspace": by_workspace, "byModel": by_model}


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

def fmt_num(n):
    return "-" if n is None else ("{:,}".format(n) if isinstance(n, int) else "{:.4f}".format(n))


def fmt_credits(obj, key="copilotCredits"):
    """Exact credits plus the x.y half-up and round-up values, e.g. "0.7193 (≈0.7 ↑0.8)"."""
    exact = obj.get(key)
    if exact is None:
        return "-"
    return "%.4f (≈%.1f ↑%.1f)" % (exact, obj.get(key + "Rounded") or 0, obj.get(key + "RoundedUp") or 0)


def render_tree(report, header_lines=(), session_lines=None, round_suffix=None):
    """Human-readable tree of a report. Collector-specific extras plug in via:
    header_lines: extra "├── …" lines after the totals; session_lines(session) -> lines;
    round_suffix(round) -> text appended to each tool-call round."""
    t = report["totals"]
    out = ["report  (%s v%s, %s)" % (report["collector"], report["collectorVersion"], report["generatedAt"]),
           "├── totals  sessions=%d requests=%d credits=%s prompt=%s completion=%s"
           % (t["sessions"], t["requests"], fmt_credits(t),
              fmt_num(t["promptTokens"]), fmt_num(t["completionTokens"]))]
    out.extend(header_lines)
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
        if session_lines:
            out.extend(p + line for line in session_lines(s))
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
                out.append(q + "            %s %s  thinking=%s  %s%s"
                           % ("└──" if xi == len(rounds) - 1 else "├──", x["modelId"],
                              fmt_num(x["thinking"]["tokens"]), tools, round_suffix(x) if round_suffix else ""))
    return "\n".join(out)


def write_report(report, fmt, out, render):
    text = render(report) if fmt == "tree" else json.dumps(report, indent=2, ensure_ascii=False)
    if out and out != "-":
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print("wrote %s  (sessions=%d, credits=%s)" % (out, report["totals"]["sessions"],
                                                      fmt_credits(report["totals"])), file=sys.stderr)
    else:
        print(text)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def load_config(path, explicit, defaults, path_keys=()):
    """Read a config.json; keys missing from it fall back to `defaults`.
    Values of `path_keys` (a path or list of paths) resolve against the config file's directory."""
    config = dict(defaults)
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
    unknown = sorted(set(data) - set(defaults))
    if unknown:
        raise SystemExit("unknown key(s) in %s: %s (allowed: %s)"
                         % (path, ", ".join(unknown), ", ".join(defaults)))
    base = os.path.dirname(os.path.abspath(path))

    def resolve(p):
        p = os.path.expanduser(os.path.expandvars(p))
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))

    for key in path_keys:
        value = data.get(key)
        if isinstance(value, list):
            data[key] = [resolve(v) for v in value]
        elif value:
            data[key] = resolve(value)
    config.update(data)
    return config


def add_common_args(ap):
    """CLI options shared by all collectors (each defaults to None = take from config)."""
    ap.add_argument("--config", help="Config file (default: config.json next to the collector).")
    ap.add_argument("--format", choices=("json", "tree"))
    ap.add_argument("--out", help="Write report to this file; '-' forces stdout.")
    ap.add_argument("--include-empty", dest="include_empty", action="store_const", const=True,
                    help="Keep sessions that have no requests.")
    ap.add_argument("--no-include-empty", dest="include_empty", action="store_const", const=False)
    ap.add_argument("--ticket-prefix", help="Jira project key used to detect tickets (built-in default: LEGO).")
