#!/usr/bin/env python3
"""Consolidate team members' combined.json reports into one team ai-usage file.

Each member runs collect.py on their machine and hands in reports/combined.json. This script
merges all of them into ai-usage.json for the team dashboard: totals plus breakdowns by member,
Jira ticket, project, surface, model and day, and one slim record per session.

By default only usage data reaches the team file (no prompts, answers, titles or local file
paths); set "content" in consolidate.config.json to include more.

Usage:
    python3 consolidate.py                         # read inputDir (team-reports/*.json) -> ai-usage.json
    python3 consolidate.py --input a.json --input b.json
    python3 consolidate.py --input-dir /shared/lego-reports --out /shared/ai-usage.json
    python3 consolidate.py --format tree           # readable summary on stdout
    python3 consolidate.py --config other.json

Precedence: command-line option > consolidate.config.json > built-in defaults.

Standard library only; works on Python 3.8+.
"""
import argparse
import datetime
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "collectors"))
from common import epoch_ms, fmt_credits, fmt_num, iso, load_config, now_iso, write_report  # noqa: E402

SCHEMA = "ai-usage"
SCHEMA_VERSION = "1.0.0"
DEFAULT_CONFIG_PATH = os.path.join(ROOT, "consolidate.config.json")
CONTENT_LEVELS = ("none", "titles", "full")
BUILTIN_DEFAULTS = {
    "team": "LEGO",
    "inputDir": "team-reports",      # folder with members' combined.json files
    "pattern": "*.json",             # file pattern inside inputDir (searched recursively)
    "inputs": [],                    # explicit files; used instead of inputDir when non-empty
    "output": "ai-usage.json",       # null = stdout
    "format": "json",                # json | tree
    "content": "none",               # none | titles | full (prompts and answers)
}
CREDIT_KEYS = ("copilotCredits", "copilotCreditsRounded", "copilotCreditsRoundedUp")


# --------------------------------------------------------------------------- #
# Reading member reports
# --------------------------------------------------------------------------- #

def find_inputs(config, cli_inputs, cli_dir):
    if cli_inputs:
        return cli_inputs
    if config["inputs"] and not cli_dir:
        return config["inputs"]
    folder = cli_dir or config["inputDir"]
    return sorted(glob.glob(os.path.join(folder, "**", config["pattern"]), recursive=True))


