#!/usr/bin/env python3
"""layout-check — a component's code may read only variables and absolute paths that
release/docs/LAYOUT.md defines. Whitelist check, not a leak scanner (release-lint does that).

  tools/layout-check.py [--page release/docs/LAYOUT.md] [--component DIR]... [--repo .]

For each component directory (default: the ones the page covers), every `os.environ[...]`,
`os.environ.get(...)`, `os.getenv(...)` name and every shell `${NAME}` / `$NAME` in .py .sh .in
.service .conf .timer files, plus every absolute path literal under /opt /var /etc /srv /run
/home /tmp, is looked up on the page. Anything missing is printed as `component  file:line
KIND  value` and the exit is 1. Runs before a component's MANIFEST line is uncommented and in
the adversarial pass. Names that are a unit's own convention (PATH, HOME, USER, …) and paths
that are the page's own placeholders are ignored.
"""
import argparse, os, re, sys
from pathlib import Path

# Components are discovered, not named: every directory (top level, or one level under portal/) that
# holds code or unit files, minus the docs and release scaffolding. The same default works on the
# private tree and on the public one, whatever a component is called there.
NOT_COMPONENTS = {"docs", "release", "ops", "chorus-init", "tools", "tests", "config", "build", "scripts"}

def discover(repo):
    out = []
    for d in sorted(Path(repo).iterdir()):
        if not d.is_dir() or d.name.startswith(".") or d.name in NOT_COMPONENTS:
            continue
        subs = [d] if d.name != "portal" else [x for x in sorted(d.iterdir()) if x.is_dir() and x.name not in ("portal", "fonts", "optimizations", "build")]
        for sd in subs:
            if any(f.suffix in EXT for f in sd.rglob("*") if f.is_file() and "/tests/" not in f.as_posix()):
                out.append(sd.relative_to(repo).as_posix())
    return out
EXT = (".py", ".sh", ".in", ".service", ".timer", ".conf")
PREFIXES = ("/opt/", "/var/", "/etc/", "/srv/", "/run/", "/home/", "/tmp/")
skip_units_seen = []
pattern_lines_seen = []   # lines marked `# layout-check: pattern` (IOC strings, never paths); printed, never silent
SHELL_IGNORE = {"PATH", "HOME", "USER", "PWD", "SHELL", "TERM", "EDITOR", "LOGNAME", "SUDO_USER",
                "IFS", "OLDPWD", "LANG", "TZ", "TMPDIR", "XDG_RUNTIME_DIR", "SYSTEMD_EXEC_PID"}

def exempt(name):
    """Names a program other than ours defines: the OS, the shell, git, systemd, python."""
    return name in SHELL_IGNORE or name.startswith(("GIT_", "SYSTEMD_", "PYTHON", "LC_", "XDG_", "TMUX"))

def expand_braces(s):
    m = re.search(r"\{([^{}]*)\}", s)
    if not m:
        return [s]
    out = []
    for alt in m.group(1).split(","):
        out += expand_braces(s[:m.start()] + alt + s[m.end():])
    return out

def load_units(text):
    """The Units table: unit base name -> the user it runs as (a `<placeholder>` means a rendered
    unit whose User= is a variable). `x.timer` + `.service` rows cover both."""
    units = {}
    for line in text.splitlines():
        if not line.startswith("| `") or ".service" not in line.split("|")[1] and ".timer" not in line.split("|")[1]:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4 or not re.fullmatch(r"`[a-z<][a-z0-9_<>-]*`", cells[1]):
            continue   # only the Units table has a bare backticked user in its second cell
        user = cells[1].strip("`")
        for m in re.finditer(r"`([a-z][a-z0-9<>-]*)\.(?:service|timer)`", cells[0]):
            units[m.group(1)] = user
        if re.search(r"`\.(?:service|timer)`", cells[0]):   # a row listing bare names + (`.timer` + `.service`)
            for m in re.finditer(r"`([a-z][a-z0-9<>-]*)`", cells[0]):
                units[m.group(1)] = user
    return units

def effective_user(unit_text):
    """systemd runs a unit with no User= line as root: an absent line is root, not unknown."""
    m = re.search(r"^\s*User=(.*)$", unit_text, re.M)
    return m.group(1).strip() if m else "root"

def load_page(path):
    text = Path(path).read_text(encoding="utf-8")
    names, paths = set(), set()
    for tok in re.findall(r"`([^`]+)`", text):
        tok = tok.strip()
        if re.fullmatch(r"[A-Z][A-Z0-9_]+", tok):
            names.add(tok)
        for piece in re.split(r"\s+", tok):          # whitespace only: commas belong to brace lists
            piece = piece.strip(",")
            if piece.startswith("/"):
                for p in expand_braces(piece):
                    p = re.sub(r"<[^>]+>", "<X>", p).rstrip("/")   # strip AFTER expanding: `{scripts/,tools/}` keeps its slashes otherwise
                    if p:
                        paths.add(p)
    return names, paths, load_units(text)

def unit_ok(rel, unit_text, units):
    """A shipped .service must be a row in the Units table and run as that row's user. A unit with
    no user line runs as root under systemd, so a new unit that says nothing is caught, not missed."""
    base = Path(rel).name[:-len(".service")]
    if base not in units:
        return f"unit {base}.service has no row in the Units table"
    want, have = units[base], effective_user(unit_text)
    if want.startswith("<"):
        return None if have.startswith("$") else f"unit {base}.service runs as literal {have!r}; the page says a rendered {want}"
    return None if have == want else f"unit {base}.service runs as {have!r}; the page says {want!r}"

