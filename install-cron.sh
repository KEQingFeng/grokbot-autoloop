#!/usr/bin/env bash
# AutoLoop wake-up installer. A loop that is never woken is not a loop.
#
#   bash install-cron.sh                      print all options, change nothing
#   bash install-cron.sh --every 10           same, with a chosen interval
#   bash install-cron.sh --write              install the crontab line (idempotent)
#   bash install-cron.sh --write --every 5
#   bash install-cron.sh --remove             drop only autoloop's own lines
#
# Three ways to wake the supervisor, in the order you should actually choose:
#
#   1. Grok Bot's own scheduled task  — the bot wakes itself, so the box being
#      "only awake when the bot is awake" is not a problem. Preferred for a bot
#      that must also report to the user.
#   2. autoloop.py watch             — a foreground 60s loop inside a session you
#      already have. Lowest delivery latency (a worker that stops typing is closed
#      and delivered within about a minute), installs nothing, dies with the session.
#   3. box crontab                   — works, but only if a cron daemon is really
#      running on that box. A sleeping or containerised box may never fire it, and
#      crontab(1) will happily accept a line it will never run. Its latency is the
#      interval: a worker finished 1 minute ago waits up to `--every` minutes.
#
# This script never claims a daemon exists. It probes, prints, writes only with
# --write, then re-reads the crontab and reports PASS or FAIL. Whichever driver you
# pick, the hourly §七 report is a *command you run*: `autoloop.py report --patrol`.
#
# Nightly auto-merge: when .autoloop/config.json says caps.merge_enabled=true (set it
# with `autoloop.py init --auto-merge`), every driver printed or installed here carries
# the merge stage too — `review` then `merge --yes-i-will-merge --now`. Without that cap
# the drivers only tick, because PROTOCOL §八 puts the merge decision with the user.
# The loop never merges inside a tick: merging happens only through this explicit stage.
#
# 轻检测与省额度汇报 (#v1.6): the business tick is expensive, so it stays on its own
# cadence. Alongside it the drivers carry two cheap stages — `light` every 15 minutes
# (a local, no-git/no-gh look at the workers in flight; silent when healthy, and it
# terminates a task after caps.anomaly_gate consecutive bad looks) and
# `report --if-needed` hourly (成功静默: prints nothing unless something needs a human
# or the heartbeat is due). Together they are what makes 「成功则无需汇报，只需每小时
# 汇报一次」 true without the bot having to run the expensive cycle to find out.
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE="$SELF_DIR/scripts/autoloop.py"
EVERY="${AUTOLOOP_EVERY:-10}"
REPO_ARG=""
ACTION="print"

while [ $# -gt 0 ]; do
  case "$1" in
    --repo)
      # A flag with no value must fail here: `shift 2` with only one argument
      # left is a no-op in bash, so the while loop would re-read `--repo` and
      # spin forever with no output and no error.
      if [ $# -lt 2 ] || [ -z "${2:-}" ]; then
        echo "!! --repo needs a non-empty repository path (try --help)" >&2
        exit 2
      fi
      REPO_ARG="$2"; shift 2 ;;
    --every)
      if [ $# -lt 2 ] || [ -z "${2:-}" ]; then
        echo "!! --every needs a positive integer of minutes (try --help)" >&2
        exit 2
      fi
      EVERY="$2"; shift 2 ;;
    --write)  ACTION="write"; shift ;;
    --remove) ACTION="remove"; shift ;;
    -h|--help) sed -n '1,39p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown flag: $1 (try --help)" >&2; exit 2 ;;
  esac
done
# `--every` goes straight into the cron field `*/$EVERY`; a non-numeric or zero
# value would install a line cron silently never runs. Validate the flag and the
# AUTOLOOP_EVERY default together, before anything is printed or written.
case "$EVERY" in
  ''|*[!0-9]*)
    echo "!! --every/AUTOLOOP_EVERY must be a positive integer (got: ${EVERY:-<empty>})" >&2
    exit 2 ;;
esac
if [ "$EVERY" -lt 1 ]; then
  echo "!! --every/AUTOLOOP_EVERY must be at least 1 minute (got: $EVERY)" >&2
  exit 2
fi

[ -f "$ENGINE" ] || { echo "engine missing at $ENGINE — plugin not installed correctly" >&2; exit 2; }
if [ -z "$REPO_ARG" ]; then REPO_ARG="${AUTOLOOP_REPO:-$(pwd)}"; fi
REPO="$(cd "$REPO_ARG" 2>/dev/null && pwd)" || { echo "repo path not found: $REPO_ARG" >&2; exit 2; }
PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "python3 missing — run bootstrap.sh first" >&2; exit 1; }

# Is the merge line on? Read the cap out of the config the engine actually uses, so a
# hand-edited .autoloop/config.json and `init --auto-merge` produce the same driver.
CFG="$REPO/.autoloop/config.json"
MERGE_ON=""
MERGE_REG=""
if [ -f "$CFG" ]; then
  MERGE_ON="$($PY -c '
