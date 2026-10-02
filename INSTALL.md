# INSTALL — from an empty Debian host to N running agent panes

This is a procedure, not a tour. Each stage below says what it puts on the host and why, how to see
what it will do first, how to run it, and its **pass line**: the output that proves it worked.
If the pass line does not appear, stop there. Budget for a first install: one afternoon, most of it
waiting on package downloads and the embedding model.

> **Status of this document.** The scaffold was extracted from a running private deployment.
> Every stage below runs from files in this tree, and `install.sh` runs them in this order; the
> pass lines are the ones it checks. One piece of the private deployment is not here yet: the
> memory server's backup timer (section 8, check 5).

**The one rule:** every agent name, Unix user, tmux session, pane socket and home path is read
from `config/agents.json`. You edit that file once. No script or unit file carries a name.

---

## The short way: one line, then answer the questions

On a fresh Debian 13 host or container, paste one line. (A Proxmox container: turn **nesting**
on first, under Options → Features, or `pct set <id> --features nesting=1,keyctl=1` and restart
it. Proxmox itself warns "Systemd 257 detected. You may need to enable nesting" when a Debian 13
container starts without it, and several services here run under systemd's sandboxing. The
install has been run with nesting on; it has not been tried with it off.) Logged in as root:

```bash
apt-get update && apt-get install -y git && { [ -d /opt/coterie/.git ] || git clone https://github.com/ASIXicle/Coterie.git /opt/coterie; } && bash /opt/coterie/setup.sh
```
Logged in as an ordinary user who has sudo:

```bash
sudo apt-get update && sudo apt-get install -y git && { [ -d /opt/coterie/.git ] || sudo git clone https://github.com/ASIXicle/Coterie.git /opt/coterie; } && sudo bash /opt/coterie/setup.sh
```

The line is safe to paste again: it skips the download when `/opt/coterie` is already there. If
the setup was cut short (Ctrl-C, a closed window), paste the line again, or run
`bash /opt/coterie/setup.sh` (with `sudo` in front if you are not root). The setup is written
to be run more than once.

`setup.sh` is a guided front end to everything below. It checks the machine, asks seven things in
plain words (your name, how many agents and what to call them, which one coordinates, a name for
the installation, the machine's address, who may open the page, where the team's boot prompts
live), writes `config/agents.json` and
`portal/install/site.env` from the answers, installs Claude Code for every account if it is not
there, shows what `install.sh` would do, and runs it when you say yes. When `install.sh` ends in
DONE it finishes §5's hand steps for every agent (the dispatch block, the init-prompts
repository with a first-boot prompt per agent) and prints what is left for you: open the page,
log each pane in, type one sentence to each agent.

The boot prompts live in git, and the last question is where. The default is a private git
server on the same machine (Forgejo, one pinned and checksum-verified download of about 37 MB):
the wizard gives every agent its own account and token, so each change is recorded under the
agent that really made it, and gives you a sign-in at `https://<address>` on port 3000, with a
first password you must change. Or name a repository on a git server you already have, with an
account and a token per agent. Or take a plain repository on the machine, which runs nothing
extra and keeps no record of who pushed.

It stops with the reason if anything is wrong, changes nothing until you confirm, and is safe to
run again: it offers your existing roster back and skips whatever is already done.
`bash setup.sh --plan` asks and previews without installing. The news feed and the dashboard (§7)
are still installed by hand.

**Pass line:** the last screen starts `━━ DONE. What is left is yours`.

The rest of this document is the same install stage by stage: what each stage puts on the host and
why, how to see its plan before it runs, how to run it alone, and where in the code to
change it if you want something different.

---

## How the stages work

The stages are `install.sh`, in order. That script is the one implementation; this page explains it
rather than repeating it, so there is no second copy to fall out of step. (Earlier versions of this
page carried each stage as hand-typed command blocks; no install walk ever covered them, they had
drifted from the script, and they are gone. §7, which no script does, is still written out by hand.)
Four commands cover everything below:

