#!/usr/bin/env bash
# AutoLoop bootstrap — one run per cloud box, safe to re-run.
#
#   bash bootstrap.sh                probe only, writes .autoloop/boot.json
#   bash bootstrap.sh --install      print the exact package commands, change nothing
#   bash bootstrap.sh --install --yes  run those commands, then probe again
#   bash bootstrap.sh --repo /path/to/repo
#
# What this script never does:
#   * install a system dependency without --install AND --yes (both flags, no default)
#   * start, register or claim any daemon, service, or scheduled task
#   * write a credentials or token anywhere
#   * pick a worker CLI for you (it lists what exists; you set runner.cmd)
#   * report ok=true for a tool it did not actually execute
#
# boot.json is the stamp the engine reads. A tick whose boot.json is missing or
# ok=false reports "never bootstrapped" instead of quietly failing later.
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="$SELF_DIR/env.manifest"
ENGINE="$SELF_DIR/scripts/autoloop.py"
REPO_ARG=""
INSTALL=0
YES=0
PLAN_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --repo)    REPO_ARG="${2:-}"; shift 2 ;;
    --install) INSTALL=1; shift ;;
    --yes)     YES=1; shift ;;
    --plan)    INSTALL=1; PLAN_ONLY=1; shift ;;
    -h|--help) sed -n '1,26p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown flag: $1 (try --help)" >&2; exit 2 ;;
  esac
done

say()  { printf '%s\n' "$*"; }
miss() { MISSING="$MISSING $1"; NOTES="$NOTES|$2"; }
MISSING=""
NOTES=""
TOOLS=""
WORKERS_FOUND=""

# ---------------------------------------------------------------- repo
if [ -z "$REPO_ARG" ]; then REPO_ARG="${AUTOLOOP_REPO:-$(pwd)}"; fi
REPO="$(cd "$REPO_ARG" 2>/dev/null && pwd)" || { say "REPO PATH NOT FOUND: $REPO_ARG"; exit 2; }
if [ ! -d "$REPO/.git" ] && ! git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
  say "NOT A GIT REPO: $REPO"
  say "  autoloop works on one repository at a time — cd into it or pass --repo."
  say "  boot.json was NOT written (a false 'ok' in the wrong place is worse than none)."
  exit 2
fi

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" 2>/dev/null | awk '{print $1}'
  else shasum -a 256 "$1" 2>/dev/null | awk '{print $1}'; fi
}
firstline() { printf '%s' "$1" | tr -d '\r' | sed -n '1p' | cut -c1-44; }

say "== AutoLoop bootstrap — $REPO"
say ""

# ---------------------------------------------------------------- probe
if [ ! -f "$MANIFEST" ]; then
  say "! $MANIFEST missing — cannot probe the environment honestly."
  exit 2
fi

TOOL_LINES=""
while IFS=$'	' read -r NAME REQ CHECK HINT WHY; do
  case "$NAME" in ''|'#'*) continue ;; esac
  [ "$NAME" = "name" ] && continue
  if [ "$REQ" = "worker" ]; then
    printf '  [info] %-10s %-8s which CLIs exist is listed below; runner.cmd decides who pays\n' "$NAME" "$REQ"
    continue
  fi
  if bash -c "$CHECK" >/dev/null 2>&1; then
    VER="$(bash -c "$CHECK" 2>/dev/null | head -1)"
    [ -z "$TOOLS" ] || TOOLS="$TOOLS, "
    if [ -n "$VER" ]; then TOOLS="$TOOLS$NAME=$(firstline "$VER")"; else TOOLS="$TOOLS$NAME"; fi
    printf '  [ok]   %-10s %-8s %s\n' "$NAME" "$REQ" "$(firstline "$VER")"
  else
    printf '  [MISS] %-10s %-8s %s\n' "$NAME" "$REQ" "$WHY"
    case "$REQ" in
      required) miss "$NAME" "required tool missing: $NAME — $WHY" ;;
      pr)       NOTES="$NOTES|gh missing — ticks will push branches and print PR bodies instead of opening PRs" ;;
      sched)    NOTES="$NOTES|crontab missing — use a Grok Bot native scheduled task to wake the bot" ;;
      *)        NOTES="$NOTES|optional: $NAME absent" ;;
    esac
  fi
  # worker row is a policy, not a binary: enumerate the real CLIs separately below
  [ "$NAME" = "worker" ] && continue
