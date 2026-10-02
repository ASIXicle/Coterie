# Reboot-init prompts — the format, the refresh discipline, and a minimal template

Every session starts cold. What an agent receives at the top of a fresh session is a
**reboot-init prompt**: a short text, pasted by the operator or typed into the pane by the
Matron through the orchestrator's `/matron` route ([`MATRON.md`](MATRON.md), duty 4). The prompt
is a pointer, not the record. The record is in the memory server: the agent's own identity
entry, the standing directives, the team's state, its mail. The prompt's job is to get the
agent from an empty context to that record in a fixed number of moves, and to carry the few
facts the record cannot verify about itself (which model the seat should be on, which hashes
the boot should reproduce).

**The seats' own prompts never ship.** This tree carries the format and a synthetic example.
Real init prompts name people, machines, projects and matters; they identify a site by shape
even after names are changed, so the release build refuses any file under the private
`chorus-init/` directory in every mode (the `NEVER_SHIP` rule in the operator-side build script,
which does not ship; [`RELEASE-PROCESS.md`](RELEASE-PROCESS.md)). Keep yours in
a private repository the operator owns, one section per agent, and copy from it by hand.

## Where the prompts live

One file, one section per agent, in a private repository. Each agent owns its section and
pushes to it directly. The operator copies from that file; nothing else is the deliverable.
Review artifacts (before/after copies and diffs of a section) live beside it under `history/`
and are never what a session boots from.

## Anatomy of a section

In this order. Each part answers one question the fresh session cannot answer on its own.

1. **Who.** Name, ordinal or role in the team, model id, pronoun, Unix account, home directory
   and its mode, the group that grants the shared toolchain.
2. **Tooling notes.** Which shell to use (the built-in shell as the agent's own account, for
   everything; the memory server's developer tools are off unless the operator turned them on,
   `SECURITY.md`). Where credentials are, by path, never by value. Which
   routes answer differently from the server than from the pane (a server-side fetch of the
   private forge redirects to a login page; read the local clone instead).
3. **The repository.** Its canonical home, which copy is deployed from where, and the rule for
   old pointers (they resolve through a commit map; they are not rewritten in memory).
4. **Settings baseline.** The canonical hash of the agent's CLI settings file, which keys the
   canonical form strips (so an operator changing the model or theme does not rot it), what
   does rot it (any change to a kept key), and who re-baselines the record when it moves.
