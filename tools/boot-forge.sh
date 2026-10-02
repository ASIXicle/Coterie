#!/usr/bin/env bash
# boot-forge.sh — a private git server on this host for the init prompts: one account and one
# token per agent, so every push is recorded under the account that made it (docs/LAYOUT.md,
# docs/SECURITY.md "The forge").
#
#   sudo bash tools/boot-forge.sh        # install, or re-check and add what is missing
#
# Reads the roster (config/agents.json: the agents, `operator`) and portal/install/site.env
# (LAN_IP). Asks nothing. What it does, each step checked before it is done again:
#   1. Forgejo, one pinned release, fetched and sha256-verified against the pin below, to
#      /opt/forge/forgejo (root-owned: the server cannot replace itself);
#   2. the system account `forge`, its state under /var/lib/forge, its config /etc/forge/app.ini
#      (generated secrets, SQLite, registration off, sign-in required, no SSH, server-side hooks
#      not editable from the web), the unit forge.service; it listens on loopback only;
#   3. accounts: the operator (administrator; a generated password that must be changed at first
#      sign-in) and one per agent (a random password nobody is shown; an agent uses its token);
#   4. the private repository <operator>/boot, every agent a collaborator, `main` protected
#      against forced pushes and deletion;
#   5. a token per agent (scope write:repository), written to that agent's own git credential
#      store (mode 600) and nowhere else.
# It prints the repository's loopback URL last. The LAN side (https://<LAN_IP> on port 3000, through the
# front door's Caddy and its allowlist) is setup.sh's step; build/Caddyfile.forge is rendered here.
#
# One standing secret stays on disk: /etc/forge/setup.token (root, 0600), a token of the operator's
# account with the two scopes this script needs (write:user, write:repository; creating a repository
# is refused with less, tried 2026-10-01), kept to add agents on a later run. It can change that
# account and its repositories; the forge's admin routes refuse it, so it is not a site-admin key.
# No secret is ever put on a command line: a process's arguments are readable by every local
# account while it runs, so the token reaches curl through a 0600 header file (found in review
# 2026-10-01: 9 of 12 calls carried it in their arguments before).
# Not done here: when an agent's token is replaced, the old one is no longer in any file but stays
# valid on the forge until you delete it on the agent's account page (revoking it from here would
# need a wider standing token than this script should hold).
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ROSTER=${COTERIE_AGENTS_JSON:-$HERE/config/agents.json}
SITE_ENV=${SITE_ENV:-$HERE/portal/install/site.env}
FORGE_VERSION=16.0.5
declare -A FORGE_XZ_SHA256=(   # of forgejo-$FORGE_VERSION-linux-<arch>.xz, read from the release page 2026-10-01
  [amd64]=b2af596265f8c6d5ee7f99da95d8b3d1f8648c2626a554dbc09635570a7d4004
  [arm64]=1942a29cc7320381d83f96d6ced47ee2c4379bea6de1b0c10c8e4919fa5402bc
)
FORGE_URL_BASE=https://codeberg.org/forgejo/forgejo/releases/download
FORGE_USER=forge
FORGE_BIN=${FORGE_BIN:-/opt/forge/forgejo}; FORGE_ETC=${FORGE_ETC:-/etc/forge}; FORGE_HOME=${FORGE_HOME:-/var/lib/forge}
FORGE_LOCAL_PORT=${FORGE_LOCAL_PORT:-3001}; FORGE_PORT=${FORGE_PORT:-3000}
# FORGE_TEST=<dir>: the unprivileged test. No account, unit or chown; the server runs as the caller
# with its pid in <dir>/forge.pid; agents' homes are <dir>/home/<agent>; FORGE_BIN_SRC is a binary
# to copy instead of fetching. Nothing else differs.
T=${FORGE_TEST:-}
say()  { printf '%s\n' "$*"; }
ok()   { say "  OK    $*"; }
doing(){ say "  DO    $*"; }
hold() { say "  HOLD  $*" >&2; exit 1; }
agents()   { jq -r '.agents[] | select(.enabled != false) | .name' "$ROSTER"; }
home_of()  { if [ -n "$T" ]; then echo "$T/home/$1"; else getent passwd "$1" | cut -d: -f6; fi; }
as_agent() { local a=$1; shift; if [ -n "$T" ]; then env HOME="$(home_of "$a")" GIT_CONFIG_NOSYSTEM=1 "$@"; else runuser -u "$a" -- env HOME="$(home_of "$a")" "$@"; fi; }
as_forge() { if [ -n "$T" ]; then "$@"; else runuser -u "$FORGE_USER" -- "$@"; fi; }
forge()    { as_forge "$FORGE_BIN" --config "$FORGE_ETC/app.ini" --work-path "$FORGE_HOME" "$@"; }
HDR=$(mktemp); chmod 600 "$HDR"          # the setup token, as a header file: never an argument
# On the way out, however this ends: the header file goes, and if the operator's account is one
# that must still set its own password, that requirement is put back (see FORCE_PW below).
FORCE_PW=0
trap 'rm -f "$HDR"; [ "$FORCE_PW" = 0 ] || forge admin user must-change-password "$OPERATOR" >/dev/null 2>&1' EXIT
load_token() { printf 'Authorization: token %s\n' "$(cat "$FORGE_ETC/setup.token" 2>/dev/null)" > "$HDR"; }   # printf is a builtin
api()      { local m=$1 p=$2; shift 2; curl -s -m 15 -X "$m" -H @"$HDR" -H 'Content-Type: application/json' "http://127.0.0.1:$FORGE_LOCAL_PORT/api/v1$p" "$@"; }
code()     { api "$@" -o /dev/null -w '%{http_code}'; }

