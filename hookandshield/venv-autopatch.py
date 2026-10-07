#!/usr/bin/env python3
"""venv-autopatch.py: audit each configured venv weekly, apply the fixes that pass the rules and the
venv's own tests, put the old versions back on any failure, and report.

  venv-autopatch.py [--config FILE] plan [--venv NAME]     what a run would do; changes nothing
  venv-autopatch.py [--config FILE] run [--venv NAME]      rehearse, apply, test, report
  venv-autopatch.py [--config FILE] status                 the ledger: user-managed, failed, last run
  venv-autopatch.py [--config FILE] adopt NAME             accept the venv as it is now (after a manual change)
  venv-autopatch.py [--config FILE] release NAME PKG       hand a user-managed package back to the tool
  venv-autopatch.py [--config FILE] retry NAME PKG         forget that PKG's versions failed here

A fix is applied automatically only if all of these hold, and is reported otherwise:
  - the package came from PyPI (no local label like +cpu, no editable, git, URL or path install);
  - nobody but this tool changed it since the last run (the ledger), and it isn't held in the config;
  - a release in the installed major version fixes every advisory against it (for a project whose
    major is a year, like pip, any newer release);
  - that release is at least `cooldown_days` old on PyPI, not yanked, has a wheel for this Python,
    has no known advisories itself, and hasn't failed this install's tests twice; of the releases
    that qualify it takes the oldest (`prefer = "oldest"`: the smallest change, public longest);
  - the installed version has a wheel on PyPI too, so the rollback can reinstall it;
  - the venv may be executed at all: its owner and test user are not root, and no uid but its owner
    can write the venv, its bin/ or site-packages/ (or the directory holding it). Otherwise the venv
    is audited and reported only.
Each fix is rehearsed on a copy of the venv (wheels only, hash-checked, installed as the venv's
owner, no dependency changes), then `pip check` and the venv's tests run on the copy. Only then is it
applied to the live venv, while the units that use it are stopped or quiet; the quick checks and
the health check run again, and any failure reinstalls the old versions and remembers the failed one.

Runs as root: it installs as each venv's owner through runuser, and stops and starts units. No venv
interpreter ever runs as root: what is installed is read with the system Python's importlib.metadata,
as the unprivileged audit user, and everything that executes a venv runs as its (non-root) owner or
test user, with a minimal environment. The tool imports only the standard library and the system's
`packaging`. The report goes straight into the mail root with the memory server's own maildir.py,
so a failure of the memory server itself is still reported. Config: TOML, see
venv-autopatch.example.toml.
"""
import argparse
import datetime as dt
import fcntl
import json
import os
import pwd
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
import tomllib
import urllib.request
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.tags import sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

DEFAULT_CONFIG = os.environ.get("VENV_AUTOPATCH_CONFIG", "/etc/venv-autopatch.toml")
PYPI = "https://pypi.org/pypi"
PIPQ = ["--no-cache-dir", "--disable-pip-version-check", "--no-input"]
TAGS = set(sys_tags())
PYVER = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
# Projects whose major version is a year, so a new major is the calendar turning, not a breaking
# change. Without this, pip (25.x -> 26.x) could never be patched. A major of 1990 or more is read as
# a year too (certifi, pytz, tzdata). A fact about each project, the same on every install.
CALVER = {"pip"}


