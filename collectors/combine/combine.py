#!/usr/bin/env python3
"""Combine the VS Code, Copilot CLI and IntelliJ collector reports into one report.

All collectors emit the same session schema, so the combined report is the same shape:
totals / byTicket / byWorkspace / byModel over every session, plus bySource and bySurface,
the latest account quota, and each session tagged with the collector it came from.
Sessions reported by more than one collector (e.g. an IntelliJ chat that the CLI collector
also picked up with --exclude-client '') are counted once. See README.md.

Usage:
    python3 combine.py                        # read the three reports, JSON to stdout
    python3 combine.py --format tree          # human-readable tree
    python3 combine.py --out combined.json    # write to file
    python3 combine.py --run                  # run the collectors first instead of reading report files
    python3 combine.py --input a.json --input b.json   # explicit report files (repeatable)
    python3 combine.py --config team.json     # other config file (default: ./config.json)

Precedence: command-line option > config file > built-in defaults.

Standard library only; works on Python 3.8+.
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTORS_DIR = os.path.dirname(HERE)
sys.path.insert(0, COLLECTORS_DIR)
from common import (add_to_bucket, fmt_credits, fmt_num, load_config, now_iso, render_tree,  # noqa: E402
                    summarise, write_report)

COLLECTOR = "combined"
COLLECTOR_VERSION = "1.0.0"
DEFAULT_CONFIG_PATH = os.path.join(HERE, "config.json")
REPORTS_DIR = os.path.join(os.path.dirname(COLLECTORS_DIR), "reports")
# collector name -> (script, default report file)
COLLECTORS = {
    "vscode-copilot-chat": ("vscode/collect.py", "vscode-copilot.json"),
    "copilot-cli": ("copilot_cli/collect.py", "copilot-cli.json"),
    "intellij": ("intellij/collect.py", "intellij.json"),
}
# When the same session appears in several reports, keep the copy from the richest source.
SOURCE_PRIORITY = {"intellij": 0, "vscode-copilot-chat": 1, "copilot-cli": 2}
BUILTIN_DEFAULTS = {
    "inputs": [os.path.join(REPORTS_DIR, f) for _, f in COLLECTORS.values()],
    "run": False,            # True -> run each collector (with its own config) instead of reading inputs
    "format": "json",        # json | tree
    "out": None,             # None -> stdout
}


# --------------------------------------------------------------------------- #
# Loading reports
# --------------------------------------------------------------------------- #

def load_reports(paths):
    reports, errors = [], []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                report = json.load(f)
        except (OSError, ValueError) as exc:
            errors.append({"file": path, "error": "%s: %s" % (type(exc).__name__, exc)})
            continue
        if "sessions" not in report or "collector" not in report:
            errors.append({"file": path, "error": "not a collector report (missing collector/sessions)"})
            continue
        reports.append((path, report))
    return reports, errors


def run_collectors():
    """Run every collector with its own config and parse its JSON from stdout."""
    reports, errors = [], []
    for name, (script, _) in COLLECTORS.items():
        path = os.path.join(COLLECTORS_DIR, script)
        proc = subprocess.run([sys.executable, path, "--format", "json", "--out", "-"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if proc.returncode != 0:
            errors.append({"file": path, "error": "exit %d: %s" % (proc.returncode, proc.stderr.strip()[-500:])})
            continue
        try:
            reports.append(("run:" + script, json.loads(proc.stdout)))
        except ValueError as exc:
            errors.append({"file": path, "error": "invalid JSON output: %s" % exc})
    return reports, errors


# --------------------------------------------------------------------------- #
# Combining
# --------------------------------------------------------------------------- #

def surface_of(collector, session):
    if collector == "intellij":
        return "intellij/" + (session.get("integration") or "unknown")
    return {"vscode-copilot-chat": "vscode", "copilot-cli": "copilot-cli"}.get(collector, collector)


def dedupe_key(session):
    # IntelliJ AI Assistant sessions are keyed by IDE task id but carry the CLI session id
    # as acpSessionId; plugin and CLI sessions use the CLI session id directly.
    return session.get("acpSessionId") or session.get("sessionId") or id(session)


def combine(reports, load_errors):
    sources, candidates = [], []
    for path, report in reports:
        collector = report.get("collector")
        sources.append({"collector": collector, "collectorVersion": report.get("collectorVersion"),
                        "generatedAt": report.get("generatedAt"), "file": path,
                        "sessions": len(report.get("sessions") or []), "totals": report.get("totals")})
        for s in report.get("sessions") or []:
            candidates.append((SOURCE_PRIORITY.get(collector, 9), collector, s))

    kept, duplicates = {}, []
    for priority, collector, s in sorted(candidates, key=lambda c: c[0]):
        key = dedupe_key(s)
        if key in kept:
            duplicates.append({"sessionId": s.get("sessionId"), "droppedFrom": collector,
                               "keptFrom": kept[key]["_source"]["collector"]})
            continue
        s = dict(s, surface=surface_of(collector, s))
        s["_source"] = dict(s.get("_source") or {}, collector=collector)
        kept[key] = s
    sessions = sorted(kept.values(), key=lambda s: s.get("creationDate") or 0)
    by_source, by_surface = {}, {}
    for s in sessions:
        add_to_bucket(by_source, s["_source"]["collector"], s)
        add_to_bucket(by_surface, s["surface"], s)

    combined = {
        "collector": COLLECTOR,
        "collectorVersion": COLLECTOR_VERSION,
        "generatedAt": now_iso(),
        "sources": sources,
        **summarise(sessions, lambda s: s["_source"].get("workspace", {}).get("folder") or "(unknown)"),
        "bySource": by_source,
        "bySurface": by_surface,
        "account": {"quota": latest_quota(r for _, r in reports)},
        "duplicatesDropped": duplicates,
        "sessions": sessions,
        "errors": load_errors + [dict(e, collector=r.get("collector")) for _, r in reports for e in r.get("errors") or []],
    }
    totals = combined["totals"]
    totals["premiumRequests"] = sum(r.get("premiumRequestCost") or 0 for s in sessions for r in s["requests"])
    totals["backgroundCredits"] = round(sum(c.get("copilotCredits") or 0 for s in sessions
                                            for c in s.get("backgroundCalls") or []), 6)
    totals["sessionsWithoutUsage"] = sum(1 for s in sessions
                                         if s["_source"]["collector"] == "intellij" and not s.get("usageSource"))
    return combined


def latest_quota(reports):
    quotas = [(r.get("account") or {}).get("quota") for r in reports]
    quotas = [q for q in quotas if q and q.get("asOf")]
    return max(quotas, key=lambda q: q["asOf"]) if quotas else None


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

def render(report):
    header = []
    for src in report["sources"]:
        header.append("├── source %s v%s  sessions=%d  generatedAt=%s  (%s)"
                      % (src["collector"], src["collectorVersion"], src["sessions"], src["generatedAt"], src["file"]))
    for name, b in report["bySurface"].items():
        header.append("├── surface %s  sessions=%d requests=%d credits=%s"
                      % (name, b["sessions"], b["requests"], fmt_credits(b)))
    quota = (report["account"] or {}).get("quota")
    for name, q in ((quota or {}).get("quotas") or {}).items():
        if q.get("entitlementRequests"):
            header.append("├── account quota %s  used≈%s of %s  (resets %s)"
                          % (name, q.get("usedEstimate"), q.get("entitlementRequests"), q.get("resetDate")))
    t = report["totals"]
    header.append("├── premiumRequests=%s  backgroundCredits=%s  sessionsWithoutUsage=%s"
                  % (t["premiumRequests"], fmt_num(t["backgroundCredits"]), t["sessionsWithoutUsage"]))
    for d in report["duplicatesDropped"]:
        header.append("├── duplicate %s  dropped from %s, kept from %s" % (d["sessionId"], d["droppedFrom"], d["keptFrom"]))

    def session_lines(s):
        return ["├── surface=%s  collector=%s" % (s["surface"], s["_source"]["collector"])]

    def round_suffix(x):
        if x.get("copilotCredits") is None:
            return ""
        sub = "  [sub-agent: %s]" % x["subAgent"]["name"] if x.get("subAgent") else ""
        return "%s  credits=%.6f" % (sub, x["copilotCredits"])

    return render_tree(report, header_lines=header, session_lines=session_lines, round_suffix=round_suffix)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Combine the VS Code, Copilot CLI and IntelliJ collector reports into one report.",
        epilog="Options not given on the command line come from the config file, then built-in defaults.")
    ap.add_argument("--config", help="Config file (default: config.json next to this script).")
    ap.add_argument("--input", action="append", help="Collector report JSON (repeatable; replaces the config list).")
    ap.add_argument("--run", dest="run", action="store_const", const=True,
                    help="Run the collectors (each with its own config) instead of reading report files.")
    ap.add_argument("--no-run", dest="run", action="store_const", const=False)
    ap.add_argument("--format", choices=("json", "tree"))
    ap.add_argument("--out", help="Write the combined report to this file; '-' forces stdout.")
    args = ap.parse_args(argv)

    config = load_config(args.config or DEFAULT_CONFIG_PATH, bool(args.config), BUILTIN_DEFAULTS,
                         path_keys=("inputs", "out"))

    def opt(cli_value, key):
        return config[key] if cli_value is None else cli_value

    fmt = opt(args.format, "format")
    if fmt not in ("json", "tree"):
        ap.error("format must be json or tree, got %r" % fmt)
    if opt(args.run, "run"):
        reports, errors = run_collectors()
    else:
        reports, errors = load_reports(args.input or config["inputs"])
    if not reports:
        ap.error("no reports loaded: %s" % "; ".join("%s (%s)" % (e["file"], e["error"]) for e in errors))
    write_report(combine(reports, errors), fmt, opt(args.out, "out"), render)


if __name__ == "__main__":
    main()
