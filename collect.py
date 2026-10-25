#!/usr/bin/env python3
"""Master collector: run every Copilot usage collector and combine them into one final report.

Runs the VS Code, Copilot CLI and IntelliJ collectors in-process with the options from ONE
configuration file (collect.config.json next to this script), optionally writes each
collector's own report, then merges them with collectors/combine into the final report.
The per-collector config.json files are not read; this file is the single source of options.

Usage:
    python3 collect.py                          # run with collect.config.json
    python3 collect.py --format tree            # final report as a readable tree (stdout unless --out)
    python3 collect.py --out final.json         # final report path (overrides combinedReport)
    python3 collect.py --only vscode,copilotCli # run a subset of collectors
    python3 collect.py --no-source-reports      # don't write the per-collector reports
    python3 collect.py --config other.json      # another master config

Precedence: command-line option > collect.config.json > built-in defaults.

Standard library only; works on Python 3.8+.
"""
import argparse
import importlib.util
import json
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
COLLECTORS_DIR = os.path.join(ROOT, "collectors")
sys.path.insert(0, COLLECTORS_DIR)
from common import DEFAULT_TICKET_PREFIX, now_iso, vscode_user_dirs, write_report  # noqa: E402

MASTER_VERSION = "1.0.0"
DEFAULT_CONFIG_PATH = os.path.join(ROOT, "collect.config.json")

# Built-in defaults; collect.config.json overrides any of them. Keys are validated: a typo is an error.
BUILTIN_DEFAULTS = {
    "ticketPrefix": DEFAULT_TICKET_PREFIX,   # shared by all collectors
    "includeEmpty": False,                   # keep sessions without requests
    "outputDir": "reports",                  # base for relative report paths
    "writeSourceReports": True,              # also write each collector's own report
    "combinedReport": "combined.json",       # final report (relative to outputDir); null = stdout
    "format": "json",                        # json | tree (final report)
    "collectors": {
        "vscode": {
            "enabled": True,
            "userDirs": [],                  # [] = auto-detect Code / Code - Insiders
            "report": "vscode-copilot.json",
        },
        "copilotCli": {
            "enabled": True,
            "copilotHomes": [],              # [] = $COPILOT_HOME or ~/.copilot
            "excludeClients": ["JetBrains*", "copilot-intellij*"],
            "vscodeUserDirs": [],            # [] = auto-detect; agent-host.db marks VS Code requests
            "report": "copilot-cli.json",
        },
        "intellij": {
            "enabled": True,
            "ideDirs": [],                   # [] = every JetBrains config dir with aia-task-history
            "copilotHomes": [],              # [] = $COPILOT_HOME or ~/.copilot
            "pluginDirs": [],                # [] = ~/.config/github-copilot
            "pluginClients": ["copilot-intellij*"],
            "report": "intellij.json",
        },
    },
}
PATH_LIST_KEYS = ("userDirs", "copilotHomes", "vscodeUserDirs", "ideDirs", "pluginDirs")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def _merge(defaults, data, where):
    """Deep-merge `data` over `defaults`, rejecting unknown keys and wrong value types."""
    unknown = sorted(k for k in data if not k.startswith("_") and k not in defaults)
    if unknown:
        raise SystemExit("unknown key(s) in %s: %s (allowed: %s)" % (where, ", ".join(unknown), ", ".join(defaults)))
    merged = dict(defaults)
    for key, value in data.items():
        if key.startswith("_"):
            continue
        default = defaults[key]
        if isinstance(default, dict):
            if not isinstance(value, dict):
                raise SystemExit("%s.%s must be an object" % (where, key))
            merged[key] = _merge(default, value, "%s.%s" % (where, key))
        elif isinstance(default, list) and not isinstance(value, list):
            raise SystemExit("%s.%s must be a list" % (where, key))
        elif isinstance(default, bool) and not isinstance(value, bool):
            raise SystemExit("%s.%s must be true or false" % (where, key))
        else:
            merged[key] = value
    return merged


def load_master_config(path, explicit):
    data = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            try:
                data = json.load(f)
            except ValueError as exc:
                raise SystemExit("invalid JSON in %s: %s" % (path, exc))
    elif explicit:
        raise SystemExit("config file not found: %s" % path)
    config = _merge(BUILTIN_DEFAULTS, data, os.path.basename(path))
    if config["format"] not in ("json", "tree"):
        raise SystemExit("format must be json or tree, got %r" % config["format"])

    base = os.path.dirname(os.path.abspath(path))

    def resolve(p, against=base):
        p = os.path.expanduser(os.path.expandvars(p))
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(against, p))

    config["outputDir"] = resolve(config["outputDir"])
    for opts in config["collectors"].values():
        for key in PATH_LIST_KEYS:
            if key in opts:
                opts[key] = [resolve(p) for p in opts[key]]
        if opts.get("report"):
            opts["report"] = resolve(opts["report"], config["outputDir"])
    if config["combinedReport"]:
        config["combinedReport"] = resolve(config["combinedReport"], config["outputDir"])
    config["_configFile"] = path if os.path.isfile(path) else None
    return config


# --------------------------------------------------------------------------- #
# Running collectors
# --------------------------------------------------------------------------- #