done < "$MANIFEST"

for CLI in dsh codex claude qoder grok aider opencode; do
  if command -v "$CLI" >/dev/null 2>&1; then
    V="$(bash -c "$CLI --version" 2>/dev/null | head -1)"
    [ -z "$WORKERS_FOUND" ] || WORKERS_FOUND="$WORKERS_FOUND, "
    WORKERS_FOUND="$WORKERS_FOUND$CLI$( [ -n "$V" ] && printf ' (%s)' "$(firstline "$V")")"
  fi
done
say ""
say "worker CLIs on this box: ${WORKERS_FOUND:-NONE}"
RUNNER="${AUTOLOOP_WORKER_CMD:-}"
[ -z "$RUNNER" ] && [ -f "$REPO/.autoloop/config.json" ] && \
  RUNNER="$(python3 -c 'import json,sys;print((json.load(open(sys.argv[1])).get("runner") or {}).get("cmd",""))' \
            "$REPO/.autoloop/config.json" 2>/dev/null)"
if [ -z "$RUNNER" ]; then
  say "runner.cmd: NOT SET — every tick will report 'no-runner' and do no coding. That is"
  say "            the engine refusing to guess which account pays for the work."
  if [ -n "$WORKERS_FOUND" ]; then
    NOTES="$NOTES|runner.cmd not set although these worker CLIs exist: $WORKERS_FOUND"
  else
    NOTES="$NOTES|no worker CLI found on PATH (dsh/codex/claude/qoder/grok/aider/opencode)"
  fi
else
  say "runner.cmd: $RUNNER"
  NOTES="$NOTES|runner.cmd is set but its headless flag is unverified — run one job by hand before scheduling"
fi

GH_AUTH="absent"
if command -v gh >/dev/null 2>&1; then
  if gh auth status >/dev/null 2>&1; then GH_AUTH="ok"; else GH_AUTH="NOT LOGGED IN"; fi
  say "gh auth: $GH_AUTH"
  [ "$GH_AUTH" = "NOT LOGGED IN" ] && \
    NOTES="$NOTES|gh is installed but unauthenticated — run 'gh auth login' on this box before relying on the PR step"
fi

ENGINE_SHA="$(sha256 "$ENGINE" 2>/dev/null)"
PY="$(python3 -c 'import platform;print(platform.python_version())' 2>/dev/null)"
PKG="none"
for C in apt-get dnf yum apk brew; do
  if command -v "$C" >/dev/null 2>&1; then PKG="$C"; break; fi
done
SUDO=""
if [ "$(id -u)" != "0" ] && command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi

# ---------------------------------------------------------------- install (opt-in)
if [ "$INSTALL" = 1 ]; then
  say ""
  say "== missing packages (install plan)"
  ANY=0
  while IFS=$'	' read -r NAME REQ CHECK HINT WHY; do
    case "$NAME" in ''|'#'*|name) continue ;; esac
    bash -c "$CHECK" >/dev/null 2>&1 && continue
    [ -z "$HINT" ] || [ "$HINT" = "—" ] && continue
    ANY=1
    say "  $SUDO bash -c \"$HINT\"     # $WHY"
    [ "$REQ" = "sched" ] && say "    ^ this one starts the box's cron daemon. A Grok Bot native" \
                             && say "      scheduled task does the same job without touching services."
  done < "$MANIFEST"
  if [ "$ANY" = 0 ]; then
    say "  nothing the manifest lists is missing."
  elif [ "$PKG" = "none" ]; then
    say "  no known package manager on PATH — install by hand, then re-run."
  elif [ "$YES" != 1 ] || [ "$PLAN_ONLY" = 1 ]; then
    say ""
    say "PLAN ONLY. Nothing was installed. Re-run with:  bash bootstrap.sh --install --yes"
    say "(this box may auto-update when idle, so a re-probe after any install is expected.)"
  else
    say ""
    say "installing (explicit --yes; this touches system packages):"
    while IFS=$'	' read -r NAME REQ CHECK HINT WHY; do
      case "$NAME" in ''|'#'*|name) continue ;; esac
      bash -c "$CHECK" >/dev/null 2>&1 && continue
      [ -z "$HINT" ] || [ "$HINT" = "—" ] && continue
      say "  $SUDO bash -c \"$HINT\""
      # word-split on purpose: "$HINT" is one command string, so run it through bash -c
      $SUDO bash -c "$HINT" >/dev/null 2>&1 || NOTES="$NOTES|install of $NAME failed: $HINT"
    done < "$MANIFEST"
    say ""
    if [ "${AUTOLOOP_BOOT_REPROBED:-}" = 1 ]; then
      say "already re-probed once after installing — stopping here instead of looping."
      say "run: bash bootstrap.sh   to read the state of this box again."
      exit 1
    fi
    say "re-probing (once; AUTOLOOP_BOOT_REPROBED guards against a loop). No --install"
    say "on the re-run, so this pass only reads the box and rewrites the stamp."
    AUTOLOOP_BOOT_REPROBED=1 exec bash "${BASH_SOURCE[0]}" --repo "$REPO"
  fi
