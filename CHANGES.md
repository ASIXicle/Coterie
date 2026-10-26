# Changes

## v0.1.6

- **The agent panes use Commit Mono**, weight 600 with bold at 700, served by the portal itself, so
  every device shows the same font. Four woff2 files and the font's license are in
  `portal/portal/fonts/`; the page loads them in each pane and refits the terminal once they are in.
  After the upgrade, hard-refresh the portal (a reload that skips the browser's cache) to load them.
- **The "Co" mark on its own**, square, in dark ink and light ink: `docs/logo.svg` and
  `docs/logo-dark.svg`, for listings and avatars.
- **Weekly venv patching** (`hookandshield/venv-autopatch.py`) can apply a fix that needs other
  packages to move. Before, it installed each fix alone, so such a fix failed its rehearsal and was
  skipped after the second week. Now these move with it, under the same rules as a fix, each as the
  oldest release that fits:
  - a newer version of a package the fix's release requires (aiohttp 3.14.4 needs
    yarl >= 1.25.1);
  - an installed package whose own requirements reject a version the batch installs
    (opentelemetry-sdk 1.45.0 requires opentelemetry-api==1.45.0, so a fix to the API moves the SDK
    too).

  A fix that would need a package added, downgraded or moved to a new major version is still
  reported, not applied. A pre-release is installed only for a package whose installed version is
  one. When a fix is turned down because something it brings has an advisory of its own, the fixes
  that were waiting on it are planned again in the same run. A fix that fails its rehearsal now
  reaches the operator's mailbox even when the rest of that venv's run was only deferred, and a
  version's failure count is cleared once it is installed.
- **`portal/install/deploy.sh`** checks the dashboard's fonts on the port the dashboard's unit
  sets (in the unit, a drop-in or its environment file), where it asked the default port and
  warned. A host whose dashboard unit is not `dashboard.service` names it in `site.env`
  (`DASHBOARD_UNIT=`); a host without the dashboard skips the check. The portal's font check now
  also requires the password's `Basic` challenge on its 401.
- **`docs/SECURITY.md`** is corrected in two places. The orchestrator's hook route can do more than
  mark an agent busy: any local account can end an agent's turn as the orchestrator sees it, clear
  its busy mark or flag it as stopped at its usage limit (nothing it carries is typed into a pane).
  And the memory server's agent door (`/agent/mcp`, a token per agent, with
  `MEMORY_IDENTITY_ENFORCE`) is now described; the installer does not set it up yet.
  `docs/LAYOUT.md` lists its variables.

## v0.1.5

- **The portal's mark** is now "Co": the Selectric C with a small o set in its opening, in the page's
  top-left corner and the browser tab. It replaces the lone C.
- **`portal/install/deploy.sh`** no longer ends a deploy that worked with STOP. Its font check
  accepts the portal's password prompt (HTTP 401) when the font file is in place; before, every
  portal behind its password failed the check after the deploy had finished. Its doorbell check
  reads the orchestrator's settings the way systemd starts it, drop-in files included, where it
  read the unit file alone.
- **INSTALL and the README** say how to upgrade, and point here.

## v0.1.4

### Security: excluded addresses were asked for the portal password (v0.1.0 to v0.1.3)

Every install that kept a client allowlist was affected, and one is on by default
(`CADDY_ALLOWLIST="private_ranges"` in `portal/install/site.env`). Caddy runs its password check
before the allowlist's `abort` unless the Caddyfile says otherwise, and it did not. So an address
outside the allowlist that could reach the portal was not cut off: it was asked for the portal
password. A wrong password got a 401 and a right one got a dropped connection, which let any such
address test guesses against the password. `docs/SECURITY.md` says such a client "is cut off before
anything is served"; on these versions that was not true.

The rendered Caddyfile's global block now carries `order abort before basicauth`, and an excluded
address is dropped before the password is asked for.

To fix an existing install, either upgrade, in the tree you cloned (`/opt/coterie` with the
one-line install; adjust the path if you cloned elsewhere):

```bash
sudo git -C /opt/coterie pull --ff-only && sudo bash /opt/coterie/setup.sh
```

Setup offers back the roster you already have, and when `/etc/caddy/Caddyfile` is the one the
installer wrote (its first line says it was rendered by `install-fresh.sh`), it replaces it with the
new render, keeps the git server's block, validates it and restarts Caddy. A Caddyfile you merged the
portal into by hand is left alone, so for that one, or without upgrading, add the line yourself
inside the global block (the `{ … }` at the top of the file), then:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl restart caddy
```

Restart, not reload: the portal's Caddyfile turns Caddy's admin API off.

### Also in this release

- **hook-detect** reports, rather than opens, a FIFO, socket or device where a watched file
  belongs. The baseline sha is now taken from the bytes the scan read, not from a second open, so a
  file swapped between two reads no longer scans clean. A file with an approved baseline that was
  deleted, or replaced by a directory, is reported; before, the scan said CLEAN. A byte that isn't UTF-8 no
  longer stops the scan, and a file over 4 MiB is reported, not read. Self-test:
  `python3 hookandshield/test_scan_files.py`.
- **The pane check** in `install-fresh.sh` now has a negative control: each pane must also refuse
  every other agent's account and `nobody`. A check that cannot run says so instead of passing.
- **The memory server** can give each agent a token of its own (`/agent/mcp`). The identity tools
  then compare the name they are given with the caller's, logging a mismatch, or refusing it with
  `MEMORY_IDENTITY_ENFORCE=1`. Setup does not configure this yet. With `MEMORY_AGENT_TOKENS` unset
  and `MEMORY_OPERATOR_DOOR` at its default (`on`), an install works exactly as before. The three
  settings are described near the top of `memory/server.py`.
- **The release lint** reads binaries' metadata (PNG and JPEG text and EXIF, font name tables)
  under every pattern, fails a PNG with any chunk outside `IHDR/PLTE/IDAT/IEND`, and runs a second
  pass with no exclusions. `docs/RELEASE-PROCESS.md` describes both passes.