[ -n "$T" ] || [ "$(id -u)" -eq 0 ] || hold "run as root:  sudo bash $0"
command -v jq >/dev/null && command -v curl >/dev/null && command -v git >/dev/null || hold "jq, curl and git are needed (install.sh stage 0)"
[ -f "$ROSTER" ] || hold "no roster at $ROSTER"
OPERATOR=$(jq -r '.operator // "operator"' "$ROSTER"); SITE=$(jq -r '.site // "coterie"' "$ROSTER")
[[ $OPERATOR =~ ^[a-z][a-z0-9_-]{0,31}$ ]] || hold "the roster's operator '$OPERATOR' is not a usable account name"
# shellcheck disable=SC1090
LAN_IP=$( [ ! -f "$SITE_ENV" ] || . "$SITE_ENV"; printf '%s' "${LAN_IP:-}" )
[[ $LAN_IP =~ ^[0-9.]+$ ]] || hold "LAN_IP is not set in $SITE_ENV"
# shellcheck disable=SC1090
ALLOW=$( [ ! -f "$SITE_ENV" ] || . "$SITE_ENV"; printf '%s' "${CADDY_ALLOWLIST:-}" )

# ---------- 1. the binary ----------
if [ -x "$FORGE_BIN" ] && "$FORGE_BIN" --version 2>/dev/null | grep -q "version $FORGE_VERSION+"; then ok "Forgejo $FORGE_VERSION at $FORGE_BIN"; else
  install -d -m 0755 "$(dirname "$FORGE_BIN")"
  if [ -n "${FORGE_BIN_SRC:-}" ]; then install -m 0755 "$FORGE_BIN_SRC" "$FORGE_BIN"; else
    case "$(dpkg --print-architecture 2>/dev/null || uname -m)" in amd64|x86_64) arch=amd64;; arm64|aarch64) arch=arm64;; *) hold "no pinned Forgejo build for this machine's architecture";; esac
    command -v xz >/dev/null || { doing "apt-get install -y xz-utils"; apt-get install -y -qq xz-utils >/dev/null; }
    tmp=$(mktemp -d); f=forgejo-$FORGE_VERSION-linux-$arch.xz
    doing "fetch $f (about 37 MB) and check it against the pinned sha256"
    curl -fsSL -m 600 -o "$tmp/$f" "$FORGE_URL_BASE/v$FORGE_VERSION/$f" || hold "could not download $FORGE_URL_BASE/v$FORGE_VERSION/$f"
    printf '%s  %s\n' "${FORGE_XZ_SHA256[$arch]}" "$tmp/$f" | sha256sum -c --quiet - || hold "the download does not match the pinned sha256; nothing was installed"
    xz -d "$tmp/$f" && install -m 0755 "$tmp/${f%.xz}" "$FORGE_BIN"; rm -f "$tmp/${f%.xz}"; rmdir "$tmp"
  fi
  "$FORGE_BIN" --version | grep -q "version $FORGE_VERSION+" || hold "$FORGE_BIN does not report version $FORGE_VERSION"
  ok "Forgejo $FORGE_VERSION installed at $FORGE_BIN"
fi

