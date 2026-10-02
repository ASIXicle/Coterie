#!/bin/bash
# verify-mirror.sh -- is everything running on this host actually in the scaffold repo?
#
#   bash tools/verify-mirror.sh            # from any clone; exit 0 = mirrored, 1 = something is not
#
# Names and paths are data: section 3's live-file → source-file pairs come from
# tools/verify-mirror.pairs (one "LIVE SOURCE" per line, # comments), or $PAIRS. The deploy
# checkout is $DEPLOY (default /opt/flock), the served tree $SRV (default /srv/portal).
#
# Protocol this tool closes: every deploy block handed to
# the operator ends with this script, the operator pastes its output back, and the sender
# verifies from their own seat before the next block. "RESULT: fully mirrored" is the receipt.
#
# Three questions, in the order they can bite:
#   1. Is my working clone fully pushed?          (work that exists only here)
#   2. Is the deploy checkout a faithful mirror?  (changes made in /opt that never came back)
#   3. Does what is RUNNING match the checkout?   (deployed artifacts drifting from source)
#
# "Behind" is not "diverged": a deploy checkout sitting on an older commit is normal
# between deploys. Local commits or local modifications in a deploy tree are not -- that
# is work living somewhere the forge has never seen.
set -u
repo=$(cd "$(dirname "$0")/.." && pwd)
DEPLOY=${DEPLOY:-/opt/flock}
SRV=${SRV:-/srv/portal}
g() { GIT_TERMINAL_PROMPT=0 git -c safe.directory="$1" -C "$1" "${@:2}" 2>/dev/null; }   # never stop to ask for a login: a remote that wants one reads as "freshness unknown"
bad=0; warn=0

same=no
if [ "$(cd "$repo" && pwd -P)" = "$(cd "$DEPLOY" 2>/dev/null && pwd -P)" ]; then same=yes; fi
if [ "$same" = yes ]; then
  echo "NOTE run from the deploy checkout itself, so sections 1 and 2 are the same tree."
  echo "     That still answers the question -- nothing here diverges from the forge -- but it"
  echo "     cannot see an authoring clone elsewhere. Run it from your authoring clone to check that too."
fi
echo "1. working clone  $repo"
g "$repo" fetch -q origin
un=$(g "$repo" status --porcelain | wc -l)
up=$(g "$repo" log origin/main..HEAD --oneline | wc -l)
[ "$un" = 0 ] && echo "   OK   no uncommitted changes" || { echo "   HOLD $un uncommitted change(s) -- not in the forge"; bad=1; }
[ "$up" = 0 ] && echo "   OK   no unpushed commits"    || { echo "   HOLD $up unpushed commit(s) -- not in the forge"; bad=1; }

echo "2. deploy checkout  $DEPLOY"
if [ -d "$DEPLOY/.git" ]; then
  # The fetch needs write access to $DEPLOY/.git (FETCH_HEAD); an unprivileged run cannot,
  # and g() hides the error. So "behind" is measured against the forge's tip read with
  # ls-remote (read-only, works for anyone with the credential), never against a stale
  # origin/main: before 2026-09-25 a non-root run compared against the LAST ROOT FETCH and
  # printed "OK up to date" for a checkout that was really behind (found in review, 2026-09-25).
  fetched=yes; g "$DEPLOY" fetch -q origin || fetched=no
  dm=$(g "$DEPLOY" status --porcelain | wc -l)
  dc=$(g "$DEPLOY" log origin/main..HEAD --oneline | wc -l)
  tip=$(g "$DEPLOY" ls-remote origin refs/heads/main | cut -f1)
  [ "$dm" = 0 ] && echo "   OK   no local modifications" || { echo "   HOLD $dm local modification(s) -- edited in place, never committed"; bad=1; }
  [ "$dc" = 0 ] && echo "   OK   no local commits"       || { echo "   HOLD $dc local commit(s) -- committed here, never pushed"; bad=1; }
  if [ -z "$tip" ]; then echo "   NOTE could not read the forge's main (no credential or no network) -- freshness unknown, run as root to be sure"; warn=1
  elif ! behind=$(g "$DEPLOY" rev-list --count "HEAD..$tip"); then echo "   NOTE forge tip $tip is not in this checkout's objects (fetched: $fetched) -- behind by an unknown count, pull to catch up"; warn=1
  elif [ "${behind:-0}" = 0 ]; then echo "   OK   up to date with the forge ($tip)"
  else echo "   NOTE $behind commit(s) behind the forge -- normal between deploys, pull to catch up"; warn=1; fi
else
  echo "   SKIP $DEPLOY is not a git checkout"
fi

echo "3. what is running vs the checkout"
src=$DEPLOY; [ -d "$DEPLOY/.git" ] || src=$repo
PAIRS=${PAIRS:-$repo/tools/verify-mirror.pairs}
if [ ! -r "$PAIRS" ]; then echo "   HOLD pairs list $PAIRS is missing or unreadable -- section 3 cannot run"; bad=1; fi
npairs=0
# Two passes. The label printed per pair is the basename, which is ambiguous once two
# installed files share one (two units' drop-ins are both 10-something.conf): a DIFFERS
# against a bare name then names neither file. So every basename that occurs more than once
# in the pairs list is printed as parent/basename, and the column is as wide as the widest label.
lives=(); rels=(); labels=(); width=0
while read -r live rel _; do
  case "$live" in ''|'#'*) continue;; esac
  [ -n "$rel" ] || { echo "   HOLD malformed line in $PAIRS: '$live' has no source path"; bad=1; continue; }
  live=${live//\$SRV/$SRV}   # only $SRV is expanded; nothing else in the line is interpreted
  lives+=("$live"); rels+=("$rel")
done < <(grep -v '^$' "$PAIRS" 2>/dev/null)
for live in "${lives[@]}"; do
  b=$(basename "$live"); n=0
  for o in "${lives[@]}"; do [ "$(basename "$o")" = "$b" ] && n=$((n+1)); done
  [ "$n" -gt 1 ] && b="$(basename "$(dirname "$live")")/$b"
  labels+=("$b"); [ ${#b} -le $width ] || width=${#b}
done
i=0
while [ $i -lt ${#lives[@]} ]; do
  live=${lives[$i]}; rel=${rels[$i]}; label=${labels[$i]}; i=$((i+1))
  want=$src/$rel; npairs=$((npairs+1))
  printf '   %-*s ' "$width" "$label"
  if [ ! -e "$live" ]; then echo "NOTE not installed -- listed in the pairs file but absent on this host (deleted, or never deployed?)"; warn=1
  elif [ ! -e "$want" ]; then echo "NOTE no counterpart at $rel in the checkout (not rendered yet?)"; warn=1
  elif [ ! -r "$live" ] || [ ! -r "$want" ]; then echo "HOLD cannot read it as $(id -un) -- run as root to compare"; bad=1
  elif cmp -s "$live" "$want"; then echo "matches the checkout"
  else echo "DIFFERS from the checkout -- redeploy, or find out who edited it"; bad=1; fi
done
[ "$npairs" != 0 ] || { echo "   HOLD no pairs listed in $PAIRS -- a green section 3 with nothing compared is not a result"; bad=1; }

echo
if [ "$bad" != 0 ]; then echo "RESULT: something is NOT mirrored. Lines marked HOLD or DIFFERS are work the forge cannot see."; exit 1; fi
[ "$warn" != 0 ] && echo "RESULT: mirrored. Notes above are ordinary lag, not drift." || echo "RESULT: fully mirrored -- repo, deploy checkout and running artifacts all agree."
exit 0
