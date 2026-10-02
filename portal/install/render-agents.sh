#!/bin/bash
# render-agents.sh -- everything that used to be hand-edited per agent, generated from
# the roster (config/agents.json; COTERIE_AGENTS_JSON overrides) into build/. Nothing here touches /etc; a human (root) copies
# what they have reviewed. Successor, 2026-09-03.
#
#   bash install/render-agents.sh           # writes build/
#   diff build/Caddyfile.portal <(sed -n '/^(portal)/,/^}/p' /etc/caddy/Caddyfile)
#
# Emits:
#   build/ttyd-<agent>.service    one per agent, loopback, base path, portal theme
#   build/Caddyfile.portal        the (portal) snippet the site block imports
#   build/sudoers.portal          orchestrator -> the typing wrapper as the others, + read-only mail listing
#   build/autostart.block         the login -> tmux -> claude block for a NEW agent's ~/.bash_profile
#   build/hook.<agent>.json       the Stop hook object for an agent's settings.json (with the chorus id)
#   build/chorusd.service         the orchestrator daemon unit; docs/LAYOUT.md defaults, site.env overrides
#   build/send-keys-to            the sudoers-granted typing wrapper (bin/send-keys-to.in, session baked in)
#   build/<stop hook>             the Stop hook body (bin/stop-hook.in, port baked in)
#   build/tmux.conf               behaviour (scrollback, OSC-52 clipboard) + the status-bar theme
#
# The roster from config/agents.json (or COTERIE_AGENTS_JSON); host facts (PORTAL_ROOT, LAN_IP,
# the CHORUSD_* overrides, AMQ_ROOT/AMQ_USER for the pre-layout sudo-find path) from
# install/site.env -- see site.env.example. Without a site.env the layout defaults apply.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"
mkdir -p "$out"

TTYD=${TTYD:-/usr/local/bin/ttyd}
SESSION=$(jq -r '.session // "agent"' "$cfg")
[[ $SESSION =~ ^[a-z][a-z0-9_-]{0,31}$ ]] || { echo "$cfg: session '$SESSION' is not a valid tmux session name" >&2; exit 1; }
# --- terminal theme ------------------------------------------------------------
# the roster's .theme carries BOTH palettes and names which one the unit is baked
# with (.theme.mode). Both ship: ttyd 1.7.7 exposes its xterm.js instance as
# `window.term` on the iframe and the portal is same-origin with it, so the portal
# swaps the live palette at runtime with no restart. The baked set is only the
# starting state and what a pane opened directly (?only=) gets.
#
# Every value reaches a systemd ExecStart= line, so shape-check it: a value
# carrying a quote, space or brace would break the unit or change what it runs
# (the lib.sh `valid` lesson, review 2026-09-11). Both sets are checked, not just
# the baked one -- the other is one `mode` edit away from being live.
THEME_KEYS="background foreground cursor cursorAccent selection
            black red green yellow blue magenta cyan white
            brightBlack brightRed brightGreen brightYellow brightBlue brightMagenta brightCyan brightWhite"