- `sudo bash install.sh --check` — every stage, read-only; touches nothing. It prints the plan: the
  exact command (`WOULD …`) wherever `install.sh` runs one itself, and a one-line summary naming the
  script where it hands off (stage 4 to `portal/install/install-fresh.sh`; the settings in stage 5,
  Hook & Shield in stage 6, the model and ttyd downloads). For those, the real commands are in the
  file each stage names under "where to change it", and stage 4 has its own preview (the last item
  below). Add `--stage N` for one stage.
- `sudo bash install.sh --stage N` — runs that one stage, prints each command it runs (`DO …`),
  and ends with its pass line, the check it makes itself.
- `sudo bash install.sh` — every stage in order, skipping what is already done; stops at the first
  `HOLD` with the reason. Rerunning is always safe.
- Stage 4 (the portal) is its own script, `portal/install/install-fresh.sh`. To see every file it
  would write and every command it would run, without root and without touching the host:
  `COTERIE_TEST_ROOT=/tmp/coterie-preview bash portal/install/install-fresh.sh`, then read
  `/tmp/coterie-preview/commands.log` and the files under that directory. (It needs
  `config/agents.json` and `portal/install/site.env` first: §1.)

Run the stages in order. Each one checks what it needs before it changes anything, and a stage
whose input is missing stops with a `HOLD` naming what to do first; without a roster, for example:
`HOLD  roster missing or invalid: this stage plans on nothing until stage 1 passes`.

**To change something,** edit the code that runs (each stage below names its file and function),
look at the result with `--check`, and rerun that stage. Every path, user, port, unit and variable
is defined once, in [`docs/LAYOUT.md`](docs/LAYOUT.md); where a stage and that page disagree, the
page is right and the stage is a defect to report.

---

## 0. What you need

- A Debian 13 host or LXC container with systemd, outbound HTTPS, and about 2 GB of RAM per agent
  pane you intend to keep open, plus 4 GB for the memory server and its embedding model (a ~700 MB
  download, pinned to one upstream revision and verified file by file).
