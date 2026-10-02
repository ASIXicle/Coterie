#!/usr/bin/env bash
# install.sh — from an empty Debian host to running agent panes, in INSTALL.md's order, idempotently.
#
#   sudo bash install.sh --check      # what each stage would change; touches nothing (works unprivileged)
#   sudo bash install.sh              # every stage, each one's receipts read from the running service
#   sudo bash install.sh --stage 4    # one stage (0 prereqs, 1 roster, 2 users, 3 memory, 4 portal,
#                                     #   5 agents, 6 shield, 7 optional, 8 verify)
#
# Inputs, and nothing else (docs/LAYOUT.md "Placeholders a stranger fills"): config/agents.json (your
# roster, from config/agents.example.json) and portal/install/site.env (LAN_IP, optional SITE_NAME and
# CADDY_ALLOWLIST). The Matron is the roster entry with `role: coordinator` (exactly one). Secrets are
# generated here and never typed. Every other value is a layout default (docs/LAYOUT.md).
#
# Shape: a HOLD exits before that stage changes anything; a stage
# whose component is not in this tree yet says so and is skipped, and the final line says INCOMPLETE.
# A stage already done is recognised by its receipt and skipped. Rerunning is always safe.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CHECK=0; ONLY=""
while [ $# -gt 0 ]; do case "$1" in --check) CHECK=1;; --stage) ONLY=$2; shift;; *) echo "usage: install.sh [--check] [--stage N]" >&2; exit 2;; esac; shift; done
ROSTER=${COTERIE_AGENTS_JSON:-$HERE/config/agents.json}
SITE_ENV=${SITE_ENV:-$HERE/portal/install/site.env}
# layout defaults (docs/LAYOUT.md); a site overrides them in site.env, never here
MEMORY_PORT=${MEMORY_PORT:-8765}; CHORUSD_PORT=${CHORUSD_PORT:-8766}; DASHBOARD_PORT=${DASHBOARD_PORT:-8767}
MEMORY_HOME=/var/lib/memory; MEMORY_DATA_DIR=$MEMORY_HOME/chromadb; MEMORY_AMQ_ROOT=/var/lib/memory-amq
MEMORY_MODEL_DIR=${MEMORY_MODEL_DIR:-/opt/memory/models/voyage-4-nano}   # the pinned embedding model (docs/LAYOUT.md)
ROSTER_LIVE=/etc/coterie/agents.json
# server.py and every file it loads from beside itself when it starts. The first install walk
# (2026-10-02) copied server.py alone and the service died on each start looking for heads/score.py.
# Keep this list equal to what server.py loads beside itself at start (its spec_from_file_location calls).
MEMORY_FILES="server.py maildir.py heads/score.py"
# Registering the memory server with an agent's Claude Code, run AS the agent with the address on
# standard input. Two things the first install walk (2026-10-02) showed about `claude mcp`:
# - it defaults to the scope of the directory it is run in. Run from root's directory, the
#   server was registered for a project called /root, which no agent session is ever in, so the
#   agents would not have had it. --scope user is "every project of this account".
# - from a directory the account cannot read it can add but not remove (EACCES on .mcp.json), so
#   a second install stopped on "already exists". Hence cd "$HOME" first.
REGISTER_MCP='cd "$HOME" && { claude mcp remove --scope user memory >/dev/null 2>&1; claude mcp remove --scope user persMEM >/dev/null 2>&1; claude mcp add --scope user --transport http persMEM "$(cat)"; }'
TTYD_BIN=${TTYD_BIN:-/usr/local/bin/ttyd}   # the path the rendered pane units start (portal/install/render-agents.sh)
TTYD_VERSION=1.7.7
TTYD_URL_BASE=${TTYD_URL_BASE:-https://github.com/tsl0922/ttyd/releases/download}
declare -A TTYD_SHA256=(   # of ttyd.<arch> in that release's SHA256SUMS, read 2026-10-02
  [x86_64]=8a217c968aba172e0dbf3f34447218dc015bc4d5e59bf51db2f2cd12b7be4f55
  [aarch64]=b38acadd89d1d396a0f5649aa52c539edbad07f4bc7348b27b4f4b7219dd4165
)
changes=0; holds=0; skipped=0
say()  { printf '%s\n' "$*"; }
ok()   { say "  OK    $*"; }
plan() { say "  WOULD $*"; changes=$((changes+1)); }
again() { say "  AGAIN $*"; }   # a preview line for work a rerun repeats without changing the outcome: said, not counted
hold() { say "  HOLD  $*"; holds=$((holds+1)); return 1; }
note() { say "  NOTE  $*"; }
skip() { say "  SKIP  $* (not in this tree yet: its owner's manifest lines are pending)"; skipped=$((skipped+1)); }
root() { [ "$(id -u)" -eq 0 ]; }
doit() { local shown; shown=$(printf '%q ' "$@"); if [ "$CHECK" = 1 ]; then plan "$shown"; else say "  DO    $shown"; "$@"; fi; }
sha()  { sha256sum "$1" 2>/dev/null | cut -c1-64; }
same() { [ -f "$1" ] && [ -f "$2" ] && [ "$(sha "$1")" = "$(sha "$2")" ]; }
need_root() { root || { [ "$CHECK" = 1 ] && { note "stage needs root to apply; --check continues read-only"; return 0; } || hold "run as root: sudo bash install.sh"; }; }
stage() { say; say "== stage $1: $2"; }

# ---------- 0. prerequisites ----------
stage0() {
  stage 0 "prerequisites (apt: git jq tmux python3 python3-venv python3-yaml curl sudo caddy; ttyd: one pinned build)"
  need_root || return 1
  [ -f /etc/debian_version ] || hold "not a Debian host (INSTALL §0)" || return 1
  command -v systemctl >/dev/null || hold "no systemd" || return 1
  local missing=""; for p in git jq tmux python3 python3-venv python3-yaml curl sudo caddy; do dpkg -s "$p" >/dev/null 2>&1 || missing="$missing $p"; done
  if [ -n "$missing" ]; then doit apt-get install -y $missing; else ok "apt packages present"; fi
  ttyd_ready
}
ttyd_ready() {   # its own function so a test can run it without root
  # ttyd: Debian 13 has no package for it, and the pane units run $TTYD_BIN whatever else is on
  # the PATH, so it is one pinned build, fetched and checked here. The first install walk
  # (2026-10-02) stopped at this line, which used to tell the operator to go and find a binary.
  if [ -x "$TTYD_BIN" ]; then ok "ttyd $("$TTYD_BIN" --version 2>/dev/null | head -1) at $TTYD_BIN"; return 0; fi
  local arch tmp; arch=$(uname -m)
  [ -n "${TTYD_SHA256[$arch]:-}" ] || hold "no pinned ttyd build for $arch: put a ttyd binary at $TTYD_BIN (INSTALL §4) and rerun" || return 1
  if [ "$CHECK" = 1 ]; then plan "fetch ttyd $TTYD_VERSION ($arch) from its release page, check it against the pinned sha256, install it at $TTYD_BIN"; return 0; fi
  say "  DO    fetch ttyd $TTYD_VERSION ($arch, about 1 MB) and check it against the pinned sha256"
  tmp=$(mktemp -d)
  curl -fsSL -m 300 -o "$tmp/ttyd" "$TTYD_URL_BASE/$TTYD_VERSION/ttyd.$arch" || hold "could not download $TTYD_URL_BASE/$TTYD_VERSION/ttyd.$arch (is this machine online?)" || return 1
  printf '%s  %s\n' "${TTYD_SHA256[$arch]}" "$tmp/ttyd" | sha256sum -c --quiet - || hold "the ttyd download does not match the pinned sha256; nothing was installed" || return 1
  install -m 0755 "$tmp/ttyd" "$TTYD_BIN"
  "$TTYD_BIN" --version >/dev/null 2>&1 || hold "$TTYD_BIN does not run on this machine" || return 1
  ok "ttyd $("$TTYD_BIN" --version | head -1) installed at $TTYD_BIN"
}
# ---------- 1. roster ----------
validate_roster() {   # exit 0 = parses, complete, no placeholders; prints the OK line only when asked
  [ -f "$ROSTER" ] || return 1
  python3 - "$ROSTER" "${1:-quiet}" <<'PY'
import json, re, sys
d = json.load(open(sys.argv[1]))
for k in ("site", "orchestrator", "agents"):
    assert k in d, f"missing top-level {k}"
names = [a["name"] for a in d["agents"]]
assert names and len(set(names)) == len(names), "agent names must be unique"
for a in d["agents"]:
    assert re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", a["name"]), f"bad name {a['name']!r}"
    assert isinstance(a["port"], int) and 1024 < a["port"] < 65536, f"bad port for {a['name']}"
ports = [a["port"] for a in d["agents"] if a.get("enabled", True)]
assert len(set(ports)) == len(ports), "ports must be unique"
for v in (d["site"], d["orchestrator"]):
    assert "<" not in str(v), f"placeholder left in roster: {v}"
assert d["orchestrator"] not in names, "the orchestrator is a dedicated account, not an agent"
t = d.get("theme") or {}
assert isinstance(t.get("light"), dict) and isinstance(t.get("dark"), dict), 'no "theme" block with "light" and "dark": copy it from config/agents.example.json (the portal cannot draw a pane without it)'
if sys.argv[2] == "say":
    print("  OK    roster:", " ".join(n for n, a in zip(names, d["agents"]) if a.get("enabled", True)), "| orchestrator", d["orchestrator"])
PY
}
need_roster() { validate_roster >/dev/null 2>&1 || hold "roster missing or invalid: this stage plans on nothing until stage 1 passes"; }
stage1() {
  stage 1 "roster: config/agents.json → $ROSTER_LIVE"
  [ -f "$ROSTER" ] || hold "no $ROSTER: cp config/agents.example.json config/agents.json and edit it (INSTALL §1)" || return 1
  validate_roster say 2>&1 || { hold "roster does not parse, is incomplete, or still carries placeholders (details above)"; return 1; }
  if same "$ROSTER" "$ROSTER_LIVE"; then ok "$ROSTER_LIVE matches"; else need_root || return 1; doit install -D -o root -g root -m 0644 "$ROSTER" "$ROSTER_LIVE"; fi
}
agents()      { jq -r '.agents[] | select(.enabled != false) | .name' "$ROSTER"; }
orchestrator(){ jq -r '.orchestrator' "$ROSTER"; }
coordinator() { jq -r '.agents[] | select(.enabled != false and .role == "coordinator") | .name' "$ROSTER"; }
# ---------- 2. users and groups ----------
stage2() {
  stage 2 "users and groups (docs/LAYOUT.md §Users): agents, memory, <orchestrator>, amq-poll, amq-read"
  need_roster || return 1
  need_root || return 1
  for g in agents amq-poll amq-read; do getent group "$g" >/dev/null && ok "group $g" || doit groupadd --system "$g"; done
  for a in $(agents); do
    if id "$a" >/dev/null 2>&1; then ok "user $a"; else doit adduser --disabled-password --gecos "" "$a"; fi
    if [ "$CHECK" = 0 ]; then usermod -aG agents "$a"; chmod 700 "/home/$a"; fi
  done
  id memory >/dev/null 2>&1 && ok "user memory" || doit adduser --system --group --home "$MEMORY_HOME" memory
  [ "$CHECK" = 0 ] && usermod -aG amq-poll,amq-read memory || true   # both: a non-root process can chgrp only to its own groups, and the server creates each mailbox dir (amq-poll) and file (amq-read)
  local o; o=$(orchestrator)
  id "$o" >/dev/null 2>&1 && ok "user $o (orchestrator)" || doit useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$o"
  [ "$CHECK" = 0 ] && usermod -aG amq-poll "$o" || true
}
# ---------- 3. memory server ----------
stage3() {
  stage 3 "memory server → /opt/memory (root-owned), state under $MEMORY_HOME, /etc/memory.env, memory.service"
  [ -f "$HERE/memory/server.py" ] || { skip "memory/server.py"; return 0; }
  need_root || return 1
  for f in memory/requirements.txt memory/systemd/memory.service memory/tools/fetch-model.py; do [ -f "$HERE/$f" ] || hold "memory/ is incomplete: $f missing" || return 1; done
  [ -x /opt/memory/venv/bin/python3 ] && ok "/opt/memory/venv" || { doit install -d -m 0755 /opt/memory; doit python3 -m venv /opt/memory/venv; doit /opt/memory/venv/bin/pip install -q --index-url https://download.pytorch.org/whl/cpu "$(grep -m1 '^torch==' "$HERE/memory/requirements.txt")"; doit /opt/memory/venv/bin/pip install -q -r "$HERE/memory/requirements.txt"; }   # torch from its index alone, the rest from PyPI alone (requirements.txt)
  local f changed=0
  for f in $MEMORY_FILES; do [ -f "$HERE/memory/$f" ] || hold "memory/ is incomplete: $f missing" || return 1; done
  for f in $MEMORY_FILES; do
    if same "$HERE/memory/$f" "/opt/memory/$f"; then ok "/opt/memory/$f"; else doit install -D -m 0644 "$HERE/memory/$f" "/opt/memory/$f"; changed=1; fi
  done
  # VERSION beside server.py: the commit of this tree. /opt/memory is not a git checkout, so
  # without it the server reports its own commit as "unknown" (the first agent to boot on a
  # fresh install noticed and said so). Not a checkout here either (an unpacked archive): a note.
  local sha; sha=$(git -C "$HERE" rev-parse HEAD 2>/dev/null || true)
  if [[ $sha =~ ^[0-9a-f]{40}$ ]]; then
    if [ "$(cat /opt/memory/VERSION 2>/dev/null)" = "$sha" ]; then ok "/opt/memory/VERSION (${sha:0:12})"
    elif [ "$CHECK" = 1 ]; then plan "write /opt/memory/VERSION (${sha:0:12}, the commit of this tree)"
    else printf '%s\n' "$sha" > /opt/memory/VERSION; chmod 0644 /opt/memory/VERSION; say "  DO    /opt/memory/VERSION written (${sha:0:12})"; changed=1; fi
  else note "$HERE is not a git checkout: the memory server will report its commit as unknown"; fi
  # MEMORY_EMBEDDING_MODEL names the model DIRECTORY itself (docs/LAYOUT.md; the server reads a dir).
  # fetch-model.py (memory owner's tool) fetches the pinned upstream revision into --into with every
  # file size+sha256-verified, or VERIFIES a directory that already exists (exit 1 if it differs: an
  # existing dir is never assumed), and writes the .modeling.sha256 the unit's ExecStartPre checks.
  # ~700 MB of download and disk. Always run: a present directory is proven, not trusted.
  local model_dir=$MEMORY_MODEL_DIR
  if [ "$CHECK" = 1 ]; then [ -f "$model_dir/config.json" ] && plan "verify the model at $model_dir against the pin (fetch-model.py)" || plan "fetch the pinned embedding model into $model_dir (~700 MB; fetch-model.py)"; else
    /opt/memory/venv/bin/python3 "$HERE/memory/tools/fetch-model.py" --into "$model_dir" >/dev/null || hold "fetch-model.py failed: the model is not installed or an existing $model_dir does not match the pin (its stderr says which)" || return 1
    [ -f "$model_dir/.modeling.sha256" ] && sha256sum -c --quiet "$model_dir/.modeling.sha256" || hold "$model_dir/.modeling.sha256 missing or failing — the unit's ExecStartPre would refuse to start" || return 1
    ok "model at $model_dir, verified against the pin"; fi
  for d in "$MEMORY_DATA_DIR" "$MEMORY_HOME/bootstrap-history"; do [ -d "$d" ] && ok "$d" || doit install -d -o memory -g memory -m 0700 "$d"; done
  [ -d "$MEMORY_AMQ_ROOT" ] && ok "$MEMORY_AMQ_ROOT" || doit install -d -o memory -g amq-poll -m 0750 "$MEMORY_AMQ_ROOT"
  if [ -f /etc/memory.env ]; then ok "/etc/memory.env exists (secret kept)"; elif [ "$CHECK" = 1 ]; then plan "write /etc/memory.env with a generated secret (0600)"; else
    local s; s=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
    umask 077; printf 'MEMORY_HOST=127.0.0.1\nMEMORY_PORT=%s\nMEMORY_DATA_DIR=%s\nMEMORY_HOME=%s\nMEMORY_AMQ_ROOT=%s\nMEMORY_EMBEDDING_MODEL=%s\nCOTERIE_AGENTS_JSON=%s\nMEMORY_SECRET_PATH=%s\n' "$MEMORY_PORT" "$MEMORY_DATA_DIR" "$MEMORY_HOME" "$MEMORY_AMQ_ROOT" "$model_dir" "$ROSTER_LIVE" "$s" > /etc/memory.env; umask 022; say "  DO    /etc/memory.env written (secret generated, not shown)"; fi
  same "$HERE/memory/systemd/memory.service" /etc/systemd/system/memory.service && ok "memory.service" || { doit install -m 0644 "$HERE/memory/systemd/memory.service" /etc/systemd/system/memory.service; doit systemctl daemon-reload; }
  if [ "$CHECK" = 0 ]; then
    systemctl enable memory >/dev/null
    if [ "$changed" = 1 ]; then systemctl restart memory; else systemctl start memory; fi   # new code is not running until it restarts
    # Type=simple: is-active is true the instant the process starts, before the model loads. Poll the
    # listener (a cold start can take a minute), then prove it is OUR server routing on the page's port.
    local up=0; for _ in $(seq 1 90); do curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$MEMORY_PORT/" && { up=1; break; }; sleep 1; done
    if ! { [ "$up" = 1 ] && systemctl is-active --quiet memory; }; then
      local n; n=$(systemctl show memory -p NRestarts --value 2>/dev/null)
      if [ "${n:-0}" -gt 0 ] 2>/dev/null; then hold "memory.service starts and then stops again ($n restarts so far). The reason is in its log:  journalctl -u memory -n 30 --no-pager" || return 1
      else hold "memory.service did not answer within 90 s. Its log:  journalctl -u memory -n 30 --no-pager" || return 1; fi
    fi
    local code; code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'content-type: application/json' --data '{"query":"x"}' "http://127.0.0.1:$MEMORY_PORT/news/search")
    case "$code" in 401|503) ok "memory.service up on $MEMORY_PORT; /news/search without a token → $code";; *) hold "http://127.0.0.1:$MEMORY_PORT/news/search answered $code, expected 401 or 503 (is that our server?)" || return 1;; esac
  fi
}
# ---------- 4. portal ----------
stage4() {
  stage 4 "portal: render → panes (ttyd) → front door (Caddy) → orchestrator (chorusd, sudoers, hooks) → bearer token"
  need_roster || return 1
  need_root || return 1
  [ -f "$SITE_ENV" ] || hold "no $SITE_ENV: cp portal/install/site.env.example portal/install/site.env and set LAN_IP" || return 1
  # shellcheck disable=SC1090
  ( set -a; . "$SITE_ENV"; set +a; [ -n "${LAN_IP:-}" ] && [[ $LAN_IP != *"<"* ]] ) || hold "LAN_IP is unset or a placeholder in $SITE_ENV" || return 1
  if [ "$CHECK" = 1 ]; then
    # a preview counts as a change only what is not there yet: on a machine that already has the
    # portal it said "14 change(s)" for a run that changed two files (the walk's rerun, 2026-10-02)
    if [ -f /etc/systemd/system/chorusd.service ] && [ -f /etc/sudoers.d/portal ]; then again "the portal is installed: its files are rendered again from the roster and put in place, and the panes and the orchestrator are restarted (portal/install/install-fresh.sh)"
    else plan "render the portal's files from the roster and install them: web root, orchestrator, wrappers, tmux.conf, sudoers, one pane unit per agent (portal/install/install-fresh.sh)"; plan "start the panes and the orchestrator on loopback; write and validate build/Caddyfile (setup.sh installs it when /etc/caddy/Caddyfile is still the package's own)"; fi
    local c n; c=$(coordinator); n=$(printf '%s\n' "$c" | grep -c .)
    case "$n" in 1) [ -f /etc/chorusd/pane-key.token ] && ok "/etc/chorusd/pane-key.token exists" || plan "stage 3b: /etc/chorusd/pane-key.token ($(orchestrator):$c 0640; $c is the roster's coordinator)";; 0) note "no agent carries role: coordinator — stage 3b would install no token (every /matron dispatch 401 until one does)";; *) hold "$n active agents carry role: coordinator; exactly one may" || return 1;; esac
    return 0; fi
  # One script, from nothing, stopping at its first failure. This stage used to run the portal's
  # four upgrade scripts in a row with no check between them; on an empty machine each of them
  # needs what a later one installs, and the first install walk (2026-10-02) got five unrelated
  # errors and a HOLD about a token.
  bash "$HERE/portal/install/install-fresh.sh" || hold "the portal did not come up: the STOP or DEAD line above says where (portal/install/install-fresh.sh; it is safe to run again)" || return 1
  # stage 3b: the bearer token the Matron uses for /matron (INSTALL §4 stage 3b)
  # shellcheck disable=SC1090
  # the Matron = the roster's coordinator (docs/LAYOUT.md rule 2): exactly one active agent, or HOLD
  local matron n; matron=$(coordinator); n=$(printf '%s\n' "$matron" | grep -c .)
  if [ "$n" = 0 ]; then note "no agent carries role: coordinator: /etc/chorusd/pane-key.token not installed; every /matron dispatch answers 401 until one does (INSTALL §4 stage 3b)"; else
    [ "$n" = 1 ] || hold "$n active agents carry role: coordinator; exactly one may" || return 1
    id "$matron" >/dev/null 2>&1 || hold "coordinator $matron is not an account on this host (stage 2)" || return 1
    if [ -f /etc/chorusd/pane-key.token ]; then ok "/etc/chorusd/pane-key.token exists"; else install -d -m 0755 /etc/chorusd; ( umask 077; head -c 32 /dev/urandom | base64 -w0 > /etc/chorusd/pane-key.token ); chown "$(orchestrator):$matron" /etc/chorusd/pane-key.token; chmod 0640 /etc/chorusd/pane-key.token; systemctl restart chorusd; ok "token installed for $(orchestrator):$matron"; fi
    local code=000 _; for _ in 1 2 3 4 5 6 7 8 9 10; do code=$(curl -s -o /dev/null -w '%{http_code}' -m 2 -X POST -H 'content-type: application/json' --data '{"bird":"nobody","text":"x"}' "http://127.0.0.1:$CHORUSD_PORT/matron") || true; [ "$code" = 000 ] || break; sleep 1; done   # a restart takes a moment to listen
    [ "$code" = 401 ] || hold "/matron without a token answered $code, expected 401" || return 1; ok "/matron gate: 401 without a token"
  fi
  local n; n=$(curl -s "http://127.0.0.1:$CHORUSD_PORT/agents" | jq -r '.agents | length'); [ "$n" = "$(agents | wc -l)" ] || hold "/agents lists $n seats, roster has $(agents | wc -l)" || return 1; ok "/agents lists $n seats"
}
# ---------- 5. each agent's CLI: hooks + the memory endpoint ----------
stage5() {
  stage 5 "agents: ~/.claude/settings.json (hooks from build/hook.<name>.json) + the memory MCP endpoint registered per agent"
  need_roster || return 1
  need_root || return 1
  [ -f /etc/memory.env ] || { note "no /etc/memory.env (stage 3 skipped or held): settings get hooks only; register the endpoint when the memory server exists"; }
  for a in $(agents); do
    if [ "$CHECK" = 1 ]; then [ -f "/home/$a/.claude/settings.json" ] && again "$a: settings rendered again (other keys kept) and the persMEM endpoint registered again" || plan "render /home/$a/.claude/settings.json (hooks + env) and register 'persMEM' for $a"; continue; fi
    [ -f "$HERE/portal/build/hook.$a.json" ] || hold "build/hook.$a.json not rendered (stage 4)" || return 1
    sudo -u "$a" install -d -m 0700 "/home/$a/.claude"
    # AS the agent, never as root: an agent can leave a symbolic link in its own home, and a root
    # process that reads, replaces, chowns or chmods a path there follows it (reviewer, 2026-10-02).
    # Run as the agent, the kernel applies the agent's own permissions and nothing needs a chown.
    # The program arrives as an argument and the hooks on a pipe, so the agent needs no access to this tree.
    cat "$HERE/portal/build/hook.$a.json" | runuser -u "$a" -- env HOME="/home/$a" python3 -c "$(cat "$HERE/tools/render-settings.py")" --agent "$a" --hooks - --into "/home/$a/.claude/settings.json" \
      || hold "$a: could not write ~/.claude/settings.json as $a" || return 1
    runuser -u "$a" -- chmod 0600 "/home/$a/.claude/settings.json"
    if [ -f /etc/memory.env ] && command -v claude >/dev/null; then
      local url; url=$( . /etc/memory.env; printf 'http://127.0.0.1:%s/%s/mcp' "$MEMORY_PORT" "$MEMORY_SECRET_PATH" )
      # the label is the product's name: it is what the agent's /mcp list and tool names show.
      # `memory` was the label before 2026-10-01; both are removed so a rerun never leaves two.
      printf '%s' "$url" | runuser -u "$a" -- env HOME="/home/$a" bash -c "$REGISTER_MCP" >/dev/null && ok "$a: settings + persMEM endpoint registered (user scope)" || hold "$a: claude mcp add failed; as $a, from its home:  claude mcp add --scope user --transport http persMEM <the address in /etc/memory.env>" || return 1
    else ok "$a: settings written (endpoint registration skipped: $([ -f /etc/memory.env ] && echo 'claude CLI not on PATH' || echo 'no memory server yet'))"; fi
  done
}
# ---------- 6. Hook & Shield ----------
stage6() {
  stage 6 "Hook & Shield → /opt/shield, /var/log/hook-detect, hook-detect.timer"
  [ -f "$HERE/hookandshield/hook-detect.py" ] || { skip "hookandshield/"; return 0; }
  need_roster || return 1
  need_root || return 1
  local n f h a
  [ -d /opt/shield ] && ok "/opt/shield" || { doit install -d -m 0755 /opt/shield /var/log/hook-detect; }
  # The unit reads its environment from this one root-owned file and will not start without it
  # (first install walk, 2026-10-02: "Job for hook-detect.service failed because of unavailable
  # resources"; nothing had ever created the file). MEMORY_URL is where a finding is stored as an
  # alert; the secret that authorises that comes with the news feed (INSTALL §7), and until it is
  # set a finding is written to the log and the service ends failed, which is the visible state.
  if [ -f /etc/shield/hook-detect.env ]; then ok "/etc/shield/hook-detect.env exists (kept)"; elif [ "$CHECK" = 1 ]; then plan "write /etc/shield/hook-detect.env (root, 0600)"; else
    install -d -m 0755 /etc/shield
    ( umask 077; printf '# Read only by hook-detect.service. Root-owned, 0600: a file another account owns must never steer a root process.\n# NEWSTRON_SECRET (the news feed, INSTALL section 7) is added here when alerts should be stored in the memory server.\nMEMORY_URL=http://127.0.0.1:%s\n' "$MEMORY_PORT" > /etc/shield/hook-detect.env )
    say "  DO    /etc/shield/hook-detect.env written (root, 0600)"
  fi
  if [ "$CHECK" = 1 ] && [ -f /opt/shield/hook_baselines.yaml ] && [ -f /etc/systemd/system/hook-detect.timer ]; then again "Hook & Shield is installed: its scanner is copied again, the registry and the timer are kept, and one scan is run"; return 0; fi
  if [ "$CHECK" = 1 ]; then plan "copy hookandshield/*.py + hook_baselines.example.yaml to /opt/shield; units; baseline each agent's settings hash into /opt/shield/hook_baselines.yaml; enable the timer"; return 0; fi
  install -m 0644 "$HERE"/hookandshield/*.py "$HERE"/hookandshield/hook_baselines.example.yaml /opt/shield/
  if [ -f /opt/shield/hook_baselines.yaml ]; then ok "/opt/shield/hook_baselines.yaml exists (kept)"; else
    # the registry's schema (hook_baselines.example.yaml): a `baselines:` list of rows, canon v1, one per agent
    # settings file; --canon prints the canonical sha on its `sha256:` line. An empty sha would be a row that
    # can never match, so it is a HOLD, not a row.
    { echo "baselines:"; for a in $(agents); do
        f="/home/$a/.claude/settings.json"
        h=$(SHIELD_BASELINES_FILE=/opt/shield/hook_baselines.example.yaml python3 /opt/shield/hook_baselines.py --canon "$f" 2>/dev/null | sed -n 's/^sha256: *//p' | grep -oE '^[0-9a-f]{64}$' || true)
        [ -n "$h" ] || { echo "HOLD: no canonical sha for $f (missing or unparseable settings; stage 5)" >&2; exit 1; }
        printf '  - filepath: %s\n    canon: v1\n    sha256: %s\n    approved_by: install\n' "$f" "$h"
      done; } > /opt/shield/hook_baselines.yaml.tmp || { rm -f /opt/shield/hook_baselines.yaml.tmp; hold "baseline registry not written (see above)"; return 1; }
    mv /opt/shield/hook_baselines.yaml.tmp /opt/shield/hook_baselines.yaml; chmod 0644 /opt/shield/hook_baselines.yaml
    n=$(grep -c '^  - filepath:' /opt/shield/hook_baselines.yaml); [ "$n" = "$(agents | wc -l)" ] || hold "registry has $n rows for $(agents | wc -l) agents" || return 1; ok "registry: $n rows, canon v1"
  fi
  install -m 0644 "$HERE"/hookandshield/systemd/hook-detect.{service,timer} /etc/systemd/system/ && systemctl daemon-reload
  systemctl enable --now hook-detect.timer >/dev/null && systemctl start hook-detect.service || hold "hook-detect.service failed: journalctl -u hook-detect" || return 1
  tail -1 /var/log/hook-detect/hook-detect.log 2>/dev/null | grep -q CLEAN && ok "hook-detect: CLEAN" || hold "hook-detect did not end in CLEAN: tail /var/log/hook-detect/hook-detect.log" || return 1
  note "drift-detect (docs/LAYOUT.md §Units) compares deployed files against a release clone at /opt/coterie; install its units once that clone exists (INSTALL §8 item 7) — not automated in this version"
}
# ---------- 7. optional components ----------
stage7() {
  stage 7 "optional: news fetcher, dashboard (INSTALL §7)"
  [ -d "$HERE/newstron" ] && { note "newstron/: follow INSTALL §7 (its service user, env file and timers); not automated in this version"; } || skip "newstron/"
  [ -d "$HERE/dashboard" ] && { note "dashboard/: follow INSTALL §7 (its user, /etc/dashboard.env, the post token); not automated in this version"; } || skip "dashboard/"
}
# ---------- 8. verify ----------
stage8() {
  stage 8 "verify from a stranger's chair (INSTALL §8): pane count, roster route, timers, mirror"
  need_roster || return 1
  if [ "$CHECK" = 1 ]; then plan "count active ttyd-<agent> units, GET /agents, list timers, run tools/verify-mirror.sh"; return 0; fi
  local want got; want=$(agents | wc -l); got=$(for a in $(agents); do systemctl is-active --quiet "ttyd-$a" && echo x; done | wc -l)
  [ "$got" = "$want" ] && ok "$got of $want panes active" || hold "$got of $want ttyd units active" || return 1
  curl -s "http://127.0.0.1:$CHORUSD_PORT/agents" | jq -e ".agents | length == $want" >/dev/null && ok "/agents == roster" || hold "/agents disagrees with the roster" || return 1
  systemctl list-timers --no-pager 2>/dev/null | grep -qE 'hook-detect' && ok "hook-detect.timer scheduled" || note "hook-detect.timer not scheduled (stage 6 skipped?)"
  # this tree is the deploy checkout here; $SRV is the web directory install-fresh.sh fills
  SRV=/srv/portal/portal DEPLOY="$HERE" bash "$HERE/tools/verify-mirror.sh" || hold "verify-mirror found a HOLD or a DIFFERS above: a file on this machine is not the one in $HERE" || return 1
}

[ "${BASH_SOURCE[0]}" = "$0" ] || return 0   # sourced (tests): functions only
say "install.sh — $( [ "$CHECK" = 1 ] && echo 'CHECK MODE: nothing will change' || echo 'APPLY' ) — tree $HERE — roster $ROSTER"
rc=0
for stage_no in 0 1 2 3 4 5 6 7 8; do   # its own name: a stage that set a plain `n` made this line report the wrong stage (first walk: "STOPPED at stage 3" for a hold in stage 6)
  [ -z "$ONLY" ] || [ "$ONLY" = "$stage_no" ] || continue
  "stage$stage_no" || { rc=1; [ "$CHECK" = 1 ] || { say; say "STOPPED at stage $stage_no (HOLD above); fix it and rerun — every stage before it is done and will be skipped"; exit 1; }; }
done
say
if [ "$CHECK" = 1 ]; then say "CHECK: $changes change(s) would be made, $holds hold(s), $skipped stage(s) not in this tree"; exit 0; fi
[ "$skipped" = 0 ] && [ "$holds" = 0 ] && say "DONE: every stage applied and receipted" || { say "INCOMPLETE: $skipped stage(s) skipped (component not in this tree), $holds hold(s)"; exit 1; }
exit $rc
