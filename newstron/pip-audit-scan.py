#!/usr/bin/env python3
"""
pip-audit-scan.py — Scan venvs for known vulnerabilities and store findings.

Runs pip-audit against specified virtualenvs, parses JSON output, stores
any findings through the memory server's news API (/news/store). Designed to
run as a weekly systemd timer alongside the news fetcher (separate scanner,
shared output).

Usage:
  SHIELD_VENVS=/path/a:/path/b python3 pip-audit-scan.py   # scan the configured venvs
  python3 pip-audit-scan.py --venvs /path/to/venv          # scan specific venv(s)
  python3 pip-audit-scan.py --dry-run                      # show findings, don't store

The venv list is site configuration: --venvs, else SHIELD_VENVS (spaces or
colons; set it in the unit's environment file), else DEFAULT_VENVS below. A
listed venv that does not exist is an ERROR, never a silent scan of nothing.

Install pip-audit if missing (into the scanner's own venv):
  <scanner-venv>/bin/pip install pip-audit

Systemd:
  cp pip-audit-scan.{service,timer} /etc/systemd/system/
  systemctl daemon-reload && systemctl enable --now pip-audit-scan.timer
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# Default: the page's two service venvs (docs/LAYOUT.md). A listed venv that does not exist is an
# ERROR, never "skipping": a scan that silently covers nothing would report clean (that is why an
# earlier version refused to have a default at all). Separators: spaces or colons.
DEFAULT_VENVS = "/opt/memory/venv /opt/newstron/venv"


def _venvs_from_env() -> list[str]:
    raw = os.environ.get("SHIELD_VENVS", DEFAULT_VENVS)
    return [p for p in re.split(r"[\s:]+", raw) if p]


# Site path from the environment (Environment= in the unit); generic default.
LOG_DIR = Path(os.environ.get("NEWSTRON_LOG_DIR", "/var/log/newstron"))


def setup_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_DIR / "pip-audit-scan.log",
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    if sys.stderr.isatty():
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logging.getLogger().addHandler(h)


def scan_venv(venv_path: str) -> "list[dict] | None":
    """Run pip-audit against a venv by freezing its packages and scanning.

    Returns the findings ([] = scanned, nothing found), or None when the scan did not happen
    (no interpreter, freeze failed, pip-audit missing or failing, unparseable output). None must
    never be read as clean: until 2026-09-25 every failure returned [], so a scanner without
    pip-audit reported "no vulnerabilities found" and exited 0."""
    python = Path(venv_path) / "bin" / "python3"
    if not python.exists():
        logging.error(f"no python3 found in {venv_path}")
        return None

    logging.info(f"scanning {venv_path}")

    # Step 1: freeze target venv's packages to a temp requirements file
    try:
        freeze_result = subprocess.run(
            [str(python), "-m", "pip", "freeze"],
            capture_output=True, text=True, timeout=60,
        )
        if freeze_result.returncode != 0:
            logging.error(f"pip freeze failed for {venv_path}: {freeze_result.stderr[:500]}")
            return None
    except Exception as e:
        logging.error(f"pip freeze error for {venv_path}: {e}")
        return None

    if not freeze_result.stdout.strip():
        logging.info(f"no packages in {venv_path}")
        return []  # a real, empty venv: scanned, nothing to find

    # Step 2: write freeze output to temp file, scan with OUR pip-audit.
    # pip-audit resolves the file against PyPI, which has no local-label builds such as
    # torch==2.11.0+cpu (PyTorch's CPU index only). One such line fails the whole venv's scan
    # (the memory server's venv installs torch from that index). Advisories are filed against the
    # upstream version, so the label is dropped for the audit and logged.
    lines, relabelled = [], []
    for line in freeze_result.stdout.splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-\[\]]+)==([^+\s]+)\+\S+$", line.strip())
        if m:
            relabelled.append(line.strip())
            line = f"{m.group(1)}=={m.group(2)}"
        lines.append(line)
    if relabelled:
        logging.info(f"{venv_path}: audited at the upstream version (local label dropped): {', '.join(relabelled)}")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tmp:
        tmp.write("\n".join(lines) + "\n")
        tmp_path = tmp.name

    # Use pip-audit from the scanner's own venv (the news fetcher's)
    scanner_pip_audit = Path(sys.executable).parent / "pip-audit"
    base = [str(scanner_pip_audit)] if scanner_pip_audit.exists() else [sys.executable, "-m", "pip_audit"]
    # JSON to its own file, spinner off: with a terminal attached, pip-audit draws its spinner on
    # STDOUT, into the JSON (2026-09-25: a manual run's output did not parse). Under systemd there
    # is no terminal, which is why the timer's runs were fine and the defect stayed latent.
    json_fd, json_path = tempfile.mkstemp(suffix=".json")
    os.close(json_fd)
    cmd = base + ["--requirement", tmp_path, "--format=json", "--progress-spinner", "off",
                  "--output", json_path]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        logging.error(f"pip-audit timed out for {venv_path}")
        os.unlink(json_path)
        return None
    except FileNotFoundError:
        logging.error(f"pip-audit not installed in scanner venv")
        os.unlink(json_path)
        return None
    except OSError as e:  # not executable, EACCES, ENOMEM...: still a named FAILED, not a traceback (review)
        logging.error(f"pip-audit could not run for {venv_path}: {e.__class__.__name__}: {e}")
        os.unlink(json_path)
        return None
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    # pip-audit returns exit code 1 when vulnerabilities found
    if result.returncode not in (0, 1):
        logging.error(f"pip-audit error ({result.returncode}): {result.stderr[:500]}")
        os.unlink(json_path)
        return None

    try:
        with open(json_path, encoding="utf-8") as jf:
            data = json.load(jf)
    except (OSError, json.JSONDecodeError) as e:
        logging.error(f"failed to read pip-audit JSON ({e.__class__.__name__}); stderr: {result.stderr[-500:]}")
        return None
    finally:
        if os.path.exists(json_path):
            os.unlink(json_path)

    # pip-audit JSON format: {"dependencies": [{"name": ..., "version": ...,
    #   "vulns": [{"id": "CVE-...", "fix_versions": [...], "description": ...}]}]}
    findings = []
    for dep in data.get("dependencies", []):
        for vuln in dep.get("vulns", []):
            findings.append({
                "package": dep["name"],
                "installed_version": dep["version"],
                "vuln_id": vuln.get("id", "unknown"),
                "description": vuln.get("description", ""),
                "fix_versions": vuln.get("fix_versions", []),
                "venv": venv_path,
            })

    logging.info(f"scan complete: {venv_path} — {len(findings)} vulnerabilities")
    return findings


def store_findings(findings: list[dict], dry_run: bool = False):
    """Store findings via news_store MCP tool."""
    if not findings:
        logging.info("no vulnerabilities found — nothing to store")
        return

    if dry_run:
        logging.info(f"DRY RUN — {len(findings)} findings would be stored:")
        for f in findings:
            logging.info(f"  {f['vuln_id']}: {f['package']}=={f['installed_version']} "
                        f"({f['venv']})")
        return

    # The news API client (the same one the fetcher uses)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    # Installed, memory_client sits beside this file (/opt/newstron); in a checkout it is in newstron/src
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent / "newstron" / "src"))
    from memory_client import NewsClient
    client = NewsClient()

    stored = 0
    for f in findings:
        content = (
            f"VULNERABILITY: {f['vuln_id']}\n"
            f"Package: {f['package']}=={f['installed_version']}\n"
            f"Venv: {f['venv']}\n"
            f"Fix: {', '.join(f['fix_versions']) or 'no fix available'}\n\n"
            f"{f['description'][:2000]}"
        )

        try:
            result = client.news_store(
                content=content,
                url=f"https://osv.dev/vulnerability/{f['vuln_id']}",
                tier=1,
                source="pip-audit-scan",
                keywords=f"{f['package']},{f['vuln_id']}",
                item_date=datetime.now(timezone.utc).date().isoformat(),
            )
            if result.get("status") == "stored":
                stored += 1
                logging.info(f"stored: {f['vuln_id']} ({f['package']})")
        except Exception as e:
            logging.error(f"news_store failed: {e}")

    logging.info(f"stored {stored}/{len(findings)} findings")


def main():
    parser = argparse.ArgumentParser(description="pip-audit venv scanner")
    parser.add_argument("--venvs", nargs="+", default=None,
                       help="Venv paths to scan (default: $SHIELD_VENVS, colon-separated)")
    parser.add_argument("--dry-run", action="store_true",
                       help="Show findings without storing")
    args = parser.parse_args()

    setup_logging()
    logging.info("=" * 60)
    logging.info("pip-audit-scan starting")

    venvs = args.venvs or _venvs_from_env()
    if not venvs:
        logging.error("no venvs to scan: set SHIELD_VENVS or pass --venvs")
        return 2
    all_findings = []

    missing = [v for v in venvs if not Path(v).exists()]
    if missing:
        logging.error(f"venv not found: {' '.join(missing)}; refusing a scan that would skip it (set SHIELD_VENVS)")
        return 2
    failed = []
    for venv in venvs:
        findings = scan_venv(venv)
        if findings is None:
            failed.append(venv)
            continue
        all_findings.extend(findings)

    store_findings(all_findings, dry_run=args.dry_run)

    if failed:
        logging.error(f"pip-audit-scan FAILED for {' '.join(failed)}: those venvs were NOT scanned "
                      f"(the unit reports failure; see the errors above)")
        return 1
    logging.info(f"pip-audit-scan complete: {len(all_findings)} total findings "
                f"across {len(venvs)} venvs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
