#!/bin/bash
# lib.sh -- shared loader for the install scripts. Sourced, not run.
#
#   . "$(dirname "$0")/lib.sh"       # sets: here inst cfg site_env out
#                                     #       ME AGENTS names  port_for()  need()
#
# The roster comes from config/agents.json (jq; COTERIE_AGENTS_JSON overrides, from site.env or
# the environment); host facts come from ${SITE_ENV:-install/site.env}. Nothing is typed twice: a script that needs a
# host fact calls `need VAR` and fails with one line if it is unset.
[ -n "${BASH_SOURCE[0]:-}" ] || { echo "lib.sh must be sourced from bash" >&2; exit 1; }
inst=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
here=$(cd "$inst/.." && pwd)
# shellcheck disable=SC2034  # for the sourcing script
out="$here/build"
site_env=${SITE_ENV:-$inst/site.env}

command -v jq >/dev/null || { echo "jq required" >&2; exit 1; }

# host facts FIRST (optional file; required variables are checked per script via need), so a
# site may point the roster elsewhere before it is read
if [ -f "$site_env" ]; then
    set -a
    # shellcheck disable=SC1090  # user-supplied file, path known only at run time
    . "$site_env"
    set +a
fi
cfg=${COTERIE_AGENTS_JSON:-$here/../config/agents.json}
[ -f "$cfg" ] || { echo "missing roster $cfg (config/agents.json, or COTERIE_AGENTS_JSON=)" >&2; exit 1; }
jq empty "$cfg" 2>/dev/null || { echo "$cfg does not parse" >&2; exit 1; }

# the roster (docs/LAYOUT.md rule 2: the list is `agents`)
ME=$(jq -r '.orchestrator // empty' "$cfg")
[ -n "$ME" ] || { echo "$cfg: orchestrator missing" >&2; exit 1; }
names=$(jq -r '.agents[].name' "$cfg")
# shellcheck disable=SC2034  # consumed by the sourcing script
AGENTS=$(echo "$names" | tr '\n' ' ' | sed 's/ $//')
port_for() { jq -r --arg n "$1" '.agents[] | select(.name == $n) | .port' "$cfg"; }

# derived defaults (host facts that follow from the roster unless site.env says otherwise).
# PORTAL_ROOT defaults to a system path, never a home: a web root inside a home forces that
# home open (SECURITY). A site that serves from elsewhere sets PORTAL_ROOT in site.env.
PORTAL_ROOT=${PORTAL_ROOT:-/srv/portal}
TLS_MODE=${TLS_MODE:-internal}
# shellcheck disable=SC2034
PORTAL_DIR=${PORTAL_DIR:-$PORTAL_ROOT/portal}
# docs/LAYOUT.md defaults for the orchestrator; a site overrides them in site.env. chorusd_port_override
# records that the port came from site.env so the unit carries it as Environment=.
chorusd_port_override=${CHORUSD_PORT:-}
CHORUSD_PORT=${CHORUSD_PORT:-8766}
# shellcheck disable=SC2034
CHORUSD_CODE=${CHORUSD_CODE:-/opt/portal/chorusd.py}
# shellcheck disable=SC2034
STOP_HOOK_NAME=${STOP_HOOK_NAME:-agent-stop-hook}
# Where each pane's shell, and so Claude Code, starts: "~" is the agent's home (systemd resolves it
# from User=). Claude Code keys an agent's project memory by this directory, so a running team keeps
# the one it began with: a site whose agents began at / sets PANE_WORKDIR=/ in site.env.
# shellcheck disable=SC2034
PANE_WORKDIR=${PANE_WORKDIR:-"~"}
# Each pane listens on a unix socket in $PANE_SOCK_ROOT/<agent>/, a directory only that agent and the
# web server's group can enter. PANE_PROXY_GROUP is that group: the one the web server (Caddy) runs in.
# shellcheck disable=SC2034
PANE_SOCK_ROOT=/run/coterie-panes
# shellcheck disable=SC2034
PANE_PROXY_GROUP=${PANE_PROXY_GROUP:-caddy}
# The portal's password: made once by install-fresh.sh, root-only; the Caddyfile carries only its hash.
# shellcheck disable=SC2034
PORTAL_PASSWORD_FILE=/etc/coterie/portal.password
# shellcheck disable=SC2034
PORTAL_HASH_FILE=/etc/coterie/portal.hash
# The front door's secret: Caddy adds it to every /chorus/* request it relays, and the orchestrator's
# page routes answer nothing without it. root:<orchestrator> 0640, read by chorusd at start.
# shellcheck disable=SC2034
FRONT_TOKEN_FILE=/etc/chorusd/front.token
portal_user() { local u; u=$(jq -r '.operator // ""' "$cfg"); [[ $u =~ ^[a-z][a-z0-9_-]{0,31}$ ]] || u=operator; echo "$u"; }   # the roster's operator, or `operator`

# valid VAR REGEX -- a host fact that reaches a sudoers, unit or Caddyfile line must match its
# shape; visudo -c checks syntax, not scope, and a value with a space or '*' widens a NOPASSWD
# rule (review, 2026-09-11). Unset values pass here; `need` decides whether they may be unset.
valid() {
    local v=$1 re=$2
    [ -z "${!v:-}" ] && return 0
    if ! [[ ${!v} =~ ^(${re})$ ]]; then   # group: an alternation must not split the anchors
        echo "$v='${!v}' is not a valid value (want /$re/); fix $site_env" >&2
        exit 1
    fi
}
valid PORTAL_ROOT '/[A-Za-z0-9._/-]+'
valid AMQ_ROOT '/[A-Za-z0-9._/-]+'
valid AMQ_USER '[a-z_][a-z0-9_-]{0,31}'
valid CHORUSD_PORT '[0-9]{2,5}'
valid CHORUSD_CODE '/[A-Za-z0-9._/-]+'
valid CHORUSD_STATE_DIR '/[A-Za-z0-9._/-]+'
valid COTERIE_AGENTS_JSON '/[A-Za-z0-9._/-]+'
valid STOP_HOOK_NAME '[a-z][a-z0-9-]{0,31}'
valid PANE_WORKDIR '~|/[A-Za-z0-9._/-]*'
valid PANE_PROXY_GROUP '[a-z_][a-z0-9_-]{0,31}'
valid LAN_IP '([0-9]{1,3}\.){3}[0-9]{1,3}|[A-Za-z0-9.-]+'
valid SITE_NAME '[A-Za-z0-9.-]+'
valid TLS_MODE 'internal|auto'
valid CADDY_ALLOWLIST 'private_ranges|[0-9./: a-fA-F]+'   # Caddy's own name for the private ranges, or addresses
if { [ -n "${AMQ_ROOT:-}" ] && [ -z "${AMQ_USER:-}" ]; } || { [ -z "${AMQ_ROOT:-}" ] && [ -n "${AMQ_USER:-}" ]; }; then
    echo "AMQ_ROOT and AMQ_USER must be set together (or neither); fix $site_env" >&2
    exit 1
fi

# need VAR [VAR...] -- one-line failure if a host fact is unset or still a placeholder
need() {
    local v
    for v in "$@"; do
        if [ -z "${!v:-}" ] || [[ ${!v} == *\<*\>* ]]; then
            echo "$v is not set: copy $inst/site.env.example to $site_env and fill it in (or export SITE_ENV=/path)" >&2
            exit 1
        fi
    done
}