fi

# ---------------------------------------------------------------- stamp
OK_JSON=true
[ -n "$MISSING" ] && OK_JSON=false
NOTES_JSON="$(printf '%s' "$NOTES" | tr '|' '\n' | sed '/^$/d' | python3 -c '
import json,sys
print(json.dumps([l.strip() for l in sys.stdin if l.strip()], ensure_ascii=False))
' 2>/dev/null)"
[ -n "$NOTES_JSON" ] || NOTES_JSON="[]"
CFG="missing"; [ -f "$REPO/.autoloop/config.json" ] && CFG="present"
GATE="missing"; [ -f "$REPO/.autoloop/verify.sh" ] && GATE="present"
DOCTOR="not-run"
if [ -n "$PY" ] && [ -n "$ENGINE_SHA" ]; then
  if python3 "$ENGINE" --repo "$REPO" doctor >/dev/null 2>&1; then DOCTOR="pass"; else DOCTOR="fail"; fi
fi

mkdir -p "$REPO/.autoloop"
BOOT_TMP="$REPO/.autoloop/boot.json.tmp"
python3 - "$BOOT_TMP" "$OK_JSON" "$MISSING" "$TOOLS" "$WORKERS_FOUND" "$RUNNER" \
          "$ENGINE_SHA" "$PY" "$PKG" "$GH_AUTH" "$CFG" "$GATE" "$DOCTOR" "$REPO" "$NOTES_JSON" <<'PY'
import json, sys, time
(out, ok, missing, tools, workers, runner, sha, py, pkg, gh, cfg, gate, doctor, repo, notes) = sys.argv[1:16]
payload = {
    "ok": ok == "true",
    "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    "repo": repo,
    "python": py or "absent",
    "pkg_manager": pkg,
    "gh_auth": gh,
    "engine_sha256": sha[:16] or "absent",
    "tools": tools,
    "missing_required": [m for m in missing.split() if m],
    "worker_clis": workers or "none",
    "runner_cmd": runner or "NOT SET",
    "config_json": cfg,
    "verify_sh": gate,
    "engine_doctor": doctor,
    "notes": json.loads(notes),
    "daemon": "none — this stamp records an environment probe, nothing was scheduled or backgrounded",
}
with open(out, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
PY
if [ $? -ne 0 ]; then say "! could not write boot.json (python3 broken?)"; exit 1; fi
mv -f "$BOOT_TMP" "$REPO/.autoloop/boot.json"
say ""
say "wrote $REPO/.autoloop/boot.json"
python3 -c 'import json;d=json.load(open("'"$REPO"'/.autoloop/boot.json"));print(json.dumps(d,ensure_ascii=False,indent=2))' 2>/dev/null

say ""
if [ "$OK_JSON" = true ]; then
  say "RESULT: ok — required tools present. Engine doctor: $DOCTOR"
else
  say "RESULT: NOT ok — missing required:${MISSING}"
fi
if [ "$CFG" = missing ]; then
  say "next: python3 $ENGINE --repo $REPO init --runner '<verified headless command {prompt_file}>'"
else
  say "next: python3 $ENGINE --repo $REPO tick   (then: status / report / suggest / queue)"
fi
say "note: no timer was installed. See install-cron.sh for the two honest options."
[ -n "$MISSING" ] && exit 1 || exit 0