def member_from_filename(path):
    # alice.json, alice.combined.json, alice-combined.json -> "alice"
    stem = os.path.basename(path).rsplit(".json", 1)[0]
    for suffix in (".combined", "-combined", "_combined"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    return stem


def load_member_reports(paths):
    reports, errors = [], []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                report = json.load(f)
        except (OSError, ValueError) as exc:
            errors.append({"file": path, "error": "%s: %s" % (type(exc).__name__, exc)})
            continue
        if report.get("schema") == SCHEMA:
            errors.append({"file": path, "error": "is an ai-usage file, not a member report (skipped)"})
            continue
        if report.get("collector") not in ("master", "combined") or "sessions" not in report:
            errors.append({"file": path, "error": "not a combined.json from collect.py (collector=%r)"
                                                  % report.get("collector")})
            continue
        member = dict(report.get("member") or {})
        if not member.get("id"):
            member = {"id": member_from_filename(path), "name": None, "idSource": "filename"}
        reports.append((path, report, member))
    return reports, errors


# --------------------------------------------------------------------------- #
# Slim records
# --------------------------------------------------------------------------- #

def project_of(session):
    ws = (session.get("_source") or {}).get("workspace") or {}
    if ws.get("repository"):
        return ws["repository"]
    folder = ws.get("folder")
    return os.path.basename(folder.rstrip("/\\")) if folder else "(unknown)"


def slim_request(r, session_surface, content):
    rounds = ((r.get("result") or {}).get("metadata") or {}).get("toolCallRounds") or []
    sub = [x for x in rounds if x.get("subAgent")]
    rec = {
        "requestId": r.get("requestId"),
        "timestamp": r.get("timestamp"),
        "surface": r.get("surface") or session_surface,
        "model": ((r.get("result") or {}).get("metadata") or {}).get("resolvedModel") or r.get("modelId"),
        **{k: r.get(k) for k in CREDIT_KEYS},
        "promptTokens": r.get("promptTokens"),
        "completionTokens": r.get("completionTokens"),
        "premiumRequestCost": r.get("premiumRequestCost"),
        "toolCallRounds": len(rounds),
        "subAgentRounds": len(sub),
        "subAgentCredits": round(sum(x.get("copilotCredits") or 0 for x in sub), 6) if sub else 0,
    }
    if content == "full":
        rec["message"] = (r.get("message") or {}).get("text")
        rec["response"] = [p.get("text") for p in r.get("response") or [] if p.get("kind") == "markdown"]
    return rec


def slim_session(s, member, report_path, report_time, content):
    ws = (s.get("_source") or {}).get("workspace") or {}
    surface = s.get("surface") or (s.get("_source") or {}).get("collector")
    requests = [slim_request(r, surface, content) for r in s.get("requests") or []]
    rec = {
        "member": member["id"],
        "sessionId": s.get("sessionId"),
        "acpSessionId": s.get("acpSessionId"),
        "surface": surface,
        "collector": (s.get("_source") or {}).get("collector"),
        "ticketKey": s.get("ticketKey"),
        "ticketFrom": ((s.get("_source") or {}).get("ticket") or {}).get("field"),
        "project": project_of(s),
        "repository": ws.get("repository"),
        "branch": ws.get("branch"),
        "creationDate": s.get("creationDate"),
        "lastActivity": max([r["timestamp"] for r in requests if r["timestamp"]] or [s.get("creationDate")]),
        "requests": requests,
        "reportFile": os.path.basename(report_path),
        "reportGeneratedAt": report_time,
    }
    if content in ("titles", "full"):
        rec["title"] = s.get("customTitle")
    return rec


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #

def new_bucket():
    return {"sessions": 0, "requests": 0, "billedRequests": 0, "premiumRequests": 0.0,
            "copilotCredits": 0.0, "copilotCreditsRounded": 0.0, "copilotCreditsRoundedUp": 0.0,
            "promptTokens": 0, "completionTokens": 0}


def add_request(b, r):
    b["requests"] += 1
    b["billedRequests"] += 1 if r["copilotCredits"] is not None else 0
    b["premiumRequests"] = round(b["premiumRequests"] + (r["premiumRequestCost"] or 0), 3)
    b["copilotCredits"] = round(b["copilotCredits"] + (r["copilotCredits"] or 0), 6)
    b["copilotCreditsRounded"] = round(b["copilotCreditsRounded"] + (r["copilotCreditsRounded"] or 0), 1)
    b["copilotCreditsRoundedUp"] = round(b["copilotCreditsRoundedUp"] + (r["copilotCreditsRoundedUp"] or 0), 1)
    b["promptTokens"] += r["promptTokens"] or 0
    b["completionTokens"] += r["completionTokens"] or 0


def day_of(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).strftime("%Y-%m-%d") if ms else "(unknown)"


def aggregate(sessions):
    totals, by_member, by_ticket, by_project, by_surface, by_model, by_day = new_bucket(), {}, {}, {}, {}, {}, {}

    def bucket(table, key, extra=None):
        if key not in table:
            table[key] = new_bucket()
            table[key].update(extra or {})
            table[key]["_sessions"] = set()
        return table[key]

    for s in sessions:
        for r in s["requests"]:
            targets = [totals,
                       bucket(by_member, s["member"]),
                       bucket(by_ticket, s["ticketKey"] or "(no ticket)", {"members": {}}),
                       bucket(by_project, s["project"]),
                       bucket(by_surface, r["surface"] or "(unknown)"),
                       bucket(by_model, r["model"] or "(unknown)"),
                       bucket(by_day, day_of(r["timestamp"]), {"members": {}})]
            for b in targets:
                add_request(b, r)
                if b is not totals:
                    b["_sessions"].add((s["member"], s["sessionId"]))
            for b in (by_ticket[s["ticketKey"] or "(no ticket)"], by_day[day_of(r["timestamp"])]):
                b["members"][s["member"]] = round(b["members"].get(s["member"], 0) + (r["copilotCredits"] or 0), 6)
    for table in (by_member, by_ticket, by_project, by_surface, by_model, by_day):
        for b in table.values():
            b["sessions"] = len(b.pop("_sessions"))
    totals["sessions"] = len(sessions)
    totals["members"] = len({s["member"] for s in sessions})
    return totals, by_member, by_ticket, by_project, by_surface, by_model, dict(sorted(by_day.items()))


def consolidate(reports, load_errors, team, content):
    inputs, candidates, member_info = [], [], {}
    for path, report, member in reports:
        generated = epoch_ms(report.get("generatedAt"))
        inputs.append({"file": os.path.basename(path), "member": member["id"], "memberIdSource": member.get("idSource"),
                       "machine": member.get("machine"), "generatedAt": report.get("generatedAt"),
                       "sessions": len(report.get("sessions") or []),
                       "copilotCredits": (report.get("totals") or {}).get("copilotCredits"),
                       "collectors": [{"collector": c.get("collector"), "status": c.get("status")}
                                      for c in (report.get("run") or {}).get("collectors") or []]})
        info = member_info.setdefault(member["id"], {"name": None, "email": None, "machines": set(),
                                                     "latestReportAt": None, "quota": None, "_quotaAt": 0})
        info["name"] = info["name"] or member.get("name")
        info["email"] = info["email"] or member.get("email")
        if member.get("machine"):
            info["machines"].add(member["machine"])
        if generated and (info["latestReportAt"] or 0) < generated:
            info["latestReportAt"] = generated
        quota = (report.get("account") or {}).get("quota")
        if quota and (quota.get("asOf") or 0) > info["_quotaAt"]:
            info["quota"], info["_quotaAt"] = quota, quota.get("asOf") or 0
        for s in report.get("sessions") or []:
            candidates.append((generated or 0, path, member, s))

    # A member may hand in several snapshots: keep each session from their newest report.
    kept, superseded = {}, 0
    for generated, path, member, s in sorted(candidates, key=lambda c: c[0], reverse=True):
        key = (member["id"], s.get("acpSessionId") or s.get("sessionId"))
        if key in kept:
            superseded += 1
            continue
        kept[key] = slim_session(s, member, path, generated, content)
    sessions = sorted(kept.values(), key=lambda s: (s["creationDate"] or 0, s["member"]))

    totals, by_member, by_ticket, by_project, by_surface, by_model, by_day = aggregate(sessions)
    for member_id, info in member_info.items():
        b = by_member.setdefault(member_id, new_bucket())
        b.pop("_sessions", None)
        b.setdefault("sessions", 0)
        member_sessions = [s for s in sessions if s["member"] == member_id]
        b.update({"name": info["name"], "email": info["email"], "machines": sorted(info["machines"]),
                  "firstActivity": min([s["creationDate"] for s in member_sessions if s["creationDate"]] or [None]),
                  "lastActivity": max([s["lastActivity"] for s in member_sessions if s["lastActivity"]] or [None]),
                  "latestReportAt": info["latestReportAt"], "accountQuota": info["quota"]})
    times = [r["timestamp"] for s in sessions for r in s["requests"] if r["timestamp"]]
    return {
        "schema": SCHEMA,
        "schemaVersion": SCHEMA_VERSION,
        "team": team,
        "generatedAt": now_iso(),
        "content": content,
        "period": {"from": min(times) if times else None, "to": max(times) if times else None},
        "inputs": inputs,
        "totals": dict(totals, supersededSessions=superseded),
        "byMember": dict(sorted(by_member.items())),
        "byTicket": by_ticket,
        "byProject": by_project,
        "bySurface": by_surface,
        "byModel": by_model,
        "byDay": by_day,
        "sessions": sessions,
        "errors": load_errors,
    }


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

def render(report):
    t = report["totals"]
    out = ["ai-usage  team=%s  (%s v%s, %s, content=%s)" % (report["team"], report["schema"], report["schemaVersion"],
                                                            report["generatedAt"], report["content"]),
           "├── period %s → %s" % (iso(report["period"]["from"]), iso(report["period"]["to"])),
           "├── totals  members=%d sessions=%d requests=%d credits=%s premiumRequests=%s superseded=%d"
           % (t["members"], t["sessions"], t["requests"], fmt_credits(t), t["premiumRequests"], t["supersededSessions"])]
    for i in report["inputs"]:
        out.append("├── input %s  member=%s (id from %s)  machine=%s  generatedAt=%s  sessions=%d"
                   % (i["file"], i["member"], i["memberIdSource"], i["machine"], i["generatedAt"], i["sessions"]))
    for e in report["errors"]:
        out.append("├── ERROR %s  %s" % (e["file"], e["error"]))
    sections = [("member", "byMember"), ("ticket", "byTicket"), ("project", "byProject"),
                ("surface", "bySurface"), ("model", "byModel"), ("day", "byDay")]
    for si, (label, key) in enumerate(sections):
        last = si == len(sections) - 1
        out.append("%s %s" % ("└──" if last else "├──", key))
        items = list(report[key].items())
        for ii, (name, b) in enumerate(items):
            pre = ("    " if last else "│   ") + ("└── " if ii == len(items) - 1 else "├── ")
            extra = ""
            if label == "member":
                q = ((b.get("accountQuota") or {}).get("quotas") or {}).get("chat") or {}
                extra = "  last=%s%s" % (iso(b.get("lastActivity")),
                                         "  quota≈%s/%s" % (q.get("usedEstimate"), q.get("entitlementRequests")) if q else "")
            elif "members" in b:
                extra = "  " + ", ".join("%s=%.4f" % kv for kv in sorted(b["members"].items()))
            out.append("%s%s  sessions=%d requests=%d credits=%s tokens=%s/%s%s"
                       % (pre, name, b["sessions"], b["requests"], fmt_credits(b),
                          fmt_num(b["promptTokens"]), fmt_num(b["completionTokens"]), extra))
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Consolidate team members' combined.json reports into one ai-usage file.",
        epilog="Options not given on the command line come from consolidate.config.json, then built-in defaults.")
    ap.add_argument("--config", help="Config file (default: consolidate.config.json next to this script).")
    ap.add_argument("--input", action="append", help="Member report (repeatable); replaces inputDir/inputs.")
    ap.add_argument("--input-dir", help="Folder of member reports (searched recursively for `pattern`).")
    ap.add_argument("--out", help="ai-usage file to write; '-' = stdout.")
    ap.add_argument("--format", choices=("json", "tree"))
    ap.add_argument("--content", choices=CONTENT_LEVELS, help="What text to keep: none | titles | full.")
    ap.add_argument("--team", help="Team name stored in the file.")
    args = ap.parse_args(argv)

    config = load_config(args.config or DEFAULT_CONFIG_PATH, bool(args.config), BUILTIN_DEFAULTS,
                         path_keys=("inputDir", "inputs", "output"))
    content = args.content or config["content"]
    if content not in CONTENT_LEVELS:
        ap.error("content must be one of %s, got %r" % (", ".join(CONTENT_LEVELS), content))
    fmt = args.format or config["format"]
    if fmt not in ("json", "tree"):
        ap.error("format must be json or tree, got %r" % fmt)
    out = args.out if args.out is not None else config["output"]

    paths = find_inputs(config, args.input, args.input_dir)
    out_abs = os.path.abspath(out) if out and out != "-" else None
    paths = [p for p in paths if os.path.abspath(p) != out_abs]   # never read our own output
    reports, errors = load_member_reports(paths)
    if not reports:
        ap.error("no member reports loaded from %s%s" % (
            args.input_dir or config["inputDir"] if not (args.input or config["inputs"]) else "inputs",
            ": " + "; ".join("%s (%s)" % (e["file"], e["error"]) for e in errors) if errors else ""))
    write_report(consolidate(reports, errors, args.team or config["team"], content), fmt, out, render)


if __name__ == "__main__":
    main()