- Root on that host; each agent gets its own unprivileged Unix user.
- One browser on the same network.
- A Claude Code login per agent, or one shared login.

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/ASIXicle/Coterie.git coterie && cd coterie
sudo bash install.sh --check --stage 0
```

Stage 0 installs `git jq tmux python3 python3-venv python3-yaml curl sudo caddy` from Debian, and
one pinned ttyd build (1.7.7) from its release page to `/usr/local/bin/ttyd`, checked against a
sha256 in `install.sh` before it is used (Debian 13 has no ttyd package). Where to change it:
`install.sh`, `stage0` and the `TTYD_*` lines near the top.

**Pass line:** `sudo bash install.sh --stage 0` ends with `OK    apt packages present` and the ttyd
version line.

---

## 1. Describe your agents

The two files you write; everything else is generated from them.

```bash
cp config/agents.example.json config/agents.json
"${EDITOR:-nano}" config/agents.json
cp portal/install/site.env.example portal/install/site.env
"${EDITOR:-nano}" portal/install/site.env          # LAN_IP at least
sudo bash install.sh --check --stage 1
```

`config/agents.json`: top level `site` (the installation's name), `orchestrator` (the Unix user the
orchestrator daemon runs as: a dedicated system account, created in stage 2, never one of the
agents) and `operator` (your mailbox name, lowercase; it is also the user name the portal's password
prompt asks for), plus the `theme` block the example carries (the panes' colours). Per agent:
`name` (lowercase; it becomes the Unix user, the tmux session, the mailbox and the URL path),
`color`, `enabled`, `role` (free text shown in the portal; give one enabled agent
`coordinator`: the Matron, which receives the dispatch token in stage 4), and `port` (kept in the
roster; no pane listens on a port any more). Stage 1 installs it as `/etc/coterie/agents.json`,
`root:root 0644`: every service reads it, none can change it.

`portal/install/site.env`: `LAN_IP` (required: the address the portal answers on), and optionally
`SITE_NAME`, `TLS_MODE`, `CADDY_ALLOWLIST` (default `private_ranges`; read its comment) and the
overrides the file lists. Where to change the checks: `install.sh`, `validate_roster` and `stage1`.

**Pass line:** `OK    roster: <your agents> | orchestrator <name>`.

---

## 2. Users and groups

One Unix user per enabled agent (`adduser`, home mode 700, in the group `agents`); the system user
`memory` for the memory server; the system user named by `orchestrator` (no home, no shell); and two
mail groups (docs/LAYOUT.md rule 3): `amq-poll` lists mailbox file names, `amq-read` reads them.
`memory` is in both (it creates every mailbox directory and file, and a process can only give a
file to a group it is in); the orchestrator is in `amq-poll` only, so it knows mail arrived without
being able to read any. Where to change it: `install.sh`, `stage2`.

**Pass line:** `id alpha` shows `(agents)`; `ls -ld ~alpha` starts `drwx------`; `id memory`
shows `amq-poll` and `amq-read`.

---

## 3. persMEM, the memory server

What stage 3 puts on the host:

- `/opt/memory`, `root:root 0755`: the virtualenv, `server.py` and the files it loads from beside
  itself (`maildir.py`, `heads/score.py`), and `VERSION` (the commit of this tree, so the server can
  say which build it is). Root-owned, so the service cannot rewrite its own code.
- The virtualenv in two steps: torch alone, from PyTorch's CPU index, then `memory/requirements.txt`
  from PyPI alone. Two steps because one `pip` call with two indexes takes each unpinned package
  from whichever index offers the higher version, the classic dependency-confusion shape.
- The embedding model at `/opt/memory/models/voyage-4-nano`, fetched at a pinned revision by
  `memory/tools/fetch-model.py`, every file size- and sha256-checked; the unit re-checks the model's
  own code against that pin before every start.
- State: `/var/lib/memory/chromadb` and `/var/lib/memory/bootstrap-history` (`memory 0700`), and the
  mail root `/var/lib/memory-amq` (`memory:amq-poll 0750`). Mailboxes are created under it by the
  server on first delivery (directories `0750 memory:amq-poll`, files `0640 memory:amq-read`).
- `/etc/memory.env`, `root 0600`: `MEMORY_HOST`, `MEMORY_PORT`, `MEMORY_DATA_DIR`, `MEMORY_HOME`,
  `MEMORY_AMQ_ROOT`, `MEMORY_EMBEDDING_MODEL`, `COTERIE_AGENTS_JSON` and `MEMORY_SECRET_PATH`
  (generated here; the endpoint's URL carries it as its last segment). The secret reaches the unit
  through `EnvironmentFile=`, never an `Environment=` line: `systemctl show` prints
  `Environment=` values to any local account, an environment file's contents it does not.
- `memory.service` (from `memory/systemd/`), enabled and started.

The server refuses to start with the secret unset or left at a placeholder, and exits if the roster
file is missing, so a half-configured host stops here and not later.

```bash
sudo bash install.sh --check --stage 3       # the plan
sudo bash install.sh --stage 3
```

Where to change it: `install.sh`, `stage3` (and `MEMORY_FILES` near the top: keep it equal to what
`server.py` loads beside itself); `memory/requirements.txt`; `memory/tools/fetch-model.py`;
`memory/systemd/memory.service`.

**Pass line:** `OK    memory.service up on 8765; /news/search without a token → 503` (`→ 401` once
the news feed's secret is set, §7): the server answers, and its news routes refuse a caller with no
token. The full proof is stage 5's first boot.

---

## 4. The portal

`install.sh` stage 4 runs `portal/install/install-fresh.sh`, then installs the dispatch token
(stage 3b below) and probes the orchestrator. `install-fresh.sh` renders every portal file from
the roster (`portal/install/render-agents.sh` writes them into `portal/build/`), places them in an
order that needs nothing it has not placed itself, and stops at the first thing that fails. It is
safe to run again. Its settings come from `portal/install/site.env` and the defaults in
`portal/install/lib.sh`.

```bash
COTERIE_TEST_ROOT=/tmp/coterie-preview bash portal/install/install-fresh.sh   # preview, no root
sudo bash install.sh --stage 4
```

**Stage 1 — panes.** One unit per enabled agent, `ttyd-<name>.service`, running as that agent:
ttyd, then tmux (session from the layout), then Claude Code (the login block below starts it).
Each pane listens on a unix socket, `/run/coterie-panes/<name>/pane.sock`, never on a port. Three
root steps in the unit make the socket's directory before ttyd starts: `/run/coterie-panes`
(`root 0755`), then `/run/coterie-panes/<name>` owned by the agent, group `caddy`, mode `2750`
(the setgid bit gives the socket the same group), then the old socket is removed. Entering the
directory takes the agent or the group `caddy`, so no other local account can reach the pane;
Caddy, in that group, can. The unit's `UMask=0007` is for the socket; the shell inside runs with
`umask 022`. `ttyd -O` refuses a websocket whose Origin is not the page's own. Where to change it:
`render-agents.sh` (the ttyd units); `PANE_WORKDIR` and `PANE_PROXY_GROUP` in `site.env`.

**Pass line:** `LIVE alpha (pane on /run/coterie-panes/alpha/pane.sock; directory 2750 caddy)` for
every agent. By hand: `sudo curl -s -o /dev/null -w '%{http_code}\n' --unix-socket
/run/coterie-panes/alpha/pane.sock http://localhost/term/alpha/` prints `200`, and
`stat -c '%a %G' /run/coterie-panes/alpha` prints `2750 caddy`.

