# LAYOUT — one host, every path, user, port, unit and variable

This page is the target every component is de-sited TO. `INSTALL.md` walks it, each unit
file names it, `tools/verify-mirror.pairs` compares against it, and a default in code that
disagrees with it is a defect. Nothing here is a site fact: every value is either a fixed
default a stranger can copy unchanged, or a `<PLACEHOLDER>` the stranger fills once.

Rules the layout follows:

1. **Code and interpreters under `/opt/<component>`, owned by root.** State under
   `/var/lib/<component>`, secrets under `/etc/<component>`, logs under `/var/log/<component>`.
   No service can write its own code, its own virtualenv or its own gate; nothing that runs as
   a service lives under anyone's home. (The auditor that scans a venv must not run from a venv
   its own user can rewrite.)
2. **One roster.** `/etc/coterie/agents.json` is the only place agent names exist. Units,
   tmux sessions, ttyd routes, mailboxes and portal panes are generated from it. Its shape
   (every reader reads these keys and no others): top level `site`, `orchestrator`,
   `operator`, `session` (default `agent`), `extra_mailboxes` (list), `retired` (list of
   names), `theme` (the portal's palettes); `agents` — a list of `{name, port, color, enabled,
   role}`, `enabled: false` or a name in `retired` marks a seat that keeps its mailbox and
   identity but gets no pane. `role` is free text except one value: exactly one active agent
   carries `role: coordinator` — the Matron's seat (docs/MATRON.md). The memory server sends
   its alarms there, the installer gives that account the orchestrator's token file, and both
   refuse to proceed when the count is not one. `config/agents.example.json` is that shape; the memory server,
   the orchestrator, the renderer, the portal page and the dashboard read `agents`, never a
   differently named list.
3. **One mail root, owned by the memory server alone.** Every Maildir is
   `/var/lib/memory-amq/<mailbox>/inbox/{tmp,new,cur}/`, written only by the `memory` user.
   Two read groups and nothing else: `amq-poll` may LIST (directory `0750 memory:amq-poll`, so a
   member sees filenames — which carry sender and timestamp — and never a body); `amq-read`
   may READ bodies (each message file `0640 memory:amq-read`, set by the server on delivery,
   before the `tmp/`→`new/` rename). The orchestrator is in `amq-poll` only. The news fetcher
   is in neither: it delivers through the memory server's authenticated endpoint, and the
   mailbox is fixed to `news` server-side. A process that parses the internet never holds a
   handle on an agent's mail.
4. **Ports are defaults, not placeholders.** Every listener binds loopback (`127.0.0.1`) on a
   documented default, overridable by one environment variable, except the panes: each listens
   on a unix socket in a directory only its agent and the web server's group can enter
   (`/run/coterie-panes/<agent>/`, below). The only
   port a network sees is the front door's 443, and 3000 where the bundled forge is installed
   (the same Caddy, the same certificate authority, the same allowlist). `INSTALL.md` uses the defaults verbatim.
5. **Secrets reach a unit through `EnvironmentFile=` only** — root-owned, mode 0600, never an
   `Environment=` line (`systemctl show` serves those to every local account) and never a
   drop-in that `systemctl revert` would delete. A unit gets the secrets it uses and no other.
6. **Variable names are the public interface of a unit, and there is one name per thing.**
   The names below are canonical and the only ones the code reads. Migrating a host that used
   earlier names is one edit per env file, done before the code is updated, never a fallback
   in the code.

## Users and groups

| account | kind | home | in groups | runs |
|---|---|---|---|---|
| `<agent>` (one per enabled roster entry) | login user, no password | `/home/<agent>` mode 700 | `agents` | that agent's pane (`ttyd-<agent>`) and CLI |
| `memory` | system | `/var/lib/memory` | `amq-poll`, `amq-read` (it creates every mailbox directory and file, and a non-root process can only chgrp to groups it is in) | `memory.service` |
| `<orchestrator>` (name from `agents.json`) | system, `nologin`, no home | — | `amq-poll` **only** | `chorusd.service` |
| `dashboard` | system, `nologin` | `/var/lib/dashboard` | `amq-poll`, `amq-read` | `dashboard.service`: lists and reads mail through the groups; everything else (memories, boot history, sending as the operator) through the memory server's `/dashboard/*` API with its own scoped secret — it never opens the vector store or the history directory |
| `newstron` | system, `nologin`, no home (`/nonexistent`) | — (state in `/var/lib/newstron`) | — | the four `newstron-*` timers, `pip-audit-scan` |
| `forge` | system, `nologin` | `/var/lib/forge` | — | `forge.service` (the bundled git server; only where it is installed). It owns the init-prompts repository and its hooks; no agent account reaches its files |
| `root` | — | — | — | `hook-detect`, `drift-detect`, everything under `/opt`, Caddy, sudoers |
| group `agents` | — | — | — | what agents deliberately share (a toolchain venv, the init-prompts remote if it lives on this host; nothing else by default) |
| groups `amq-poll`, `amq-read` | — | — | — | rule 3; no login user is ever added to either |

## Paths

| path | owner:group mode | what |
|---|---|---|
| `/etc/coterie/agents.json` | `root:root 0644` | the roster (rule 2) |
| `/opt/memory/{server.py,scripts/,tools/,venv/,models/,VERSION}` | `root:root` (dirs `0755`) | memory server code, its script (`amq-hygiene.sh`: what its timer runs; the news purge is the fetcher's `purger`, through the news API, never a second writer on the vector store) and tools (the doorbell verifier and stats), venv, the pinned embedding model — installed by root, read by `memory`. `VERSION` is written by INSTALL from the release tag; the server reports it as `server_commit` (a tree without `.git` is the normal public case, not a regression) |
| `/etc/memory.env` | `root:root 0600` | `MEMORY_*` variables incl. the secret path segment |
| `/var/lib/memory/chromadb/` | `memory:memory 0700` | vector store |
| `/var/lib/memory/bootstrap-history/` | `memory:memory 0700` | boot manifests, `manifest-hashes/<agent>.log`, patch pre-images |
| `/var/lib/memory-amq/<mailbox>/inbox/{tmp,new,cur}/` | dirs `0750 memory:amq-poll`, files `0640 memory:amq-read` | every mailbox: one per agent, `news`, the operator's, any `extra_mailboxes` (rule 3) |
| `/opt/portal/chorusd.py` | `root:root 0644` | the orchestrator daemon (root-owned: it must not be able to replace itself) |
| `/srv/coterie/boot.git` | `root:root`; only `objects/` and `refs/` are `root:agents`, group-writable (`tools/boot-repo.sh`) | optional: the init-prompts remote every agent clones (`-b main`), if you chose the plain local repository over a forge (no record of who pushed) (docs/INIT-PROMPTS.md §Delivery by the Matron). Never group-writable at the top level, in `hooks/` or in `config`: a push runs its hooks as the pusher |
| `/opt/forge/forgejo`, `/etc/forge/app.ini`, `/etc/forge/setup.token`, `/var/lib/forge/` | `root:root 0755`; `root:forge 0640`; `root:root 0600`; `forge:forge 0750` | the bundled forge (`tools/boot-forge.sh`): one pinned, sha256-verified Forgejo binary the server cannot replace; its config, which it reads and never writes; a token of the operator's forge account (scopes `write:user`, `write:repository`; the admin routes refuse it), kept for adding agents on a later run; its database and repositories |
| `/home/<agent>/.git-credentials` | `<agent>:<agent> 0600` | that agent's token for the init-prompts repository (the bundled forge, or your own): the one place it is stored |
| `/etc/chorusd/pane-key.token` | `<orchestrator>:<coordinator> 0640` | the bearer token for `/matron` and `/pane-key`; the group is the roster's `coordinator` seat |
| `/etc/chorusd/front.token` | `root:<orchestrator> 0640` | the front door's secret, made once by `install-fresh.sh`: Caddy adds it to every `/chorus/*` request it relays (header `X-Coterie-Front`, filled into the site's Caddyfile; the generated snippet carries only a placeholder), and the page's routes (`/fire`, `/doorbell`, `/abort`) refuse any request without it. Read once, at start |
| `/var/lib/chorusd/` | `<orchestrator>: 0711` | daemon state: `doorbell.json` `0600` (budget), `doorbell.log` `0644` (ring ledger; the dashboard reads it by name) |
| `/usr/local/bin/send-keys-to` | `root:root 0755` | the only thing sudoers grants the orchestrator |
| `/etc/sudoers.d/portal` | `root:root 0440` | generated; names the wrapper and nothing else |
| `/srv/portal/portal/{index.html,tokens.css,fonts/,agents.json}` | `root:root 0644` | Caddy's web root; `agents.json` is a copy of the roster, refreshed by the render step |
| `/etc/caddy/Caddyfile`, `/etc/caddy/snippets.portal` | `root:caddy 0640` | the site block (rendered by `install-fresh.sh`, or hand-written by INSTALL §4; holds `<SITE_HOSTNAME>`, the allowlist and the portal password's bcrypt hash), the generated snippet |
| `/etc/coterie/portal.password`, `/etc/coterie/portal.hash` | `root:root 0600` (directory `0755`) | the portal's password, made once by `install-fresh.sh` (`sudo cat` it to read it again), and its bcrypt hash, made from standard input; the Caddyfile carries only the hash. Delete both and rerun to change the password |
| `/etc/caddy/Caddyfile.pre-portal`, `/var/log/caddy/portal.log`, `/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt` | Caddy's | the pre-install backup stage 2 keeps, the site's access log, the internal CA's root (imported once into each browser) |
| `/etc/systemd/system/` | `root:root` | every unit (`memory`, `chorusd`, `dashboard`, `ttyd-<agent>`, `hook-detect`, `drift-detect`, `pip-audit-scan`, `newstron-*`, `amq-hygiene`), installed from `build/` (rendered) or `<component>/systemd/` (fixed) |
| `/etc/tmux.conf` | `root:root 0644` | rendered (`build/tmux.conf`): behaviour lines the paste bridge depends on + the status-bar theme |
| `/etc/systemd/system/ttyd-<agent>.service` | `root:root 0644` | generated, one per enabled agent |
| `/run/coterie-panes`, `/run/coterie-panes/<agent>/` | parent `root:root 0755`; each agent's `<agent>:caddy 2750` (`PANE_PROXY_GROUP`) | made by the `ttyd-<agent>` unit's root steps (`install -d`, owner, group and setgid mode set together, before every start; not `RuntimeDirectory=`, which systemd re-applies before each command and so undid the group). The stale socket is removed before each start; nothing removes it at stop. Holds `pane.sock` (`srw-rw----`, the same owner and group), the pane's only listener |
| `/opt/dashboard/` | `root:root` | dashboard code and static files; runs from the memory venv |
| `/etc/dashboard.env` | `root:dashboard 0640` | the dashboard's variables and `DASHBOARD_SECRET` (scoped to `/dashboard/*`; **never `MEMORY_SECRET_PATH`**) |
| `/etc/dashboard/post.token` | `root:dashboard 0640` | the write token every dashboard POST must carry |
| `/opt/shield/` | `root:root` | Hook & Shield code (its own small memory client; it imports nothing from `/opt/newstron`), `hook_baselines.yaml` (yours), `hook_baselines.example.yaml`. Runs on `/usr/bin/python3` with `python3-yaml` from apt |
| `/etc/shield/{hook-detect,drift-detect}.env` | `root:root 0600` | the memory endpoint + secret the timers alert through |
| `/var/log/hook-detect/`, `/var/log/drift-detect/` | `root:root 0750` | timer logs |
| `/opt/newstron/{*.py,venv/}` | `root:root` | feed fetcher, digest, purger and their venv — installed by root, read by `newstron` |
| `/etc/newstron/newstron.env` | `root:newstron 0640` | memory endpoint + the news secret |
| `/etc/newstron/feeds.yaml` | `root:newstron 0640` | the feed list (config, not code: it does not live under `/opt`) |
| `/var/lib/newstron/`, `/var/log/newstron/` | `newstron:newstron 0750` | fetch state (etags, seen ids), logs |
| `/opt/coterie/` | `root:root` | optional: a clone of the public tree. It is the SOURCE side of `verify-mirror.pairs`; nothing runs from it |

## Ports (loopback, or a unix socket where noted; defaults)

| listener | default | variable | notes |
|---|---|---|---|
| front door (Caddy) | 443 | — | the portal; with the forge's 3000, the only ports on the LAN; TLS internal CA or ACME |
| forge, LAN side (Caddy) | 3000 | `FORGE_PORT` | only where the bundled forge is installed: the operator's sign-in page, proxied to the loopback listener below; same allowlist as the portal |
| forge (Forgejo) | 3001 | `FORGE_LOCAL_PORT` | loopback; what every agent's `~/repos/boot` clones and pushes to, each with its own token |
| `ttyd-<agent>` | no port: `/run/coterie-panes/<agent>/pane.sock` | — | one unix socket per agent (Paths, above); Caddy proxies `/term/<agent>` to it. The roster still carries a `port` per agent (setup writes one, `render-agents.sh` refuses duplicates); nothing listens on it |
| memory server | 8765 | `MEMORY_PORT` | MCP endpoint; the URL's last segment is the secret. Its `/news/*` routes (the feed fetcher's whole interface, bearer `NEWSTRON_SECRET`) and `/dashboard/*` routes (`collections`, `items`, `boots`, `edits`, `send`; bearer `DASHBOARD_SECRET`; `send` fixes the sender to the roster's `operator` and checks recipients against the roster server-side) answer loopback peers only, whatever the bind |
| orchestrator | 8766 | `CHORUSD_PORT` | `/agents`, `/matron`, `/pane-key`, `/hook`; refuses requests that arrived through the front door |
| dashboard | 8767 | `DASHBOARD_PORT` | every `/api/*` read and write needs the dashboard token (`post.token`, held by the operator's browser); only the page shell, stylesheet and fonts are open; bind wider only on a network you trust |

## Units

| unit | user | `EnvironmentFile=` | `ExecStart` |
|---|---|---|---|
| `memory.service` | `memory` | `/etc/memory.env` | `/opt/memory/venv/bin/python3 /opt/memory/server.py` |
| `memory-backup.timer` + `.service` | `memory` | — | nightly copy of `/var/lib/memory` (queued: not in this cut; INSTALL §8 item 5 says so) |
| `chorusd.service` (+ optional drop-in `20-pane-key.conf`) | `<orchestrator>` | — (no secret; the token is a file path) | `/usr/bin/python3 /opt/portal/chorusd.py` |
| `ttyd-<agent>.service` | `<agent>` | — | `ttyd -i /run/coterie-panes/<agent>/pane.sock -b /term/<agent> … sh -c 'umask 022; exec tmux new-session -A -s <session>'`, with `UMask=0007` (for the socket; the shell keeps 022) and three `ExecStartPre=+` root steps: `install -d -m 0755 /run/coterie-panes`, `install -d -o <agent> -g <PANE_PROXY_GROUP> -m 2750 /run/coterie-panes/<agent>`, and `rm -f` of the old `pane.sock` (generated; `session` is one roster field, default `agent`, baked into the wrapper and every unit) |
| `dashboard.service` (`After=memory.service`) | `dashboard` | `/etc/dashboard.env` | `/opt/memory/venv/bin/python3 /opt/dashboard/dashboard.py` |
| `forge.service` | `forge` | — (its secrets are in `/etc/forge/app.ini`, `root:forge 0640`) | `/opt/forge/forgejo --config /etc/forge/app.ini --work-path /var/lib/forge web` (only where the bundled forge is installed) |
| `hook-detect.timer` + `.service` | `root` | `/etc/shield/hook-detect.env` | `/usr/bin/python3 /opt/shield/hook-detect.py` (weekly) |
| `drift-detect.timer` + `.service` | `root` | `/etc/shield/drift-detect.env` | `/usr/bin/python3 /opt/shield/drift-detect.py` (daily): every LIVE path in `verify-mirror.pairs` against its SOURCE in `/opt/coterie`; a deployed file edited in place is the drift it exists to catch |
| `pip-audit-scan.timer` + `.service` | `newstron` | `/etc/newstron/newstron.env` | `/opt/newstron/venv/bin/python3 /opt/newstron/pip-audit-scan.py` (weekly; the venvs it audits are root-owned, rule 1) |
| `newstron-fetch`, `newstron-security`, `newstron-digest`, `newstron-purger` (`.timer` + `.service`) | `newstron` | `/etc/newstron/newstron.env` | `/opt/newstron/venv/bin/python3 /opt/newstron/<script>.py`; the digest posts to the memory server, it does not touch a Maildir |
| `amq-hygiene.timer` + `.service` | `memory` | — | weekly: mail older than 30 days moves from `new/` to `cur/`; nothing is deleted |

## Variables (canonical name → default)

persMEM, the memory server (`/etc/memory.env`):
`MEMORY_HOST` → `127.0.0.1` · `MEMORY_PORT` → `8765` · `MEMORY_DATA_DIR` → `/var/lib/memory/chromadb` ·
`MEMORY_HOME` → `/var/lib/memory` (the working root of the server's file, shell and git tools when they are on; nothing else derives from it) ·
`MEMORY_DEV_TOOLS` → empty (off: the developer tools are not registered; exactly `1` turns on `shell_exec`, the file tools, `git_op` and `diff_generate`, which run as the server's account, `SECURITY.md`) ·
`MEMORY_AMQ_ROOT` → `/var/lib/memory-amq` (one root; the `news` mailbox is under it like every other) ·
`MEMORY_SECRET_PATH` → **required, no default** · `MEMORY_EMBEDDING_MODEL` → `/opt/memory/models/voyage-4-nano` · `MEMORY_HEADS_DIR` → empty (no heads: nothing decides, items only gain `embed_fp`); when set, a directory of `<name>.head.json` + `<name>.ref.npy` readable by the memory user, loaded at start, a head trained on another embedder fingerprint is skipped with a log line
(the model DIRECTORY itself, filled by `fetch-model.py --into` that path; the pinned model is the default) · `COTERIE_AGENTS_JSON` → `/etc/coterie/agents.json` ·
`DASHBOARD_SECRET` → required only when the dashboard is installed (the server's `/dashboard/*` routes check it; the same value sits in `/etc/dashboard.env`) ·
`NEWSTRON_SECRET` → required only when the feed fetcher is installed (the server's news endpoint
checks it; the same value sits in the fetcher's env file) · `MEMORY_SEARCH_URL` → empty (an
optional local web-search endpoint; unset = the tool is absent). Derived, no variable of their
own: bootstrap-history = `dirname($MEMORY_DATA_DIR)/bootstrap-history` (so it sits beside the vector
store wherever that is, and a host whose home is elsewhere still works); the vector store's sqlite file =
`$MEMORY_DATA_DIR/chroma.sqlite3`; the doorbell ledger any tool reads = `$CHORUSD_STATE_DIR/doorbell.log`.

Orchestrator (unit `Environment=` lines are fine here: none of these is a secret):
`COTERIE_AGENTS_JSON` → `/etc/coterie/agents.json` · `CHORUSD_PORT` → `8766` ·
`CHORUSD_AMQ_ROOT` → `/var/lib/memory-amq` (listed, never read; rule 3) · `CHORUSD_STATE_DIR` →
`/var/lib/chorusd` · `CHORUSD_SEND_KEYS_TO` → `/usr/local/bin/send-keys-to` · `CHORUSD_PANE_KEY_TOKEN_PATH` →
`/etc/chorusd/pane-key.token` · `CHORUSD_PANE_KEY_ENABLED` → `0` (the drop-in sets `1`) ·
`CHORUSD_FRONT_TOKEN_PATH` → `/etc/chorusd/front.token`.

Panes (`portal/install/site.env`, read when the units are rendered): `PANE_WORKDIR` → `~` (each pane's shell,
and so Claude Code, starts in the agent's home; Claude Code keys an agent's project memory by this directory,
so set it before the first boot, never after) · `PANE_PROXY_GROUP` → `caddy` (the group the web server runs
in: the only group that can enter a pane's socket directory; change it only if your web server runs as
another group).

Forge (`tools/boot-forge.sh`, read at install; not a unit environment): `FORGE_LOCAL_PORT` → `3001` · `FORGE_PORT` → `3000`.

Dashboard (`/etc/dashboard.env`): `DASHBOARD_HOST` → `127.0.0.1` · `DASHBOARD_PORT` → `8767` ·
`DASHBOARD_POST_TOKEN_PATH` → `/etc/dashboard/post.token` (the browser→dashboard gate, reads and writes) ·
`DASHBOARD_SECRET` → **required, no default** (the dashboard→server bearer for `/dashboard/*`; a scoped
secret, never `MEMORY_SECRET_PATH`) · `MEMORY_URL` → `http://127.0.0.1:8765` (memories, boot history,
edits and sending as the operator all go through the server's `/dashboard/*`; the dashboard never
opens the vector store or the history directory) · `DASHBOARD_OPERATOR` → the roster's `operator` ·
`DASHBOARD_DOORBELL_LEDGER` → `/var/lib/chorusd/doorbell.log` · `DASHBOARD_TOKENS_CSS` →
`/srv/portal/portal/tokens.css` · `DASHBOARD_FONTS_DIR` → `/srv/portal/portal/fonts` · `MEMORY_AMQ_ROOT` and
`COTERIE_AGENTS_JSON` as above (mail listed and read through groups `amq-poll` + `amq-read`; the roster
read directly) · `SHIELD_LOG_DIR` and `SHIELD_BASELINES_FILE` as below (it displays hook state; it does
not own it).

Hook & Shield: `SHIELD_BASELINES_FILE` → `/opt/shield/hook_baselines.yaml` · `SHIELD_LOG_DIR`
→ `/var/log/hook-detect` · `SHIELD_LOOPBACK_PORTS` → `8765 8766 8767` (the listeners a hook may
legitimately call) · `SHIELD_VENVS` → `/opt/memory/venv /opt/newstron/venv` ·
`DRIFT_PAIRS` → `/opt/coterie/tools/verify-mirror.pairs` · `DRIFT_SOURCE_ROOT` → `/opt/coterie` ·
`DRIFT_LOG_DIR` → `/var/log/drift-detect` · `MEMORY_URL` + `NEWSTRON_SECRET` in the env file:
where alerts go.

News feed (`/etc/newstron/newstron.env`, loaded by every `newstron-*` unit and `pip-audit-scan` as their
`EnvironmentFile`): `NEWSTRON_FEEDS_FILE` → `/etc/newstron/feeds.yaml` · `NEWSTRON_STATE_DIR` →
`/var/lib/newstron` · `NEWSTRON_LOG_DIR` → `/var/log/newstron` · `NEWSTRON_DIGEST_HEAD` → `security` (the
memory-server head whose "yes" pulls items into the digest's security section; nothing is pulled until a
head of that name is trained; empty turns the lookup off) · `NEWSTRON_USER_AGENT` → `newstron/0.1
(+https://github.com/ASIXicle/Coterie)` (a feed entry's own `user_agent` still wins) · `MEMORY_URL` →
`http://127.0.0.1:8765` + `NEWSTRON_SECRET`. The mailbox is `news`, fixed by the server: no variable names it.

Portal install scripts read host facts from a gitignored `portal/install/site.env` (never a
unit's environment): `PORTAL_ROOT` → `/srv/portal` (`PORTAL_DIR` → its `portal/` web root) · `SITE_ENV` → the file's own path ·
`TTYD` → `/usr/local/bin/ttyd` · `LAN_IP` → `<LAN_IP>` (required) · `SITE_NAME`,
`TLS_MODE` → `internal`, `CADDY_ALLOWLIST` · and, only on a host laid out differently from this page,
`CHORUSD_PORT`, `CHORUSD_CODE` → `/opt/portal/chorusd.py`, `CHORUSD_STATE_DIR`,
`COTERIE_AGENTS_JSON`, `STOP_HOOK_NAME` → `agent-stop-hook` (installed at `/usr/local/bin/<name>`,
`root:root 0755`), `AMQ_ROOT` + `AMQ_USER` (the pre-layout sudo-find path only). `deploy.sh` alone
also takes `DEST` → `PORTAL_ROOT`, `OWNER` → the orchestrator, `CHORUSD_UNIT` →
`/etc/systemd/system/chorusd.service`, `CADDYFILE` → `/etc/caddy/Caddyfile`, `TMUX_CONF` → `/etc/tmux.conf`. Pane-side variables
the autostart block uses: `NO_CLAUDE` (escape hatch), `AGENT_CLAUDE` (stops a nested shell from starting a second CLI),
`CLAUDE_CODE_NO_FLICKER`.

Exempt from the tables: names a program other than ours defines (`PATH`, `HOME`, `USER`, `TZ`,
git's `GIT_*`, systemd's own). Code reads every name above through `os.environ` directly, not
through a helper that takes the name as a string, so the check below can see every read.

## Placeholders a stranger fills (and nothing else)

`<SITE_HOSTNAME>` (Caddy site block; also `site` in `agents.json`) ·
`<LAN_IP>` (portal `site.env`; the address Caddy serves) · `<orchestrator>` and the `coordinator` role
(names from your roster) · the secret path segment and the two token files (generated on the
host by the commands in INSTALL, never typed). Everything else on this page is a default.

## Reading this page from the code

`tools/layout-check.py` walks every component's `os.environ` / `getenv` reads (and any helper
called with an all-caps name, e.g. `_from_env("X")`) and every absolute path literal under
`/opt /var /etc /srv /run /home /tmp`, and reports each one that has no row on this page. It runs before a component's MANIFEST line is uncommented and in the adversarial
pass; it is a whitelist check, not a leak scanner (the release lint does that). A site's own units live
under `ops/site/<component>/systemd/` (never discovered, never shipped) and are paired to their live
paths in `tools/verify-mirror.pairs`; the check skips exactly those and prints the count, so a site unit
is never silently exempt. A unit anywhere else, paired or not, is checked against the Units table. A line that carries a path as a DETECTION STRING (an IOC list a scanner matches against) says so in
place with `# layout-check: pattern`; the check skips that line and prints how many it skipped.
