#!/usr/bin/env python3
"""
hook-detect.py — Scan for AI-specific persistence hooks (IOC detection).

Detects .claude/settings.json SessionStart hooks and .vscode/tasks.json
folderOpen tasks planted by supply chain attacks (e.g., PyTorch Lightning
Shai-Hulud variant, April 2026), and changes to .claude/CLAUDE.md, which
carries the operator's dispatch authority (2026-09-29).

Designed to run weekly alongside pip-audit-scan. Separate scanner, shared
output (news_store).

Usage:
  python3 hook-detect.py                    # scan default paths
  python3 hook-detect.py --scan-dirs /home  # scan specific directory tree
  python3 hook-detect.py --dry-run          # show findings, don't store

Install:
  cp hook-detect.{service,timer} /etc/systemd/system/
  systemctl daemon-reload && systemctl enable --now hook-detect.timer
"""

import argparse
import errno
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Sibling modules — baseline gate and content-severity classifier.
# See hook_baselines.yaml + hook_analyzer.py headers for the design.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from hook_analyzer import evaluate_claude_settings, evaluate_claude_md
import hook_baselines

# Default directories to scan
DEFAULT_SCAN_DIRS = [
    "/home",
    "/root",
    "/opt",
]

# IOC patterns: file to find, directory it should be in, how to evaluate.
# Each pattern has one of:
#   "baseline_gate": True  + "analyzer": callable
#         Primary gate is baseline-hash match. If match → silent. If
#         mismatch/missing → alert; analyzer runs on the content to
#         annotate severity (high if it trips escalation rules, medium
#         if the content is analyzer-clean but the file changed anyway).
#   "grep": list[str]
#         Substring tokens; any hit alerts. Used for surfaces we haven't
#         structurally modeled or baselined yet (.vscode, .cursor).
#
# .claude/settings.json is baseline-gated because Bird Portal telemetry
# lives there and content-rule-only matching gave us 24 identical FPs
# in six weeks (the 2026-08-16 review; see hook_analyzer.py header).
IOC_PATTERNS = [
    {
        "file": "settings.json",
        "path_contains": ".claude",
        "baseline_gate": True,
        "analyzer": evaluate_claude_settings,
        "description": "Claude settings.json changed outside its approved baseline",
    },
    # ~/.claude/CLAUDE.md is the user's standing instructions to every session, and
    # the flock's copy holds the operator's Coterie dispatch block (who may direct the
    # agent). Raw-hash baseline; any edit alerts, content signals raise it to high.
    {
        "file": "CLAUDE.md",
        "path_contains": ".claude",
        "baseline_gate": True,
        "analyzer": evaluate_claude_md,
        "description": "Claude CLAUDE.md (standing instructions, dispatch authority) changed outside its approved baseline",
    },
    {
        "file": "tasks.json",
        "path_contains": ".vscode",
        "grep": ["router_runtime", "_runtime/", "curl ", "wget ", "bash -c"],
        "description": "VSCode tasks.json with malicious payload (supply chain IOC)",
    },
    {
        "file": "settings.json",
        "path_contains": ".cursor",
        "grep": ["router_runtime", "_runtime/", "curl ", "wget ", "bash -c"],
        "description": "Cursor settings.json with malicious hooks (supply chain IOC)",
    },
]

# Site path from the environment (Environment= in hook-detect.service); the default
# is the generic location the unit's LogsDirectory= also provisions.
LOG_DIR = Path(os.environ.get("SHIELD_LOG_DIR", "/var/log/hook-detect"))


def setup_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_DIR / "hook-detect.log",
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    if sys.stderr.isatty():
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logging.getLogger().addHandler(h)