for mode in light dark; do
  jq -e --arg m "$mode" '.theme[$m] | type == "object"' "$cfg" >/dev/null \
    || { echo "$cfg: .theme.$mode is missing or not an object" >&2; exit 1; }
  for k in $THEME_KEYS; do
    v=$(jq -r --arg m "$mode" --arg k "$k" '.theme[$m][$k] // empty' "$cfg")
    [ -n "$v" ] || { echo "$cfg: .theme.$mode.$k is missing" >&2; exit 1; }
    [[ $v =~ ^#[0-9a-fA-F]{6}$ ]] || { echo "$cfg: .theme.$mode.$k='$v' is not a #rrggbb colour" >&2; exit 1; }
  done
done
TMUX_STATUS=$(jq -r '.theme.status // "on"' "$cfg")
[[ $TMUX_STATUS =~ ^(on|off)$ ]] || { echo "$cfg: .theme.status must be 'on' or 'off'" >&2; exit 1; }
THEME_MODE=$(jq -r '.theme.mode // "light"' "$cfg")
[[ $THEME_MODE =~ ^(light|dark)$ ]] || { echo "$cfg: .theme.mode must be 'light' or 'dark'" >&2; exit 1; }
# xterm.js ITheme: the ANSI names pass through as-is; only `selection` is renamed.
THEME="theme=$(jq -c --arg m "$THEME_MODE" '.theme[$m] | {background, foreground, cursor, cursorAccent,
    selectionBackground: .selection,
    black, red, green, yellow, blue, magenta, cyan, white,
    brightBlack, brightRed, brightGreen, brightYellow, brightBlue,
    brightMagenta, brightCyan, brightWhite}' "$cfg")"

# --- ttyd units ---------------------------------------------------------------
# Each pane listens on a UNIX SOCKET, not a port: $PANE_SOCK_ROOT/<agent>/pane.sock, in a directory
# that is the agent's own, group $PANE_PROXY_GROUP (the web server's), mode 2750. Entering it takes the
# owner or that group, so no other local account can reach the socket; setgid gives the socket the
# same group, so the web server can connect. A loopback port let every local account type into every
# pane (adversarial pass, 2026-10-02). The shell inside keeps umask 022: UMask=0007 is for the socket.
# Made by a root step (install -d), NOT RuntimeDirectory=: systemd re-applies a RuntimeDirectory's
# owner and mode before every command it starts, so it undid the group and setgid before ttyd ran
# (first H1 walk, 2026-10-02: the probe read '750 alpha'). The stale socket goes first: a restart
# finds the old one, since nothing removes it at stop.
# -O: refuse a websocket whose Origin is not this Host, so a page on another site can't type into
# a pane through a browser that trusts the portal's CA. It stops browsers only; any client can
# send a matching Origin. ttyd 1.7.7 also refuses an IPv6-literal Origin ([...]), so reach the
# portal by name or IPv4. KillMode=process: a restart stops only ttyd, and tmux keeps each agent.
while read -r name port label; do
  title=${label:-$name}
  wd=""; [ "$PANE_WORKDIR" = / ] || wd="WorkingDirectory=$PANE_WORKDIR"$'\n'   # none for /: systemd's own default
  cat > "$out/ttyd-$name.service" <<EOF
[Unit]
Description=Portal pane: $name (ttyd on $PANE_SOCK_ROOT/$name/pane.sock, /term/$name)
After=network.target

[Service]
User=$name
UMask=0007
ExecStartPre=+/usr/bin/install -d -m 0755 $PANE_SOCK_ROOT
ExecStartPre=+/usr/bin/install -d -o $name -g $PANE_PROXY_GROUP -m 2750 $PANE_SOCK_ROOT/$name
ExecStartPre=+/usr/bin/rm -f $PANE_SOCK_ROOT/$name/pane.sock
${wd}ExecStart=$TTYD -i $PANE_SOCK_ROOT/$name/pane.sock -b /term/$name -W -O -t titleFixed=$title -t disableLeaveAlert=true -t fontSize=14 -t '$THEME' sh -c 'umask 022; exec tmux new-session -A -s $SESSION'
Restart=on-failure
RestartSec=2
KillMode=process

[Install]
WantedBy=multi-user.target
EOF
done < <(jq -r '.agents[] | "\(.name) \(.port) \(.label // "")"' "$cfg")

# --- Caddy snippet ------------------------------------------------------------
{
  echo "# generated by install/render-agents.sh from the roster -- do not hand-edit"
  echo "(portal) {"
  echo "	root * $PORTAL_DIR"
  echo "	file_server"
  echo
  # The daemon's loopback-only routes never answer through the front door (it refuses a
  # proxied request itself; this is the second lock). Wildcard-shaped because the daemon
  # routes by suffix: /chorus/anything/pane-key would reach the same handler.
  echo "	@private path /chorus/*pane-key /chorus/*matron /chorus/*hook"
  echo "	respond @private 403"
  # header_up SETS the header (a client's own X-Coterie-Front is overwritten). The secret is a
  # placeholder here, and this file is not private: install-fresh.sh fills it in as it writes the
  # site's Caddyfile, which is (0600 build, 0640 root:caddy live).
  echo "	reverse_proxy /chorus/* 127.0.0.1:$CHORUSD_PORT {"
  echo "		header_up X-Coterie-Front @@COTERIE_FRONT_SECRET@@"
  echo "	}"
  echo
  jq -r --arg sock "$PANE_SOCK_ROOT" '.agents[] | "\treverse_proxy /term/\(.name)*" + (" " * (8 - (.name | length))) + "unix/\($sock)/\(.name)/pane.sock"' "$cfg"
  echo "}"
} > "$out/Caddyfile.portal"

# --- sudoers ------------------------------------------------------------------
others=$(jq -r --arg me "$ME" '[.agents[].name | select(. != $me)] | join(",")' "$cfg")
{
  cat <<EOF
# portal chorus: the orchestrator ($ME) may type into the other agents' tmux
# sessions through the send-keys-to wrapper (two arguments, literal keystrokes, session
# fixed) and nothing else. Generated from the roster.
$ME ALL=($others) NOPASSWD: /usr/local/bin/send-keys-to $SESSION *
EOF
  if [ -n "${AMQ_ROOT:-}" ] && [ -n "${AMQ_USER:-}" ]; then
    cat <<EOF
# pre-layout hosts only: read-only mail listing through the owner of a root under a 700 home
$ME ALL=($AMQ_USER) NOPASSWD: /usr/bin/find $AMQ_ROOT -type f
EOF
  fi
} > "$out/sudoers.portal"

# --- chorusd unit (runs as the orchestrator; docs/LAYOUT.md defaults, site.env overrides as
#     Environment= -- none of these is a secret) ----------------------------------------
{
  cat <<EOF
[Unit]
Description=Portal chorus orchestrator
After=network.target

[Service]
User=$ME
ExecStart=/usr/bin/python3 $CHORUSD_CODE
EOF
  [ -z "${AMQ_ROOT:-}" ] || echo "Environment=CHORUSD_AMQ_ROOT=$AMQ_ROOT"
  [ -z "${AMQ_USER:-}" ] || echo "Environment=CHORUSD_AMQ_USER=$AMQ_USER"
  [ -z "${chorusd_port_override:-}" ] || echo "Environment=CHORUSD_PORT=$CHORUSD_PORT"
  [ -z "${CHORUSD_STATE_DIR:-}" ] || echo "Environment=CHORUSD_STATE_DIR=$CHORUSD_STATE_DIR"
  [ -z "${COTERIE_AGENTS_JSON:-}" ] || echo "Environment=COTERIE_AGENTS_JSON=$COTERIE_AGENTS_JSON"
  cat <<'EOF'
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
} > "$out/chorusd.service"

# --- the typing wrapper (session literal baked in; deploy.sh installs it root:root 0755) ---
sed "s/@SESSION@/$SESSION/g" "$here/bin/send-keys-to.in" > "$out/send-keys-to"
chmod 0755 "$out/send-keys-to"
bash -n "$out/send-keys-to"

# --- autostart block (same for every agent; append to a NEW agent's ~/.bash_profile) ---
cat > "$out/autostart.block" <<'EOF'

# >>> portal claude autostart >>>
# Interactive login (SSH pane or browser pane) -> persistent tmux session
# -> Claude Code. tmux keeps Claude alive when the window closes;
# reattaching from Windows Terminal or the browser resumes the SAME session.
# Plain shell instead:  ssh <agent>@<host> -t 'NO_CLAUDE=1 bash -l'
# AGENT_CLAUDE guards recursion (claude's own subshells inherit it).
if [[ $- == *i* ]] && [ -t 0 ] && [ -z "${NO_CLAUDE:-}" ] && [ -z "${AGENT_CLAUDE:-}" ]; then
    if [ -z "${TMUX:-}" ] && command -v tmux >/dev/null 2>&1; then
        exec tmux new-session -A -s $SESSION
    elif command -v claude >/dev/null 2>&1; then
        # Fullscreen renderer so the mouse wheel scrolls the transcript; the classic
        # renderer under tmux turns wheel events into arrow keys (2026-09-04).
        export CLAUDE_CODE_NO_FLICKER=1
        export AGENT_CLAUDE=1
        exec claude
    fi
fi
# <<< portal claude autostart <<<
EOF

# --- Stop + UserPromptSubmit hook objects, with the chorus id ---------------------
# The Stop hook reads Claude Code's JSON on stdin, finds the last chorus id in the
# transcript, and reports it; chorusd ignores a Stop from a different chorus.
# Without jq or a transcript it degrades to the plain {"bird": ...} post.
# The UserPromptSubmit hook posts {"event":"prompt"} so the doorbell knows the
# agent is mid-turn and holds its ring until the Stop (chorusd v4, 2026-09-10).
# The Stop hook body lives in bin/stop-hook.in, not inline here: the inline version had
# grown past reading, and every edit to it rewrote four settings.json files that Hook &
# Shield hash-baselines. Behind a stable command line, improving the hook no longer moves
# any baseline. @PORT@ is baked; the agent is the argument.
sed "s/@PORT@/$CHORUSD_PORT/g; s/@STOP_HOOK_NAME@/$STOP_HOOK_NAME/g" "$here/bin/stop-hook.in" > "$out/$STOP_HOOK_NAME"
chmod 0755 "$out/$STOP_HOOK_NAME"
bash -n "$out/$STOP_HOOK_NAME"

for name in $names; do
  cmd="/usr/local/bin/$STOP_HOOK_NAME $name"
  pcmd="curl -sm 3 -X POST -H 'Content-Type: application/json' -d '{\"bird\":\"$name\",\"event\":\"prompt\"}' http://127.0.0.1:$CHORUSD_PORT/hook >/dev/null 2>&1 || true"
  jq -n --arg cmd "$cmd" --arg pcmd "$pcmd" '{"Stop":[{"hooks":[{"type":"command","command":$cmd,"async":true}]}],"UserPromptSubmit":[{"hooks":[{"type":"command","command":$pcmd,"async":true}]}]}' > "$out/hook.$name.json"
done

# --- tmux config (behaviour carried forward verbatim + the generated theme) ------
# READ FIRST, RESTORED AS FOUND (Directive Five corollary): the behaviour block below is the
# hand-written /etc/tmux.conf this generator replaces, reproduced line for line. The portal's
# paste bridge depends on the three clipboard lines -- dropping them silently breaks copy and
# paste in every pane. deploy.sh refuses to install a tmux.conf that drops a live setting.
# The behaviour block and the manifest of its option names. deploy.sh protects exactly
# these across a regeneration; everything below the manifest is theme and is free to change,
# which is what a retheme IS. Deriving the manifest from the block means the two cannot drift.
BEHAVIOUR=$(cat <<'EOF'
# portal: deep scrollback; resize to the client actually viewing
set -g history-limit 100000
setw -g aggressive-resize on
# portal clipboard: forward copies to the outer terminal via OSC 52
set -s set-clipboard on
set -as terminal-features ',xterm*:clipboard'
set -as terminal-overrides ',xterm*:Ms=\E]52;%p1%s;%p2%s\007'
EOF
)
BEHAVIOUR_OPTS=$(printf '%s\n' "$BEHAVIOUR" | awk '$1=="set"||$1=="setw"{for(i=2;i<=NF;i++) if ($i !~ /^-/) {print $i; break}}' | sort -u | tr '\n' ' ')
{
  echo "# generated by install/render-agents.sh from the roster -- do not hand-edit"
  echo
  echo "# behaviour: carried forward verbatim from the pre-generator /etc/tmux.conf"
  echo "# behaviour-options: $BEHAVIOUR_OPTS"
  printf '%s\n' "$BEHAVIOUR"
  echo
  echo "# theme: ANSI indices and \`default\` ONLY -- no hex anywhere."
  echo "# The strip lives inside the terminal, so writing it in palette references means it"
  echo "# follows whichever palette the terminal is on, including a runtime toggle, with no"
  echo "# regeneration and no redeploy. Hex here would freeze it to one theme."
  if [ "$TMUX_STATUS" = off ]; then
    echo "# .theme.status = off: the portal pane header already names the agent and its state."
    echo "set -g status off"
  else
    cat <<'EOF'
# The portal pane header already carries the agent name, so status-left is empty and the bar
# is left to the one thing the chrome does not know: what the harness is doing right now.
# bg=default makes the strip the terminal's own ground -- a whisper, not a band.
set -g status on
set -g status-position bottom
set -g status-justify left
set -g status-style "bg=default,fg=colour8"
set -g status-left ""
set -g status-left-length 0
set -g status-right " #[fg=default]#{=50:pane_title} #[fg=colour8]%H:%M "
set -g status-right-length 70
setw -g window-status-format " #I #W "
setw -g window-status-style "fg=colour8"
setw -g window-status-current-format " #I #W "
setw -g window-status-current-style "fg=default,bold"
setw -g window-status-activity-style "fg=colour4"
EOF
  fi
  cat <<'EOF'
set -g pane-border-style "fg=colour8"
set -g pane-active-border-style "fg=colour4"
set -g message-style "fg=colour4,bold"
set -g mode-style "reverse"
EOF
} > "$out/tmux.conf"

echo "rendered into $out:"; ls -1 "$out"
