#!/usr/bin/env python3
"""release-lint — prove a ref carries no private strings: tree, history, dates, binaries.

Usage:
  tools/release-lint.py --ref <ref> [--patterns <private patterns file>]
                        [--repo /path/to/repo] [--allow-binary PATH_REGEX]...

Exit 0 and print CLEAN when nothing matches; exit 1 with every hit otherwise.

Scopes. Every pattern belongs to one scope; the private patterns file declares them with
section headers [all], [prose], [code] (default [all]). A blob is PROSE if its extension is
one of .md .txt .rst .html .htm .svg .svgz .pdf .mmd; commit messages, author and committer
fields are PROSE; everything else is CODE. Prose-only patterns are the single common words
whose leak is an inference a reader of prose could draw; code-only patterns are proper nouns
and multi-word shapes. Nothing is exempted by name — a scope narrows where a rule looks, it
never lists a file to skip.

Location allowlists (positional or declared with an owner, never by string):
  - a MANIFEST `vendor` entry (third-party file with owner + upstream sha, passed as --vendor)
    is scanned with the built-in shapes only;
  - trailer lines of a commit message (the final paragraph when every line is `Key: value`)
    are scanned with the built-in shapes only, so credits may name people there;
  - the block under a `# Credits` heading in a Markdown file, likewise.

Decoding. Gzip blobs are inflated; PDF FlateDecode streams are inflated; base64 `data:` URIs
are decoded (and inflated if gzip) and their text scanned; a decoded payload that is still not
text is a hit. Any undecodable blob is a hit unless --allow-binary matches its path — the
build passes the MANIFEST's `binary` entries, which each carry an owner.

What it scans:
  tree     every blob at the ref, decoded as above, line by line; every path
  history  every commit: author, committer, date offsets (must be +0000), message

DENYLIST (second input, stricter contract; adversarial reviewer's constraints 2026-09-25).
  A list of regexes that must never appear anywhere on the ref — tree, paths, commit
  messages, trailers, credits, vendored files: no allowlist applies. It lives OUTSIDE this
  repository (a private repo of the reviewer who owns the matter) and is read by path from
  $RELEASE_DENYLIST or --denylist. FAIL CLOSED: variable unset, file missing or unreadable,
  or zero entries → exit 1, never a zero-hit PASS; a lint that cannot read its list must not
  say CLEAN. COUNT-ONLY on every output channel: a hit prints the term's INDEX in the list, a
  count, and the public path or commit sha — never the term, never a sample; a path that
  itself matches is printed as its listing index. Errors in this stage print a fixed line and
  withhold details, because a traceback could echo a matched string.
"""
import argparse, base64, gzip, os, re, subprocess, sys, zlib

PROSE_EXT = (".md", ".txt", ".rst", ".html", ".htm", ".svg", ".svgz", ".pdf", ".mmd")

BUILTIN = [  # name-free shapes; scope all
    (r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "rfc1918-10"),
    (r"\b172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b", "rfc1918-172"),
    (r"\b192\.168\.\d{1,3}\.\d{1,3}\b", "rfc1918-192"),
    (r"\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b", "cgnat-100.64"),
    (r"\bfd[0-9a-f]{2}:[0-9a-f:]+", "ula-ipv6"),
    (r"/home/[a-z_][a-z0-9_-]*\b", "home-path"),
    (r"\b[a-z0-9-]+\.[a-z0-9-]+\.ts\.net\b", "overlay-host"),
    (r"\b[a-z0-9._-]+@[a-z0-9.-]+\.(local|lan|home|internal)\b", "local-email"),
    (r"claude\.ai/code/session_[A-Za-z0-9]+", "session-url"),
    (r"^Claude-Session:", "session-trailer"),
]

def sh(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True).stdout

def load_patterns(path):
    out = []
    scope = "all"
    if not path:
        return out
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            m = re.fullmatch(r"\[(all|prose|code)\]", s)
            if m:
                scope = m.group(1)
                continue
            out.append((s, f"patterns:{n}", scope))
    return out

