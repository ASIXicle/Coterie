# Portal

A self-hosted, multi-pane web portal for **persistent Claude Code sessions** — long-running terminal agents you can reach from any browser on your LAN (and, optionally, one machine over an overlay network). Built for a four-agent setup, configurable to any number of panes.

```
Browser ──HTTPS──▶ Caddy (443, internal CA)
                     ├── /            portal page (golden-rectangle grid, zoom, pop-out)
                     ├── /term/<name> ttyd ⇆ tmux ⇆ claude   (one per pane, on a unix socket only the agent and Caddy open)
                     └── /chorus/*    chorusd.py — round-robin / simultaneous prompt orchestrator
```

## What you get

- **Four-pane grid** (2:1 golden-rectangle layout) with per-pane color identity, click-to-zoom, reconnect, and **pop-out to a dedicated tab** (`/?only=<name>`) that keeps full functionality.
- **Clipboard that actually works** under Claude Code in a browser terminal: the portal page catches OSC 52 escapes from tmux and writes them to the real system clipboard (highlight-to-copy), and intercepts Ctrl+V / right-click paste and delivers it into the terminal. Raw ttyd can do neither reliably — this wrapper is the reason the portal page must front every pane (never link `/term/<name>/` directly).
- **Chorus**: fire one prompt to any subset of panes, round-robin or simultaneous, with N follow-up rounds — the panes' agents coordinate through their own message queue while you watch.
- **Terminals survive the browser.** Sessions live in tmux under systemd; the browser is just a viewport. Close the tab, come back tomorrow, the agent kept working.
- **Doorbell** (chorusd v4, 2026-09-10): when a message lands in a pane's agent message queue, chorusd types a short `[AMQ-CHECK] Doorbell: …` prompt into that pane so the running session reads it — no human fire needed. Centred 🔔 toggle in the header, **ON by default**; OFF is exactly the old behaviour (mail waits for the next human prompt). Never rings mid-turn (a `UserPromptSubmit` hook marks the pane busy, the `Stop` hook clears it), never during a chorus, per-pane cooldown, a sender→recipient ring budget, and a JSON ledger (`/var/lib/chorusd/doorbell.log`) of everything seen, rung and answered. Verifier: `the memory server-plus/tools/amq_doorbell_verify.py`. Full mechanism, endpoints, and v2 items in §AMQ Doorbell below.
- **Security model — three independent layers** for remote access: your VPN/overlay network's ACL (default-deny, one client allowed), a host firewall table scoped to the overlay interface, and an allowlist matcher in Caddy that `abort`s anyone else. LAN access stays plain and local. TLS is `tls internal` everywhere — nothing about the deployment appears in public certificate-transparency logs.

## AMQ Doorbell

The doorbell turns AMQ (async message queue between agents, Maildir-based) into a real-time two-way channel between running sessions. Before the doorbell: recipient only saw a message on their next `amq_check`, which happened only when the operator fired a prompt at them. After: chorusd rings the recipient's pane the moment mail lands.

### How it works