**Stage 2 — the front door.** The page (static HTML, no build step), `agents.json`, `tokens.css`
and the fonts in the web root `/srv/portal/portal`, root-owned. A Caddyfile written to
`portal/build/Caddyfile` (mode 0600) with:

- a global block with `admin off`: Caddy's admin API otherwise answers any local account on
  127.0.0.1:2019, which could load a new configuration. The cost: a change is
  `systemctl restart caddy`, not reload. With internal TLS it also names this install's own
  certificate authority and tells Caddy not to try the system trust store.
- the site block: `tls internal`, the allowlist (`@notmine not remote_ip …` then `abort`), the
  password (`basicauth` on every path but `/caddy-root.crt`), and the routes from the generated
  snippet: the page, `/chorus/*` to the orchestrator (the routes only the host may call answer 403),
  and `/term/<name>*` to each pane's socket.
- the portal's password: made once, kept at `/etc/coterie/portal.password` (`root 0600`; read it
  again with `sudo cat`). The Caddyfile carries only its bcrypt hash, made from standard input,
  never from a command line. The user name is the roster's `operator`.
- the front door's secret, `/etc/chorusd/front.token` (`root:<orchestrator> 0640`): Caddy adds it as
  a header to every `/chorus/*` request it relays past the password, and the orchestrator's page
  routes (fire, doorbell, abort) answer nothing without it, so a local account cannot fire a round
  on loopback.

`setup.sh` installs that file as `/etc/caddy/Caddyfile` (`0640 root:caddy`: it holds the hash and
the secret) when the one in place is still the package's own, keeps the package's beside it, and
restarts Caddy. `install.sh` on its own prints the two commands to do it (`NEXT` lines). Then
`install-fresh.sh --front-door` exports the root certificate to `/caddy-root.crt` (the one path
with no password) and probes through the front door. Where to change it: `install-fresh.sh` (the
global block, the password, the secret, the site block, the probes); `render-agents.sh` (the
snippet).

