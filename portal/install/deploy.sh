#!/bin/bash
# deploy.sh -- put the repo's portal/ and chorus/ where Caddy and systemd read them,
# then restart chorusd. The repo is canon; the deployed copy is a build product.
# Successor, 2026-09-03; site-neutral 2026-09-11.
#
#   sudo bash install/deploy.sh            # copy + restart
#   sudo bash install/deploy.sh --check    # show what would change, touch nothing
#   sudo bash install/deploy.sh --no-doorbell  # deploy with no AMQ facts (doorbell off) on purpose
#
# Also installs build/tmux.conf to /etc/tmux.conf (TMUX_CONF=) -- the pane interiors are terminal
# content, so the ttyd theme and that file are the only way they match the chrome.
#
# Destination is PORTAL_ROOT from install/site.env (default /srv/portal; never under a home);
# the copy is owned by the orchestrator (from the roster). chorusd learns its AMQ facts from Environment= in chorusd.service
# (rendered by render-agents.sh from site.env); if site.env names an AMQ_ROOT but
# the installed unit does not carry it, deploy stops before restarting chorusd.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"
DEST=${DEST:-$PORTAL_ROOT}
OWNER=${OWNER:-$ME}
TMUX_CONF=${TMUX_CONF:-/etc/tmux.conf}
[ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo bash $0" >&2; exit 1; }
# In-memory compile, not py_compile: py_compile writes __pycache__ beside the source as whoever
# runs this (root), leaving a root-owned directory inside the orchestrator-owned tree
# (first seen 2026-09-16 after the chorusd uid split). compile() catches the same errors.
python3 -c 'import sys; compile(open(sys.argv[1]).read(), sys.argv[1], "exec")' "$here/chorus/chorusd.py" && echo "OK   chorusd.py compiles"
echo "OK   roster parses ($AGENTS; orchestrator $ME)"

if [ "${1:-}" = "--check" ]; then
  diff -ru "$DEST/portal" "$here/portal" || true
  diff -u "$DEST/chorus/chorusd.py" "$here/chorus/chorusd.py" || true
  [ -f "$here/build/tmux.conf" ] && { diff -u "$TMUX_CONF" "$here/build/tmux.conf" || true; }
  exit 0
fi
# Guard (review, 2026-09-11): chorusd reads its AMQ facts from Environment= in the unit; with none,
# the maildir poll is off and the doorbell is dark. Never deploy into that state silently.
UNIT=${CHORUSD_UNIT:-/etc/systemd/system/chorusd.service}
if [ ! -f "$site_env" ]; then
  echo "STOP no site.env at $site_env (or SITE_ENV): deploy.sh needs the host facts to know whether the doorbell is configured;" >&2
  echo "     cp install/site.env.example install/site.env and fill it in" >&2
  exit 1
fi
# --- doorbell guard
# The mail root chorusd polls is the one systemd starts it with: the unit AND its drop-ins (a host may
# set CHORUSD_AMQ_ROOT in a drop-in), so it is read from systemd, not from the unit file.
unit_root=$(systemctl show "$(basename "$UNIT")" -p Environment --value 2>/dev/null | tr ' ' '\n' | sed -n 's/^CHORUSD_AMQ_ROOT=//p' | tail -n 1)
dark=""
if [ -n "${AMQ_ROOT:-}" ]; then
  if [ "$unit_root" != "$AMQ_ROOT" ]; then
    echo "STOP chorusd's unit, with its drop-ins, does not carry CHORUSD_AMQ_ROOT=$AMQ_ROOT (site.env sets it); the running chorusd would poll nothing:" >&2
    echo "     bash install/render-agents.sh && install -m 0644 build/chorusd.service $UNIT && systemctl daemon-reload" >&2
    exit 1
  fi
elif [ -n "$unit_root" ]; then
  if [ -d "$unit_root" ]; then
    echo "OK   site.env names no AMQ_ROOT; chorusd's unit sets CHORUSD_AMQ_ROOT=$unit_root"
  else
    dark="chorusd's unit sets CHORUSD_AMQ_ROOT=$unit_root, which does not exist"
  fi
elif [ -d /var/lib/memory-amq ]; then
  # chorusd's own default (docs/LAYOUT.md): with no CHORUSD_AMQ_ROOT it polls this root directly,
  # as a member of the group that may list it. An empty AMQ_ROOT is the normal state there.
  echo "OK   site.env names no AMQ_ROOT: chorusd polls the layout's mail root, /var/lib/memory-amq"
else
  dark="site.env sets no AMQ_ROOT/AMQ_USER and /var/lib/memory-amq does not exist"
fi
if [ -n "$dark" ]; then
  echo "WARN $dark: chorusd will run with the DOORBELL OFF (no maildir poll, no early-stop)." >&2
  if [ "${1:-}" != "--no-doorbell" ]; then
    echo "STOP re-run with --no-doorbell to deploy in that state on purpose." >&2
    exit 1
  fi
fi
# --- end doorbell guard
# Guard (review, 2026-09-11): DEST must be where the running services READ from, or this copy is
# a silent no-op: chorusd restarts on the old file and "LIVE chorusd" is reported anyway.
CADDYFILE=${CADDYFILE:-/etc/caddy/Caddyfile}
if ! grep -qsE "^\s*ExecStart=.*\s$CHORUSD_CODE(\s|$)" "$UNIT"; then
  echo "STOP $UNIT does not run $CHORUSD_CODE (CHORUSD_CODE in $site_env; default /opt/portal/chorusd.py); the deploy would not be what runs." >&2
  echo "     set CHORUSD_CODE in $site_env to the path the unit's ExecStart uses, or re-render and reinstall the unit for the new path" >&2
  exit 1
fi
if ! grep -qsE "root\s+\*?\s*$DEST/portal(\s|$)" "$CADDYFILE"; then
  echo "STOP $CADDYFILE does not serve $DEST/portal; the copied page would not be what the browser gets." >&2
  echo "     set PORTAL_ROOT in $site_env to the served root, or move the Caddy root first, then deploy" >&2
  exit 1
fi
# The web root is root's: the orchestrator serves nothing from it and must not be able to rewrite the page
# the operator's browser runs (it types into every pane). $DEST/chorus stays the orchestrator's: on a
# pre-layout host its state (the doorbell ledger) lives there (adversarial pass, 2026-10-02).
install -d -o root -g root -m 0755 "$DEST/portal"
install -d -o "$OWNER" -g "$OWNER" "$DEST/chorus"
install -o root -g root -m 0644 "$here"/portal/index.html "$DEST/portal/"
install -o root -g root -m 0644 "$cfg" "$DEST/portal/agents.json"   # the page reads the roster as agents.json
install -o root -g root -m 0644 "$here"/portal/tokens.css "$DEST/portal/"
# Webfonts. tokens.css @font-face src is ROOT-relative (/fonts/...), so it resolves against
# the host and not the stylesheet: the files must answer at /fonts/ on every host that serves
# tokens.css. Caddy gets it from this root; the dashboard has its own /fonts/ route.
install -d -o root -g root -m 0755 "$DEST/portal/fonts"
install -o root -g root -m 0644 "$here"/portal/fonts/* "$DEST/portal/fonts/"
# the daemon is ROOT-owned (docs/LAYOUT.md: it must not be able to replace itself); the orchestrator
# only reads it. Its state dir (CHORUSD_STATE_DIR) is the orchestrator's; this file is not.
install -d -m 0755 "$(dirname "$CHORUSD_CODE")"
install -o root -g root -m 0644 "$here"/chorus/chorusd.py "$CHORUSD_CODE"
# the typing wrapper: rendered by render-agents.sh, root-owned so the sudoers target cannot be edited by any agent
if [ -f "$here/build/send-keys-to" ]; then
  install -o root -g root -m 0755 "$here/build/send-keys-to" /usr/local/bin/send-keys-to
  echo "OK   /usr/local/bin/send-keys-to installed (root:root 0755)"
else
  echo "STOP build/send-keys-to not rendered (run install/render-agents.sh); chorusd types through the wrapper and nothing else" >&2
  exit 1
fi
# the tmux config: behaviour + theme, rendered from the roster's .theme. The pane INTERIORS are
# terminal content -- no portal CSS can reach them -- so the ttyd theme and this file are the only
# way the terminals match the chrome.
# Guard (read first and restore as found): the generated file replaces a
# hand-written one whose clipboard lines the portal's paste bridge depends on. Refuse to install a
# config that drops any setting that is live right now, whatever the reason.
if [ -f "$here/build/tmux.conf" ]; then
  if [ -f "$TMUX_CONF" ]; then
    # Protect the BEHAVIOUR options only -- the ones the generated file declares in its
    # own `# behaviour-options:` manifest (scrollback, resize, OSC-52 clipboard). Theme
    # lines are meant to change; freezing the whole file would block every retheme, which
    # is what the first version of this guard did.
    opts=$(sed -n 's/^# behaviour-options: //p' "$here/build/tmux.conf")
    if [ -z "$opts" ]; then
      echo "STOP $here/build/tmux.conf carries no behaviour-options manifest; re-render with the current install/render-agents.sh" >&2
      exit 1
    fi
    dropped=$(while IFS= read -r l; do
                case "$l" in \#*|"") continue;; esac
                # option name = first word after set/setw that is not a -flag. Done with
                # awk rather than `set -- $l`, which would clobber the positional parameters.
                o=$(printf '%s\n' "$l" | awk '{for(i=2;i<=NF;i++) if ($i !~ /^-/) {print $i; exit}}')
                case " $opts " in *" $o "*) grep -qxF "$l" "$here/build/tmux.conf" || echo "  $l";; esac
              done < "$TMUX_CONF")
    if [ -n "$dropped" ]; then
      echo "STOP $here/build/tmux.conf changes or drops a BEHAVIOUR setting that is live in $TMUX_CONF:" >&2
      echo "$dropped" >&2
      echo "     these are not theme: the portal's paste bridge depends on the clipboard lines." >&2
      echo "     carry them into the behaviour block of install/render-agents.sh, re-render, and deploy again" >&2
      exit 1
    fi
    cp -a "$TMUX_CONF" "$TMUX_CONF.bak-$(date +%Y%m%d%H%M%S)"
  fi
  install -o root -g root -m 0644 "$here/build/tmux.conf" "$TMUX_CONF"
  echo "OK   $TMUX_CONF installed (previous kept as .bak-*)"
  # Both halves of the pane interior are pending after this script, and neither is obvious
  # from the output. Say so here or the operator hard-refreshes and sees no change at all.
  echo "NOTE the pane interior is NOT live yet -- two steps this script deliberately does not take:"
  echo "  1. ttyd units carry the terminal colours and are reviewed-then-copied by a human:"
  echo "       install -m 0644 $here/build/ttyd-*.service /etc/systemd/system/ && systemctl daemon-reload"
  echo "       systemctl restart $(for b in $AGENTS; do printf 'ttyd-%s ' "$b"; done)"
  echo "     (safe mid-session: ttyd is only the web front-end; tmux keeps each agent alive and"
  echo "      the browser reattaches on refresh)"
  echo "  2. running tmux servers keep the old theme until reloaded:"
  echo "       for b in $AGENTS; do sudo -u \$b tmux source-file $TMUX_CONF; done"
  echo "     (an agent with no running server reports 'no server running' and is skipped)"
else
  echo "NOTE build/tmux.conf not rendered (run install/render-agents.sh); tmux keeps its stock green status bar" >&2
fi
# the Stop hook body, root-owned: every agent's settings.json invokes it by a fixed path,
# so an agent pointing at a missing script means a silently dark doorbell -- that pane would
# simply never report a turn end and would look permanently busy.
if [ -f "$here/build/$STOP_HOOK_NAME" ]; then
  install -o root -g root -m 0755 "$here/build/$STOP_HOOK_NAME" "/usr/local/bin/$STOP_HOOK_NAME"
  echo "OK   /usr/local/bin/$STOP_HOOK_NAME installed (root:root 0755)"
else
  for b in $AGENTS; do
    if grep -qs "$STOP_HOOK_NAME" "/home/$b/.claude/settings.json" 2>/dev/null; then
      echo "STOP $b's settings.json calls $STOP_HOOK_NAME and build/$STOP_HOOK_NAME is not rendered;" >&2
      echo "     that agent's turn ends would go unreported and its pane would look permanently busy." >&2
      echo "     run: bash install/render-agents.sh" >&2
      exit 1
    fi
  done
  echo "NOTE build/$STOP_HOOK_NAME not rendered; agents still on the inline Stop hook are unaffected" >&2
fi
if grep -qs 'send-keys-to' /etc/sudoers.d/portal; then
  if grep -qs '/usr/bin/tmux send-keys' /etc/sudoers.d/portal; then
    echo "STOP sudoers still carries a direct tmux rule; chorusd no longer uses it -- install build/sudoers.portal (wrapper only)" >&2
    exit 1
  fi
  echo "OK   sudoers grants the wrapper only"
else
  echo "STOP sudoers has no send-keys-to rule (/etc/sudoers.d/portal): chorusd cannot type; install build/sudoers.portal first" >&2
  exit 1
fi
systemctl restart chorusd
sleep 1
systemctl -q is-active chorusd && echo "LIVE chorusd" || { echo "DEAD chorusd -- journalctl -u chorusd" >&2; exit 1; }
curl -s "http://127.0.0.1:$CHORUSD_PORT/agents" && echo
# Font probe. If /fonts/ 404s the page does not break -- it silently falls back to
# ui-sans-serif, and only on the host that is missing the route, so the portal can look
# perfect while the dashboard is wrong. Nobody would think to check it; check it here.
FONT=archivo-latin.woff2
probe() {  # $1 = label, $2 = url, $3 = hard|soft, $4 = the font's file, for a surface behind a password
  # The headers, then the status code on a line of its own (000 when nothing answers).
  out=$(curl -sk -o /dev/null -D - -w '\n%{http_code}' -m 5 "$2" 2>/dev/null || true)
  code=$(printf '%s\n' "$out" | tail -n 1); [ -n "$code" ] || code=000
  if [ "$code" = 200 ]; then
    echo "OK   $1 serves /fonts/$FONT"
  elif [ "$code" = 401 ] && [ -n "${4:-}" ] && [ -f "$4" ] && grep -qi '^www-authenticate:[[:space:]]*basic' <<<"$out"; then
    # The portal asks for its password on /fonts/ too, so this unauthenticated probe gets 401 with the
    # password's Basic challenge: Caddy answers, and the file it serves from is in place. A 401 without
    # that challenge is not the portal's password.
    echo "OK   $1 answers behind its password (HTTP 401, Basic), and $4 is in place"
  elif [ "$3" = hard ]; then
    echo "STOP $1 does not serve /fonts/$FONT (HTTP $code): tokens.css would fall back to the system font" >&2
    exit 1
  else
    echo "WARN $1 does not serve /fonts/$FONT (HTTP $code) -- if that surface is up, its fonts are wrong" >&2
    echo "     (the dashboard is optional and serves its own /fonts/ route; if it is installed, restart it after a pull)" >&2
  fi
}
probe "portal (https://${LAN_IP:-127.0.0.1})" "https://${LAN_IP:-127.0.0.1}/fonts/$FONT" hard "$DEST/portal/fonts/$FONT"
# --- dashboard port
# The port is the one systemd starts the dashboard with: DASHBOARD_PORT in its Environment= (a drop-in
# may set it), else in its EnvironmentFile (the shipped unit reads /etc/dashboard.env), else the
# dashboard's default. Its unit is dashboard.service unless site.env's DASHBOARD_UNIT names another; a
# host without it skips the probe. (2026-10-20: this probe read its own environment, asked the default
# port, got no answer and warned, while the dashboard served the font on the port its unit sets.)
dunit=${DASHBOARD_UNIT:-dashboard.service}
dport=""
if [ "$(systemctl show "$dunit" -p LoadState --value 2>/dev/null)" = loaded ]; then
  dport=$(systemctl show "$dunit" -p Environment --value 2>/dev/null | tr ' ' '\n' | sed -n 's/^DASHBOARD_PORT=//p' | tail -n 1 || true)
  if [ -z "$dport" ]; then
    while IFS= read -r f; do
      f=${f% (ignore_errors=*}
      if [ -r "$f" ]; then
        p=$(sed -n "s/^[[:space:]]*DASHBOARD_PORT=[\"']\{0,1\}\([0-9]\{1,\}\).*/\1/p" "$f" | tail -n 1)
        [ -z "$p" ] || dport=$p
      fi
    done < <(systemctl show "$dunit" -p EnvironmentFiles --value 2>/dev/null || true)
  fi
  [[ $dport =~ ^[0-9]{2,5}$ ]] || dport=8767
fi
# --- end dashboard port
if [ -n "$dport" ]; then
  probe "dashboard ($dunit, 127.0.0.1:$dport)" "http://127.0.0.1:$dport/fonts/$FONT" soft
else
  echo "NOTE no $dunit on this host (DASHBOARD_UNIT in site.env names the dashboard's unit): its font probe is skipped"
fi
echo "Done. Hard-refresh the portal (Ctrl+Shift+R)."
