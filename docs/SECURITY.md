# Security model

What the scaffold protects, what it deliberately does not, and the one trust decision you
are making when you install it. Written for the reader deciding whether to run this on a
machine they care about.

## Trust boundaries

- **The host is the boundary.** Everything runs on one machine. There is no multi-tenant
  claim: the orchestrator can type into every agent's pane, and root can read everything.
  Any other account reaches an agent's terminal only the way a browser does, through the front
  door and its password (see "The panes" below).
- **Agents are separate Unix users with private homes.** As installed by INSTALL §2, every
  home directory is mode 700: no agent, and no service user, reads another's files. Anything
  agents share goes through the memory server or a directory made for sharing, never through
  an open home. The portal is served from `/srv/portal`, never from under an agent's home;
  a web root inside a home forces that home open and everything beneath it with it. Mailboxes live under
  one root owned by the memory server (`docs/LAYOUT.md` rule 3): directories `0750 memory:amq-poll`,
  message files `0640 memory:amq-read`, both set by the server before a delivery is renamed into
  `new/`. The orchestrator is in `amq-poll` only, so it can see that mail arrived (names carry the
  sender and a timestamp) and never a body. Agents reach mail only through the server's tools,
  and those tools cannot tell which agent is calling them (next item).
- **The memory server (persMEM) listens on loopback** and its MCP endpoint carries a secret path
  segment from an environment file that is never in the repository. Anyone on the host
  who can read that file can read and write every agent's memory. Rotate it like a password.
  **The endpoint has no caller identity.** Every agent you register holds it, and through it
  any holder can read any agent's memories and any agent's mailbox, and send mail under any
  agent's name. The MCP tools refuse two things only: the operator's mailbox as a sender
  (only the dashboard sends as the operator, below), and a message id that is not a plain
  file name. Keeping each agent to its own mail is a convention the agents follow, not a
  boundary the server enforces.
  The server redacts the path from its own log lines: the startup line prints it as
  `<secret, N chars>` and every access-log line as `<secret>`, so whoever you let read the
  journal is not handed the key. No shipped service uses the path at all: the news feed,
  Hook & Shield and the dashboard reach the server through routes of their own, with a bearer
  token in a header, so no URL they log carries a secret. The agents' connectors are the
  path's only users. One exception: while `install.sh` registers each agent's connector, the
  endpoint is on the `claude mcp add` command line for a moment, where any local account can read
  it (Claude Code takes it no other way). If you put a reverse proxy in front of the
  endpoint, for a remote MCP client say, filter the request URI in **both** its access log and
  its default or error log, because an error line (a 502 while the server restarts) carries the
  full request. In Caddy 2.6 and later the same block goes in the site's `log` and, as the
  global options `log`, at the top of the Caddyfile:

  ```
  format filter {
      wrap json
      fields {
          request>uri regexp "^/[A-Za-z0-9_-]{20,}/" "/<secret>/"
      }
  }
  ```

  To rotate the path: it lives in the server's environment file, in each client's environment
  file, in the proxy's route matcher if the proxy routes on it, and in any remote connector
  configured for the account. Change all of them in one hard cut, with every agent session
  exited first: a Claude Code session reads its connector list at start and is not documented
  to re-read it, so a session started under the old path cannot be trusted to follow the cut.
  A secret reaches a unit only through an `EnvironmentFile=` that root alone can read, never
  through an `Environment=` line in the unit or a drop-in:
  systemd serves `Environment=` values to every local account through `systemctl show`, and
  a mode-600 drop-in does not change that. And never `systemctl revert` a unit whose drop-ins
  hold secrets or site configuration; it deletes the unit's whole drop-in directory.
- **The server's developer tools are off.** `shell_exec`, `file_read`, `file_write`,
  `file_patch`, `git_op` and `diff_generate` run as the memory server's own account, which owns
  every memory, the mailboxes and the server's Python environment. `shell_exec` and `git_op`
  hand their text to bash and the file tools take any path, so with them on, the MCP secret is a
  shell as that account. They are not registered unless `MEMORY_DEV_TOOLS=1` (`docs/LAYOUT.md`),
  and the server's start line says which. Leave them off: each agent has its own shell as its
  own account. The command list in front of `shell_exec` is not a boundary (it checks the first
  word of each segment, and `awk`, `find -exec`, `sed` and a newline all reach bash); the
  switch is. `web_fetch` stays on and fetches `http` and `https` only, as the first URL and as a
  redirect: Python's default opener also reads `file://`, which would hand any holder of the
  secret every file the server can read. It and `web_search` make outbound requests from the
  server's own address, loopback included, so anything on `127.0.0.1` that answers a plain GET
  answers them too.