def log(msg):
    print(f"{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


class Fail(Exception):
    """A step failed; the message says which and why."""


class Terminated(Exception):
    """SIGTERM (systemctl stop, the unit's time limit, a shutdown): raised so the rollback and every
    `finally` still run. Further SIGTERMs are ignored while they do."""


STOPPING = False   # set by the first SIGTERM; nothing new starts after it, whoever caught the exception
# systemd's stop, Ctrl-C in a hand `run`, a closed terminal: all three take the same rollback path. A bare
# KeyboardInterrupt would skip the rollback and leave units restarted on a half-patched venv.
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


def on_sigterm(signum, frame):
    global STOPPING
    STOPPING = True
    for s in STOP_SIGNALS:
        signal.signal(s, signal.SIG_IGN)
    raise Terminated(f"stopped by {signal.Signals(signum).name}")


def check_stop(what):
    if STOPPING:
        raise Terminated(f"stopped by a stop signal before {what}")


# ---------- config ----------

VENV_KEYS = {"name", "path", "owner", "umask", "mode", "test_user", "tests", "checks", "import_check",
             "stop_units", "quiet_units", "ignore_units", "idle_command", "idle_wait_minutes",
             "health_command", "health_timeout_seconds", "holds", "test_timeout_seconds"}
REPORT_KEYS = {"maildir_root", "maildir_user", "maildir_py", "mailbox"}
REPORT_OPTIONAL = {"failures_mailbox"}


def root_only(path):
    """As root, refuse a file or directory another uid could change: this tool runs the config's
    commands as root and imports the report client (the hookandshield root-environment invariant)."""
    if os.geteuid() != 0:
        return
    st = os.stat(path)
    if st.st_uid != 0 or st.st_mode & 0o022:
        raise SystemExit(f"{path} must be owned by root and writable only by root "
                         f"(uid {st.st_uid}, mode {oct(st.st_mode & 0o777)})")


def load_config(path):
    root_only(path)
    with open(path, "rb") as f:
        c = tomllib.load(f)
    c.setdefault("cooldown_days", 7)
    c.setdefault("state_dir", "/var/lib/venv-autopatch")
    c.setdefault("quiet_minutes", 10)
    c.setdefault("prefer", "oldest")
    if c["prefer"] not in ("oldest", "newest"):
        raise SystemExit("config: prefer must be oldest or newest")
    rep_ = c.setdefault("report", {})
    if set(rep_) - REPORT_KEYS - REPORT_OPTIONAL or (rep_ and not REPORT_KEYS <= set(rep_)):
        raise SystemExit(f"config: [report] needs {sorted(REPORT_KEYS)} (optional: {sorted(REPORT_OPTIONAL)})")
    c.setdefault("budget_minutes", 165)   # keep below the unit's TimeoutStartSec (3h)
    for k in ("pip_audit", "audit_user"):
        if not c.get(k):
            raise SystemExit(f"config: '{k}' is required")
    if uid_of(c["audit_user"]) == 0:
        raise SystemExit("config: audit_user must not be root")
    if rep_ and uid_of(rep_["maildir_user"]) == 0:
        raise SystemExit("config: [report] maildir_user must not be root")
    if not os.path.isabs(c["state_dir"]) or c["state_dir"].rstrip("/") in ("", "/var", "/var/lib", "/opt"):
        raise SystemExit(f"config: state_dir must be a dedicated absolute directory, not {c['state_dir']!r}")
    names = set()
    for v in c.get("venv", []):
        unknown = set(v) - VENV_KEYS
        if unknown:
            raise SystemExit(f"config: venv {v.get('name')!r} has unknown keys {sorted(unknown)}")
        for k in ("name", "path", "owner"):
            if not v.get(k):
                raise SystemExit(f"config: every [[venv]] needs '{k}'")
        if v["name"] in names or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", v["name"]):
            raise SystemExit(f"config: venv name {v['name']!r} is repeated or not [a-z0-9_-]")
        names.add(v["name"])
        v.setdefault("umask", "022")
        v.setdefault("mode", "auto")
        if v["mode"] not in ("auto", "report"):
            raise SystemExit(f"config: venv {v['name']}: mode must be auto or report")
        v.setdefault("test_user", v["owner"])
        uid_of(v["owner"]), uid_of(v["test_user"])   # a missing user is a config error, not a crash later
        for k in ("tests", "checks", "import_check", "stop_units", "quiet_units", "ignore_units"):
            v.setdefault(k, [])
        v.setdefault("idle_wait_minutes", 30)
        v.setdefault("health_timeout_seconds", 180)
        v.setdefault("test_timeout_seconds", 1800)
        v.setdefault("holds", {})
        v["holds"] = {canonicalize_name(k): r for k, r in v["holds"].items()}
    return c


# ---------- running things ----------

def uid_of(user):
    try:
        return pwd.getpwnam(user).pw_uid
    except KeyError:
        raise SystemExit(f"config: no such user {user!r}")


# Absolute: subprocess finds a bare name on the CHILD's PATH, and run_as's minimal PATH has no sbin, so a
# bare "runuser" fails as root with "No such file or directory". The tests run without root and never take
# this branch.
RUNUSER = "/usr/sbin/runuser"


def run_as(user, umask, argv, cwd="/tmp", timeout=1800, env_extra=None, input_text=None):
    """Run argv as `user` with `umask` and a minimal environment (not root's: no secret, no loader or
    interpreter variables, the user's own HOME). Root drops to the user with runuser; anyone else may
    only run as themselves (that is how the tests drive this tool without root)."""
    me = pwd.getpwuid(os.geteuid()).pw_name
    pw = pwd.getpwnam(user)
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": pw.pw_dir, "USER": user,
           "LOGNAME": user, "PIP_CONFIG_FILE": os.devnull, "PYTHONDONTWRITEBYTECODE": "1", **(env_extra or {})}
    if user == me:
        return run_group(argv, cwd=cwd, env=env, timeout=timeout, input_text=input_text,
                         preexec_fn=lambda: os.umask(int(umask, 8)))
    if os.geteuid() != 0:
        raise Fail(f"must run as root to act as {user}")
    wrapped = [RUNUSER, "-u", user, "--", "sh", "-c", 'umask "$0"; exec "$@"', umask, *argv]
    return run_group(wrapped, cwd=cwd, env=env, timeout=timeout, input_text=input_text)


def run_group(argv, cwd, env, timeout, input_text=None, preexec_fn=None):
    """subprocess.run(capture_output, text), except the child leads its own process group and the whole
    group is killed when this side gives up: on a timeout, or on any exception (Terminated included).
    subprocess.run kills only its direct child, and runuser forks, so `pip install` would run on as an
    orphan while the rollback started."""
    with subprocess.Popen(argv, cwd=cwd, env=env, text=True, start_new_session=True,
                          stdin=subprocess.PIPE if input_text is not None else None,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, preexec_fn=preexec_fn) as p:
        try:
            out, err = p.communicate(input=input_text, timeout=timeout)
        except BaseException:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.wait()
            raise
    return subprocess.CompletedProcess(argv, p.returncode, out, err)


def must(p, what):
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip().splitlines()[-3:]
        raise Fail(f"{what} failed (exit {p.returncode}): {' | '.join(tail)}")
    return p


def systemctl(*args):
    return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=300)


# ---------- reading a venv ----------

def venv_python(path):
    return str(Path(path) / "bin" / "python3")


def check_interpreter(path):
    """The wheel-tag and requires-python checks use this interpreter's tags, so the venv must run
    the same Python version."""
    cfg = Path(path) / "pyvenv.cfg"
    if not cfg.is_file():
        raise Fail(f"{path} is not a venv (no pyvenv.cfg)")
    m = re.search(r"^version(?:_info)?\s*=\s*(\d+)\.(\d+)", cfg.read_text(), re.M)
    if not m or (int(m[1]), int(m[2])) != sys.version_info[:2]:
        raise Fail(f"{path} runs Python {m[1] + '.' + m[2] if m else '?'}, this tool runs {PYVER}; "
                   "run the tool under the same Python as the venv")