def _load(name, relpath):
    # Every collector script is called collect.py, so load each under its own module name.
    spec = importlib.util.spec_from_file_location(name, os.path.join(COLLECTORS_DIR, relpath))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_vscode(opts, shared):
    mod = _load("vscode_collect", "vscode/collect.py")
    dirs = opts["userDirs"] or vscode_user_dirs()
    if not dirs:
        return None, "no VS Code User directory found"
    return mod.collect(dirs, **shared), None


def run_copilot_cli(opts, shared):
    mod = _load("copilot_cli_collect_master", "copilot_cli/collect.py")
    homes = opts["copilotHomes"] or mod.default_copilot_homes()
    if not homes:
        return None, "no Copilot CLI home found"
    return mod.collect(homes, exclude_clients=[c for c in opts["excludeClients"] if c],
                       vscode_dirs=opts["vscodeUserDirs"] or vscode_user_dirs(), **shared), None


def run_intellij(opts, shared):
    mod = _load("intellij_collect", "intellij/collect.py")
    ide_dirs = opts["ideDirs"] or mod.default_ide_dirs()
    homes = opts["copilotHomes"] or mod.cli.default_copilot_homes()
    if not ide_dirs and not homes:
        return None, "no JetBrains config dir or Copilot home found"
    return mod.collect(ide_dirs, homes, plugin_dirs=opts["pluginDirs"] or mod.default_plugin_dirs(),
                       plugin_clients=opts["pluginClients"], **shared), None


RUNNERS = {"vscode": run_vscode, "copilotCli": run_copilot_cli, "intellij": run_intellij}


def run_all(config, only, write_sources):
    shared = {"include_empty": config["includeEmpty"], "ticket_prefix": config["ticketPrefix"]}
    reports, runs = [], []
    for name, runner in RUNNERS.items():
        opts = config["collectors"][name]
        run = {"collector": name, "enabled": opts["enabled"] and (not only or name in only),
               "status": None, "sessions": None, "copilotCredits": None, "report": None, "error": None}
        runs.append(run)
        if not run["enabled"]:
            run["status"] = "disabled"
            continue
        try:
            report, skip_reason = runner(opts, shared)
        except Exception as exc:  # one broken collector must not stop the others
            run.update(status="failed", error="%s: %s" % (type(exc).__name__, exc))
            print("collector %s failed:\n%s" % (name, traceback.format_exc()), file=sys.stderr)
            continue
        if report is None:
            run.update(status="skipped", error=skip_reason)
            continue
        run.update(status="ok", sessions=report["totals"]["sessions"],
                   copilotCredits=report["totals"]["copilotCredits"])
        label = "collector:%s" % name
        if write_sources and opts.get("report"):
            write_report(report, "json", opts["report"], None)
            run["report"] = label = opts["report"]
        reports.append((label, report))
    return reports, runs


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Run all Copilot usage collectors and combine them into one final report.",
        epilog="Options not given on the command line come from collect.config.json, then built-in defaults.")
    ap.add_argument("--config", help="Master config file (default: collect.config.json next to this script).")
    ap.add_argument("--format", choices=("json", "tree"), help="Final report format.")
    ap.add_argument("--out", help="Final report path ('-' = stdout). Overrides combinedReport.")
    ap.add_argument("--only", help="Comma-separated subset to run: %s." % ", ".join(RUNNERS))
    ap.add_argument("--source-reports", dest="source_reports", action="store_const", const=True,
                    help="Write each collector's own report (writeSourceReports).")
    ap.add_argument("--no-source-reports", dest="source_reports", action="store_const", const=False)
    args = ap.parse_args(argv)

    config = load_master_config(args.config or DEFAULT_CONFIG_PATH, bool(args.config))
    only = None
    if args.only:
        only = {x.strip() for x in args.only.split(",") if x.strip()}
        bad = sorted(only - set(RUNNERS))
        if bad:
            ap.error("unknown collector(s) in --only: %s (choose from %s)" % (", ".join(bad), ", ".join(RUNNERS)))
    write_sources = config["writeSourceReports"] if args.source_reports is None else args.source_reports

    reports, runs = run_all(config, only, write_sources)
    if not reports:
        ap.error("no collector produced a report: %s"
                 % "; ".join("%s=%s%s" % (r["collector"], r["status"], " (%s)" % r["error"] if r["error"] else "")
                             for r in runs))

    combine = _load("combine_master", "combine/combine.py")
    final = combine.combine(reports, [])
    final["collector"] = "master"
    final["collectorVersion"] = MASTER_VERSION
    final["generatedAt"] = now_iso()
    final["run"] = {"configFile": config["_configFile"], "ticketPrefix": config["ticketPrefix"].upper(),
                    "includeEmpty": config["includeEmpty"], "collectors": runs}

    fmt = args.format or config["format"]
    out = args.out if args.out is not None else config["combinedReport"]

    def render(report):
        lines = combine.render(report).split("\n")
        status = ["├── run %s: %s%s%s" % (r["collector"], r["status"],
                                         "  sessions=%s credits=%s" % (r["sessions"], r["copilotCredits"])
                                         if r["status"] == "ok" else "",
                                         "  (%s)" % r["error"] if r["error"] else "")
                  for r in report["run"]["collectors"]]
        return "\n".join(lines[:1] + status + lines[1:])

    write_report(final, fmt, out, render)
    failed = [r for r in runs if r["status"] == "failed"]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