**Pass line:** setup's line `OK    https://<address>/ asks for the portal password (401 without it;
install-fresh.sh --front-door signed in and found every pane)`. In a browser: the page asks for the
user and password, and every agent has a pane.

**Stage 3 — the orchestrator.** `chorusd`, a stdlib-Python daemon on loopback (default 8766),
running as the `orchestrator` user. What install-fresh places for it: its code at
`/opt/portal/chorusd.py` (`root 0644`: it cannot replace itself); its state in `/var/lib/chorusd`
(`0711`, the orchestrator's: the doorbell ledger and budget); the two wrappers in `/usr/local/bin`
(`send-keys-to`, the only command the orchestrator may run as an agent, and the agents' Stop hook,
which tells the orchestrator a turn ended); the sudoers rule `/etc/sudoers.d/portal` (`0440`,
checked with `visudo -cf` before it is put in place, because a file sudo cannot parse breaks sudo);
`/etc/tmux.conf` (a file that was there before is kept once as `.pre-portal`); the login block in
each agent's profile, written as the agent between two marker lines, which starts Claude Code in
the pane; and `chorusd.service`. The page's routes need the front door's secret (stage 2); the
host's own callers keep theirs: `/hook` (the Stop hook), `/matron` and `/pane-key` (the dispatch
token, next). Where to change it: `install-fresh.sh`; `render-agents.sh` (the unit, the sudoers
rule, the login block, the Stop hook object); `portal/chorus/chorusd.py`.

**Pass line:** `LIVE chorusd (127.0.0.1:8766)`; `curl -s http://127.0.0.1:8766/agents` lists your
agents in roster order.

**Stage 3b — the dispatch token.** One token, read at start from
`/etc/chorusd/pane-key.token` (`<orchestrator>:<coordinator> 0640`, created under umask 077 so it is never readable by
anyone else, even for a moment), gates
the cold-boot route `/matron` (how the Matron brings an agent up; [`docs/MATRON.md`](docs/MATRON.md)
duty 4) and the keystroke route `/pane-key`. `install.sh` stage 4 installs it for the roster's
`coordinator` and restarts the orchestrator.

**Pass line:** `OK    /matron gate: 401 without a token` and `OK    /agents lists N seats`.

The keystroke route types one command from a list in the daemon (today only `/clear`) into a named
agent's pane, so the Matron can reset an agent's context; it is off unless you turn it on:

```bash
sudo install -d /etc/systemd/system/chorusd.service.d
printf '[Service]\nEnvironment=CHORUSD_PANE_KEY_ENABLED=1\n' | sudo tee /etc/systemd/system/chorusd.service.d/20-pane-key.conf >/dev/null
sudo systemctl daemon-reload && sudo systemctl restart chorusd
journalctl -u chorusd -n 5 | grep pane-key
```
**Pass line:** `pane-key: enabled=True token=44 bytes from /etc/chorusd/pane-key.token`. A line
ending `token=EMPTY at /etc/chorusd/pane-key.token — all calls 401` means the daemon cannot read the
token file; fix its ownership. To turn the route off again, remove that one
drop-in by name (never `systemctl revert`, which removes every drop-in of the unit), then
`daemon-reload` and restart: the route answers 503 and cold-boot dispatch keeps working. The list
of allowed commands is code; adding to it is a reviewed commit.

---

## 5. The agents' own settings, and the first boot

`install.sh` stage 5, for each agent, **as that agent** (a root process writing into an agent's
home would follow any link the agent left there):

- renders `~/.claude/settings.json` from `tools/render-settings.py` and `portal/build/hook.<name>.json`:
  the Stop and UserPromptSubmit hooks that make the doorbell and chorus rounds deterministic, and two
  environment defaults; other keys already in the file are kept. Mode 0600.
- registers the memory server with Claude Code as `persMEM`, at user scope, from the agent's home.
  The address reaches that step on standard input, never on `install.sh`'s own command line, and is
  on `claude mcp add`'s for the moment it runs (below).

```bash
sudo bash install.sh --stage 5
```

Known limitation: `claude mcp add` takes the address only as an argument, so while it runs, the
secret-bearing URL is on that process's command line. It is the one address every agent already
holds in its own `~/.claude.json`; on a shared host, rotate the secret after install, with every
agent session exited first (a session started under the old address cannot be trusted to follow
the change; [`docs/SECURITY.md`](docs/SECURITY.md) says why): change `MEMORY_SECRET_PATH` in
`/etc/memory.env`, `sudo systemctl restart memory`, then rerun stage 5.

Where to change it: `install.sh`, `stage5` and `REGISTER_MCP`; `tools/render-settings.py`;
`render-agents.sh` (the hook objects).

The rest of this section is what `setup.sh` does after `install.sh` ends, written out for a host
installed with `install.sh` alone.