def decode_blob(path, blob):
    """Return (text or None, extra_texts, note). extra_texts are decoded embedded payloads."""
    if blob[:2] == b"\x1f\x8b":
        try:
            blob = gzip.decompress(blob)
        except OSError:
            return None, [], "gzip-corrupt"
    extras = []
    if blob[:5] == b"%PDF-" or path.lower().endswith(".pdf"):
        raw = blob.decode("latin-1")
        for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", blob, re.S):
            try:
                extras.append(("pdf-stream", zlib.decompress(m.group(1)).decode("latin-1")))
            except zlib.error:
                pass
        return raw, extras, None
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        return None, [], "binary"
    for m in re.finditer(r"data:[a-z0-9.+/-]+;base64,([A-Za-z0-9+/=\s]+)", text, re.I):
        try:
            payload = base64.b64decode(re.sub(r"\s", "", m.group(1)), validate=False)
        except Exception:
            extras.append(("data-uri", "<undecodable base64>"))
            continue
        if payload[:2] == b"\x1f\x8b":
            try:
                payload = gzip.decompress(payload)
            except OSError:
                pass
        try:
            extras.append(("data-uri", payload.decode("utf-8")))
        except UnicodeDecodeError:
            extras.append(("data-uri-opaque", ""))
    return text, extras, None

def scan_lines(text, pats, scope, where, kind, hits, builtin_only=False):
    for ln, line in enumerate(text.splitlines(), 1):
        for rx, tag, psc in pats:
            if builtin_only and not tag.startswith("builtin"):
                continue
            if psc != "all" and psc != scope:
                continue
            m = rx.search(line)
            if m:
                lo, hi = max(0, m.start() - 60), min(len(line), m.end() + 60)
                sample = ("…" if lo else "") + line[lo:hi].strip() + ("…" if hi < len(line) else "")
                hits.append((f"{where}:{ln}", kind, tag, sample[:200]))

def split_trailers(msg):
    paras = re.split(r"\n\s*\n", msg.strip())
    if len(paras) > 1 and all(re.match(r"^[A-Za-z][A-Za-z-]*: ", l) for l in paras[-1].splitlines()):
        return "\n\n".join(paras[:-1]), paras[-1]
    return msg, ""

def split_credits(text):
    """Return (body_lines_text, credits_text) for Markdown: lines under a '# Credits' heading."""
    body, credits, in_credits = [], [], False
    for line in text.splitlines():
        if re.match(r"^#{1,6}\s+credits\b", line, re.I):
            in_credits = True
            credits.append(line)
            continue
        if in_credits and re.match(r"^#{1,6}\s", line):
            in_credits = False
        (credits if in_credits else body).append(line)
    return "\n".join(body), "\n".join(credits)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", required=True)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--patterns", default=None)
    ap.add_argument("--vendor", action="append", default=[],
                    help="path regex of vendored third-party files (MANIFEST vendor entries, each with owner + "
                         "upstream sha): scanned with the built-in shapes only")
    ap.add_argument("--allow-binary", action="append", default=[],
                    help="path regex of blobs that may be non-text (the MANIFEST's binary entries)")
    ap.add_argument("--denylist", default=os.environ.get("RELEASE_DENYLIST"),
                    help="path of the external deny list (default $RELEASE_DENYLIST); required — "
                         "the lint fails closed without it")
    a = ap.parse_args()
    deny_texts = []   # (kind, where, text) — everything the deny list is matched against

    pats = [(re.compile(p, re.I | re.M), "builtin:" + tag, "all") for p, tag in BUILTIN]
    pats += [(re.compile(p, re.I | re.M), tag, sc) for p, tag, sc in load_patterns(a.patterns)]
    allow_bin = [re.compile(x) for x in a.allow_binary]
    vendor = [re.compile(x) for x in a.vendor]
    hits = []

    # --- tree
    listing = sh(a.repo, "ls-tree", "-r", "-z", a.ref).decode().split("\0")
    nfiles = 0
    for entry in (l for l in listing if l):
        meta, path = entry.split("\t", 1)
        mode, kind, sha = meta.split()
        for rx, tag, psc in pats:
            if rx.search(path):
                hits.append((f"{a.ref}:{path}", "PATH", tag, path))
        deny_texts.append(("PATH", f"listing#{len(deny_texts)}", path))
        if kind != "blob":
            continue
        nfiles += 1
        scope = "prose" if path.lower().endswith(PROSE_EXT) else "code"
        text, extras, note = decode_blob(path, sh(a.repo, "cat-file", "-p", sha))
        if text is None:
            if not any(x.search(path) for x in allow_bin):
                hits.append((path, "TREE", f"binary-blob({note})", "undecodable blob not a MANIFEST binary entry"))
            continue
        deny_texts.append(("TREE", path, text))
        for _etag, _etext in extras:
            if isinstance(_etext, str):
                deny_texts.append(("TREE", path, _etext))
        if any(x.search(path) for x in vendor):
            scan_lines(text, pats, scope, path + "[vendor]", "TREE", hits, builtin_only=True)
            continue
        if path.lower().endswith(".md"):
            text, credits = split_credits(text)
            scan_lines(credits, pats, scope, path + "[credits]", "TREE", hits, builtin_only=True)
        scan_lines(text, pats, scope, path, "TREE", hits)
        for etag, etext in extras:
            if etag == "data-uri-opaque":
                hits.append((path, "TREE", "data-uri-opaque", "base64 data: URI decodes to non-text"))
            else:
                scan_lines(etext, pats, "prose", f"{path}[{etag}]", "TREE", hits)

    # --- history
    fmt = "%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%ai%x1f%ci%x1f%B%x1e"
    raw = sh(a.repo, "log", f"--format={fmt}", a.ref).decode("utf-8", "replace")
    ncommits = 0
    for rec in raw.split("\x1e"):
        if not rec.strip():
            continue
        ncommits += 1
        h, an, ae, cn, ce, ad, cd, msg = rec.lstrip("\n").split("\x1f", 7)
        for field, val in (("author-date", ad), ("committer-date", cd)):
            if not val.strip().endswith("+0000"):
                hits.append((h[:10], f"HIST/{field}", "commit-tz", val.strip()))
        scan_lines(f"{an} <{ae}>", pats, "prose", h[:10], "HIST/author", hits)
        scan_lines(f"{cn} <{ce}>", pats, "prose", h[:10], "HIST/committer", hits)
        body, trailers = split_trailers(msg)
        scan_lines(body, pats, "prose", h[:10], "HIST/message", hits)
        scan_lines(trailers, pats, "prose", h[:10] + "[trailers]", "HIST/message", hits, builtin_only=True)
        deny_texts.append(("HIST", h[:10], f"{an} <{ae}>\n{cn} <{ce}>\n{msg}"))

    for where, kind, tag, sample in hits:
        print(f"{kind:14} {tag:20} {where}  |  {sample}")
    print(f"scanned ref={a.ref} files={nfiles} commits={ncommits} patterns={len(pats)}")

    deny_rc = denylist_stage(a.denylist, deny_texts)

    if hits or deny_rc:
        print(f"DIRTY: {len(hits)} hit(s)" + ("" if not deny_rc else " + deny list (see DENYLIST lines)"))
        return 1
    print("CLEAN")
    return 0