- **The forge is where "who wrote this" is decided.** The init prompts are what an agent boots
  from, so who may change them, and whether a change can be pinned on its author, matters. With
  the bundled forge (`tools/boot-forge.sh`) or your own, each agent pushes over HTTP with its
  own account and token, kept in its own home at mode 600. The forge records the account that
  pushed, and an author name typed into a commit does not change that record; `main` refuses
  forced pushes and deletion; the repository and its hooks belong to the forge's account, which
  no agent can act as. The bundled forge listens on loopback, with registration off and
  sign-in required. Through the front door's Caddy on port 3000, behind the portal's client
  allowlist, the LAN reaches its whole HTTP surface: the web pages, the API and git over HTTPS, each
  behind a sign-in or a token. So an agent's token works from any allowed machine on the LAN,
  not only from this host. Its limits: an agent with a shell can read its own token, and no
  other account's (no token is ever passed as a command-line argument, which every local
  account could read), so it can push as itself and as no one else; and one token per agent
  proves the account, not what prompted the agent. A token that was replaced stays valid on
  the forge until you delete it on that account's page. `/etc/forge/setup.token` (root, 0600)
  is a token of your own forge account, limited to that account and its repositories; the
  forge's administrator routes refuse it, but it can change the boot repository, so treat it
  like root. With your own forge, the proof is only as good as your
  accounts: one account per agent proves the pusher, several tokens on one account do not.
  The third choice, a plain repository on this host (`tools/boot-repo.sh`), has no accounts at
  all: any agent can commit under any name and can write a branch file directly. It is closed
  against one agent running code as another, and that is all it promises.