def path_ok(lit, paths):
    lit = re.sub(r"<[^>]+>", "<X>", lit.rstrip("/"))
    if lit in paths:
        return True
    # a literal under a page DIRECTORY is fine (the page names the dir, code names a file in it) —
    # but a bare root like /opt or /tmp, which the page's prose mentions, whitelists nothing
    return any(lit.startswith(p + "/") for p in paths if p.count("/") >= 2)

def site_units(repo):
    """This host's unit files: any SOURCE under ops/site/ that tools/verify-mirror.pairs pairs to a
    live path. ops/site/ is the convention for a site's own units (never discovered, never shipped);
    the pairing is what makes them this host's. Pairing ALONE cannot be the rule: in the PUBLIC tree
    the pairs file pairs the public units, and a rule keyed on pairing would skip exactly the units
    the Units-table check exists for (review, 2026-09-25). Skipped units are counted, never silent."""
    pairs = Path(repo) / "tools/verify-mirror.pairs"
    out = set()
    if pairs.is_file():
        for line in pairs.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            parts = line.split()
            if len(parts) == 2 and parts[1].startswith("ops/site/") and "/systemd/" in parts[1]:
                out.add(parts[1])
    return out

def scan(repo, comp, names, paths, skip_units, units):
    bad = []
    root = Path(repo) / comp
    if not root.is_dir():
        return bad
    for f in sorted(root.rglob("*")):
        if not f.is_file() or f.suffix not in EXT or "/tests/" in f.as_posix() or "/optimizations/" in f.as_posix():
            continue
        rel = f.relative_to(repo).as_posix()
        if rel in skip_units:
            skip_units_seen.append(rel)
            continue
        if f.suffix == ".service":
            problem = unit_ok(rel, f.read_text(encoding="utf-8", errors="replace"), units)
            if problem:
                bad.append((comp, rel, "UNIT", problem))
        for ln, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            s = line.split("#", 1)[0] if f.suffix in (".py", ".sh", ".in", ".service", ".timer", ".conf") else line
            if not s.strip():
                continue
            for n in re.findall(r"""environ(?:\.get)?\(?\[?\s*["']([A-Z][A-Z0-9_]+)["']|getenv\(\s*["']([A-Z][A-Z0-9_]+)["']|\b\w*env\w*\(\s*["']([A-Z][A-Z0-9_]{3,})["']""", s):
                n = n[0] or n[1] or n[2]
                if n not in names and not exempt(n):
                    bad.append((comp, f"{rel}:{ln}", "ENV", n))
            if f.suffix in (".sh", ".in"):
                # shell: only the host-fact shape counts as interface — ${NAME:-default} reads an
                # environment/site.env value; a bare $NAME is a script local (SESSION, THEME, ...)
                for n in re.findall(r"\$\{([A-Z][A-Z0-9_]{2,}):-", s):
                    if n not in names and not exempt(n):
                        bad.append((comp, f"{rel}:{ln}", "ENV", n))
            if f.suffix in (".service", ".timer", ".conf"):
                for n in re.findall(r"^Environment=([A-Z][A-Z0-9_]+)=", s.strip()):
                    if n not in names and not exempt(n):
                        bad.append((comp, f"{rel}:{ln}", "ENV", n))
            if "layout-check: pattern" in line:   # a detection string, not a path the component uses
                pattern_lines_seen.append(f"{rel}:{ln}")
                continue
            for lit in re.findall(r"(?<![\w./-])(/(?:opt|var|etc|srv|run|home|tmp)/[A-Za-z0-9_./<>{}-]*)", s):
                if not path_ok(lit, paths):
                    bad.append((comp, f"{rel}:{ln}", "PATH", lit))
    return bad

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", default=None, help="the layout page (default: docs/LAYOUT.md, or release/docs/LAYOUT.md on the private tree)")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--component", action="append", default=[])
    a = ap.parse_args()
    page = a.page or next((p for p in ("docs/LAYOUT.md", "release/docs/LAYOUT.md") if os.path.isfile(os.path.join(a.repo, p))), None)
    if not page:
        sys.exit("layout-check: no docs/LAYOUT.md (or release/docs/LAYOUT.md) under the repo; pass --page")
    names, paths, units = load_page(os.path.join(a.repo, page))
    comps = a.component or discover(a.repo)
    site = site_units(a.repo)
    bad = []
    for c in comps:
        bad += scan(a.repo, c, names, paths, site, units)
    if pattern_lines_seen:
        print(f"pattern lines skipped (marked `# layout-check: pattern`: detection strings, not paths): "
              f"{len(pattern_lines_seen)} — " + ", ".join(pattern_lines_seen))
    if skip_units_seen:
        print(f"site units skipped (ops/site/ sources paired to live paths in tools/verify-mirror.pairs, never shipped): "
              f"{len(skip_units_seen)} — " + ", ".join(skip_units_seen))
    seen = set()
    for comp, where, kind, val in bad:
        if (comp, kind, val) in seen:
            continue
        seen.add((comp, kind, val))
        print(f"{comp:18} {where:52} {kind:5} {val}")
    print(f"layout-check: page names={len(names)} paths={len(paths)} units={len(units)} components={len(comps)} "
          f"off-page={len(seen)} (first occurrence each)")
    if seen:
        print("NOT ON PAGE")
        return 1
    print("ON PAGE")
    return 0

if __name__ == "__main__":
    sys.exit(main())