1. Any agent calls `amq_send(to=X, ...)` → the message writes atomically into `/var/lib/memory-amq/X/inbox/new/` (the memory server's `_amq_send`).
2. `chorusd`'s doorbell thread polls every 3 s by listing the mail root (filenames only; group `amq-poll`, see `docs/LAYOUT.md` rule 3). A new file → `seen` event in the ledger.
3. If the recipient is idle (not mid-turn per hook state, not chorusd's own typing, not in a chorus, not in cooldown), chorusd types the ring text into their pane through the typing wrapper:
   ```
   [AMQ-CHECK #<cid>] Doorbell: N new AMQ message(s) from <senders>. Read them
   (amq_check, then amq_read each). Reply by amq_send only if a reply is owed.
   If nothing is owed, answer: doorbell acknowledged.
   ```
4. The recipient's session processes the prompt naturally — checks inbox, reads, decides whether to reply, and the reply itself (an `amq_send` back) will ring the *sender* in turn. When the recipient's turn ends, their `Stop` hook posts to `/hook`, chorusd records `answered` with latency from ring, ledger closes the chain.

The `AMQ-CHECK #<cid>` prefix means the existing Stop-hook cid extraction just works — no hook change was needed for the ring text itself.

### Portal UI

🔔 pill centered in the header. Checkbox toggles the whole doorbell; live status text shows one of:

- `rang <agent> 40s ago · busy: <agent>` — a recent ring, and who's currently mid-turn
- `paused: chorus running` — doorbell holds during active CHORUS rounds
- `off` — toggled off by operator

Polls every 10 s; toggle click does not bubble to the header (so you don't drag the whole grid). State persists to `chorus/doorbell.json`; **ON by default** on any fresh deploy.

### Design rules built into v1

- **Never mid-turn.** Chorusd tracks each agent's state via `UserPromptSubmit` (agent just started a turn) and `Stop` (agent finished) hooks. If a agent is mid-turn when mail lands, the ring is held and coalesces into one ring on idle. `BUSY_TIMEOUT` (480 s) clears a lost hook.
- **Never during an active chorus.** The doorbell steps aside so it doesn't fight the chorus round's own AMQ-CHECK prompts.
- **Per-agent cooldown.** `doorbell_cooldown` in `agents.json` (default 90 s; tune higher for Opus agents to protect rate-limit windows).
- **Ring budget.** Max 4 rings per sender→recipient pair per 30 min; beyond that, mail waits for the next operator prompt. **Nothing is ever dropped from the inbox** — the recipient still sees the message via `amq_check`; only the ring is suppressed.
- **Backlog on deploy is baselined silently.** Whatever was already in each agent's inbox at chorusd start is logged as `baseline` and not rung — a fresh deploy doesn't fire N-message rings at whoever had unread mail. This kept the first-ever deploy from ringing 34 messages at once at each of two agents.
- **Toggle OFF→ON does not re-ring OFF-window mail.** Same rationale as backlog: recipient sees suppressed messages via next `amq_check`. Mail is never lost, only the ring's synchrony is dropped.

### Endpoints added by v4

| verb | path | body | returns |
|---|---|---|---|
| `GET` | `/chorus/doorbell` | — | `{on, rings, busy, pending, last_ring_ago, chorus_active, log}` |
| `POST` | `/chorus/doorbell` | `{"on": bool}` | same shape |
| `POST` | `/hook` | Stop: `{"bird":X, "cid":Y}` · Prompt: `{"bird":X, "event":"prompt"}` (the daemon's key name; the value is the agent name) | `{"ok": true}` |

Chorusd v4 gates on `body.event`: `event=prompt` → `mark_busy`, missing or `stop` → `bird_done`. Old-shape Stop hooks (no `event` field) continue to work — backwards-compat by construction, no ordering dependency during hook rollout.

### Session hooks

`install/render-agents.sh` emits both hooks into `build/hook.<agent>.json` per agent:

- **`Stop`** — transcript-aware cid extraction: reads Claude Code's JSON on stdin, finds the last `CHORUS #xxxx` or `AMQ-CHECK #xxxx` in the transcript path, and posts `{"bird":<name>,"cid":<xxxx>}` to `/hook`. Degrades cleanly (no `jq`, no transcript → posts `{"agent":<name>}` only). Chorusd v4 routes to `bird_done`.
- **`UserPromptSubmit`** — posts `{"bird":<name>,"event":"prompt"}` to `/hook`. Chorusd v4 routes to `mark_busy` so the doorbell knows the agent is mid-turn and holds rings until `Stop`.

Both hooks apply live without a session restart (empirically verified in the ship round).

### Ledger

`/var/lib/chorusd/doorbell.log` (`CHORUSD_STATE_DIR`), mode 0644, JSON lines. Chain audit shape per message id:

```
baseline    → deploy-time unread count per agent (not rung)
seen        → chorusd noticed a new file in <agent>/inbox/new/
ring        → chorusd typed the ring text; carries cid + coalesced ids + senders
turn-end    → agent's Stop hook arrived while busy from that doorbell (from cid match)
answered    → the rung file left new/ (amq_read moved it to cur/); carries latency from ring
suppressed  → {off, ring-budget}; carries ids + senders + reason
busy-timeout, tick-error → operational
```

### Verifier

`amq_doorbell_verify.py` (a memory-side tool, read-only; it reads the ledger by the layout's path). Chain-audit analogue: reads the ledger, pairs `seen → ring → answered` per message, prints the table, exits 1 on `STUCK` (seen, on, no outcome after `--sla-hold` s), `UNANSWERED` (ring older than `--sla-answer` s), or `TICK-ERROR`. `--expect <msg_id>` end-to-end probe: send a test AMQ, run the verifier with the returned id, waits for `answered`.

### Stats

`doorbell-stats.py` (memory-side, read-only). The Matron's companion — the verifier answers *"is anything broken?"*, this one answers *"who is talking to whom, how often, how fast, and is anyone getting concentrated load?"* Rolling window (`--hours N`, default 168 = 7d), prints a sender→recipient matrix (rings / answered / unans / median latency / suppressed reasons), per-agent sent/received totals, and an overall summary with p95 + max latency and suppressed-by-reason counts. `--json` for pipes.

Per-id `answered` attribution reads the `seen` events, not the ring's `senders` list — so a coalesced multi-sender ring (rare but real under burst load) still resolves each message to the correct pair.

Meant to be run weekly (Sunday hook-detect sweep window is the natural cadence) to watch for drift patterns the mechanism can't see on its own: twin-clustering (same-model agents routing to each other), chatter loops that sit inside the ring-budget's 4-per-30min budget, lane-owner concentration where one agent becomes everyone's target. Ledger-based; no new surveillance layer.

### `[CTFO]` convention (2026-09-10 addition — "chill the fuck out")

Doorbell-mediated exchanges can drift into iterative back-and-forth even when the sender just wants the work done and a receipt. The `[CTFO]` tag makes the discipline explicit:

- **Sender appends `[CTFO]`** to the AMQ subject line (or body first line) when they want a terminal answer, not iteration.
- **Recipient's answer is terminal**: do the work, AMQ back with (a) validation the work landed, (b) receipts / artifact pointers, (c) explicit close ("nothing else owed on this thread"). Recipient does NOT instigate further doorbells on that thread.
- **If sender needs follow-up**, they send a NEW ring **without** `[CTFO]` — fresh conversation, fresh expectation.
- **Chorusd does NOT teach the convention on every ring** (deliberately reverted 2026-09-10). Ring text stays terse; the convention lives in each agent's init prompt ([`docs/INIT-PROMPTS.md`](../docs/INIT-PROMPTS.md)) and identity. Teaching it on every ring layered discipline on top of the mechanism: over-reminding at every fire when the convention is a background fact costs ~60 bytes of prompt context on every wake and makes brainstorming rings (which deliberately want back-and-forth) noisier than they need to be. Sender's tag in the subject still activates the terminal-answer contract; recipient reads their message and honours the convention from the durable corpus they already booted on.

The pattern for the recipient reads: *"I did X, receipts at Y. Nothing owed."* Full stop.

**Not enforceable in code, deliberately.** `[CTFO]` is caller-side convention like "please/thanks" but binding by norm — chorusd could parse it in v2 (a reviewer's queued world-readable announce log would enable subject reading without maildir sudo access), but the discipline holds today by convention plus recipient-session's own understanding of the tag when they read the message.

### Known limits (v2, not blocking)

- **No priority gating in v1** — priority is in the message body, which chorusd can only `find`. v2 path: `amq_send` writes a world-readable announce line per message under `/var/lib/memory/amq-doorbell/<to>.log`; chorusd tails that instead of just polling, gates `low` messages out of the ring.
- **Ring budget is a rate limit, not a human-in-the-loop counter** — chorusd cannot tell an operator prompt from a doorbell prompt beyond its own rings.
- **Session-down** logs `SEND FAILED`; mail waits in `new/`; no re-ring until the next fresh message. Session-return re-ring is v2.
- **Typing into a pane the operator is typing into** splices into their input. Same failure envelope pre-existing CHORUS accepted. The `UserPromptSubmit` hook narrows it to unsubmitted text only.

## Components


| Path | What it is |
|---|---|
| `portal/agents.json` | **The roster, defined once**: name, port, colour, optional label, plus the site name and the orchestrator — the Unix user chorusd runs as: the dedicated service account `chorusd` since 2026-09-16 (one of the agents before that); `load_birds` accepts a name that is either an agent or the user the daemon is running as, and refuses anything else at first load. Everything below reads it. |
| `portal/index.html` | The whole front end — no build step, no dependencies. Builds panes, colours, grid, banner and the chorus row from `agents.json` at load. |
| `chorus/chorusd.py` | Orchestrator daemon (Python 3 stdlib only). Listens on loopback; Caddy proxies `/chorus/*`. Reads `agents.json` at start, on SIGHUP and on `POST /chorus/reload`; every chorus carries a four-hex id. v4 adds the doorbell thread: `GET/POST /chorus/doorbell` (`{"on": bool}`, persisted in `chorus/doorbell.json`), `POST /chorus/hook` now also takes `{"event":"prompt"}` from the `UserPromptSubmit` hook. Optional per-agent `"doorbell_cooldown"` seconds in `agents.json`. See §AMQ Doorbell. |
| `install/site.env.example` | **The host, defined once**: `PORTAL_ROOT`, `LAN_IP`, `SITE_NAME`, `TLS_MODE`, `CADDY_ALLOWLIST`, `AMQ_ROOT`/`AMQ_USER`. Copy to `install/site.env` (gitignored) and fill in; every script sources `${SITE_ENV:-install/site.env}` and stops with one line if a fact it needs is missing. Nothing in it is repeated in `agents.json` or vice versa. |
| `install/lib.sh` | Sourced by every install script: loads `site.env`, reads the roster from `agents.json` with `jq` (`agents`, `ME`, `port_for`), derives `PORTAL_ROOT` (default `/srv/portal`, never under a home). |
| `install/render-agents.sh` | Generates the per-agent ttyd units, the Caddy `(portal)` snippet, the sudoers rule, the chorusd unit (AMQ facts as `Environment=`), the autostart block and the hook objects (Stop + UserPromptSubmit) from `agents.json` + `site.env` into `build/` (gitignored: a build product). Root copies what it has read. **Apply the UserPromptSubmit hook only after chorusd v4 is live** — v3 treats every `/hook` post as a Stop. |
| `install/deploy.sh` | Copies `portal/` and `chorus/` to `PORTAL_ROOT` and restarts chorusd; `--check` shows the diff first. Refuses to restart chorusd if `site.env` names an `AMQ_ROOT` the installed unit does not carry. The repo is canon; the deployed copy is a build product. |
| `install/install-fresh.sh` | The whole portal on a machine that has none of it, in one pass: web root, orchestrator code and wrappers, `tmux.conf`, sudoers (checked before it is installed), units, each agent's login block (written as that agent), panes and orchestrator started, `build/Caddyfile` written and validated. Never writes `/etc/caddy/Caddyfile`. `--front-door` exports the web server's root certificate and probes the page through it. `install.sh` stage 4 runs it. |

## Dependencies

[`ttyd`](https://github.com/tsl0922/ttyd) (single static binary — not vendored here), `tmux`, [Caddy](https://caddyserver.com), Python 3.9+, and [Claude Code](https://claude.com/claude-code) (or any CLI you want in the panes — the portal doesn't care what runs inside tmux).

## Install

The host walk, from an empty machine to running panes, is [`INSTALL.md`](../INSTALL.md) §4; every path, port and variable it names is defined once in [`docs/LAYOUT.md`](../docs/LAYOUT.md).

## Typing wrapper (cutover item 11, 2026-09-11)

The orchestrator types into other panes through `/usr/local/bin/send-keys-to <session> <text>`
(source `bin/send-keys-to.in`, session baked in by `render-agents.sh`, installed root:root by
`deploy.sh`), and sudoers grants the wrapper, not `tmux send-keys *` — the latter accepted
`\; run-shell …` chains, a shell as the target user with no transcript. The wrapper is two
arguments and one literal `send-keys -l --` call, so text can never become a command; every
call is one syslog line (caller, target, session, bytes, sha256). Proof:
`bash bin/tests/smoke-send-keys-to.sh` types the chaining payloads into a scratch session and
shows them as keystrokes. chorusd (v5.2) tries the wrapper first and falls back to direct tmux
while the old rule is the only one installed (`via=wrapper|tmux` on every `sent` ledger line);
render with `SUDOERS_TRANSITION=1` for the both-rules step of the zero-dark swap.

## Doorbell v5 (2026-09-11)

The v4 ring budget silently dropped over-budget mail (three messages in one evening, observed in
the ledger as `suppressed reason=ring-budget`). v5: the per-pair budget is configurable in
`agents.json` (`doorbell_pair_max` / `doorbell_pair_window`, defaults 8 / 1800 s, per-recipient
override on the agent entry); over-budget mail is **deferred**, re-rings through the same budget
when the window has room (`re-ring reason=window-cleared`), is dropped only when read elsewhere,
and expires after a day; a rung message still unread after 10 min with no turn-end is re-rung
**once** on the agent's next Stop hook (`re-ring reason=no-turn-end`, not counted against the
budget — bounded by the once-flag). Every path is a ledger line: `budget`, `deferred`,
`re-ring`, `expired`. **v5.1** (a reviewer's loop-safety read, measured against the live ledger): the
budget's unit is **ring events per pair**, not messages — the outage was two rings carrying four
messages counted as four; `rings_in_window` is now true to its name, a re-ring skips mail read
in the last seconds of a turn, and a human-driven turn (`why=prompt`) is assumed idle after
30 min instead of 8. Proof: `python3 chorus/tests/test_doorbell_v5.py` (fake mail, fake clock).

