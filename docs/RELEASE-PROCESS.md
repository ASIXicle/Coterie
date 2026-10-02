# Release process — how a release is built, staged, checked and signed

## How the public tree is made

- **`main`** is the living development home. It is site-specific: real agent names, real
  hosts, full history, review artifacts. It is never sanitized and never public.
- **The public tree is generated, never branched.** `tools/release-build.py` reads
  `release/MANIFEST`, an allowlist of every path that ships with a mode and an owner, and
  `release/rename.map`, and emits the tree from the committed state of `main`. A path not in
  the manifest never ships. A manifest path missing from `main` fails the build. The
  hand-written public docs (this file, the README, INSTALL, the diagrams) live under
  `release/` on `main` and are inputs to the build, not outputs of it.
- **The build and publish scripts are operator-side.** `tools/release-build.py` and
  `tools/release-publish.py` live in the working repository and do not ship: they read the
  manifest, the maps and the pin from it. What ships is the gate: `tools/release-lint.py`, which a
  fork can run with its built-in shapes and a deny list of its own.
- **A build lands in a repository of its own, never on a branch of the working one.**
  `tools/release-publish.py` builds from the working repository's HEAD, and refuses unless that
  commit is already on the pushed `origin/main` (`--source-ref`). It writes the emitted tree as one
  commit under the project identity, lints that commit where it will live, and refuses to push
  if any commit in the target also exists in the working repository. No object store is
  shared, so no push out of the target can carry the working `main`.
- **There are two such repositories, and they never mix.** The **release repository** takes
  only builds whose lint is `CLEAN`, on `main`: every ref in it is fit to publish, and what a
  reviewer reads there is exactly what would ship. The **test repository** takes any build, on
  `staging`, for a test install or a review. The script takes them under two different
  argument names, and each kind is an allowlist over every ref on the remote: a release
  repository holds `main` and tags, a test repository holds `staging`, and a target carrying
  anything else (another branch, a note, a pull-request ref) is refused.
  A rule about one branch would not be enough: a plain clone, a mirror push or
  an import carries whatever the repository holds.
- **Publication is the operator's push of the release repository's `main`** to the public
  remote: one commit per release, nothing squashed or rewritten on the way.

How a file crosses depends on what it is. Hand-written public docs are copied. Prose and
configuration cross through a rename map. **Code crosses through a separate code map of literal,
word-bounded, exact-case tokens**: every rule must match at least one shipped file or the build
fails (a dead rule means the code moved and the map did not), and the output is syntax-checked
before it is committed, so a rename that breaks code fails the build rather than the reader.
**Third-party files cross as vendored entries** that carry an owner, the upstream URL and a
sha256 the build verifies; the lint scans them for site-fact shapes only, since a minified
library cannot be reworded, and the owner answers for every hit that exemption allows. A file
that a third-party tool generated from our own sources (a stylesheet built from our templates)
is not third-party: we can reword it, so it crosses as `copy` under every rule. Nothing of ours
is ever appended to a vendored file. v0.1.0 shipped one of our comments that way.

**Never merge, cherry-pick or rebase from `main` into any public-facing ref.** One merge
carries every private path, name and message across. A change on `main` that the public
tree needs is either already covered by a manifest line, or gets one.

This is the one way this project differs from the DSVP release model. DSVP's `RELEASE` is a
fast-forward graft of `main` with internal docs removed by pattern, because DSVP's source is
already generic. This scaffold's source is not, so the tree is generated through an allowlist.

## Refs and what they promise

| ref | promise |
|---|---|
| `main` (working repo) | Everything. Private. |
| `staging` (test repo) | The latest test build, lint green or not: for review and for a test install. One commit, replaced by each build. Never published, and never in the release repository. |
| `main` (release repo) | The last signed release plus at most one candidate. Lint green by construction: the publish script moves it only on `CLEAN` under the pinned deny list, and nothing else enters this repository. A candidate is parented on the newest tag and is replaced by the next build. `CLEAN` is not signed: the tip is never what gets published. Not public. |
| a tag (release repo) | A signed release. Lightweight, placed by the signer on the built commit named in the sign-off note. |
| public repository `main` | Every commit is a signed release: lint green, adversarial pass done, sign-off recorded. |

Signing means: the lint is green on the built ref, the adversarial reviewer has attacked the
lint's blind spots for this cut and found nothing in a tree whose sha they named, the built
tree is that tree, and the signer records the build's source sha, the built sha, the tree sha
and the lint output in the sign-off note.

## What must be TRUE before a cut

1. `release-lint` green on the built ref — tree, history (author, committer, dates,
   message trailers) and binaries, not just the working copy — with the deny list: a file
   OUTSIDE every repository, placed by the signer at a root-only path on the signing machine
   (root:root 0600) and read by that one principal. The lint fails closed without it, and its
   `DENYLIST:` summary line names the file it read and the entry count. For the release
   repository the publish script goes further and accepts only the list whose sha256 is pinned
   on the working `main` (step 0 below), so a build linted under a synthetic list cannot reach it.
2. Every commit on the built ref authored and committed under the project identity with
   UTC dates. The build does this; a hand commit on the ref is a defect.
3. No transcripts, identities, calibration notes, private review artifacts or queue
   excerpts. Absent, not redacted: a `[REDACTED]` in a public file is an invitation to ask.
4. No site facts: no RFC-1918 or CGNAT addresses, no hostnames, no Unix usernames, no
   non-default ports, no overlay-network details. No site's own configuration ships as an example; templates carry
   `<PLACEHOLDER>`s and say so at the top.