def denylist_stage(path, texts):
    """Return 0 only when the list was read, had entries, and matched nothing. Every other
    outcome returns 1. Prints counts and indices only; never a term, never a sample."""
    if not path:
        print("DENYLIST: FAIL-CLOSED — $RELEASE_DENYLIST unset and --denylist not given; no list, no CLEAN")
        return 1
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = [l.rstrip("\n") for l in fh]
    except OSError:
        print(f"DENYLIST: FAIL-CLOSED — list at {path} missing or unreadable")
        return 1
    entries = [l.strip() for l in raw if l.strip() and not l.lstrip().startswith("#")]
    if not entries:
        print(f"DENYLIST: FAIL-CLOSED — list at {path} has zero entries")
        return 1
    terms = []
    for i, e in enumerate(entries, 1):
        try:
            terms.append((i, re.compile(e, re.I)))
        except re.error:
            print(f"DENYLIST: FAIL-CLOSED — term #{i} does not compile (text withheld)")
            return 1
    try:
        # which paths are themselves hits: those are never printed, only their listing index
        path_hit = {}
        for kind, where, text in texts:
            if kind == "PATH":
                for i, rx in terms:
                    if rx.search(text):
                        path_hit[text] = where
        counts = {}   # (term index, where) -> count
        for kind, where, text in texts:
            for i, rx in terms:
                n = len(rx.findall(text))
                if n:
                    label = where if kind == "HIST" else (
                        f"<path withheld, {path_hit[where]}>" if where in path_hit else
                        (f"<path withheld, {where}>" if kind == "PATH" else where))
                    counts[(i, kind, label)] = counts.get((i, kind, label), 0) + n
        for (i, kind, label), n in sorted(counts.items()):
            print(f"DENYLIST       term#{i:<4} {kind:5} {n} hit(s) in {label}")
        total = sum(counts.values())
        print(f"DENYLIST: {path} entries={len(terms)} hits={total}")
        return 1 if total else 0
    except Exception:
        print("DENYLIST: FAIL-CLOSED — internal error in the deny-list stage (details withheld)")
        return 1

if __name__ == "__main__":
    sys.exit(main())