import json,sys
try:
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(0)
if (cfg.get("caps") or {}).get("merge_enabled"):
    print("yes")
' "$CFG" 2>/dev/null || true)"
  MERGE_REG="$($PY -c '
import json,sys
try:
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(0)
print((cfg.get("merge_regression_cmd") or "").strip())
' "$CFG" 2>/dev/null || true)"
fi

MARK="# autoloop-tick"
TICK_STAGE="$PY $ENGINE --repo $REPO tick --quiet >> $REPO/.autoloop/cron.out 2>&1"
# `;` not `&&`: a tick that found the lock held, or a review that had nothing to grade,
# must not silently cancel the merge stage of the round it was scheduled after.
MERGE_STAGE="$PY $ENGINE --repo $REPO review --json >> $REPO/.autoloop/merge.out 2>&1; $PY $ENGINE --repo $REPO merge --yes-i-will-merge --now --json >> $REPO/.autoloop/merge.out 2>&1"
# 轻检测 (#v1.6): cheap and frequent, on its own line so it does not need the business
# tick's cadence. It writes nothing to stdout when healthy, so light.out stays empty
# until something is actually wrong — that is the point of running it every 15 minutes.
LIGHT_MARK="# autoloop-light"
LIGHT_LINE="*/15 * * * * cd $REPO && $PY $ENGINE --repo $REPO light >> $REPO/.autoloop/light.out 2>&1 $LIGHT_MARK"
# 省额度汇报 (#v1.6): hourly, and silent unless a human is needed or the heartbeat is
# due. Running it on a schedule means the bot relays whatever is in report.out instead
# of deciding for itself whether to speak.
REPORT_MARK="# autoloop-report"
REPORT_LINE="0 * * * * cd $REPO && $PY $ENGINE --repo $REPO report --if-needed >> $REPO/.autoloop/report.out 2>&1 $REPORT_MARK"
if [ -n "$MERGE_ON" ]; then
  LINE="*/$EVERY * * * * cd $REPO && $TICK_STAGE; $MERGE_STAGE $MARK"
else
  LINE="*/$EVERY * * * * cd $REPO && $TICK_STAGE $MARK"
fi
LINES="$(printf '%s\n%s\n%s\n' "$LINE" "$LIGHT_LINE" "$REPORT_LINE")"

echo "== AutoLoop wake-up — repo $REPO, every ${EVERY} min"
if [ -n "$MERGE_ON" ]; then
  echo "   合并线：已启用（caps.merge_enabled=true）—— 下面每个驱动都在 tick 之后接 review → merge。"
  if [ -z "$MERGE_REG" ]; then
    echo "   ! 未配 merge_regression_cmd：合并之后不跑回归，每条合并按 pending_verification（待验证）记账。"
  else
    echo "   合并后回归：$MERGE_REG（每条合并各跑一次，红了就停队列、不回滚）"
  fi
else
  echo "   合并线：关闭（PROTOCOL §八 的默认值）—— 驱动里只有 tick，产出停在分支+PR 由你早上合。"
  echo "   要夜间自己合：python3 $ENGINE --repo $REPO init --auto-merge --merge-regression-cmd '<回归命令>'，然后重跑本脚本。"
fi
echo ""
echo "Option 1 (preferred for a reporting bot): Grok Bot native scheduled task"
echo "  Three schedules, three prices. Do not collapse them into one: a light check that"
echo "  costs nothing every 15 minutes, and a tick that costs a fetch + gh every few"
echo "  minutes, are not the same job."
echo ""
echo "  a) 轻检测，每 15 分钟（最便宜，也是真正在省额度的那个）:"
echo ""
echo "    cd $REPO && python3 $ENGINE --repo $REPO light"
echo ""
echo "     健康时它什么都不打印 —— 所以 bot 可以直接把「有输出」当成「需要你处理」。"
echo "     连续 caps.anomaly_gate(默认 3) 次异常它会停掉 worker 并终止任务，避免额度空耗。"
echo ""
echo "  b) 业务 tick，每 5-10 分钟（贵的那趟）:"
echo ""
echo "    cd $REPO && python3 $ENGINE --repo $REPO tick"
echo ""
echo "  c) 汇报，每小时（成功静默）:"
echo ""
echo "    cd $REPO && python3 $ENGINE --repo $REPO report --if-needed"
echo ""
echo "     一切正常时它只打印到点的那一行心跳，有异常立刻出完整巡检；把它的输出原样转给用户即可。"
echo "     (no --quiet here: the report on stdout is what you read and relay — one footer line."
echo "     if it reports cap/halt/maintenance, say so; otherwise keep it to that one line.)"
if [ -n "$MERGE_ON" ]; then
  echo ""
  echo "    合并线已开，在 b) 之后按顺序再跑这两条（review 只读、merge 只合 auto_merge_ok 的）："
  echo "    cd $REPO && python3 $ENGINE --repo $REPO review --json"
  echo "    cd $REPO && python3 $ENGINE --repo $REPO merge --yes-i-will-merge --now --json"
  echo "    单批上限是 caps.review_batch_max（默认 5）：review 每轮最多给这么多条 PR 出结论，"
  echo "    merge 只按结论动手，所以一轮夜合的实际条数被它压住。--now 是跳过 merge_threshold(5)"
  echo "    的凑批等待，不改任何逐条检查。"
