#!/bin/bash
# install-fresh.sh -- bring the portal up on a machine that has none of it yet, in one pass.
#
# The portal's earlier install steps grew on a host that already had a portal: each checked that
# the others' results were in place, so on an empty machine none of them could go first, and the
# first install walk (2026-10-02) stopped here. This is the from-nothing path; deploy.sh beside
# it is for updating a portal that is already up. It places everything render-agents.sh renders, in an order that
# needs nothing it has not placed itself, stops at the first thing that fails, and is safe to
# run again: a file in place is replaced by the same file, a unit already running is restarted.
#
# In order: render -> web root -> orchestrator code, the two wrappers, tmux.conf -> sudoers
# (checked BEFORE it is installed) -> units -> each agent's login block -> start panes and the
# orchestrator -> the Caddy file, written and validated -> probes on loopback.
#
# What it leaves alone: /etc/caddy/Caddyfile. It writes build/Caddyfile and says how to install
# it; release/setup.sh installs it when the file in place is still the package's own. Each
# agent's ~/.claude/settings.json is install.sh stage 5's.
#
# Nothing here writes into an agent's home as root. An agent can leave a symbolic link in its
# own home, and a root process that opens, moves or chowns a path there follows it: on a rerun
# that is a way from an agent's account to root's (reviewer, 2026-10-02). The one edit made in
# a home, the login block, is made AS that agent, so the kernel applies the agent's own
# permissions and there is nothing to chown afterwards.
#
#   install-fresh.sh --front-door    after /etc/caddy/Caddyfile serves the portal: export the
#                                    web server's root certificate and probe the page and each
#                                    pane through it. Nothing else is touched.
#
# Reads the roster and install/site.env through lib.sh (LAN_IP required).
# COTERIE_TEST_ROOT=<dir> (tests only): every destination lands under <dir>, ownership is not
# set, and the commands that need root or a running system (systemctl, visudo, caddy) are
# written to <dir>/commands.log instead of being run.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
need LAN_IP
R=${COTERIE_TEST_ROOT:-}
[ -n "$R" ] || [ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo bash $0" >&2; exit 1; }
[ -z "$R" ] || mkdir -p "$R"
stop() { echo "STOP $*" >&2; exit 1; }
sys()  { if [ -n "$R" ]; then printf '%s\n' "$*" >> "$R/commands.log"; else "$@"; fi; }
put()  {   # put MODE OWNER SRC DEST
    [ -f "$3" ] || stop "$3 is missing: the tree is incomplete"
    if [ -n "$R" ]; then install -D -m "$1" "$3" "$R$4"; else install -D -m "$1" -o "$2" -g "$2" "$3" "$4"; fi
}
as_user() {   # as_user NAME HOME CMD...: run CMD as that account, HOME set; in a test root, as whoever runs the test
    local u=$1 h=$2; shift 2
    if [ -n "$R" ]; then env HOME="$h" "$@"; else runuser -u "$u" -- env HOME="$h" "$@"; fi
}
LIVE=$(jq -r '.agents[] | select(.enabled != false) | .name' "$cfg" | tr '\n' ' ')   # an agent switched off has no account and gets no pane

# --- --front-door: the certificate and the probes that need the web server ------------------
if [ "${1:-}" = "--front-door" ]; then
    [ -z "$R" ] || { echo "TEST ROOT $R: --front-door needs the running web server; nothing done"; exit 0; }
    grep -q 'import portal' /etc/caddy/Caddyfile 2>/dev/null || stop "/etc/caddy/Caddyfile does not serve the portal yet"
    CA=/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt
    if [ "$TLS_MODE" = internal ]; then
        for _ in 1 2 3 4 5 6 7 8 9 10; do [ -f "$CA" ] && break; curl -sk -o /dev/null -m 2 "https://$LAN_IP/" || true; sleep 1; done
        [ -f "$CA" ] || stop "the web server has not made its root certificate yet ($CA); run this again in a minute"
        # into the web directory: a certificate is public, and the operator can then fetch it with
        # the browser instead of copying a file off this machine
        install -m 0644 -o root -g root "$CA" "$PORTAL_DIR/caddy-root.crt"
        echo "OK   root certificate at $PORTAL_DIR/caddy-root.crt, served as https://$LAN_IP/caddy-root.crt"
    fi
    fail=0
    # the password goes in a header built here (printf and the read are builtins; base64 reads stdin),
    # so it is never on a command line
    [ -s "$PORTAL_PASSWORD_FILE" ] || stop "$PORTAL_PASSWORD_FILE is missing: run install-fresh.sh without --front-door first"
    auth="Authorization: Basic $(printf '%s:%s' "$(portal_user)" "$(cat "$PORTAL_PASSWORD_FILE")" | base64 -w0)"
    code=$(curl -sk -o /dev/null -w '%{http_code}' -m 5 "https://$LAN_IP/") || code=000
    if [ "$code" = 401 ]; then echo "LOCKED portal without the password -> 401"; else echo "OPEN portal answered $code without the password (want 401) -- the password gate is not in /etc/caddy/Caddyfile" >&2; fail=1; fi
    code=$(curl -sk -o /dev/null -w '%{http_code}' -m 5 -H @<(printf '%s\n' "$auth") "https://$LAN_IP/") || code=000
    if [ "$code" = 200 ]; then echo "LIVE portal -> https://$LAN_IP/"; else echo "DEAD portal (HTTP $code with the password) -- journalctl -u caddy -n 20 --no-pager" >&2; fail=1; fi
    for u in $LIVE; do
        code=$(curl -sk -o /dev/null -w '%{http_code}' -m 5 -H @<(printf '%s\n' "$auth") "https://$LAN_IP/term/$u/") || code=000
        if [ "$code" = 200 ]; then echo "LIVE $u -> https://$LAN_IP/term/$u/"; else echo "DEAD $u (HTTP $code) -- journalctl -u ttyd-$u -n 20 --no-pager" >&2; fail=1; fi
    done
    exit "$fail"
fi

# --- render, and make sure everything this script places was rendered ---------------------
bash "$inst/render-agents.sh" >/dev/null
for f in Caddyfile.portal sudoers.portal chorusd.service send-keys-to autostart.block tmux.conf "$STOP_HOOK_NAME"; do
    [ -f "$out/$f" ] || stop "build/$f was not rendered"
done
for u in $LIVE; do [ -f "$out/ttyd-$u.service" ] || stop "build/ttyd-$u.service was not rendered"; done
first=${LIVE%% *}
ttyd_bin=$(sed -n 's/^ExecStart=\([^ ]*\) .*/\1/p' "$out/ttyd-$first.service")
if [ -z "$R" ]; then
    [ -x "$ttyd_bin" ] || stop "$ttyd_bin is not installed (install.sh stage 0 puts it there)"
    for u in $LIVE $ME; do id "$u" >/dev/null 2>&1 || stop "no account '$u' on this machine (install.sh stage 2 creates them)"; done
    command -v caddy >/dev/null || stop "caddy is not installed (install.sh stage 0)"
fi

# --- web root: the page, the roster it reads, its stylesheet and fonts --------------------
# root-owned: nothing writes here at run time, and the page is what the operator's browser runs
# and what types into every pane, so the orchestrator's account must not be able to rewrite it
if [ -n "$R" ]; then mkdir -p "$R$PORTAL_DIR/fonts"; else install -d -m 0755 -o root -g root "$PORTAL_ROOT" "$PORTAL_DIR" "$PORTAL_DIR/fonts"; fi
put 0644 root "$here/portal/index.html" "$PORTAL_DIR/index.html"
put 0644 root "$cfg"                    "$PORTAL_DIR/agents.json"
put 0644 root "$here/portal/tokens.css" "$PORTAL_DIR/tokens.css"
for f in "$here"/portal/fonts/*; do put 0644 root "$f" "$PORTAL_DIR/fonts/$(basename "$f")"; done
echo "OK   web root $PORTAL_DIR (root-owned)"

# --- the orchestrator's code and the two wrappers it and the agents call ------------------
put 0644 root "$here/chorus/chorusd.py"   "$CHORUSD_CODE"
put 0755 root "$out/send-keys-to"         /usr/local/bin/send-keys-to
put 0755 root "$out/$STOP_HOOK_NAME"      "/usr/local/bin/$STOP_HOOK_NAME"
echo "OK   $CHORUSD_CODE, /usr/local/bin/send-keys-to, /usr/local/bin/$STOP_HOOK_NAME"

# --- tmux: a file that was there before is kept once, as .pre-portal -----------------------
if [ -f "$R/etc/tmux.conf" ] && ! cmp -s "$out/tmux.conf" "$R/etc/tmux.conf" && [ ! -e "$R/etc/tmux.conf.pre-portal" ]; then
    cp -p "$R/etc/tmux.conf" "$R/etc/tmux.conf.pre-portal"
fi
put 0644 root "$out/tmux.conf" /etc/tmux.conf
echo "OK   /etc/tmux.conf"

# --- sudoers: checked first. A file sudo cannot parse, once in /etc/sudoers.d, breaks sudo. --
sys visudo -cf "$out/sudoers.portal" >/dev/null || stop "build/sudoers.portal does not pass visudo -c; nothing was put in /etc/sudoers.d"
put 0440 root "$out/sudoers.portal" /etc/sudoers.d/portal
echo "OK   /etc/sudoers.d/portal ($ME may type into the agents' sessions through the wrapper, nothing else)"

# --- the orchestrator's state directory, and the units -------------------------------------
state=${CHORUSD_STATE_DIR:-/var/lib/chorusd}
if [ -n "$R" ]; then mkdir -p "$R$state"; else install -d -m 0711 -o "$ME" "$state"; fi
put 0644 root "$out/chorusd.service" /etc/systemd/system/chorusd.service
for u in $LIVE; do put 0644 root "$out/ttyd-$u.service" "/etc/systemd/system/ttyd-$u.service"; done
sys systemctl daemon-reload
echo "OK   units: chorusd, $(for u in $LIVE; do printf 'ttyd-%s ' "$u"; done)"

# --- each agent's login block: a shell in its pane starts the agent program ----------------
# Placed before the panes start, so the first shell each pane opens already has it. Replaced,
# not appended, on a rerun: whatever sits between the two marker lines is ours.
MARKER='>>> portal claude autostart >>>'
MARKER_CLOSE='<<< portal claude autostart <<<'
# shellcheck disable=SC2016   # the snippet's variables are the agent shell's, not this one's
EDIT='set -eu
cd "$HOME"
target=""
for f in .bash_profile .bash_login .profile; do if [ -f "$f" ]; then target=$f; break; fi; done
[ -n "$target" ] || target=.bash_profile
tmp=$(mktemp "$target.XXXXXX")
{ if [ -f "$target" ]; then awk -v o="$1" -v c="$2" '"'"'index($0,o){skip=1} !skip{print} index($0,c){skip=0}'"'"' "$target"; fi; cat; } > "$tmp"
mv "$tmp" "$target"'
for u in $LIVE; do
    if [ -n "$R" ]; then home=$R/home/$u; mkdir -p "$home"; else home=$(getent passwd "$u" | cut -d: -f6); fi
    [ -n "$home" ] && [ -d "$home" ] || stop "$u has no home directory"
    # the block arrives on standard input, so the agent needs no read access to this tree
    sed "s|ssh <agent>@<host>|ssh $u@$LAN_IP|" "$out/autostart.block" \
        | as_user "$u" "$home" bash -c "$EDIT" edit "$MARKER" "$MARKER_CLOSE" \
        || stop "could not write the login block as $u"
done
echo "OK   login block in each agent's profile (written as the agent)"

# --- the front door's secret, before the orchestrator starts: chorusd reads it once, at start. Caddy
# adds it to every /chorus/* request it relays (the site's Caddyfile, below); the page's routes answer
# nothing without it. Made once; root:<orchestrator> 0640, the pane-key token's pattern.
front_file=$R$FRONT_TOKEN_FILE
if [ ! -s "$front_file" ]; then
    install -d -m 0755 "$(dirname "$front_file")"
    ( umask 077; head -c 32 /dev/urandom | base64 | tr -d '/+=\n' > "$front_file" )
fi
[ -n "$R" ] || { chown "root:$ME" "$front_file"; chmod 0640 "$front_file"; }
echo "OK   $FRONT_TOKEN_FILE (the front door's secret for the orchestrator)"

# --- start: panes on loopback, then the orchestrator ---------------------------------------
for u in $LIVE; do sys systemctl -q enable "ttyd-$u.service"; sys systemctl restart "ttyd-$u.service"; done
sys systemctl -q enable chorusd.service
sys systemctl restart chorusd.service

# --- the Caddy file: written and validated here, installed by the operator or by setup.sh ---
hosts="https://$LAN_IP"
[ -z "${SITE_NAME:-}" ] || hosts="$hosts, https://$SITE_NAME"
# This machine's certificate authority gets a name of its own. Left alone, every Caddy that signs
# its own certificates calls its root "Caddy Local Authority - <year> ECC Root", and a browser
# that already trusts one such root meets a second machine's certificate, matches the root BY
# NAME, finds the signature does not verify and refuses outright, with no "proceed anyway"
# (first install walk, 2026-10-02: Firefox, SEC_ERROR_BAD_SIGNATURE, a second install on one
# network). The suffix is from this machine's id, so it is the same on every run here.
site=$(jq -r '.site // "portal"' "$cfg" | tr -cd 'A-Za-z0-9.-')
ca_id=$( { cat /etc/machine-id 2>/dev/null || hostname; } | sha256sum | cut -c1-8)

# The portal's password. Every request through the front door asks for it, except the root
# certificate (public, and the browser needs it before it trusts this site). Without it, every
# account on this machine and every device the allowlist admits could type into every pane: the
# panes' sockets stop a direct connection, but the front door relays for whoever it admits
# (adversarial pass, 2026-10-02). Made once and kept root-only, so the operator can read it again
# with sudo; the Caddyfile carries only its bcrypt hash, made from standard input, never argv.
pw_file=$R$PORTAL_PASSWORD_FILE; hash_file=$R$PORTAL_HASH_FILE; user=$(portal_user)
if [ ! -s "$pw_file" ]; then
    install -d -m 0755 "$(dirname "$pw_file")"
    ( umask 077; head -c 18 /dev/urandom | base64 | tr -d '/+=\n' > "$pw_file" )
    rm -f "$hash_file"
fi
if [ ! -s "$hash_file" ] || [ "$pw_file" -nt "$hash_file" ]; then
    if [ -n "$R" ] && ! command -v caddy >/dev/null; then h='$2a$14$test.root.only.no.caddy.here'
    else h=$(printf '%s\n' "$(cat "$pw_file")" | caddy hash-password) || stop "caddy hash-password failed"; fi
    ( umask 077; printf '%s\n' "$h" > "$hash_file" )
fi
hash=$(cat "$hash_file")
front=$(cat "$front_file"); snippet=$(cat "$out/Caddyfile.portal")
( umask 077
{
    echo "# Portal -- LAN, ${TLS_MODE} TLS. Rendered by install-fresh.sh from the roster"
    echo "# (config/agents.json) + install/site.env. Further site blocks go below and"
    echo "# 'import portal' so they cannot drift from this one."
    echo "{"
    # No admin API. Caddy's default is an HTTP endpoint on 127.0.0.1:2019 that ANY local account can
    # use to load a new config (drop the allowlist, serve the CA's key). Nothing here needs it; the
    # cost is that a config change is `systemctl restart caddy`, not reload (adversarial pass, 2026-10-02).
    echo "	admin off"
    if [ "$TLS_MODE" = internal ]; then
        # Caddy otherwise tries, at every start, to put its root into the system's trust store with
        # sudo; its account has no sudo rights, so each start mails root "*** SECURITY information
        # ... caddy : user NOT in sudoers" (first install walk). Browsers on other machines never
        # read this machine's store anyway: the operator imports the root where the browser is.
        echo "	skip_install_trust"
        echo "	pki {"
        echo "		ca local {"
        echo "			name \"Coterie $site\""
        echo "			root_cn \"Coterie $site root $ca_id\""
        echo "			intermediate_cn \"Coterie $site intermediate $ca_id\""
        echo "		}"
        echo "	}"
    fi
    echo "}"
    printf '%s\n' "${snippet//@@COTERIE_FRONT_SECRET@@/$front}"   # in-shell: the secret is on no command line
    echo
    echo "$hosts {"
    [ "$TLS_MODE" = internal ] && echo "	tls internal"
    if [ -n "${CADDY_ALLOWLIST:-}" ]; then
        echo "	@notmine not remote_ip $CADDY_ALLOWLIST"
        echo "	abort @notmine"
    fi
    echo "	@locked not path /caddy-root.crt"
    echo "	basicauth @locked {"
    echo "		$user $hash"
    echo "	}"
    echo "	import portal"
    echo
    echo "	log {"
    echo "		output file /var/log/caddy/portal.log"
    echo "		format json"
    echo "	}"
    echo "}"
} > "$out/Caddyfile"
)   # 0600: it carries the password's hash
sys caddy validate --config "$out/Caddyfile" >/dev/null 2>&1 \
    || stop "build/Caddyfile does not validate:  caddy validate --config $out/Caddyfile"
echo "OK   $out/Caddyfile validates"

[ -z "$R" ] || { echo "TEST ROOT $R: files placed, commands logged, nothing started"; exit 0; }

# --- probes, on loopback: what this script started must be up ------------------------------
fail=0
for u in $LIVE; do
    d=$PANE_SOCK_ROOT/$u; sock=$d/pane.sock; up=0
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        [ -S "$sock" ] && curl -fs -m 2 --unix-socket "$sock" -o /dev/null "http://localhost/term/$u/" && { up=1; break; }; sleep 1
    done
    gate=$(stat -c '%a %G' "$d" 2>/dev/null || true)   # the gate itself, not only that something answers
    if [ "$up" = 1 ] && [ "$gate" = "2750 $PANE_PROXY_GROUP" ]; then echo "LIVE $u (pane on $sock; directory $gate)"
    elif [ "$up" = 1 ]; then echo "DEAD $u: the pane answers, but $d is '$gate', not '2750 $PANE_PROXY_GROUP': any local account could reach it" >&2; fail=1
    else echo "DEAD $u: no pane on $sock -- journalctl -u ttyd-$u -n 20 --no-pager" >&2; fail=1; fi
done
up=0
for _ in 1 2 3 4 5 6 7 8 9 10; do curl -fs -m 2 -o /dev/null "http://127.0.0.1:$CHORUSD_PORT/agents" && { up=1; break; }; sleep 1; done
if [ "$up" = 1 ] && systemctl -q is-active chorusd; then echo "LIVE chorusd (127.0.0.1:$CHORUSD_PORT)"; else echo "DEAD chorusd -- journalctl -u chorusd -n 20 --no-pager" >&2; fail=1; fi
[ "$fail" = 0 ] || exit 1

if grep -q 'import portal' /etc/caddy/Caddyfile 2>/dev/null; then
    echo "OK   /etc/caddy/Caddyfile already imports (portal)"
else
    echo "NEXT the web server does not serve the portal yet. release/setup.sh installs $out/Caddyfile"
    echo "     when /etc/caddy/Caddyfile is still the package's own; by hand:"
    echo "     cp -n /etc/caddy/Caddyfile /etc/caddy/Caddyfile.pre-portal"
    echo "     install -m 0640 -o root -g $PANE_PROXY_GROUP $out/Caddyfile /etc/caddy/Caddyfile && systemctl restart caddy"
    echo "     (0640: the file holds the password's hash and the front door's secret)"
fi