def site_packages(path):
    return str(Path(path) / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages")


INSPECT = r"""
import json, sys
from importlib.metadata import distributions
out = []
for d in distributions(path=[sys.argv[1]]):
    du = d.read_text("direct_url.json")
    out.append({"name": d.metadata["Name"], "version": d.version, "direct_url": json.loads(du) if du else None})
print(json.dumps(out))
"""


def inspect_venv(cfg, path):
    """{canonical name: {name, version, from_pypi, why, public}}: the venv's dist-info read by the
    system Python's importlib.metadata, as the audit user. Nothing from the venv executes."""
    p = must(run_as(cfg["audit_user"], "022", [sys.executable, "-I", "-S", "-c", INSPECT, site_packages(path)],
                    timeout=300), f"read the packages in {path}")
    out = {}
    for d in json.loads(p.stdout):
        name, ver = d["name"], d["version"]
        why, public = "", None
        if d.get("direct_url"):
            why = "installed from " + (d["direct_url"].get("url") or "a direct URL")
        else:
            try:
                if Version(ver).local:
                    why, public = f"local build {ver} (not a PyPI release)", Version(ver).public
            except InvalidVersion:
                why = f"unparseable version {ver}"
        out[canonicalize_name(name)] = {"name": name, "version": ver, "from_pypi": not why, "why": why,
                                        "public": public}
    return out


def group_others(gid, owner, _cache={}):
    """Members of group `gid` other than `owner` and root (listed members and primary-group users)."""
    if gid not in _cache:
        import grp
        g = grp.getgrgid(gid)
        members = set(g.gr_mem) | {u.pw_name for u in pwd.getpwall() if u.pw_gid == gid}
        _cache[gid] = (g.gr_name, members)
    name, members = _cache[gid]
    return {f"{m} (group {name})" for m in members - {owner, "root"}}


def other_writers(st, owner):
    """Users other than `owner` and root who can write the file with this lstat (owner, group or
    world bits). ACLs are checked separately."""
    who = set()
    if st.st_uid not in (0, uid_of(owner)):
        who.add(pwd.getpwuid(st.st_uid).pw_name)
    if st.st_mode & 0o002:
        who.add("everyone")
    if st.st_mode & 0o020:
        who |= group_others(st.st_gid, owner)
    return who


def has_acl(path):
    """An extended POSIX ACL can grant a named user write without showing in the mode bits."""
    for attr in ("system.posix_acl_access", "system.posix_acl_default"):
        try:
            os.getxattr(path, attr, follow_symlinks=False)
            return attr
        except OSError:
            pass
    return ""


def refusal(v):
    """Why this venv must not be executed by this tool, or "" if it may: a
    venv interpreter run as root, or one another uid can write, hands that uid root or the owner.
    Checks every directory from / down to the venv, and every file and directory in it (lstat, no
    symlinks followed), for owner, group and world write and for ACLs."""
    for who, what in ((v["owner"], "owner"), (v["test_user"], "test_user")):
        if uid_of(who) == 0:
            return f"its {what} is root (no venv interpreter runs as root; give it an unprivileged owner)"
    root = Path(v["path"])
    if root.is_symlink() or not root.is_dir():
        return f"{root} is not a real directory"
    why = chain_refusal(root.parent, v["owner"])
    if why:
        return why
    errors = []
    for dirpath, dirs, files in os.walk(root, onerror=errors.append):
        for name in [None] + dirs + files:
            p = dirpath if name is None else os.path.join(dirpath, name)
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                continue
            w = other_writers(st, v["owner"])
            if w:
                return f"{p} is writable by {', '.join(sorted(w))}"
            if has_acl(p):
                return f"{p} has an ACL"
    if errors:
        return f"could not read all of {root}: {errors[0]}"
    # What the venv points outside itself at: the interpreter its bin/python* links resolve to, and
    # pyvenv.cfg's home with the standard library beside it. A user who can write those runs code
    # as the owner.
    targets = {os.path.realpath(p) for p in (root / "bin").glob("python*") if p.is_symlink()}
    cfg = root / "pyvenv.cfg"
    if cfg.is_file():
        m = re.search(r"^home\s*=\s*(.+?)\s*$", cfg.read_text(), re.M)
        if m:
            home = os.path.realpath(m[1])
            ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
            targets |= {home, os.path.join(os.path.dirname(home), "lib", ver)}
    for t in sorted(targets):
        if os.path.exists(t):
            why = chain_refusal(Path(t), v["owner"])
            if why:
                return f"{why} (outside the venv, reached from it)"
    return ""


def chain_refusal(path, owner):
    """Is `path`, or any directory above it, writable by someone other than `owner` and root? A
    root-owned sticky directory (/tmp) is allowed: others can't replace what they don't own."""
    p = Path(path)
    for q in [p] + list(p.parents):
        st = os.lstat(q)
        w = other_writers(st, owner)
        if w and not (stat.S_ISDIR(st.st_mode) and st.st_mode & stat.S_ISVTX and st.st_uid == 0):
            return f"{q} is writable by {', '.join(sorted(w))}"
        if has_acl(str(q)):
            return f"{q} has an ACL"
    return ""


def pip_check(path, user, umask):
    p = run_as(user, umask, [venv_python(path), "-m", "pip", "check", *PIPQ], timeout=300)
    return (p.stdout + p.stderr).strip()


# ---------- the audit and PyPI ----------

def audit(cfg, pins):
    """pip-audit over name==version pins: {canonical name: [{id, fix_versions}]}. Runs as audit_user,
    with no scratch venv (--no-deps --disable-pip), like pip-audit-scan.py."""
    if not pins:
        return {}
    adir = Path(cfg["state_dir"]) / "audit"   # root's; audit_user only reads the list
    owned_dir(adir, None)
    req = adir / f"audit-{os.getpid()}-{len(pins)}.txt"
    req.write_text("".join(f"{n}=={v}\n" for n, v in sorted(pins.items())))
    req.chmod(0o644)
    # pip-audit keeps a cache under HOME by default, and a service user's HOME may not exist
    # (/nonexistent): every audit then failed with PermissionError. Its own scratch cache per run.
    cache = adir / f"cache-{os.getpid()}"
    owned_dir(cache, cfg["audit_user"], 0o700)
    try:
        p = run_as(cfg["audit_user"], "022",
                   [cfg["pip_audit"], "--requirement", str(req), "--no-deps", "--disable-pip",
                    "--format=json", "--progress-spinner", "off", "--cache-dir", str(cache)], timeout=900)
        if p.returncode not in (0, 1):
            raise Fail(f"pip-audit failed (exit {p.returncode}): {(p.stderr or '').strip()[-300:]}")
        try:
            data = json.loads(p.stdout)
        except ValueError:
            raise Fail(f"pip-audit printed no JSON: {(p.stderr or p.stdout or '').strip()[-300:]}")
    finally:
        req.unlink(missing_ok=True)
        safe_rmtree(cfg, cache)
    found = {}
    for dep in data.get("dependencies", []):
        if dep.get("vulns"):
            found[canonicalize_name(dep["name"])] = [
                {"id": x.get("id", "?"), "fix_versions": x.get("fix_versions") or []} for x in dep["vulns"]]
    return found


def pypi_releases(name):
    with urllib.request.urlopen(f"{PYPI}/{name}/json", timeout=30) as r:
        return json.load(r)["releases"]


def compatible_wheels(files):
    """The release's wheels this interpreter can install: [{filename, sha256, uploaded, yanked}]."""
    ok = []
    for f in files:
        if f.get("packagetype") != "bdist_wheel":
            continue
        try:
            _, _, _, tags = parse_wheel_filename(f["filename"])
        except Exception:
            continue
        rp = f.get("requires_python")
        if rp:
            try:
                if not SpecifierSet(rp).contains(PYVER):
                    continue
            except Exception:
                continue
        if tags & TAGS:
            ok.append({"filename": f["filename"], "sha256": f["digests"]["sha256"],
                       "uploaded": f.get("upload_time_iso_8601") or f.get("upload_time"),
                       "yanked": bool(f.get("yanked"))})
    return ok


def choose(installed, vulns, releases, now, cooldown_days, failed_versions, calver=False, prefer="oldest"):
    """Pick a release that fixes every advisory without leaving the installed major version (any newer
    release, for a calendar-versioned project): the oldest that qualifies by default, the smallest
    change and the one public longest, or the newest. Returns (version, wheels) or
    (None, reason). Pure: the tests drive it with recorded PyPI data."""
    cur = Version(installed)
    calver = calver or cur.major >= 1990
    in_scope = (lambda x: True) if calver else (lambda x: x.major == cur.major)
    need = []
    for v in vulns:
        fixes = []
        for f in v["fix_versions"]:
            try:
                fv = Version(f)
            except InvalidVersion:
                continue
            if fv > cur:
                fixes.append(fv)
        same = [f for f in fixes if in_scope(f)]
        if not fixes:
            return None, f"{v['id']}: no fix released yet"
        if not same:
            return None, f"{v['id']}: fixed only in a new major version ({min(fixes)})"
        need.append(min(same))
    floor = max(need)
    best, why_not = None, "no release fits"
    for s, files in releases.items():
        try:
            rv = Version(s)
        except InvalidVersion:
            continue
        if rv.is_prerelease or rv.is_devrelease or rv.local or not in_scope(rv) or rv < floor:
            continue
        if s in failed_versions:
            why_not = f"{s} failed this install's tests twice"
            continue
        wheels = [w for w in compatible_wheels(files) if not w["yanked"]]
        if not wheels:
            why_not = f"{s} has no wheel for Python {PYVER}"
            continue
        newest_upload = max(dt.datetime.fromisoformat(w["uploaded"].replace("Z", "+00:00")) for w in wheels)
        age = (now - newest_upload).total_seconds() / 86400
        if age < cooldown_days:
            why_not = f"{s} is {age:.1f} days old (cool-down {cooldown_days})"
            continue
        if best is None or (rv < best[0] if prefer == "oldest" else rv > best[0]):
            best = (rv, wheels)
    if best is None:
        return None, why_not
    return str(best[0]), best[1]


# ---------- the ledger ----------

def ledger_path(cfg, name):
    return Path(cfg["state_dir"]) / f"{name}.json"


def load_ledger(cfg, name):
    p = ledger_path(cfg, name)
    if p.exists():
        return json.loads(p.read_text())
    return {"freeze": None, "user_managed": {}, "failed": {}, "history": []}


def save_ledger(cfg, name, led):
    p = ledger_path(cfg, name)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(led, indent=1, sort_keys=True))
    os.replace(tmp, p)