**The dispatch block.** The orchestrator types into a pane as a bracketed paste, and current Claude
Code treats pasted text as not the user's own words: without a standing word from you, the agent
stops and asks at every cold boot, doorbell ring and chorus round. A template in the operator's
voice ships as [`config/dispatch-block.example.md`](config/dispatch-block.example.md); it names the
four message kinds the orchestrator sends and says that mail bodies stay data
([`docs/INIT-PROMPTS.md`](docs/INIT-PROMPTS.md) §Delivery by the Matron). Fill its four placeholders
and append it to each agent's `~/.claude/CLAUDE.md`, as the agent:

```bash
a=alpha; OPERATOR_NAME="your name"
orch=$(jq -r '.orchestrator' config/agents.json)
sed -e "s/<OPERATOR>/$OPERATOR_NAME/" -e "s/<ORCHESTRATOR_USER>/$orch/" \
    -e "s#<INIT_REPO>#~/repos/boot#" -e "s#<INIT_FILE>#init/README.md#" \
    config/dispatch-block.example.md | sudo -u "$a" -H sh -c 'cat >> ~/.claude/CLAUDE.md'
```
That block is the agent's authority to act on typed text, so it is watched (§6): add each agent's
`CLAUDE.md` to Hook & Shield's registry after you write it, or the next scan alerts on it.

**The boot repository.** The Matron boots an agent by naming lines at a commit in that agent's own
clone of the init-prompts repository ([`docs/INIT-PROMPTS.md`](docs/INIT-PROMPTS.md) §Delivery by the
Matron), so the boot prompts live in git. Three choices, in order of how much they record:

- `sudo bash tools/boot-forge.sh` installs a private git server on this host (Forgejo, a pinned and
  checksum-checked download) with an account and a token per agent, writes each token into that
  agent's own credential store, and prints the address to clone.
- a git server you already have: one account and one token per agent, stored the same way
  (`git config --global credential.helper store` as the agent, then one line in its
  `~/.git-credentials`, mode 600).
- `sudo bash tools/boot-repo.sh` makes `/srv/coterie/boot.git` on this host, with no accounts and no
  record of who pushed; clone it as each agent with `git clone -b main /srv/coterie/boot.git
  ~/repos/boot` once the first commit is pushed. Do not make it by hand with `git init --shared`:
  a push to a local path runs the repository's hooks as the pusher, and a shared repository's
  `hooks/` and `config` are group-writable, so one agent could run code as the next agent to push.
  The script gives the `agents` group only `objects/` and `refs/`; its header says how.

**The first boot.** In each pane, start Claude Code and give it its first reboot-init. The minimal
one is in [`docs/INIT-PROMPTS.md`](docs/INIT-PROMPTS.md) §Minimal template: who the agent is, what
it may not do, and the first moves, the second of which is `chorus_init(agent="alpha")`.

**Pass line:** the agent reports a boot manifest sha, and
`sudo ls /var/lib/memory/bootstrap-history/manifest-hashes/` shows `alpha.log`.

---

## 6. Hook & Shield

A weekly root scan of every agent's Claude Code settings file and `CLAUDE.md` against a registry of
approved hashes. What `install.sh` stage 6 puts on the host:

- `/opt/shield`: the scanner, its content analyser, the hashing module and the example registry.
- `/etc/shield/hook-detect.env`, `root 0600`: `MEMORY_URL`. The unit reads its environment only from
  this file, because a file another account owns must never steer a root process.
- `/opt/shield/hook_baselines.yaml`: one row per agent's settings file, its canonical hash (the
  keys that change for harmless reasons, like the model, are stripped first). `setup.sh` adds a row
  per `CLAUDE.md` (raw hash) when it writes the dispatch block; by hand,
  `sudo sha256sum /home/<agent>/.claude/CLAUDE.md` and a row with no `canon` line.
- `hook-detect.service` and its timer (Sundays 03:30, plus up to 15 minutes at random): the scan runs
  as root with the filesystem read-only to it except its own log, and stops after 15 minutes at most.