- **The dashboard is one gated surface.** Every `/api/*` request — the reads (every memory,
  every mailbox, every agent's boot and hook state) as much as the writes — needs a bearer
  token read at startup from a root-installed file (an absent, unreadable or empty file refuses
  every request); the browser attaches the token it was given once. Only the page shell, its
  stylesheet and fonts answer without it. It binds to loopback unless you say otherwise; bound
  wider, whoever reaches the address sees a login-less shell and nothing behind it. (Until
  2026-09-25 the reads were open to whoever reached the port; that was a defect, not a design.)
  Its one write is the compose box. A composed message is checked twice more: its sender is always the operator's
  mailbox, fixed by the server, so a caller cannot ring an agent's doorbell under another
  agent's name; and its recipient is checked against the roster before it becomes a path, so
  a name that is not an active agent's never touches the filesystem. Treat the token like the
  memory secret: it is a right to put words in the operator's mouth.
- **The portal's front door is Caddy** with an internal certificate authority, a client
  allowlist (`remote_ip`) and a password. A client outside the list is cut off before anything
  is served. A client inside it is asked for the password (HTTP Basic, over TLS) on every
  request except the root certificate, which is public and which a browser needs before it
  trusts the site. Setup makes the password once, keeps it root-only at
  `/etc/coterie/portal.password` (read it again with `sudo cat`), and the Caddyfile carries only
  its bcrypt hash, made from standard input. The user name is the roster's operator. Whoever
  holds the password types into every pane, so treat it like the memory secret.
  What the list holds is your choice at setup. The default is Caddy's `private_ranges`, which
  Caddy 2.6 expands to the three IPv4 blocks of RFC 1918, the locally assigned half of IPv6
  unique-local space, and loopback on both.
  It admits every device on any private network that can reach the host. It does not admit the
  shared range that VPN overlays use, or IPv6 link-local: to browse over a VPN, take the other
  choice and list the address the VPN gives you. That choice narrows the list to the addresses
  you name. An empty list admits anyone who reaches ports 443 and 3000.
  The panes behind it are writable terminals: whoever
  gets through types as that agent. Expose the portal only on a network you trust, with
  the list narrowed to your own devices on any network you share, and never on an interface
  strangers can reach. On a LAN that is the whole story; over an overlay
  network you add the network's own access control in front of it, and a host firewall rule
  scoped to the overlay interface behind it. Three layers, each sufficient on its own.
  When setup installs the Caddyfile (it replaces only the package's untouched one), Caddy's admin
  API is off, so changing the front door takes a root edit of the Caddyfile and a restart, and no
  local account can reconfigure it with a request. If you keep a Caddyfile of your own, put
  `admin off` in its global options and the password in its site block yourself: the Caddyfile
  `portal/install/install-fresh.sh` writes to `portal/build/Caddyfile` is a working example of
  both, and INSTALL §4 stage 2 says what each part is for. Caddy's default is an endpoint on loopback that
  any local account can use to load a new configuration, which drops the allowlist or serves
  the certificate authority's key.
- **The panes.** Each agent's terminal server listens on a unix socket,
  `/run/coterie-panes/<agent>/pane.sock`, never on a port. Its directory belongs to that agent
  and to the web server's group, mode 2750, so only the agent, the web server and root can open
  the socket. The web server relays a pane only to a client that passed the front door, the
  allowlist and the password. So an account on this host that is not the agent reaches its
  pane the way a laptop on the LAN does, with the password, and not otherwise. A pane is a
  shell as its agent, with that agent's files, tokens and memory connector, which is why the
  gate is checked rather than assumed: the install's probe fails unless every pane's directory
  is exactly mode 2750 in the web server's group.

## The trust decision: the orchestrator's user

The orchestrator daemon types prompts into agent panes and rings the doorbell. It does that
with one narrow privilege grant: the orchestrator's Unix user may run a root-owned wrapper,
`send-keys-to`, as the other agents' users, and nothing else. The wrapper takes exactly two
arguments, a fixed session name and the text, refuses control bytes, wraps the text in a
bracketed paste itself, and hands it to the terminal multiplexer as one literal argument, so
nothing in the text can become a multiplexer command or a control key; it logs every call to
syslog with the caller, the target, the byte count and a hash. Two dependencies, stated
plainly: the bound holds because the pane's program is the agent CLI, started with `exec`
and with no shell behind it to fall through to; and the syslog line is best-effort (a logging
failure never blocks a message, and journald rate-limits under a flood), so the multiplexer
pane itself remains the transcript and the orchestrator's own ledger the count. The grant and the wrapper are
generated from `config/agents.json` and installed under `/etc/sudoers.d/` and
`/usr/local/bin/`. (Granting the multiplexer's own send-keys command instead would allow
command chaining and a shell as the target user with no transcript; that is why the wrapper
exists.)

**Read that plainly: whoever controls the orchestrator's user controls every pane.** A
compromise of that one account is a compromise of every agent's session, including anything
those agents are authorised to do on your behalf. This is the design, not an oversight: the
scaffold exists so that one operator can drive several agents from a browser, and something
has to hold the keys. Mitigations that ship:

- the orchestrator runs as a dedicated system user (no shell, no home, no other role, in no
  group but `amq-poll`), so no agent's account holds the grant, and the daemon refuses to
  start under a name that is neither an agent nor the user it is actually running as;
- the grant is the narrowest that works: one root-owned wrapper with a fixed argument
  shape, so the text typed can never be parsed as a command;
- the orchestrator listens on loopback only and is reached through the same front door as
  everything else. The routes the page posts to (`/fire`, `/doorbell`, `/abort`) answer only a
  request that Caddy relayed from past the password, proven by a secret Caddy adds and the
  daemon checks (`/etc/chorusd/front.token`, `root:<orchestrator> 0640`; a client's own copy of
  the header is overwritten). So a local account posting to the loopback port is refused, and
  cannot type a `[CHORUS]` line, which agents take as the operator's words, into any pane. The
  same routes accept only a JSON body from the portal's own origin, so another web page open in
  the operator's browser cannot fire a round either. The loopback routes keep their own gates:
  `/matron` and `/pane-key` the bearer token below, and `/hook` (each agent's Stop hook) the proxy
  fence alone, which costs availability, never injection;
- every agent's settings file, including its hooks, is hash-baselined by Hook & Shield and
  any change alerts within the scan interval.

Mitigations you choose: keep the orchestrator's user off every other duty and out of every
other group as the host evolves; treat any account that can `sudo` to it like the secret
file above.

## The second key: one whitelisted keystroke

The orchestrator's `/pane-key` route types one command from a list hard-coded in the daemon
(today `/clear`, which drops the target agent's context) into any agent's pane. It is the only
route that can erase rather than inject, so it carries its own gate on top of the trust decision
above:

- a bearer token, read at startup from a file that only the orchestrator's user and the
  Matron's account can read; an empty or unreadable file refuses every call;
- a proxy fence: this route, the cold-boot route and the hook route answer on loopback only,
  and any request carrying a forwarded-for header from the front door is refused before
  anything else is read. Loopback alone is not a boundary, because every local account can
  reach it, so the cold-boot route (`/matron`, which types a boot pointer into an agent's
  pane; the pointer names a full commit id, so the git remote behind it is an availability
  dependency, not a trust one) requires this same bearer token after the fence. The hook route stays
  fence-only: the Stop hook that calls it runs as each agent, so gating it needs a per-agent
  credential, and what it can do is mark an agent busy for a bounded time, an availability
  cost, not an injection;
- a per-agent cooldown of one minute, and a refusal while the target agent is mid-turn;
- a kill switch that is one systemd drop-in, so revoking the feature is a file removal and a
  restart, with no other state to unwind.

Rejections that happen before authentication are written to the journal, never to the
orchestrator's ledger, so an unauthenticated caller cannot grow the audit file. Read plainly:
**whoever holds the token can clear any agent's context, once a minute per agent, and can
dispatch a cold-boot pointer to any agent.** Treat the token like the memory secret. The
kill-switch drop-in turns off the keystroke route only; dispatch stays on while the token file
is readable, and an empty or unreadable token file refuses both routes. What the holder cannot
do: type anything outside the list. The list is code, and changing it is a reviewed commit that
the settings-hook and release gates both see.

## What Hook & Shield does and does not do

It answers one bounded question weekly and on demand: did any agent's settings hooks change
from the hash you approved? A hash miss alerts regardless of content; the content analyser
only ranks severity. It does not inspect what an agent does inside a session, and it does not
protect against a hook you approved. A re-approval is one command and one line in the
registry, and that friction is the point.

## Out of scope, on purpose

No network exposure of the memory server, no remote code execution surface beyond the
panes themselves, no secrets in the repository, no telemetry. Each of those is a line you
can verify with grep, and the release lint that gates every public build verifies the
last two on every commit.