def drift(led, live):
    """Packages someone other than this tool changed since its last run: {name: what changed}."""
    if led["freeze"] is None:
        return {}
    old, out = led["freeze"], {}
    for n, d in live.items():
        if n not in old:
            out[n] = f"added at {d['version']}"
        elif old[n] != d["version"]:
            out[n] = f"changed {old[n]} -> {d['version']}"
    return out


# ---------- planning ----------

def plan_venv(cfg, v, now):
    """Decide what happens to each finding. Changes nothing except the ledger's user-managed list."""
    check_interpreter(v["path"])
    led = load_ledger(cfg, v["name"])
    live = inspect_venv(cfg, v["path"])
    first = led["freeze"] is None
    for n, what in drift(led, live).items():
        led["user_managed"].setdefault(n, f"{what}, outside this tool ({now.date()})")
    pins = {d["name"]: d["version"] if d["from_pypi"] else d["public"]
            for d in live.values() if d["from_pypi"] or d["public"]}
    findings = audit(cfg, pins)
    picks, report = [], []
    for n, vulns in sorted(findings.items()):
        d = live[n]
        ids = ", ".join(sorted({x["id"] for x in vulns}))
        if not d["from_pypi"]:
            report.append(f"{d['name']} {d['version']} ({ids}): not patched: {d['why']}")
            continue
        if n in v["holds"]:
            report.append(f"{d['name']} {d['version']} ({ids}): held in the config: {v['holds'][n]}")
            continue
        if n in led["user_managed"]:
            report.append(f"{d['name']} {d['version']} ({ids}): user-managed ({led['user_managed'][n]}); "
                          f"`release {v['name']} {d['name']}` hands it back")
            continue
        try:
            rels = pypi_releases(d["name"])
        except Exception as e:
            report.append(f"{d['name']} {d['version']} ({ids}): PyPI lookup failed: {e}")
            continue
        old_wheels = [w for w in compatible_wheels(rels.get(d["version"], []))]
        ver, wheels = choose(d["version"], vulns, rels, now, cfg["cooldown_days"],
                             led["failed"].get(n, {}), calver=n in CALVER, prefer=cfg["prefer"])
        if ver is None:
            report.append(f"{d['name']} {d['version']} ({ids}): {wheels}")
            continue
        if not old_wheels:
            report.append(f"{d['name']} {d['version']} ({ids}): {ver} would fix it, but {d['version']} has no "
                          f"wheel on PyPI to roll back to")
            continue
        picks.append({"key": n, "name": d["name"], "from": d["version"], "to": ver, "ids": ids,
                      "wheels": wheels, "old_wheels": old_wheels})
    # Installed from a URL, path or VCS: no PyPI version to audit, so named every run.
    for n, d in sorted(live.items()):
        if not d["from_pypi"] and not d["public"]:
            report.append(f"{d['name']}: not audited or patched: {d['why']}")
    # The fixes must themselves be clean.
    if picks:
        clean = audit(cfg, {p["name"]: p["to"] for p in picks})
        for p in list(picks):
            if p["key"] in clean:
                report.append(f"{p['name']} {p['from']} -> {p['to']}: the fix has advisories of its own "
                              f"({', '.join(x['id'] for x in clean[p['key']])})")
                picks.remove(p)
    return {"ledger": led, "live": live, "first": first, "findings": findings, "picks": picks,
            "report": report}