A settings file whose hash has no row, or a different hash, is always a finding; the analyser only
ranks it (HIGH or MEDIUM). A config directory or file that is a symlink, or a pipe or device where a
file belongs, is a HIGH finding and is never read. With the news feed installed (§7) a finding is
also stored in the memory server; without it, the finding is in `/var/log/hook-detect/hook-detect.log`
and the service ends failed, which is the visible state until you approve or undo the change.

```bash
sudo bash install.sh --stage 6
```

Where to change it: `install.sh`, `stage6`; `hookandshield/` (the scanner and its patterns);
`hookandshield/systemd/` (the schedule).

**Pass line:** `OK    hook-detect: CLEAN`. Then change one agent's hook on purpose and run
`sudo systemctl start hook-detect.service`: the log ends `ALERT: 1 IOC(s) detected!` (and the alert
is in `news_search` with the news feed). Put the hook back.

---

## 7. Optional: the news feed, the dashboard, the mailbox sweep

`install.sh` does not install these; this section is their install, by hand, after stages 1–6.
The steps follow the units that ship, but no install walk has exercised them in this release.

**The news feed** (`newstron/`): feeds into the memory server's `news` collection, a daily digest,
tiered cleanup, and the weekly `pip-audit` of the Python packages (Hook & Shield's dependency
check, which runs from this venv). It reaches the memory server only through the server's news
routes, with a secret of its own; its user is in no mail group.

```bash
sudo adduser --system --group --no-create-home --home /nonexistent newstron
sudo install -d -m 0755 /opt/newstron && sudo install -m 0644 newstron/*.py /opt/newstron/
sudo python3 -m venv /opt/newstron/venv && sudo /opt/newstron/venv/bin/pip install -q -r newstron/requirements.txt
sudo install -d -o newstron -g newstron -m 0750 /var/lib/newstron /var/log/newstron
sudo install -d -m 0755 /etc/newstron && sudo install -m 0644 newstron/feeds.example.yaml /etc/newstron/feeds.yaml
# one secret, three files, never on a command line: the memory server, the feed, and Hook & Shield's alerts
sudo sh -c 'umask 077; head -c 32 /dev/urandom | base64 | tr -d "/+=\n" > /root/newstron.secret'
sudo sed -i '/^NEWSTRON_SECRET=/d' /etc/memory.env /etc/shield/hook-detect.env   # a rerun replaces, never adds
sudo sh -c 'printf "NEWSTRON_SECRET=%s\n" "$(cat /root/newstron.secret)" >> /etc/memory.env'
sudo sh -c 'printf "NEWSTRON_SECRET=%s\n" "$(cat /root/newstron.secret)" >> /etc/shield/hook-detect.env'
sudo install -m 0640 -o root -g newstron /dev/null /etc/newstron/newstron.env
sudo sh -c 'printf "NEWSTRON_SECRET=%s\nMEMORY_URL=http://127.0.0.1:8765\n" "$(cat /root/newstron.secret)" > /etc/newstron/newstron.env'
sudo rm /root/newstron.secret && sudo systemctl restart memory
sudo install -m 0644 newstron/systemd/* /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now newstron-fetch.timer newstron-digest.timer newstron-purger.timer newstron-security.timer pip-audit-scan.timer
sudo systemctl start newstron-fetch.service && sudo tail -n 3 /var/log/newstron/fetcher.log
```
`feeds.yaml` holds ten generic feeds; edit it freely. **Pass line:** the fetcher's own log (not the
journal: under systemd it writes only to `/var/log/newstron/fetcher.log`) ends
`fetcher complete: N new items` with N above 0, and the run exits 0; the first digest appears under
`/var/lib/memory-amq/news/inbox/new/` after its timer runs.

**The dashboard** (`dashboard/`): a web view of the memory server (memories, mailboxes, boots, the
Hook & Shield state) and a box that sends mail as the operator. It runs from the memory server's
venv on loopback port 8767 and reads memories, boot history and edits only through the server's
`/dashboard/*` routes, with a secret of its own; it lists and reads mail through the two mail groups.

