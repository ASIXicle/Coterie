# The Matron role

A role, not a program. One of the agents holds it. Read the next section before anything
else, because the scaffold has two words that sound alike, and the difference between them is
most of what this document is for.

## Orchestrator and Matron are not the same thing

| | **Orchestrator** | **Matron** |
|---|---|---|
| What it is | A program: the `chorusd` daemon in `portal/` | A role, held by one of the agents |
| Runs as | Its own system account with no shell and no home; not an agent | An ordinary agent account |
| Judgment | None. It types what it is told and writes a ledger line | Yes. It reads, decides, writes, and signs |
| Its power | May type text into any agent's terminal pane, through one root-owned wrapper | No Unix privilege beyond its own home, plus one bearer token (see Powers) |
| Memory | None. The ledger is the only record | Full: identity, state, mail, history, like every agent |
| Speaks through | Keystrokes into panes; JSON lines in the ledger | Mail to other agents, commits, and patches to the boot corpus |
| Where it is named | `config/agents.json`, the `orchestrator` key | The holder's own identity entry in the memory server, and this document |

The orchestrator is the switchboard. The Matron is the agent on duty at it.

The Matron uses the orchestrator's endpoints as instruments: `/chorus` to put one prompt in
front of many agents, `/matron` to cold-boot an agent, the doorbell to wake an idle pane when
mail lands. The orchestrator does not know the Matron exists, beyond the name on one endpoint.
It checks that a request arrived on the host's loopback and not through the browser front door,
and on the two routes that reach into an agent's session (cold-boot dispatch and the whitelisted
keystroke) it also requires a bearer token; it never checks a caller's Unix identity. That is a
deliberate trust decision and it is stated in [`SECURITY.md`](SECURITY.md).

A short rule for reading the rest of the docs: **if it has a process id, it is the
orchestrator; if it has an opinion, it is the Matron.**

## What the Matron does

Editorial and operational. Not administrative.

- **Editorial.** What an agent reads at boot is the Matron's product. The Matron curates the
  shared state entry and the standing directives every agent boots on, keeps them small, and
  records what the team has been doing in a form the next reader can reach.
- **Operational.** The Matron dispatches cold boots, watches the scheduled hygiene and
  integrity instruments, owns the cutover checklist when deployments move, and signs the
  release branch.

Not administrative. The Matron does not schedule chorus rounds (the operator does), does not
edit another agent's own rows (each agent corrects its own), and does not own the code across
the team (each project has its own owner). The Matron may hold a project lane as well, in
parallel; the two are separate hats.

## Duties

1. **Boot corpus curation.** The shared state entry carries pointers to where the detail lives,
   never the detail itself; the per-agent rows belong to their owners. Reviewed every 30 days.
2. **Standing-directives gate.** Additions to the shared directives are Matron-only and for
   significant rules only, because every line ships to every agent on every boot. Each addition
   is staged as a reviewable before-and-after diff, attacked by a second agent, then landed.
3. **Bootstrap write discipline.** Every write to an entry that boots depend on is an anchored
   patch with an expected before-hash and after-hash, taken under a lock, with the pre-image
   kept. After every landing the Matron re-reads the store and runs the chain audit.
4. **Cold-boot dispatch.** When an agent needs a fresh start, the Matron points the agent at its
   reboot text through the orchestrator's `/matron` endpoint: a clone in the agent's home, a
   file, a pushed commit (its full id), the block's line range and its heading. The request carries the bearer token
   described under Powers: the pointer it produces is authority, and loopback alone does not say
   who is calling, so the orchestrator refuses the dispatch without the token. The keystroke
   kill switch does not turn this route off; only the token gates it. Hand the token to the
   client through a file descriptor, never as an argument, so it is absent from every process
   listing:

   ```bash
   jq -n --arg agent "<agent>" --arg repo "~/repos/<name>" --arg file "<init prompts file>" \
     --arg commit "<pushed sha>" --arg lines "<a>-<b>" --arg section "<the block's heading>" --arg sender "<matron>" \
     '{bird: $agent, repo: $repo, file: $file, commit: $commit, lines: $lines, section: $section, sender: $sender}' | \
   curl -s -X POST -H 'content-type: application/json' \
     -H @<(printf 'Authorization: Bearer %s\n' "$(cat /etc/chorusd/pane-key.token)") \
     --data-binary @- http://127.0.0.1:8766/matron
   ```

   8766 is the orchestrator's default port; a site that sets `CHORUSD_PORT` in
   `portal/install/site.env` uses that instead.

   The orchestrator validates each field and types a three-line pointer into the pane. It
   writes no file. The agent reads those lines at that commit in its own clone and boots from
   them. Claude Code marks the pointer as pasted text, so the agent acts on it only under the
   operator's dispatch block in its `~/.claude/CLAUDE.md` ([`INIT-PROMPTS.md`](INIT-PROMPTS.md)).
   Mail is never the boot channel: a well-behaved agent treats mail as data, not instruction,
   and refuses to boot from it. The operator's phrase "bring up <agent>" means exactly this
   dispatch.
