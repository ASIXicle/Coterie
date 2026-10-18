#!/usr/bin/env python3
"""release-lint — prove a ref carries no private strings: tree, history, dates, binaries.

Usage:
  tools/release-lint.py --ref <ref> [--patterns <private patterns file>]
                        [--repo /path/to/repo] [--allow-binary PATH_REGEX]...
                        [--vendor PATH_REGEX]... [--no-exclusions [--expect-binary PATH_REGEX]...]

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

Binary metadata. A binary's bytes are not text, but its metadata is, and a waiver must never
hide it: every binary, waived or not, has its metadata decoded and fed to both stages (the deny
list, and every pattern whatever its scope). PNG: any chunk outside IHDR/PLTE/IDAT/IEND is a hit
and its text and EXIF are decoded; the critical chunks are held to their shape (IHDR 13 bytes,
PLTE read, IEND empty, IDAT one zlib stream with nothing after it, nothing after IEND). JPEG:
every marker to EOI, APPn and COM read wherever they sit, nothing after EOI. Fonts (ttf, otf,
woff, woff2): every name record and the printable runs of every table, read through fontTools
(woff2 also needs brotli; both are this lint's requirements). Pixels and entropy-coded image data
are not scanned. A binary of any other type, or one whose metadata fails to decode, is a hit:
metadata that cannot be read cannot be cleared.

No-exclusion pass (--no-exclusions, with --expect-binary once per MANIFEST binary row). The same
scan with no vendor scope and no binary waiver: vendored files get every pattern, and the run is CLEAN only
when its hits are exactly one `binary-blob` per expected row and nothing else. The build and
publish scripts run it after the normal pass; a release records both lines.

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
import argparse, base64, gzip, io, os, re, subprocess, sys, zlib

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

PNG_SIG = b"\x89PNG\r\n\x1a\n"
FONT_MAGIC = (b"\x00\x01\x00\x00", b"OTTO", b"true", b"wOFF", b"wOF2")

def runs(data):
    """Printable runs of 4+ characters, single-byte and UTF-16LE (EXIF's XP* tags are UTF-16)."""
    one = [m.decode("latin-1") for m in re.findall(rb"[\x20-\x7e]{4,}", data)]
    two = [m.decode("utf-16-le") for m in re.findall(rb"(?:[\x20-\x7e]\x00){4,}", data)]
    return "\n".join(one + two)

def png_meta(blob):
    """Walk every chunk: IHDR is 13 bytes, PLTE at most 256 entries (its bytes are read), IEND
    carries nothing, and the IDAT chunks hold one zlib stream with nothing after it. Any other
    chunk is refused and its text decoded. The pixels themselves are not scanned."""
    texts, bad, idat = [], [], []
    i = 8
    while True:
        if i + 12 > len(blob):
            bad.append("png-truncated")
            break
        n, t = int.from_bytes(blob[i:i + 4], "big"), blob[i + 4:i + 8]
        data = blob[i + 8:i + 8 + n]
        if len(data) != n:
            bad.append("png-truncated")
            break
        i += 12 + n
        if t == b"IEND":
            if n:
                bad.append("png-IEND-length")
                texts.append(runs(data))
            if i < len(blob):
                bad.append("png-bytes-after-IEND")
                texts.append(runs(blob[i:]))
            break
        if t == b"IHDR":
            if n != 13:
                bad.append("png-IHDR-length")
        elif t == b"IDAT":
            idat.append(data)
        elif t == b"PLTE":
            if n % 3 or n > 768:
                bad.append("png-PLTE-length")
            texts.append(runs(data))
        else:
            bad.append("png-chunk:" + t.decode("latin-1"))
            key, _, rest = data.partition(b"\0")
            try:
                if t == b"zTXt":
                    rest = zlib.decompress(rest[1:])
                elif t == b"iTXt" and rest[:1] == b"\x01":
                    head, _, body = rest[2:].partition(b"\0")
                    tkey, _, body = body.partition(b"\0")
                    rest = head + b" " + tkey + b" " + zlib.decompress(body)
            except zlib.error:
                bad.append("png-undecodable:" + t.decode("latin-1"))
            texts.append(key.decode("latin-1") + "\n" + rest.decode("utf-8", "replace") + "\n" + runs(rest))
    z, pending = zlib.decompressobj(), b"".join(idat)
    try:
        while pending and not z.eof:           # inflate in bounded steps; only the stream's end matters
            z.decompress(pending, 1 << 20)
            pending = z.unconsumed_tail
        if not z.eof:
            bad.append("png-IDAT-unterminated")
        if z.unused_data:
            bad.append("png-bytes-after-IDAT")
            texts.append(runs(z.unused_data))
    except zlib.error:
        bad.append("png-IDAT-undecodable")
    return texts, bad

def jpeg_meta(blob):
    """Walk every marker to EOI: fill bytes skipped, TEM/RSTn/SOI have no length, APPn and COM read
    wherever they sit (between scans too), entropy-coded data stepped over, bytes after EOI refused."""
    texts, bad = [], []
    i = 2
    while True:
        if i >= len(blob):
            bad.append("jpeg-no-EOI")
            break
        if blob[i] != 0xFF:
            bad.append("jpeg-undecodable")
            break
        while i < len(blob) and blob[i] == 0xFF:
            i += 1
        if i >= len(blob):
            bad.append("jpeg-undecodable")
            break
        m = blob[i]
        i += 1
        if m == 0xD9:
            if i < len(blob):
                bad.append("jpeg-bytes-after-EOI")
                texts.append(runs(blob[i:]))
            break
        if m == 0x01 or 0xD0 <= m <= 0xD8:
            continue
        n = int.from_bytes(blob[i:i + 2], "big")
        if i + 2 > len(blob) or n < 2 or i + n > len(blob):
            bad.append("jpeg-truncated")
            break
        if 0xE0 <= m <= 0xEF or m == 0xFE:
            texts.append(runs(blob[i + 2:i + n]))
        i += n
        if m == 0xDA:
            while i < len(blob) and not (blob[i] == 0xFF and i + 1 < len(blob)
                                         and blob[i + 1] != 0 and not 0xD0 <= blob[i + 1] <= 0xD7):
                i += 1
    return texts, bad

def binary_meta(blob):
    """Return (texts, refusals) for a binary blob: its metadata as text for both stages, and every
    reason it fails closed (see "Binary metadata" in the module docstring)."""
    if blob[:8] == PNG_SIG:
        return png_meta(blob)
    if blob[:2] == b"\xff\xd8":
        return jpeg_meta(blob)
    if blob[:4] not in FONT_MAGIC:
        return [], ["unknown-type"]
    try:
        from fontTools.ttLib import TTFont
        font = TTFont(io.BytesIO(blob), lazy=True)
        texts = ["\n".join(r.toUnicode(errors="replace") for r in font["name"].names)]
        return texts + [runs(font.reader[tag]) for tag in font.reader.keys()], []
    except ImportError:
        return [], ["font-needs-fontTools-and-brotli"]
    except Exception:
        return [], ["font-undecodable"]

def scan_lines(text, pats, scope, where, kind, hits, builtin_only=False):
    for ln, line in enumerate(text.splitlines(), 1):
        for rx, tag, psc in pats:
            if builtin_only and not tag.startswith("builtin"):
                continue
            if psc != "all" and psc != scope and scope != "meta":   # metadata gets every pattern
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
    ap.add_argument("--no-exclusions", action="store_true",
                    help="the no-exclusion pass: --vendor and --allow-binary are ignored, and CLEAN means "
                         "the hits are exactly one binary-blob per --expect-binary and nothing else")
    ap.add_argument("--expect-binary", action="append", default=[],
                    help="with --no-exclusions: path regex of one MANIFEST binary row")
    ap.add_argument("--denylist", default=os.environ.get("RELEASE_DENYLIST"),
                    help="path of the external deny list (default $RELEASE_DENYLIST); required — "
                         "the lint fails closed without it")
    a = ap.parse_args()
    deny_texts = []   # (kind, where, text) — everything the deny list is matched against

    pats = [(re.compile(p, re.I | re.M), "builtin:" + tag, "all") for p, tag in BUILTIN]
    pats += [(re.compile(p, re.I | re.M), tag, sc) for p, tag, sc in load_patterns(a.patterns)]
    expect = [re.compile(x) for x in a.expect_binary]
    if expect and not a.no_exclusions:
        sys.exit("--expect-binary belongs to the --no-exclusions pass")
    allow_bin = [] if a.no_exclusions else [re.compile(x) for x in a.allow_binary]
    vendor = [] if a.no_exclusions else [re.compile(x) for x in a.vendor]
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
        raw = sh(a.repo, "cat-file", "-p", sha)
        text, extras, note = decode_blob(path, raw)
        if text is None:
            if not any(x.search(path) for x in allow_bin):
                hits.append((path, "TREE", f"binary-blob({note})", "undecodable blob not a MANIFEST binary entry"))
            meta, refused = binary_meta(raw)
            for why in refused:
                hits.append((path, "TREE", f"binary-meta({why})", "metadata refused or unreadable"))
            for mtext in meta:
                deny_texts.append(("TREE", path, mtext))
                scan_lines(mtext, pats, "meta", path + "[meta]", "TREE", hits)
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

    if a.no_exclusions:
        waived = [h for h in hits if h[2].startswith("binary-blob") and any(x.search(h[0]) for x in expect)]
        per_row = [sum(1 for h in waived if x.search(h[0])) for x in expect]
        hits = [h for h in hits if h not in waived]
        print(f"NO-EXCLUSIONS: {len(waived)} binary-blob hit(s) for {len(expect)} MANIFEST binary row(s); "
              f"{len(hits)} other hit(s)")
        if any(n != 1 for n in per_row):
            hits.append(("<rows>", "TREE", "binary-rows", "a binary row matched no blob, or more than one"))
            print("NO-EXCLUSIONS: a binary row matched no blob, or more than one")

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