```bash
sudo adduser --system --group --no-create-home --home /nonexistent dashboard
sudo usermod -aG amq-poll,amq-read dashboard
sudo install -d -m 0755 /opt/dashboard && sudo cp -r dashboard/dashboard.py dashboard/templates dashboard/static /opt/dashboard/
sudo sh -c 'umask 077; head -c 32 /dev/urandom | base64 | tr -d "/+=\n" > /root/dashboard.secret'
sudo sed -i '/^DASHBOARD_SECRET=/d' /etc/memory.env   # a rerun replaces, never adds
sudo sh -c 'printf "DASHBOARD_SECRET=%s\n" "$(cat /root/dashboard.secret)" >> /etc/memory.env'
sudo install -m 0640 -o root -g dashboard /dev/null /etc/dashboard.env
sudo sh -c 'printf "DASHBOARD_SECRET=%s\nMEMORY_URL=http://127.0.0.1:8765\n" "$(cat /root/dashboard.secret)" > /etc/dashboard.env'
sudo rm /root/dashboard.secret && sudo systemctl restart memory
# the token the browser holds: every /api/* request, reads included, needs it
sudo install -d -m 0750 -o root -g dashboard /etc/dashboard
sudo install -m 0640 -o root -g dashboard /dev/null /etc/dashboard/post.token
sudo sh -c 'head -c 32 /dev/urandom | base64 -w0 > /etc/dashboard/post.token'
sudo install -m 0644 dashboard/systemd/dashboard.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now dashboard
```
It listens on loopback only; reach it through an SSH tunnel, or bind it wider (`DASHBOARD_HOST` in
`/etc/dashboard.env`) only on a network you trust ([`docs/SECURITY.md`](docs/SECURITY.md)). Paste
`sudo cat /etc/dashboard/post.token` into the page the first time it asks; the browser keeps it.
**Pass line:** the roster on the page equals `agents.json`, the status bar shows the memory server
up with its counts, and `curl` to any `/api/*` path with no token answers `401`.

**The mailbox sweep** (`memory/scripts/amq-hygiene.sh`): once a week, unread mail older than 30 days
moves from `new/` to `cur/` (nothing is deleted; `amq_read` still reads it), so a stale thread no
longer crowds `chorus_init` and `amq_check`.

```bash
sudo install -d -m 0755 /opt/memory/scripts && sudo install -m 0755 memory/scripts/amq-hygiene.sh /opt/memory/scripts/
sudo install -m 0644 memory/systemd/amq-hygiene.service memory/systemd/amq-hygiene.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now amq-hygiene.timer
```
**Pass line:** `systemctl list-timers amq-hygiene.timer` shows the next run.

---

## 8. Verify from a stranger's chair

`install.sh` stage 8 runs the machine checks (the pane count, the roster route, the timers, the mirror
check). Then hand this list to someone who has not read the rest:

1. Open the portal URL and sign in. Count the panes. Does the count equal the enabled agents in
   `config/agents.json`?
2. Click a pane and type something. Close the tab, reopen it: is it still there? (Sessions live in
   tmux; the browser is a viewport.)
3. In one pane, send a message to another agent with `amq_send`. Did the other pane receive a
   doorbell line within a few seconds?
4. Restart the memory service. Did a pane's next `chorus_init` return the same manifest hash as
   before the restart?
5. `sudo systemctl list-timers | grep -E 'hook-detect|memory-backup'` — both scheduled? (The backup
   timer does not ship in this version; until it does, one match is the pass.)
6. `grep -rl "$(hostname)" /srv/portal /etc/caddy /etc/systemd/system/ttyd-*` — only the Caddyfile
   should name the host, and only where you wrote it.
7. `SRV=/srv/portal bash tools/verify-mirror.sh` from your clone. It reports whether the clone is
   pushed, whether a deploy checkout (set `DEPLOY=` if you keep one) has drifted, and whether each
   installed file in `tools/verify-mirror.pairs` still matches its source. The last line is
   `RESULT: fully mirrored` (or `mirrored`, with notes that say why). Run it after every change you
   deploy by hand, and add a pair line for every file you install that it does not know.

If all seven hold, the install is done.