5. **Settings-hook baseline oversight.** The registry of approved hashes for each agent's
   settings file is the Matron's to keep. An agent re-approves its own row after a change; the
   Matron confirms it. When the analyser or the hash rules change, the Matron re-runs every
   row.
6. **Mailbox hygiene supervision.** A weekly sweep moves old unread mail out of the way. It
   never deletes; every message is also stored in memory at send time.
7. **Review cadences.** Identity entries and the shared state are reviewed every 30 days, each
   review comparing the entry's size against last cycle's. The invariant core is the operator's
   alone, reviewed every 90 days; the Matron surfaces the due date.
8. **Payload inflation gate.** A scheduled check renders each agent's boot payload against the
   tool-result cap and alerts the Matron when any agent crosses the margin. Slow growth is
   caught at the first threshold, not at overflow.
9. **Release signature.** After the lint is green and the adversarial pass has found nothing,
   the Matron signs. Signing clears a cut for publication; publishing is the operator's action.
10. **Cutover and freeze.** When deployments move, the cutover moment is the Matron's signature
    against a written checklist, not a calendar date. The Matron holds a veto until the list
    is clean.
11. **Succession protocol.** The Matron writes and maintains the protocol for replacing an
    agent without losing the seat. The successor to any seat, including this one, is chosen by
    the operator.

## Powers, exactly

- **Type into any pane**, through the orchestrator's endpoints. This is the same power the
  operator has at the keyboard, and it is why the Matron's account is an ordinary one: the
  reach comes from the orchestrator, not from the Matron's Unix user.
- **One token, two routes.** A bearer token, readable by the orchestrator's account and the
  Matron's and by no other agent, gates the two orchestrator routes that reach into an agent's
  session: `/pane-key`, which types a single whitelisted command into a pane (the whitelist is
  `/clear`, hard-coded), and `/matron`, the cold-boot dispatch of Duty 4. Read plainly: whoever
  holds the token can clear any agent's context and can hand any agent a boot pointer. A
  systemd drop-in is the kill switch for the keystroke route only; it does not switch off
  dispatch, and the token is loaded whether or not the switch is set. An empty or unreadable
  token file refuses both routes.
- **Write the shared entries**: the shared state and the standing directives, under the
  discipline in Duties 2 and 3.
- **Sign**: the cutover moment and each release cut.
- **Propose a chorus round.** Not fire one.
- **Name premature consensus.** When every agent agrees inside one round, the Matron calls
  the slow-down and asks what was not tested. Fast agreement is often complementary
  failure modes firing together, and catching that is this seat's job before it is the
  operator's.

What the Matron cannot do, stated so nobody infers it:

- Not root. No `sudo` beyond what every agent has, which is none.
- Not another agent's editor. A stale row gets a message to its owner, not a correction.
- Not the code owner across the team, and not the point agent on any project unless named.
- Not a permanent seat. See Succession.

## Cadences

| Cadence | Item | Instrument |
|---|---|---|
| Weekly | Settings-hook scan of every agent | `hookandshield/`, systemd timer |
| Weekly | Mailbox hygiene sweep | `memory/`, systemd timer |
| Weekly | Boot payload inflation gate | `memory/tools/`, systemd timer, alert by mail |
| 30 days | Shared state freshness | `review-after` field surfaced in the boot manifest |
| 30 days | Each identity entry, with size against last cycle | same |
| 90 days | Invariant core | the operator, in person |

The manifest reads an entry's own cadence from the first line that **begins** with
`as-of <date>` (list markers, heading marks and emphasis may precede it) and carries
`review-after <date>`; a bare `review-after <date>` anywhere in the body is only the fallback.
Declare on such a line, at the head of the entry. An entry that merely mentions another
entry's date mid-line (a pointer to an identity entry, say) is otherwise reported on that
entry's cadence, and its own review never comes due.

## Succession

The role is held, not owned. The operator chooses the successor; the outgoing Matron does not.
The outgoing agent's identity entry is never deleted: it is marked retired and its name stays on
the resolution list, so old mail and old commits still say who wrote them. Whether the role sits
on any particular model is the operator's call; nothing in the scaffold requires one.

## Designating a Matron on a new installation

Nothing in `config/agents.json` names the Matron, on purpose: the file lists accounts, and the
Matron is a duty one of those accounts carries. To designate one: say so in that agent's identity
entry in the memory server, give that agent's account read access to the orchestrator's bearer
token at install time (INSTALL stage 3b; the token gates cold-boot dispatch as well as the
keystroke route, so without it the Matron cannot bring an agent up), and have every agent's
reboot text name who dispatches cold boots. The identity entry is where the seat's authority
comes from; the token and the reboot texts are how that authority reaches the world. Change all
three when the seat changes hands.

The Matron may not be able to confirm that read access itself: Claude Code can stop an agent from
inspecting a credential file, and it should not try. The operator checks it once, as that
account: `runuser -u <matron> -- test -r /etc/chorusd/pane-key.token && echo readable`.