def scan_directory(scan_dir: str) -> list[dict]:
    """Walk directory tree looking for IOC patterns."""
    findings = []
    skipped_unreadable = 0  # review note D — count permission/oserror skips
                            # so "CLEAN" reads against denominator, not silence

    # Directory names a pattern looks inside (.claude, .vscode, .cursor). os.walk never descends a
    # symlinked directory, and Claude Code (or an editor) follows one, so a linked config directory
    # is reported, not silently skipped: otherwise a hook behind ~/.claude -> elsewhere scans CLEAN.
    watched_dirs = {ioc["path_contains"] for ioc in IOC_PATTERNS}

    for root, dirs, files in os.walk(scan_dir):
        for d in dirs:
            linked = os.path.join(root, d)
            if d in watched_dirs and os.path.islink(linked):
                findings.append({
                    "filepath": linked,
                    "description": f"a {d} directory that is a symlink: not followed, nothing in it scanned",
                    "matched_patterns": ["symlink:not-followed"],
                    "severity": "high",
                    "sha256": "",
                    "baseline_status": "",
                    "file_size": 0,
                    "file_mtime": datetime.fromtimestamp(os.lstat(linked).st_mtime, tz=timezone.utc).isoformat(),
                })
                logging.warning(f"IOC FOUND (high): {linked} — ['symlink:not-followed']")
        # Skip obvious noise
        dirs[:] = [d for d in dirs if d not in (
            "node_modules", ".git", "__pycache__", ".cache",
            "site-packages", "dist-packages",
        )]

        for ioc in IOC_PATTERNS:
            if ioc["file"] not in files:
                continue
            # Anchor path match to real directory names, not substrings
            # e.g. ".claude" matches ~/.claude/ but not /foo/.claude.backup/
            if ioc["path_contains"] not in Path(root).parts:
                continue

            filepath = os.path.join(root, ioc["file"])
            try:
                with hook_baselines.open_nofollow(filepath) as f:
                    content = f.read(50000)  # cap at 50KB
            except hook_baselines.NotRegularFile:
                # A FIFO, device or socket where a config file belongs: never read, always reported.
                findings.append({
                    "filepath": filepath,
                    "description": ioc["description"] + " (not a regular file: not read)",
                    "matched_patterns": ["not-a-regular-file"],
                    "severity": "high",
                    "sha256": "",
                    "baseline_status": "",
                    "file_size": 0,
                    "file_mtime": datetime.fromtimestamp(os.lstat(filepath).st_mtime, tz=timezone.utc).isoformat(),
                })
                logging.warning(f"IOC FOUND (high): {filepath} — ['not-a-regular-file']")
                continue
            except (PermissionError, OSError) as e:
                if e.errno == errno.ELOOP:
                    # A symlink where Claude Code reads its config. Root never follows it into another
                    # file, and Claude Code DOES follow it, so its content goes unscanned: that alone is
                    # a finding, or a hook hidden behind a link would scan CLEAN.
                    findings.append({
                        "filepath": filepath,
                        "description": ioc["description"] + " (a symlink: not followed, content not scanned)",
                        "matched_patterns": ["symlink:not-followed"],
                        "severity": "high",
                        "sha256": "",
                        "baseline_status": "",
                        "file_size": 0,
                        "file_mtime": datetime.fromtimestamp(
                            os.lstat(filepath).st_mtime, tz=timezone.utc
                        ).isoformat(),
                    })
                    logging.warning(f"IOC FOUND (high): {filepath} — ['symlink:not-followed']")
                    continue
                skipped_unreadable += 1
                continue

            severity = "high"       # default for substring-path alerts
            sha256 = ""
            baseline_status = ""

            if ioc.get("baseline_gate"):
                # Consult the baseline. check() now owns the sha shape
                # decision (raw vs canonical) via the entry's `canon` field —
                # signature swapped 2026-09-10 for the canonical-hash migration
                # (see hook_baselines.yaml header).
                try:
                    bl = hook_baselines.check(filepath)
                except (PermissionError, OSError):
                    skipped_unreadable += 1
                    continue
                sha256 = bl["current_sha"]
                baseline_status = bl["status"]
                # Denominator rule (see hook_baselines.py header):
                # every finding line carries canon mode + stripped + kept +
                # guard_failed so a reader can see WHAT was compared. INFO-
                # log on match too so "CLEAN" reads against "evaluated".
                # Review note B: guard_failed names strip-list keys whose
                # value shape was wrong (exfil-edge reporting path).
                canon_line = (
                    f"{bl['note']} stripped={bl['stripped']} "
                    f"kept={bl['kept']} guard_failed={bl['guard_failed']}"
                )
                if baseline_status == "match":
                    # Approved content — silent alert-wise, but log the
                    # evaluation for the run summary (per denominator rule).
                    logging.info(
                        f"baseline match: {filepath} {canon_line}"
                    )
                    continue
                # Baseline gate failed. Alert regardless of content.
                content_findings = ioc["analyzer"](filepath, content)
                if content_findings:
                    # Per review item 6 (2026-09-10): analyser
                    # findings may carry severity_hint="medium" to downgrade
                    # cases where the trust boundary is already established
                    # (e.g., payload interpolation to loopback+allowlisted-port
                    # cannot leave the box). If ANY finding is HIGH-equivalent
                    # (no hint, or hint != "medium"), the whole IOC stays HIGH.
                    # Only demote when every finding self-hints medium.
                    hints = [f.get("severity_hint") for f in content_findings]
                    if hints and all(h == "medium" for h in hints):
                        severity = "medium"
                    else:
                        severity = "high"
                    reasons = [f"baseline:{baseline_status}", canon_line] + [
                        f["reason"] for f in content_findings
                    ]
                else:
                    severity = "medium"
                    reasons = [f"baseline:{baseline_status}", canon_line]
                if baseline_status == "mismatch":
                    reasons.append(f"approved-was:{bl['entry']['sha256'][:12]}...")
                matched_patterns = reasons
            else:
                matched_patterns = [p for p in ioc["grep"] if p in content]
                if not matched_patterns:
                    continue

            findings.append({
                "filepath": filepath,
                "description": ioc["description"],
                "matched_patterns": matched_patterns,
                "severity": severity,
                "sha256": sha256,
                "baseline_status": baseline_status,
                "file_size": os.path.getsize(filepath),
                "file_mtime": datetime.fromtimestamp(
                    os.path.getmtime(filepath), tz=timezone.utc
                ).isoformat(),
            })
            logging.warning(f"IOC FOUND ({severity}): {filepath} — {matched_patterns}")

    # Review note D denominator: log unreadable-skip count so "CLEAN" reads
    # against a real coverage number instead of silence. INFO log so cron
    # keeps clean stdout unless something interesting happens.
    if skipped_unreadable:
        logging.info(f"scan denominator: skipped_unreadable={skipped_unreadable}")

    return findings