# ---------- 2. account, state, config, unit ----------
if [ -z "$T" ]; then
  id "$FORGE_USER" >/dev/null 2>&1 && ok "user $FORGE_USER" || { doing "useradd --system $FORGE_USER"; useradd --system --home-dir "$FORGE_HOME" --shell /usr/sbin/nologin "$FORGE_USER"; }
  install -d -o "$FORGE_USER" -g "$FORGE_USER" -m 0750 "$FORGE_HOME"
  install -d -o root -g "$FORGE_USER" -m 0750 "$FORGE_ETC"
else mkdir -p "$FORGE_HOME" "$FORGE_ETC" "$T/home"; fi
ROOT_URL="https://$LAN_IP:$FORGE_PORT/"
if [ -f "$FORGE_ETC/app.ini" ]; then
  grep -qxF "ROOT_URL = $ROOT_URL" "$FORGE_ETC/app.ini" && ok "$FORGE_ETC/app.ini" || {
    [ -z "$T" ] || chmod 0640 "$FORGE_ETC/app.ini"
    sed -i -e "s|^ROOT_URL = .*|ROOT_URL = $ROOT_URL|" -e "s|^DOMAIN = .*|DOMAIN = $LAN_IP|" "$FORGE_ETC/app.ini"; [ -z "$T" ] || chmod 0440 "$FORGE_ETC/app.ini"; doing "app.ini: address updated to $ROOT_URL"; restart=1; }
else
  # generated first, each checked: a command substitution inside the heredoc below would fail silently
  k1=$("$FORGE_BIN" generate secret SECRET_KEY) && k2=$("$FORGE_BIN" generate secret INTERNAL_TOKEN) && k3=$("$FORGE_BIN" generate secret JWT_SECRET) || hold "$FORGE_BIN could not generate the forge's secrets"
  [ "${#k1}" -ge 32 ] && [ "${#k2}" -ge 32 ] && [ "${#k3}" -ge 32 ] || hold "$FORGE_BIN generated an empty or short secret; nothing was written"
  umask 027
  cat > "$FORGE_ETC/app.ini" <<EOF
; Written by tools/boot-forge.sh. The forge listens on loopback; the LAN reaches it through Caddy.
APP_NAME = $SITE boot prompts
RUN_MODE = prod
WORK_PATH = $FORGE_HOME
[server]
DOMAIN = $LAN_IP
ROOT_URL = $ROOT_URL
HTTP_ADDR = 127.0.0.1
HTTP_PORT = $FORGE_LOCAL_PORT
DISABLE_SSH = true
OFFLINE_MODE = true
[database]
DB_TYPE = sqlite3
PATH = $FORGE_HOME/data/forgejo.db
[repository]
ROOT = $FORGE_HOME/repos
DEFAULT_PRIVATE = private
[security]
INSTALL_LOCK = true
DISABLE_GIT_HOOKS = true
SECRET_KEY = $k1
INTERNAL_TOKEN = $k2
[oauth2]
JWT_SECRET = $k3
[service]
DISABLE_REGISTRATION = true
REQUIRE_SIGNIN_VIEW = true
[openid]
ENABLE_OPENID_SIGNIN = false
ENABLE_OPENID_SIGNUP = false
[session]
COOKIE_SECURE = true
[actions]
ENABLED = false
[packages]
ENABLED = false
[log]
LEVEL = Warn
EOF
  umask 022; unset k1 k2 k3
  # the server reads its config and never writes it: root:forge 0640 (the test stands in with 0440)
  if [ -n "$T" ]; then chmod 0440 "$FORGE_ETC/app.ini"; else chown root:"$FORGE_USER" "$FORGE_ETC/app.ini"; chmod 0640 "$FORGE_ETC/app.ini"; fi
  doing "$FORGE_ETC/app.ini written (secrets generated, not shown)"
fi
forge migrate >/dev/null 2>&1 || hold "the forge could not prepare its database:  runuser -u $FORGE_USER -- $FORGE_BIN --config $FORGE_ETC/app.ini --work-path $FORGE_HOME migrate"