fi
echo ""
echo "  Why preferred: the box is a cloud agent's machine — it may be asleep between"
echo "  bot turns, and a crontab line on a sleeping box silently never runs."
echo ""
echo "Option 2 (lowest latency, installs nothing): autoloop.py watch"
echo "  Run it in the foreground of a session you already have:"
echo ""
echo "    cd $REPO && python3 $ENGINE --repo $REPO watch --minutes 240"
echo ""
echo "  It ticks every 60s, so a worker that stops typing gets its session closed,"
echo "  the gate run, and its PR delivered inside about a minute — a tick-only driver"
echo "  checks at its own cadence and lets finished work sit. It is not a daemon: it"
echo "  dies with the session, Ctrl-C stops it, and after 6 idle ticks it exits 4 with"
echo "  空转 6 轮 instead of burning turns till dawn. Pair it with a scheduled task if"
echo "  you need the loop to outlive this conversation, and say which one is running."
if [ -n "$MERGE_ON" ]; then
  echo ""
  echo "  注意：watch 只跑 tick，它不合并任何东西 —— 60s 一次去重读 gh 再合 PR 会把"
  echo "  rate limit 烧穿，也会让你没来得及看的 PR 先消失。用 Option 2 当开发驱动时，"
  echo "  合并仍然要 Option 1 或 3 那条低频调度来跑。"
fi
echo ""
echo "  轻检测（light，每 15 分钟）与每小时汇报（report --if-needed）不在这条前台循环里："
echo "  watch 只跑 tick。要那两层就按 Option 1 或 3 单独调度 —— 它们很便宜、成功静默，"
echo "  不配也不会坏，只是少了「15 分钟内发现卡死」和「每小时一次心跳」这两层。"
echo ""
echo "Option 3: box crontab"
echo "  $LINE"
echo "  $LIGHT_LINE"
echo "  $REPORT_LINE"
echo "  --quiet is deliberate on the tick: the full report goes to $REPO/.autoloop/log/<day>.md"
echo "  anyway, and cron.out then only holds errors worth reading."
echo "  轻检测每 15 分钟一次，健康时什么都不打印（light.out 空着就说明一切正常）；"
echo "  report --if-needed 每小时一次，同样成功静默 —— 两者合起来才是「成功则无需汇报，"
echo "  每小时汇报一次」：bot 不用为了知道好不好而去跑那趟昂贵的 tick。"
if [ -n "$MERGE_ON" ]; then
  echo "  合并两条的输出在 $REPO/.autoloop/merge.out —— 那是能被读回、被 grep 的 JSON，"
  echo "  不是终端上滚过去的散文。合并结论同时写进 .autoloop/merge.json 和 events.jsonl。"
fi
echo "  Latency is the interval: a worker that finished a minute ago is discovered up to"
echo "  ${EVERY} minutes later. If that gap matters on this box, run Option 2 as well."
echo ""

if ! command -v crontab >/dev/null 2>&1; then
  echo "PROBE: no crontab(1) on this box. Option 3 is unavailable — use Option 1 or 2."
  exit 0
fi
echo "PROBE: crontab present. $(crontab -l 2>/dev/null | wc -l | tr -d ' ') existing entries"
if pgrep -f '[c]ron' >/dev/null 2>&1; then
  echo "PROBE: a cron daemon process was found."
else
  echo "PROBE: NO cron daemon process detected. A line can still be written, but it"
  echo "       may never fire — treat Option 1 or 2 as the real driver on this box."
fi
echo ""

CUR="$(crontab -l 2>/dev/null || true)"
CLEAN="$(printf '%s\n' "$CUR" | grep -v -e "$MARK" -e "$LIGHT_MARK" -e "$REPORT_MARK" \
  -e 'autoloop.py .*tick' -e 'autoloop.py .*light' -e 'autoloop.py .*report' || true)"

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
    echo "NOTHING WAS WRITTEN. Re-run with --write to install Option 3."
    exit 0
    ;;
esac

if printf '%s\n' "$CLEAN" "$LINES" | sed '/^$/d' | crontab - 2>/dev/null; then
  BACK="$(crontab -l 2>/dev/null || true)"
  if printf '%s' "$BACK" | grep -q "$MARK"; then
    echo "PASS: crontab now contains the tick line (verified by reading it back):"
    printf '%s\n' "$BACK" | grep -e "$MARK" -e "$LIGHT_MARK" -e "$REPORT_MARK"
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
