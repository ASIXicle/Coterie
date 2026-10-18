# Changes

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