# ---------- 3. accounts (the command line works on the database; the server need not be up) ----------
have_user() { forge admin user list 2>/dev/null | awk -v u="$1" 'NR == 1 {for (i = 1; i <= NF; i++) if ($i == "Username") c = i} NR > 1 && c && $c == u {f = 1} END {exit !f}'; }   # the column by its header, not its position
FIRST_PW=""
# The operator's first password is printed once, so it must be changed at the first sign-in. Two
# facts about the forge shape how (both met on the first install walk and then exercised):
# - it exempts the FIRST account ever created from "must change password", and this is that
#   account, so the printed password stayed valid for good;
# - an account that must change its password cannot use an access token, and this script does
#   its setup through that account's token.
# So the requirement is set at the END of the run (FORCE_PW, and the EXIT trap if a step stops
# it), and lifted for the length of a rerun that finds it still set.
must_change() { as_forge python3 -c 'import sqlite3, sys; r = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True).execute("select must_change_password from user where name = ?", (sys.argv[2],)).fetchone(); print(r[0] if r else "")' "$FORGE_HOME/data/forgejo.db" "$1" 2>/dev/null; }
if have_user "$OPERATOR"; then ok "forge account $OPERATOR (administrator)"
  # still set, or the first password is still on file (nobody has seen the closing card yet, so
  # it was never used: a run that was killed outright left the requirement lifted): lift it for
  # this run, put it back at the end
  if [ "$(must_change "$OPERATOR")" = 1 ] || [ -s "$FORGE_ETC/operator-first-password" ]; then FORCE_PW=1; forge admin user must-change-password --unset "$OPERATOR" >/dev/null 2>&1 || hold "could not lift the password requirement on $OPERATOR for the length of this run"; fi
else
  out=$(forge admin user create --admin --username "$OPERATOR" --email "$OPERATOR@$SITE.invalid" --random-password 2>&1) || hold "the forge refused the operator account '$OPERATOR': $(tail -1 <<<"$out")"
  FIRST_PW=$(sed -n "s/^generated random password is '\(.*\)'$/\1/p" <<<"$out")
  [ -n "$FIRST_PW" ] || hold "the operator account was created but its first password was not printed; set one:  $FORGE_BIN ... admin user change-password --username $OPERATOR --password <new>"
  FORCE_PW=1
  # on file at once, root only: every step below can stop the run, and a password kept only in
  # this shell would be gone with it, with the account already made (reviewer, 2026-10-02).
  # setup.sh's closing card shows it and removes the file on whichever run reaches the card.
  ( umask 077; printf '%s\n' "$FIRST_PW" > "$FORGE_ETC/operator-first-password" )
  doing "forge account $OPERATOR created (administrator)"
fi
for a in $(agents); do
  if have_user "$a"; then ok "forge account $a"; else
    out=$(forge admin user create --username "$a" --email "$a@$SITE.invalid" --random-password --must-change-password=false 2>&1) || hold "the forge refused the account '$a': $(tail -1 <<<"$out")"
    doing "forge account $a created"
  fi
done

# ---------- the service ----------
if [ -n "$T" ]; then
  if [ -f "$T/forge.pid" ] && kill -0 "$(cat "$T/forge.pid")" 2>/dev/null; then ok "forge running (test, pid $(cat "$T/forge.pid"))"; else
    "$FORGE_BIN" --config "$FORGE_ETC/app.ini" --work-path "$FORGE_HOME" web >"$T/forge.log" 2>&1 & echo $! > "$T/forge.pid"; fi
else
  unit=$HERE/forge/forge.service
  [ -f "$unit" ] || hold "$unit is missing from this tree"
  cmp -s "$unit" /etc/systemd/system/forge.service || { install -m 0644 "$unit" /etc/systemd/system/forge.service; systemctl daemon-reload; doing "forge.service installed"; restart=1; }
  systemctl enable forge >/dev/null 2>&1
  if [ "${restart:-0}" = 1 ] || ! systemctl is-active --quiet forge; then systemctl restart forge; fi
fi
up=0; for _ in $(seq 1 60); do curl -s -o /dev/null -m 1 "http://127.0.0.1:$FORGE_LOCAL_PORT/api/v1/version" && { up=1; break; }; sleep 0.5; done
[ "$up" = 1 ] || hold "the forge did not answer on 127.0.0.1:$FORGE_LOCAL_PORT within 30 s:  journalctl -u forge -n 30"
ok "forge answers on 127.0.0.1:$FORGE_LOCAL_PORT"

# ---------- 4. the repository ----------
token_works() { [ -s "$FORGE_ETC/setup.token" ] && load_token && [ "$(code GET /user)" = 200 ]; }
if token_works; then ok "$FORGE_ETC/setup.token"; else
  umask 077; forge admin user generate-access-token --username "$OPERATOR" --token-name "setup-$(date +%s)" --scopes write:user,write:repository --raw 2>/dev/null | tail -1 > "$FORGE_ETC/setup.token"; umask 022
  token_works || hold "the setup token the forge issued does not work"
  doing "$FORGE_ETC/setup.token written (root only)"