# ---------- applying ----------

def req_lines(picks, which):
    out = []
    for p in picks:
        ver = p["to"] if which == "to" else p["from"]
        ws = p["wheels"] if which == "to" else p["old_wheels"]
        out.append(f"{p['name']}=={ver} " + " ".join(f"--hash=sha256:{w['sha256']}" for w in ws) + "\n")
    return "".join(out)


def owned_dir(path, user, mode=0o755):
    """A directory owned by `user`, or by root when `user` is None. Root writes only into directories
    it owns, so no other uid can plant a symlink where root writes."""
    path.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        if user is None:
            os.chown(path, 0, 0)
        else:
            pw = pwd.getpwnam(user)
            os.chown(path, pw.pw_uid, pw.pw_gid)
    path.chmod(mode)


def safe_rmtree(cfg, path):
    sd = Path(cfg["state_dir"]).resolve()
    p = Path(path).resolve()
    if sd not in p.parents:
        raise Fail(f"refusing to remove {p}: not under {sd}")
    shutil.rmtree(p, ignore_errors=True)


def pip_install(v, python, req, wheels_dir):
    return must(run_as(v["owner"], v["umask"],
                       [python, "-m", "pip", "install", *PIPQ, "-q", "--no-deps", "--only-binary", ":all:",
                        "--require-hashes", "--no-index", "--find-links", str(wheels_dir), "-r", str(req)]),
                f"pip install into {Path(python).parent.parent}")


def versions_now(cfg, path, picks, which):
    live = inspect_venv(cfg, path)
    for p in picks:
        want = p["to"] if which == "to" else p["from"]
        got = live.get(p["key"], {}).get("version")
        if got != want:
            raise Fail(f"{p['name']} is {got} in {path}, want {want}")
    return live


def fill(cmd, new, old, work):
    return cmd.replace("{new}", shlex.quote(new)).replace("{old}", shlex.quote(old)).replace(
        "{work}", shlex.quote(str(work)))


IMPORT_CHECK = r"""
import ast, importlib, os, sys
script = sys.argv[1]
here = os.path.dirname(os.path.abspath(script))
mods = set()
for node in ast.walk(ast.parse(open(script).read(), script)):
    if isinstance(node, ast.Import):
        mods.update(a.name.split(".")[0] for a in node.names)
    elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        mods.add(node.module.split(".")[0])
# Only what an installed distribution provides: a script's own modules (beside it, or on a path it
# adds at run time) are its code, not the venv's.
from importlib.metadata import packages_distributions
installed = packages_distributions()
third = sorted(m for m in mods if m not in sys.stdlib_module_names and m in installed
               and not os.path.exists(os.path.join(here, m + ".py")) and not os.path.isdir(os.path.join(here, m)))
other = sorted(m for m in mods if m not in sys.stdlib_module_names and m not in third)
for m in third:
    importlib.import_module(m)
print(f"imports ok: {script}: {', '.join(third) or 'none'}" + (f" (not checked: {', '.join(other)})" if other else ""))
"""


def run_tests(v, cmds, new, old, work, stage):
    """The import checks, then each command; any non-zero exit is a failure."""
    for script in v["import_check"]:
        p = run_as(v["test_user"], v["umask"], [new, "-c", IMPORT_CHECK, script], cwd=str(work),
                   timeout=v["test_timeout_seconds"])
        must(p, f"{stage}: import check of {script}")
    for c in cmds:
        p = run_as(v["test_user"], v["umask"], ["sh", "-c", fill(c, new, old, work)], cwd=str(work),
                   timeout=v["test_timeout_seconds"])
        must(p, f"{stage}: test `{c}`")


def unit_busy(cfg, unit):
    """A unit that is running, or whose timer fires within quiet_minutes."""
    if systemctl("is-active", "--quiet", unit).returncode == 0:
        return f"{unit} is running"
    st = systemctl("is-active", unit).stdout.strip()
    if st == "activating":
        return f"{unit} is starting"
    timer = unit.rsplit(".", 1)[0] + ".timer"
    p = systemctl("list-timers", "--all", "--output=json", "--no-pager", timer)
    try:
        nxt = next((t.get("next") for t in json.loads(p.stdout or "[]") if t.get("unit") == timer), None)
    except ValueError:
        nxt = None
    if nxt:
        left = nxt / 1e6 - time.time()
        if 0 <= left < cfg["quiet_minutes"] * 60:
            return f"{timer} fires in {int(left)}s"
    return ""