def store_findings(findings: list[dict], dry_run: bool = False):
    """Store findings via news_store MCP tool."""
    if not findings:
        logging.info("no IOCs found — clean scan")
        return

    logging.warning(f"ALERT: {len(findings)} IOC(s) detected!")

    if dry_run:
        for f in findings:
            logging.info(f"  {f['filepath']}: {f['matched_patterns']}")
        return

    # Alerts are stored through the news feed's API. An install without the feed (INSTALL section 7)
    # has no NEWSTRON_SECRET; say so here instead of letting the client exit mid-alert. The unit
    # still fails: on such a host that failure and this log are the only signal.
    if not os.environ.get("NEWSTRON_SECRET"):
        logging.error("no news feed configured (NEWSTRON_SECRET unset, INSTALL section 7): "
                      "the alert above is in this log only and was not stored")
        sys.exit(1)

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    # memory_client (the news API client) lives in newstron/src; same dir on the pre-cutover deploy
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent / "newstron" / "src"))
    from memory_client import NewsClient
    client = NewsClient()

    for f in findings:
        sev = f.get("severity", "high").upper()
        sha = f.get("sha256", "")
        sha_line = f"SHA-256: {sha}\n" if sha else ""
        bl_line = ""
        if f.get("baseline_status") == "mismatch":
            bl_line = "Baseline: hash MISMATCH (see hook_baselines.yaml for the approved sha)\n"
        elif f.get("baseline_status") == "missing":
            bl_line = ("Baseline: NO ENTRY (this file has never been approved — add an entry to "
                       "hook_baselines.yaml if this is intentional)\n")
        action = (
            "ACTION REQUIRED: Inspect the file. If the change is intentional, "
            "compute its sha256 and update hook_baselines.yaml. If not, treat "
            "as a possible supply-chain compromise and do NOT execute any "
            "commands from it until reviewed."
        )
        content = (
            f"IOC DETECTED [{sev}]: {f['description']}\n"
            f"File: {f['filepath']}\n"
            f"Matched: {', '.join(f['matched_patterns'])}\n"
            f"{sha_line}"
            f"{bl_line}"
            f"Size: {f['file_size']} bytes\n"
            f"Modified: {f['file_mtime']}\n\n"
            f"{action}"
        )

        try:
            result = client.news_store(
                content=content,
                url="",
                tier=1,
                source="hook-detect",
                keywords="ioc,supply-chain,persistence-hook",
                item_date=datetime.now(timezone.utc).date().isoformat(),
            )
            if result.get("status") == "stored":
                logging.info(f"stored IOC finding: {f['filepath']}")
        except Exception as e:
            logging.error(f"news_store failed: {e}")


def main():
    parser = argparse.ArgumentParser(description="AI persistence hook IOC scanner")
    parser.add_argument("--scan-dirs", nargs="+", default=None,
                       help="Directories to scan (default: /home, /root, /opt)")
    parser.add_argument("--dry-run", action="store_true",
                       help="Show findings without storing")
    args = parser.parse_args()

    setup_logging()
    logging.info("=" * 60)
    logging.info("hook-detect starting")

    scan_dirs = args.scan_dirs or DEFAULT_SCAN_DIRS
    all_findings = []

    for d in scan_dirs:
        if not os.path.isdir(d):
            logging.warning(f"scan dir not found: {d}")
            continue
        findings = scan_directory(d)
        all_findings.extend(findings)

    store_findings(all_findings, dry_run=args.dry_run)

    status = "CLEAN" if not all_findings else f"ALERT: {len(all_findings)} IOC(s)"
    logging.info(f"hook-detect complete: {status}")
    return 1 if all_findings else 0


if __name__ == "__main__":
    sys.exit(main())
