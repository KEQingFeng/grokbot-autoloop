#!/usr/bin/env bash
# AutoLoop wake-up installer. A loop that is never woken is not a loop.
#
#   bash install-cron.sh                      print both options, change nothing
#   bash install-cron.sh --every 10           same, with a chosen interval
#   bash install-cron.sh --write              install the crontab line (idempotent)
#   bash install-cron.sh --write --every 5
#   bash install-cron.sh --remove             drop only autoloop's own lines
#
# Two ways to wake the supervisor, in the order you should actually choose:
#
#   1. Grok Bot's own scheduled task  — the bot wakes itself, so the box being
#      "only awake when the bot is awake" is not a problem. Preferred.
#   2. box crontab                    — works, but only if a cron daemon is
#      really running on that box. A sleeping or containerised box may never
#      fire it, and crontab(1) will happily accept a line it will never run.
#
# This script never claims a daemon exists. It probes, prints, writes only with
# --write, then re-reads the crontab and reports PASS or FAIL.
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE="$SELF_DIR/scripts/autoloop.py"
EVERY="${AUTOLOOP_EVERY:-10}"
REPO_ARG=""
ACTION="print"

while [ $# -gt 0 ]; do
  case "$1" in
    --repo)   REPO_ARG="${2:-}"; shift 2 ;;
    --every)  EVERY="${2:-10}"; shift 2 ;;
    --write)  ACTION="write"; shift ;;
    --remove) ACTION="remove"; shift ;;
    -h|--help) sed -n '1,24p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown flag: $1 (try --help)" >&2; exit 2 ;;
  esac
done

[ -f "$ENGINE" ] || { echo "engine missing at $ENGINE — plugin not installed correctly" >&2; exit 2; }
if [ -z "$REPO_ARG" ]; then REPO_ARG="${AUTOLOOP_REPO:-$(pwd)}"; fi
REPO="$(cd "$REPO_ARG" 2>/dev/null && pwd)" || { echo "repo path not found: $REPO_ARG" >&2; exit 2; }
PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "python3 missing — run bootstrap.sh first" >&2; exit 1; }

MARK="# autoloop-tick"
LINE="*/$EVERY * * * * cd $REPO && $PY $ENGINE --repo $REPO tick --quiet >> $REPO/.autoloop/cron.out 2>&1 $MARK"

echo "== AutoLoop wake-up — repo $REPO, every ${EVERY} min"
echo ""
echo "Option 1 (preferred): Grok Bot native scheduled task"
echo "  Create a scheduled/recurring task in the bot itself with this prompt text —"
echo "  the bot wakes, runs one tick, and the tick returns immediately because the"
echo "  workers it launched are detached processes, not children of its turn:"
echo ""
echo "    cd $REPO && python3 $ENGINE --repo $REPO tick"
echo "    (no --quiet here: the report on stdout is what you read and relay — one footer line."
echo "    if it reports cap/halt/maintenance, say so; otherwise keep it to that one line.)"
echo ""
echo "  Why preferred: the box is a cloud agent's machine — it may be asleep between"
echo "  bot turns, and a crontab line on a sleeping box silently never runs."
echo ""
echo "Option 2: box crontab"
echo "  $LINE"
echo "  --quiet is deliberate here: the full report goes to $REPO/.autoloop/log/<day>.md"
echo "  anyway, and cron.out then only holds errors worth reading."
echo ""

if ! command -v crontab >/dev/null 2>&1; then
  echo "PROBE: no crontab(1) on this box. Option 2 is unavailable — use Option 1."
  exit 0
fi
echo "PROBE: crontab present. $(crontab -l 2>/dev/null | wc -l | tr -d ' ') existing entries"
if pgrep -f '[c]ron' >/dev/null 2>&1; then
  echo "PROBE: a cron daemon process was found."
else
  echo "PROBE: NO cron daemon process detected. A line can still be written, but it"
  echo "       may never fire — treat Option 1 as the real driver on this box."
fi
echo ""

CUR="$(crontab -l 2>/dev/null || true)"
CLEAN="$(printf '%s\n' "$CUR" | grep -v -e "$MARK" -e 'autoloop.py .*tick' || true)"

case "$ACTION" in
  remove)
    printf '%s\n' "$CLEAN" | sed '/^$/d' > /tmp/alo_ctab.$$ 2>/dev/null || true
    if crontab /tmp/alo_ctab.$$ 2>/dev/null; then
      CNT="$(crontab -l 2>/dev/null | grep -c "$MARK")"
      echo "removed autoloop lines. autoloop entries now: ${CNT:-0} (grep -c exits 1 on zero, that is the 0)"
    else
      echo "FAIL: crontab refused the rewritten table. Nothing was removed."
    fi
    rm -f /tmp/alo_ctab.$$
    exit 0
    ;;
  print)
    echo "NOTHING WAS WRITTEN. Re-run with --write to install Option 2."
    exit 0
    ;;
esac

if printf '%s\n' "$CLEAN" "$LINE" | sed '/^$/d' | crontab - 2>/dev/null; then
  BACK="$(crontab -l 2>/dev/null || true)"
  if printf '%s' "$BACK" | grep -q "$MARK"; then
    echo "PASS: crontab now contains the tick line (verified by reading it back):"
    printf '%s\n' "$BACK" | grep "$MARK"
  else
    echo "FAIL: crontab command returned 0 but the line is not readable back."
  fi
else
  echo "FAIL: crontab rejected the write (permissions?). Use Option 1 instead."
  exit 1
fi
echo ""
echo "Overlap is safe: a second tick while one is running exits with"
echo "'another supervisor holds the lock' and changes nothing."
echo "First tick to run does not need the workers to be fast — a tick is a report, not a build."
echo "State lives in $REPO/.autoloop/ ; the log of the ticks themselves is $REPO/.autoloop/cron.out"