def uses_venv(pid, root):
    """Does process `pid` run from the venv? Its command line names the venv (relative paths read
    against its working directory), it runs with the venv activated (VIRTUAL_ENV), or it has a file
    from the venv mapped (a C extension). A pure-Python script started some other way is not seen."""
    argv = [a.decode(errors="replace") for a in open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0") if a]
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        cwd = "/"
    for a in argv[:3]:
        if root in a or os.path.abspath(os.path.join(cwd, a)).startswith(root):
            return True
    try:
        env = open(f"/proc/{pid}/environ", "rb").read().split(b"\0")
        if any(e.startswith(b"VIRTUAL_ENV=") and os.path.realpath(e[12:].decode(errors="replace")) + "/" == root
               for e in env):
            return True
    except OSError:
        pass
    try:
        return root in open(f"/proc/{pid}/maps").read()
    except OSError:
        return False


def venv_in_use(v):
    """Processes using the venv, outside the units this run stopped or was told to ignore."""
    root = os.path.realpath(v["path"]).rstrip("/") + "/"
    allowed = tuple(f"/{u}" for u in v["ignore_units"])
    hits = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        if int(pid) == os.getpid():
            continue
        try:
            if not uses_venv(pid, root):
                continue
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace")
            cg = open(f"/proc/{pid}/cgroup").read()
        except OSError:
            continue
        if any(a in cg for a in allowed):
            continue
        hits.append(f"{pid} {cmd.strip()[:80]}")
    return hits


def wait_idle(v):
    if not v.get("idle_command"):
        return True
    deadline = time.time() + v["idle_wait_minutes"] * 60
    while True:
        if subprocess.run(["sh", "-c", v["idle_command"]], capture_output=True, timeout=60).returncode == 0:
            return True
        if time.time() > deadline:
            return False
        time.sleep(30)


def healthy(v):
    if not v.get("health_command"):
        return True
    deadline = time.time() + v["health_timeout_seconds"]
    while time.time() < deadline:
        if subprocess.run(["sh", "-c", v["health_command"]], capture_output=True, timeout=60).returncode == 0:
            return True
        time.sleep(5)
    return False


def names_of(picks):
    return ", ".join(f"{p['name']} {p['from']} -> {p['to']}" for p in picks)


def rehearse(cfg, v, picks, work, wheels, base_check, live_py, tdir):
    """Install `picks` into a fresh copy of the venv and run pip check and the tests there; raises Fail."""
    copy = work / "venv"
    safe_rmtree(cfg, copy)
    log(f"{v['name']}: rehearsing {names_of(picks)} on a copy")
    must(subprocess.run(["cp", "-a", v["path"], str(copy)], capture_output=True, text=True, timeout=1800),
         "copy the venv")
    try:
        req = work / "rehearse.txt"
        req.write_text(req_lines(picks, "to"))
        req.chmod(0o644)
        copy_py = venv_python(copy)
        pip_install(v, copy_py, req, wheels)
        versions_now(cfg, str(copy), picks, "to")
        c = pip_check(str(copy), v["owner"], v["umask"])
        if c != base_check:
            raise Fail(f"pip check on the copy changed: {c}")
        run_tests(v, v["tests"] + v["checks"], copy_py, live_py, tdir, "rehearsal")
    except (Fail, Terminated):
        raise
    except Exception as e:
        raise Fail(f"rehearsal: {type(e).__name__}: {e}")
    finally:
        safe_rmtree(cfg, copy)


def patch_venv(cfg, v, pl, work, report):
    """Rehearse on a copy, then apply to the live venv. Returns 'patched', 'partly patched',
    'not applied', 'report only', 'deferred', 'rolled back' or 'failed'."""
    picks = pl["picks"]
    led = pl["ledger"]
    owned_dir(work, None)
    wheels, old_wheels, tdir = work / "wheels", work / "old-wheels", work / "t"
    for d in (wheels, old_wheels):
        owned_dir(d, v["owner"])
    owned_dir(tdir, v["test_user"], mode=0o700)
    up, down = work / "up.txt", work / "down.txt"
    up.write_text(req_lines(picks, "to"))
    down.write_text(req_lines(picks, "from"))
    up.chmod(0o644)
    down.chmod(0o644)
    live_py = venv_python(v["path"])
    tried = len(picks)
    # Downloads first, both directions, so a rollback never needs the network.
    must(run_as(v["owner"], v["umask"], [live_py, "-m", "pip", "download", *PIPQ, "-q", "--no-deps",
                                         "--only-binary", ":all:", "--require-hashes", "-r", str(up),
                                         "-d", str(wheels)]), "download the fixes")
    must(run_as(v["owner"], v["umask"], [live_py, "-m", "pip", "download", *PIPQ, "-q", "--no-deps",
                                         "--only-binary", ":all:", "--require-hashes", "-r", str(down),
                                         "-d", str(old_wheels)]), "download the current versions (rollback)")
    base_check = pip_check(v["path"], v["owner"], v["umask"])

    # --- rehearsal, on a copy. A failed batch is retried one fix at a time, so one bad release
    # doesn't take the others down with it; the survivors are rehearsed together again. ---
    def failed(p, e):
        # Two strikes, like a live failure: one network blip in a test must not disqualify a fix for
        # good. `retry` clears both counts.
        rf = led.setdefault("rehearsal_failures", {})
        n = rf.setdefault(p["key"], {}).get(p["to"], 0) + 1
        rf[p["key"]][p["to"]] = n
        if n >= 2:
            led["failed"].setdefault(p["key"], {})[p["to"]] = f"failed its rehearsal twice: {e}"[:300]
        report.append(f"NOT APPLIED {names_of([p])} (rehearsal failure {n} of 2): {e}")

    check_stop("the rehearsal")
    try:
        rehearse(cfg, v, picks, work, wheels, base_check, live_py, tdir)
    except Fail as e:
        if len(picks) == 1:
            failed(picks[0], e)
            return "not applied"
        report.append(f"the batch failed its rehearsal ({str(e)[:200]}); trying each fix alone")
        good = []
        for p in picks:
            check_stop("the next rehearsal")
            try:
                rehearse(cfg, v, [p], work, wheels, base_check, live_py, tdir)
                good.append(p)
            except Fail as e1:
                failed(p, e1)
        if not good:
            return "not applied"
        if len(good) > 1:
            try:
                rehearse(cfg, v, good, work, wheels, base_check, live_py, tdir)
            except Fail as e2:
                report.append(f"NOT APPLIED {names_of(good)}: each passes alone, together they fail: {e2}")
                return "not applied"
        picks = good
        up.write_text(req_lines(picks, "to"))
        down.write_text(req_lines(picks, "from"))
    names = names_of(picks)
    if v["mode"] != "auto":
        report.append(f"would apply (mode = report): {names}; the rehearsal passed")
        return "report only"

    # --- apply ---
    check_stop("the apply")
    if time.time() > cfg["_deadline"] - 15 * 60:
        report.append(f"deferred {names}: less than 15 minutes of the run's time budget left")
        return "deferred"
    busy = [b for u in v["quiet_units"] if (b := unit_busy(cfg, u))]
    if busy:
        report.append(f"deferred {names}: {'; '.join(busy)}")
        return "deferred"
    if not wait_idle(v):
        report.append(f"deferred {names}: still busy after {v['idle_wait_minutes']} min (idle_command)")
        return "deferred"
    was_running = [u for u in v["stop_units"] if systemctl("is-active", "--quiet", u).returncode == 0]
    stopped = []
    try:
        for u in was_running:
            must(systemctl("stop", u), f"systemctl stop {u}")
            stopped.append(u)
        users = venv_in_use(v)
        if users:
            report.append(f"deferred {names}: the venv is in use: {'; '.join(users[:3])}")
            return "deferred"
        now_live = {n: d["version"] for n, d in inspect_venv(cfg, v["path"]).items()}
        if now_live != {n: d["version"] for n, d in pl["live"].items()}:
            report.append(f"deferred {names}: the venv changed during this run")
            return "deferred"
        log(f"{v['name']}: applying {names}")
        try:
            pip_install(v, live_py, up, wheels)
            versions_now(cfg, v["path"], picks, "to")
            c = pip_check(v["path"], v["owner"], v["umask"])
            if c != base_check:
                raise Fail(f"pip check changed: {c}")
            run_tests(v, v["checks"], live_py, live_py, tdir, "after applying")
            for u in stopped:
                must(systemctl("start", u), f"systemctl start {u}")
            stopped = []
            if not healthy(v):
                raise Fail(f"health check did not pass within {v['health_timeout_seconds']}s")
        except Exception as e:  # any failure after the install starts rolls back, not only a Fail
            e = e if isinstance(e, (Fail, Terminated)) else Fail(f"{type(e).__name__}: {e}")
            log(f"{v['name']}: {e}; rolling back")
            # The rehearsal passed, so this may be the moment (a slow start); a version is skipped
            # only after it fails on the live venv twice.
            lf = led.setdefault("live_failures", {})
            for p in (picks if not isinstance(e, Terminated) else []):   # a stop is not the version's fault
                n = lf.setdefault(p["key"], {}).get(p["to"], 0) + 1
                lf[p["key"]][p["to"]] = n
                if n >= 2:
                    led["failed"].setdefault(p["key"], {})[p["to"]] = f"failed on the live venv twice: {e}"[:300]
            for u in was_running:
                systemctl("stop", u)
            stopped = list(was_running)
            try:
                pip_install(v, live_py, down, old_wheels)
                versions_now(cfg, v["path"], picks, "from")
            except Exception as e2:
                report.append(f"FAILED {names}: {e}; THE ROLLBACK FAILED TOO ({e2}): {v['path']} needs a person")
                return "failed"
            report.append(f"ROLLED BACK {names}: {e}")
            return "rolled back"
        report.append(f"patched {names}")
        return "patched" if len(picks) == tried else "partly patched"
    finally:
        for u in stopped:
            systemctl("start", u)
        if stopped and not healthy(v):
            report.append(f"WARNING: {v['name']}'s units restarted but the health check did not pass")


# ---------- commands ----------

# What a person hears about. A failure or a rollback fails the unit. Outcomes
# that leave a known advisory open also go to the operator's mailbox, without failing the unit: a fix that was
# never applied and a venv the tool may not patch, every time (neither fixes itself); a run deferred because
# the venv was in use only when the previous run was deferred too (one deferral is normal).
BAD = ("failed", "rolled back")
ATTENTION = ("not applied", "partly patched", "audit only")


def needs_attention(outcome, previous):
    """True when this venv's outcome should reach the operator although nothing failed."""
    return outcome in ATTENTION or (outcome == "deferred" and previous == "deferred")


def report_route(report_cfg, bad, attention):
    """(subject, mailboxes) for this run's report: every week the shared mailbox; on a failure or anything
    needing attention, also the operator's (`failures_mailbox`), which a person reads and no pane rings for."""
    if bad:
        subject = "venv-autopatch: FAILURES, see the report"
    elif attention:
        subject = "venv-autopatch: needs attention: " + ", ".join(attention)
    else:
        subject = "venv-autopatch: weekly report"
    boxes = [report_cfg["mailbox"]]
    if (bad or attention) and report_cfg.get("failures_mailbox"):
        boxes.append(report_cfg["failures_mailbox"])
    return subject, boxes


def cmd_plan_or_run(cfg, args, apply):
    now = dt.datetime.now(dt.timezone.utc)
    cfg["_deadline"] = time.time() + cfg["budget_minutes"] * 60
    for s in STOP_SIGNALS:
        signal.signal(s, on_sigterm)
    state = Path(cfg["state_dir"])
    state.mkdir(parents=True, exist_ok=True)
    lock = open(state / "lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("another venv-autopatch run holds the lock")
    venvs = [v for v in cfg.get("venv", []) if not args.venv or v["name"] == args.venv]
    if args.venv and not venvs:
        raise SystemExit(f"no venv named {args.venv!r} in the config")
    lines, bad, attention = [f"venv-autopatch {now.date()} (cool-down {cfg['cooldown_days']} days)"], 0, []
    stop = False
    for v in venvs:
        if stop or STOPPING:
            lines.append(f"\n{v['name']} ({v['path']}): not run (the run was stopped)")
            continue
        report, outcome = [], "ok"
        work = state / "work" / v["name"]
        pl, previous = None, None
        try:
            safe_rmtree(cfg, work)
            pl = plan_venv(cfg, v, now)
            previous = pl["ledger"]["history"][-1]["outcome"] if pl["ledger"]["history"] else None
            report += pl["report"]
            if pl["first"]:
                report.append("first run: the ledger starts from the venv as it is now")
            why = refusal(v) if pl["picks"] else ""
            if not pl["picks"]:
                outcome = "nothing to apply"
            elif why:
                report += [f"{p['name']} {p['from']} -> {p['to']} ({p['ids']}) would fix it; not patched: "
                           f"{why}" for p in pl["picks"]]
                outcome = "audit only"
            elif not apply:
                report += [f"would rehearse and apply: {p['name']} {p['from']} -> {p['to']} ({p['ids']})"
                           for p in pl["picks"]]
                outcome = "plan"
            else:
                outcome = patch_venv(cfg, v, pl, work, report)
        except Exception as e:
            outcome = "failed"
            report.append(f"FAILED: {type(e).__name__}: {e}")
            stop = isinstance(e, Terminated)
        finally:
            # A SIGTERM landing here must not skip the report.
            try:
                safe_rmtree(cfg, work)
                # Always record what is installed now, whatever happened, so next week's drift check
                # never mistakes this run's own changes for a hand change.
                if apply and pl is not None:
                    led = pl["ledger"]
                    try:
                        led["freeze"] = {n: d["version"] for n, d in inspect_venv(cfg, v["path"]).items()}
                    except Exception as e:
                        report.append(f"could not record the venv's packages ({e}); next run re-reads them")
                    led["history"] = (led["history"] + [{"at": now.isoformat(timespec="seconds"),
                                                         "outcome": outcome, "notes": report}])[-20:]
                    save_ledger(cfg, v["name"], led)
            except Terminated as e:
                stop = True
                report.append(f"{e} while recording this venv's result; the ledger may be a week behind")
        bad += outcome in BAD
        if apply and needs_attention(outcome, previous):
            attention.append(f"{v['name']} {outcome}" + (" twice running" if outcome == "deferred" else ""))
        lines.append(f"\n{v['name']} ({v['path']}): {outcome}")
        lines += [f"  - {r}" for r in report]
    text = "\n".join(lines)
    print(text, flush=True)
    if apply:
        (state / "last-report.txt").write_text(text + "\n")
        deliver(cfg, text, bad, attention)
    return 1 if bad else 0


DELIVER = r"""
import datetime, importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location("maildir", sys.argv[1])
maildir = importlib.util.module_from_spec(spec); spec.loader.exec_module(maildir)
root, box, subject = sys.argv[2], sys.argv[3], sys.argv[4]
body = sys.stdin.read()
msg_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S-%f") + "_venv-autopatch_" + os.urandom(4).hex()
header = {"schema": 1, "id": msg_id, "from": "venv-autopatch", "to": "broadcast", "subject": subject[:300],
          "kind": "digest", "priority": "low", "created": datetime.datetime.now(datetime.timezone.utc).isoformat()}
print(maildir.deliver(root, box, msg_id, "---json\n" + json.dumps(header, indent=2) + "\n---\n" + body + "\n"))
"""


def deliver(cfg, text, bad, attention=()):
    """Drop the report into a mailbox with the memory server's maildir.py, as the mail root's user:
    the same frontmatter as the digest, and no memory server needed, so a failure of the memory
    server's own venv is still reported."""
    r = cfg.get("report")
    if not r:
        return
    subject, boxes = report_route(r, bad, attention)
    for box in boxes:
        try:
            root_only(r["maildir_py"])
            p = run_as(r["maildir_user"], "027", [sys.executable, "-I", "-S", "-c", DELIVER, r["maildir_py"],
                                                   r["maildir_root"], box, subject], timeout=60,
                       input_text=text)
            must(p, f"deliver the report to {box}")
            log(f"report delivered: {p.stdout.strip()}")
        except Exception as e:
            log(f"report not delivered to {box}: {e}")


def cmd_status(cfg):
    for v in cfg.get("venv", []):
        led = load_ledger(cfg, v["name"])
        last = led["history"][-1] if led["history"] else None
        print(f"{v['name']} ({v['path']}, mode {v['mode']}): last run "
              f"{last['at'] + ' ' + last['outcome'] if last else 'never'}")
        why = refusal(v)
        if why:
            print(f"  audit only: {why}")
        for n, why in sorted(led["user_managed"].items()):
            print(f"  user-managed: {n}: {why}")
        for n, vs in sorted(led["failed"].items()):
            for ver, why in vs.items():
                print(f"  failed here: {n} {ver}: {why}")
        for kind in ("rehearsal_failures", "live_failures"):
            for n, vs in sorted(led.get(kind, {}).items()):
                for ver, k in vs.items():
                    if k < 2:
                        print(f"  one strike ({kind.split('_')[0]}): {n} {ver}")
        for n, why in sorted(v["holds"].items()):
            print(f"  held (config): {n}: {why}")
    return 0


def cmd_adopt(cfg, name):
    v = next((x for x in cfg.get("venv", []) if x["name"] == name), None)
    if not v:
        raise SystemExit(f"no venv named {name!r}")
    led = load_ledger(cfg, name)
    led["freeze"] = {n: d["version"] for n, d in inspect_venv(cfg, v["path"]).items()}
    led["user_managed"] = {}
    Path(cfg["state_dir"]).mkdir(parents=True, exist_ok=True)
    save_ledger(cfg, name, led)
    print(f"{name}: adopted as it is now ({len(led['freeze'])} packages); nothing is user-managed")
    return 0


def cmd_release(cfg, name, pkg):
    led = load_ledger(cfg, name)
    if led["user_managed"].pop(canonicalize_name(pkg), None) is None:
        raise SystemExit(f"{pkg} is not user-managed in {name}")
    save_ledger(cfg, name, led)
    print(f"{name}: {pkg} is the tool's again")
    return 0


def cmd_retry(cfg, name, pkg):
    led = load_ledger(cfg, name)
    key = canonicalize_name(pkg)
    gone = [led.get(k, {}).pop(key, None) for k in ("failed", "rehearsal_failures", "live_failures")]
    if not any(gone):
        raise SystemExit(f"{pkg} has no failed versions in {name}")
    save_ledger(cfg, name, led)
    print(f"{name}: {pkg}'s failed versions are forgotten; the next run tries them again")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    sp = ap.add_subparsers(dest="cmd", required=True)
    for c in ("plan", "run"):
        p = sp.add_parser(c)
        p.add_argument("--venv")
    sp.add_parser("status")
    p = sp.add_parser("adopt")
    p.add_argument("name")
    for c in ("release", "retry"):
        p = sp.add_parser(c)
        p.add_argument("name")
        p.add_argument("pkg")
    a = ap.parse_args()
    cfg = load_config(a.config)
    if a.cmd in ("plan", "run"):
        return cmd_plan_or_run(cfg, a, a.cmd == "run")
    if a.cmd == "status":
        return cmd_status(cfg)
    if a.cmd == "adopt":
        return cmd_adopt(cfg, a.name)
    if a.cmd == "retry":
        return cmd_retry(cfg, a.name, a.pkg)
    return cmd_release(cfg, a.name, a.pkg)


if __name__ == "__main__":
    sys.exit(main())
