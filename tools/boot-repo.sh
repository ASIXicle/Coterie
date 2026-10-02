#!/usr/bin/env bash
# boot-repo.sh — create, or re-harden, the local init-prompts repository (docs/LAYOUT.md).
#
#   sudo bash tools/boot-repo.sh [/srv/coterie/boot.git]
#   then, per agent:  git clone -b main /srv/coterie/boot.git ~/repos/boot
#
# Every agent pushes to this repository, and a push to a local path runs the repository's hooks
# AS THE PUSHER. A plain `git init --bare --shared=group` leaves `hooks/`, `config` and the top
# directory writable by the whole group, so one agent could plant a hook, or point
# core.hooksPath or a `commondir` file at one, and the next agent to push would run it under its
# own account (found in review 2026-10-01, reproduced). So the group gets exactly what a push
# writes, `objects/` and `refs/`, and everything else stays the owner's:
#
#   - top directory, hooks/, info/, config, HEAD: owner only (root on a real host);
#   - objects/, refs/: owner:agents, setgid, group-writable;
#   - HEAD names a branch nobody pushes (`unused`). Updating the branch HEAD names takes a lock
#     file beside HEAD, in the top directory, which the agents can no longer write; with HEAD
#     elsewhere a push touches only objects/ and refs/. The cost: a plain `git clone` checks
#     nothing out, so clone with `-b main`;
#   - receive.autogc off (gc writes at the top level); non-fast-forward pushes and branch
#     deletion refused, so a push cannot drop another agent's commits.
#
# What this does not do: an agent can still write under refs/ and objects/ directly, so the two
# refusals bind pushes, not a hostile account's file writes. What it closes is running code as
# another account. Rerunning is safe; it HOLDs on a hook or a hooksPath it did not put there.
set -euo pipefail
REPO=${1:-/srv/coterie/boot.git}
OWNER=${BOOT_OWNER:-root}; GROUP=${BOOT_GROUP:-agents}     # overridable for the test, which cannot be root
say()  { printf '%s\n' "$*"; }
hold() { say "  HOLD  $*" >&2; exit 1; }
[ "$(id -un)" = "$OWNER" ] || hold "run as $OWNER:  sudo bash $0 $REPO"
getent group "$GROUP" >/dev/null || hold "no group '$GROUP' on this host (install.sh stage 2 creates it)"
[[ $REPO == /* ]] || hold "give the repository as an absolute path"

# git refuses a repository owned by another account unless it is named safe; every agent reads this one
git config --system --get-all safe.directory 2>/dev/null | grep -qxF "$REPO" || git config --system --add safe.directory "$REPO"
if [ -d "$REPO" ]; then say "  OK    $REPO exists"; else
  install -d -m 0755 "$(dirname "$REPO")"
  git init -q --bare --shared=group -b main "$REPO"; say "  DO    $REPO created"
fi
[ "$(git --git-dir="$REPO" rev-parse --is-bare-repository 2>/dev/null)" = true ] || hold "$REPO is not a bare git repository"
# fail closed on anything that would run: this script must never bless a hook it did not write
planted=$(find "$REPO/hooks" -mindepth 1 ! -name '*.sample' -print 2>/dev/null | head -5)
[ -z "$planted" ] || hold "hooks/ holds something that is not a sample: $(tr '\n' ' ' <<<"$planted"). Read it, remove it if it is not yours, rerun."
[ -z "$(git --git-dir="$REPO" config --get core.hooksPath || true)" ] || hold "core.hooksPath is set in $REPO/config; unset it if it is not yours, rerun."
[ ! -e "$REPO/commondir" ] || hold "$REPO/commondir exists: git would read hooks and config from wherever it points. Remove it if it is not yours, rerun."

cfg() { [ "$(git --git-dir="$REPO" config --get "$1" || true)" = "$2" ] || git --git-dir="$REPO" config "$1" "$2"; }
cfg receive.denyNonFastForwards true
cfg receive.denyDeletes true
cfg receive.autogc false
[ "$(git --git-dir="$REPO" symbolic-ref HEAD)" = refs/heads/unused ] || git --git-dir="$REPO" symbolic-ref HEAD refs/heads/unused

chown -R "$OWNER:" "$REPO"                                  # everything the owner's, owner's own group
chgrp -R "$GROUP" "$REPO/objects" "$REPO/refs"              # what a push writes
find "$REPO/objects" "$REPO/refs" -type d -exec chmod 2775 {} +
find "$REPO" -mindepth 1 -maxdepth 1 ! -name objects ! -name refs -exec chmod -R go-w {} +
chmod 0755 "$REPO"
say "  OK    $REPO: top level, hooks/ and config are $OWNER's; objects/ and refs/ are $OWNER:$GROUP, group-writable"
say "  OK    pushes: fast-forward only, no branch deletion, no gc. Clone with:  git clone -b main $REPO"