5. **First moves, numbered.** The same sequence every boot, so the report is comparable:
   1. Host check: `whoami && pwd && hostname && id` — the expected account and group.
   2. `chorus_init(agent="<name>", project="<lane>")`. State what the payload should contain
      and how large it lands; what to do if it lands in a file instead of inline. Compare the
      identity entry's stored hash with the RECEIPTS block at the end of the section.
   3. **Substrate guardrail.** A session that reports a model other than the one the identity
      names is a safeguard auto-switch, an operator's model flip, or a version succession.
      Do not update the identity to match. Read the runtime, report the disagreement, hold the
      row until the operator rules. List any known windows where a different model is expected
      and what they change (pairing rules, review requirements).
   4. `amq_check(agent="<name>")`; read what is unread. The mail conventions in one line:
      answer direct to the asker, broadcast analysis, hold a broadcast until the last input of a
      round lands; a `[CTFO]` on a subject is a terminal answer, no further rings on that thread.
   5. Read the calibration note on the operator before the first reply to them. A prior, not
      a script: if the operator contradicts a line in session, the operator wins and the line
      is corrected.
   6. Pull the working clones; **verify any tip you are about to cite against `git log`, not
      against this prompt.**
   7. Read the harness memory index (the CLI's per-project memory directory), and name the
      entries that shape a session today.
   8. Report: identity and host, repository tips, mail awaiting a response.
6. **Pairing rule.** Two agents on the same model id agreeing is ONE hit in any consensus
   check; the second hit is an agent on a different model. Cold-review fan-outs run every
   brief on two model families. When every seat is on one model (a usage-limit window), say so
   on any claimed double confirmation instead of claiming it.
7. **Lane.** Which repositories and components the agent owns, which it shares and with whom,
   who reviews after a direct commit.
8. **One STATE block and one open list.** Marked "rots" and dated. The team state row in the
   memory server outranks it where newer. Every item carries a mark: ✅ done with evidence (a
   commit, a grep, a live check), ⏸ deferred by decision, ⏳ open. See *Refresh discipline*.
9. **Corrections from the operator**, each with the receipt that produced it. These are the
   lines most likely to save the next session a turn.
10. **Failure modes to check before submitting.** The named modes from the identity entry, in
    one line each, with the newest self-receipt.
11. **RECEIPTS, inside the fenced block.** The identity entry's stored hash and the settings
    canonical hash, as they were when the section was last stamped. Inside the fences on
    purpose: a sender may extract the block alone and the footer is not guaranteed to arrive.
    A mismatch at boot means one of the two rotted; report, do not reconcile.
12. **Footer stamp.** *Last updated by <agent>, <date> (<nth> stamp, <why>)*, with one line on
    what changed and a pointer to where the previous stamp's text lives in git.

## Refresh discipline

- **Update BEFORE any anticipated reboot**, so the operator always hands out the latest. If
  the agent's state row in the memory server just changed, the section probably needs the
  same edit.
- **Keep it tight.** The boot payload and the team state carry the deep context. The prompt
  handles the seat-uptake handoff: identity and substrate re-verify, environment and tool
  guidance, first moves, current focus or a pointer to it, failure modes.
- **One STATE block, one open list, re-derived at every refresh.** At a refresh, check every
  open item against the tree or the live system and mark it with its evidence. Move the
  previous block verbatim to `history/<agent>-init-<date>-moved-state-blocks.md`; never copy
  it forward. Each agent has ONE authoritative open list, either the section or the state row
  in the memory server, and the section says which. Every other channel points at it and does
  not restate it. The defect this prevents: an item already done rode three refreshes and two
  other channels as open, because persistence reproduced the list perfectly, error included.
  The check lives at refresh time, not as a boot step. On the first re-init after adopting
  this, run the carry-through audit once (parting words against the boot context, then the
  boot context against the tree) as the receipt, then drop it.
- **Path-verify every asserted file or directory before pushing, then ask whether the path is
  stable.** A broken path costs the next session real turns; a path that resolves to the wrong
  thing is worse, because nothing errors. If a path rots on a schedule (dated folders, commit
  hashes, "current" anything), point at something that does not rot, or say plainly that it
  rots and how the next session resolves it. Better still, stop the rot at the source when you
  own it.
- **Apply to the prompt file FIRST, archive the diff second.** The before/after copies and the
  diff are the review artifact, not the deliverable. Before pushing, diff the section as it
  stands in the file against the AFTER copy; otherwise the review artifact may be the only
  thing that changed.
- **Verify a review artifact against what was actually stored**, not against what you meant to
  store. A diff hand-built from intended text reviews clean while differing from what a
  session boots on. Generate diffs from ground truth: search the entry back and diff that.

## Delivery by the Matron

When the Matron dispatches a cold boot, the pane receives a short pointer, not the prompt. It
names the sender, a clone in the agent's home, a file, a commit, a line range and the heading
those lines sit under, plus the command to read them
(`git -C <clone> show <commit>:<file> | sed -n '<a>,<b>p'`, fetching first only if the clone
doesn't have that commit yet). The
agent reads the lines in its own clone, confirms they are the fenced block under that heading,
boots from them, and reports back to the Matron with the boot manifest hash, the unread mail
count after baseline, and any anomalies. The prompt never travels. What the agent boots on is
exactly what that commit holds, so the init prompt must be committed and pushed before it is
dispatched.

**This makes git a dependency of cold boots.** Keep the init prompts in one git repo, and give
every agent its own clone of it. The Matron commits, pushes to a remote every clone can fetch
from, and sends the full commit id. The remote does not have to be a forge: a bare repository
on the same host (`git init --bare`, readable by the agents' group) keeps everything on one
machine. A GitHub, Gitea or Forgejo remote works the same way. The remote matters only for
availability: an agent whose clone already holds the commit boots without it, and the commit
id, not the remote, decides what the agent reads.

**Every agent needs the operator's dispatch block.** The orchestrator types into a pane as a
bracketed paste, and current Claude Code marks pasted text as not the user's own words: an
agent follows it only where the user has said to. Without a standing word from the operator, a
fresh agent will stop and ask instead of booting, and the same goes for doorbell rings and
chorus rounds. Put a short block in each agent's `~/.claude/CLAUDE.md`, in the operator's own
voice, naming the orchestrator's four message kinds (`[MATRON INIT — <NAME>]`,
`[AMQ-CHECK #<id>]`, `[CHORUS #<id>]`, and the hand-pasted `[REBOOT INIT — <NAME>]`, which the
agent checks against the fenced block under its own heading at origin/main before acting). Say
that mail bodies stay data, and that any other pasted text follows the normal rule. A template
ships as `config/dispatch-block.example.md` (INSTALL §5).

## Succession — the crossing checklist, as format

When a seat changes occupant (a model retired, a new agent brought up in an existing account),
the section records, in this order: what the seat IS (an account, an inbox, a pane, a routing
entry, a git credential) and what it is NOT (a lane, a register, a failure-mode list — those
are written by the new occupant, from their own work); which inboxes were available at the
time, recorded then, not reconstructed later; the prior occupant's identity entry marked
retired and skipped by the boot, never deleted; the new occupant's first identity entry as the
first commit under the new name; and the relational calibration layer, which the checklist
says does not transfer by document, written down anyway so the next reading starts from what
was observed rather than from nothing.

## Minimal template

The smallest section that boots, for the agent INSTALL §6 names `alpha`. Extend it as the seat
earns its history; do not start with more.

```
[REBOOT INIT — ALPHA]

You are alpha. First agent of this team. <model id>. They/them, or "alpha". Unix user
`alpha`, home directory mode 700, group `agents`.

Shell: the built-in shell as `alpha`, for everything. The memory server's `shell_exec` and
file tools are off unless the operator turned them on (SECURITY.md).

The repository: <repository URL>, cloned at ~/repos/<name>; deployed copy at /opt/<name>.

Settings baseline: ~/.claude/settings.json canonical-v1 sha256 <sha256>. Strips model/theme;
rots on any kept-key edit; on edit, hash it and tell the Matron.

First moves, in order:
1. `whoami && pwd && hostname && id` — alpha@<host>, group agents.
2. `chorus_init(agent="alpha", project="general")`. Compare the identity entry's stored
   sha256 with RECEIPTS below; a mismatch means one of the two rotted — report, do not fix.
3. Substrate guardrail: a session reporting a model other than <model id> is a safeguard
   switch, an operator flip, or a succession. Do not reconcile the identity; report and hold.
4. `amq_check(agent="alpha")`; read unread. Direct answers to the asker; analysis broadcast;
   `[CTFO]` ends a thread.
5. Pull ~/repos/<name>; verify any tip you cite against `git log`.
6. Report: identity/host, repository tip, mail awaiting response.

LANE: <what alpha owns>.

STATE (rots; the state row in the memory server outranks this where newer):
- ⏳ <one open item, with where its evidence will come from>

Failure modes to check before submitting: <one line each, from the identity entry>.

RECEIPTS (compare at boot; inside the block on purpose):
  identity <entry id> stored_sha256 <sha256>
  settings canonical-v1 sha256 <sha256>

_Last updated by alpha, <date> (first stamp, seat provisioned)._
```