5. Every diagram has its source in the tree: either a generator file beside the rendered SVG
   that reproduces it, or a hand-authored SVG that is its own source (the five in
   `docs/architecture/` are the latter). No bitmaps.
   **One exception to items 4 and 5, by the operator's decision:** the two portal screenshots in
   the README. A screenshot is a bitmap by nature; both are declared `binary` in the manifest,
   and they show a test machine as the portal displays it: its hostname in the page header and
   in the agents' own first-boot text, the example agent names, and, in one agent's reply, the
   name of an agent credited in the README. No lint reads pixels: what
   is visible in them was looked at and accepted by the operator, not checked by a tool.
6. A stranger can follow `INSTALL.md` from an empty host to a running pane. Verified by
   someone who did not write it.
7. License chosen and present.

If any item fails, the cut waits.

## Cutting a release

One step per message, each with its pass line. Steps 0 to 3 put one candidate in the release
repository; 4 to 7 sign it, look it over, publish it and read it back.

0. **The deny list is in place and pinned.** The signer installs the list root-only on the
   signing machine (`root:root 0600`, in a `0700` directory) and commits its `sha256sum` line
   verbatim as `release/denylist.sha256` on the working `main`, then pushes it. Pass line:
   `sha256sum <list>` equals the committed pin. Done once per list; a changed list is a new pin.
1. **Test build.** `git pull --rebase`, push `main`, then
   `tools/release-publish.py --test-remote <test repository>`, run by anyone. Pass line: the
   record, whose `tree` line is the sha the review reads. A build run by an account that cannot
   read the deny list stages as not `CLEAN`; that is expected here, and the reviewer reads the
   tree, not the verdict.
2. **Adversarial pass** on that tree (for a later release, on the diff since the last signed
   build) → a written "nothing found" that **names the tree sha it read**, or findings, each
   fixed on `main` (manifest, map, or the source) as its own commit, and back to 1. A verdict
   that names no tree sha is not a verdict.
3. **Release build**, run by the principal that can read the deny list:
   `tools/release-publish.py --remote <release repository> --expect-tree <tree sha from step 2>`
   → `emitted N file(s) from main <sha>`, the lint's `scanned …` and `DENYLIST: … entries=…
   hits=0` lines and its bare `CLEAN`, then `PUSHED main` and the record (source sha, built sha,
   tree, file count, the first eight hex digits of the deny list's pin, so each sign-off note
   says which list gated it). A build whose tree is not the reviewed one STOPs before anything
   is pushed. A build that is not `CLEAN` prints `NOT PUSHED`. Either way the cut goes back to
   step 1. The script uses one deny list only: the file whose sha256 equals the pin committed at
   `release/denylist.sha256` on the working `main`. It ignores `$RELEASE_DENYLIST`, and it refuses
   to start for the release repository under any other list or none, because the lint says
   `CLEAN` under any readable list and the word alone does not say which one was read.
4. **Sign.** The signer reads the release repository's `main` against the record and records the
   sign-off note (source sha, built sha, tree sha and the reviewer who named it, lint output,
   date). Then the signer puts a **lightweight** tag on the built commit and pushes it to the
   release repository:
   `git tag <tag> <built sha>`, `git push <release repository> refs/tags/<tag>`, and
   `git ls-remote <release repository> refs/tags/<tag>` → the built sha (anything else is a
   STOP). Never `-a` or `-s`: an annotated tag is an object of its own that carries the
   tagger's name, address and date, and neither the lint nor step 7 reads it. The tag name is
   `v<MAJOR>.<MINOR>.<PATCH>` and nothing else, because it reaches the public remote and no lint
   reads it; the publish script refuses to build into a release repository that carries any
   other tag name. The tag is the release. Later builds take their parent from the release
   repository's newest tag, never from `main`'s tip, so a tag left only in the signer's clone
   means the next build has no parent and replaces `main`. A build that was `CLEAN` but never
   signed is replaced and is no ancestor of anything.
5. **Look-over** — the operator's, on their own machine, in the clone that step 6 pushes from:
   `git clone --single-branch --branch <tag> <release repository> <dir>`; in it,
   `git cat-file -t <tag>` → `commit` (anything else is a STOP),
   `git rev-parse <tag>^{commit}` → the built sha in the sign-off note (anything else is a
   STOP), the tag name has the form above (anything else is a STOP), and `git log` shows one
   commit per release and nothing else. Then read the tree as a stranger would.
6. **Publish** — the operator's action, from that same clone, never from an agent seat (no
   agent credential reaches the public remote). What is published is the tag, not `main`:
   `git push <public remote> <tag>^{commit}:refs/heads/main <tag>` over SSH. The publish
   script already refused any shared ancestry with the working repository in step 3, the
   clone holds no other ref, and nothing is rewritten between sign-off and push.
7. **Read the public repository back** with a stranger's eye: README renders, the SVG loads,
   `git log` shows one identity, UTC dates, and no private string.

## Public identity

Every built and published commit is authored and committed as the project's public identity,
`ASIXicle <APPsix@protonmail.com>`, set by the build script and never by a person's git
config, with UTC dates. The build appends `Co-Authored-By` trailers: one per model in use, and one
per credited agent, from a list declared on the working `main` (a reviewer owns no manifest line,
so credit is declared, not derived from the owner column). The build fails if a manifest owner
is missing from that list. The names in the README's Credits section are generated from the same
list at build time, so the README and the trailers cannot disagree. Agent names are legal
in exactly two places, both positional: commit trailers and the README's Credits section.
Anywhere else on the public tree a name is a lint defect.

Trailer addresses use the reserved `.invalid` domain, which no registrar can issue, because a
name in a resolvable address is a link to whoever owns that account: the GitHub no-reply form
resolved one agent's name to an unrelated organisation. **Known lint blind spot:** the lint
checks where a name sits, not whose account an address resolves to. That check is the
adversarial reviewer's, by hand, on every new address form.