fi
REPO=/repos/$OPERATOR/boot
case "$(code GET "$REPO")" in
  200) ok "repository $OPERATOR/boot";;
  404) [ "$(code POST /user/repos -d '{"name":"boot","private":true,"default_branch":"main","auto_init":false,"description":"Init prompts: one section per agent"}')" = 201 ] || hold "the forge refused to create $OPERATOR/boot"; doing "private repository $OPERATOR/boot created";;
  *)   hold "the forge answered $(code GET "$REPO") for $OPERATOR/boot";;
esac
for a in $(agents); do
  [ "$(code GET "$REPO/collaborators/$a")" = 204 ] && continue
  [ "$(code PUT "$REPO/collaborators/$a" -d '{"permission":"write"}')" = 204 ] || hold "could not add $a to $OPERATOR/boot"
  doing "$a may push to $OPERATOR/boot"
done
# a protected branch refuses forced pushes and deletion; every collaborator may still push to it
if [ "$(code GET "$REPO/branch_protections/main")" = 200 ]; then ok "main is protected"; else
  [ "$(code POST "$REPO/branch_protections" -d '{"rule_name":"main","enable_push":true}')" = 201 ] || hold "could not protect main in $OPERATOR/boot"
  doing "main protected: no forced push, no deletion"
fi

# ---------- 5. a token per agent, in that agent's own credential store ----------
URL=http://127.0.0.1:$FORGE_LOCAL_PORT/$OPERATOR/boot.git
for a in $(agents); do
  home=$(home_of "$a"); [ -d "$home" ] || hold "$a has no home directory (install.sh stage 2)"
  if as_agent "$a" env GIT_TERMINAL_PROMPT=0 git ls-remote "$URL" >/dev/null 2>&1; then ok "$a: its token reaches the repository"; continue; fi
  tok=$(forge admin user generate-access-token --username "$a" --token-name "boot-$(date +%s)" --scopes write:repository --raw 2>/dev/null | tail -1)
  [ "${#tok}" -ge 20 ] || hold "the forge issued no token for $a"
  as_agent "$a" git config --global credential.helper store
  # one line per host in the store: drop this forge's old line, then add the new one, as the agent
  # (git rewrites a line it has used with the port's colon as %3a, so both spellings are dropped)
  printf 'http://%s:%s@127.0.0.1:%s\n' "$a" "$tok" "$FORGE_LOCAL_PORT" | as_agent "$a" sh -c '
    umask 077; f=$HOME/.git-credentials; touch "$f"; chmod 600 "$f"
    grep -v -i -E "@127\.0\.0\.1(:|%3a)$1\$" "$f" > "$f.tmp" || true; cat >> "$f.tmp"; mv "$f.tmp" "$f"' sh "$FORGE_LOCAL_PORT"
  unset tok
  as_agent "$a" env GIT_TERMINAL_PROMPT=0 git ls-remote "$URL" >/dev/null 2>&1 || hold "$a's new token does not reach $URL"
  doing "$a: token issued and stored in ~/.git-credentials (0600)"
done

# ---------- the LAN side, rendered for setup.sh (or for you, by hand) ----------
mkdir -p "$HERE/portal/build"
{ say "# >>> coterie forge >>>  (rendered by tools/boot-forge.sh; the git server, through the same front door)"
  say "https://$LAN_IP:$FORGE_PORT {"
  say "	tls internal"
  [ -z "$ALLOW" ] || { say "	@notmine not remote_ip $ALLOW"; say "	abort @notmine"; }
  say "	reverse_proxy 127.0.0.1:$FORGE_LOCAL_PORT"
  say "}"
  say "# <<< coterie forge <<<"; } > "$HERE/portal/build/Caddyfile.forge"
if [ "$FORCE_PW" = 1 ]; then   # last, after every call made with the operator's own token
  forge admin user must-change-password "$OPERATOR" >/dev/null 2>&1 || hold "could not require a new password for $OPERATOR at sign-in:  runuser -u $FORGE_USER -- $FORGE_BIN --config $FORGE_ETC/app.ini --work-path $FORGE_HOME admin user must-change-password $OPERATOR"
  FORCE_PW=0
  say "  OK    $OPERATOR must set a new password at the first sign-in"
fi
say "  OK    every agent pushes as itself; the forge records who pushed"
say "BOOT_URL=$URL"
