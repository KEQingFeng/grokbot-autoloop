# grokbot-autoloop

A 7×24 development loop for an agent that has its own cloud computer. The bot
(Grok Bot, or any supervisor agent with a plan window worth protecting) stays the
**supervisor**: it picks tasks, fills in the judgement fields of a PR, approves risky
work. The **code** is written by headless agent CLIs on that box, paid by their own
keys, inside `git worktree` sandboxes, judged by the repository's own test command.
Everything stops at a branch plus a pull request. The human merges in the morning.

Built from the prompt 《Guizang AutoLoop：7×24 自动开发监工与双分支调度系统》 — the
prompt described the contract in prose; this is the contract as an engine you can run,
with every rule in it provable by a test.

## What it is / what it is not

- **Is** a deterministic scheduler + acceptance gate + red-line checker with a
  JSON-file state machine. Stdlib only, no pip install, no daemon.
- **Is not** an autonomous merge bot. No auto-merge, no deploy, no migration, no
  paid API, no outbound mail. It will refuse, and the refusal is in the log.
- **Is not** a daemon. Nothing schedules itself: `install-cron.sh` prints the options
  and only writes a crontab with `--write`, and box cron may never fire on a sleeping
  cloud box — the bot's own scheduled task is the recommended driver.
- **Is not** a token meter. It changes *which account does the work*; it cannot read
  or rewrite xAI's server-side plan window.

## Layout

| file | lines | what it does |
|---|---|---|
| `SKILL.md` | 192 | the supervisor-side policy: one instruction → loop, tick contract, red lines |
| `scripts/autoloop.py` | 2705 | the engine: 24 subcommands, state machine, gate, red lines, PR |
| `bootstrap.sh` | 246 | probe the box; `--install --yes` to fill gaps; writes `.autoloop/boot.json` |
| `env.manifest` | 22 | TAB-separated tool list (required / sched / pr / optional / worker) |
| `install-cron.sh` | 118 | prints the two wake-up options, `--write` to install box cron, verifies |
| `config.example.json` | 75 | every knob, with the reason each cap exists |
| `tests/selftest.py` | 1155 | 26 scenarios / 282 assertions against throwaway real git repos |
| `tests/fake_worker.py` | 106 | stand-in worker, 11 modes: `good` and `slow` plus 9 misbehaviours |
| `tests/fake_gh.py` | 136 | `gh` stand-in; refuses `pr merge`, records every call |

## Install on the cloud box

```bash
# 1. get the plugin onto the box (clone, scp, or drop it in the bot's skill directory)
git clone https://github.com/KEQingFeng/grokbot-autoloop.git ~/grokbot-autoloop && cd ~/grokbot-autoloop

# 2. probe the box; add --install to see the package plan, --install --yes to run it
bash bootstrap.sh --repo /path/to/your/repo

# 3. teach the loop how to prove its work, and who does it
python3 scripts/autoloop.py --repo /path/to/your/repo init \
    --runner 'dsh headless {prompt_file}' \
    --test-cmd 'python3 -m pytest -q' \
    --lint-cmd 'ruff check .'

# 4. one tick, by hand, before you trust a night with it
python3 scripts/autoloop.py --repo /path/to/your/repo suggest --limit 5
python3 scripts/autoloop.py --repo /path/to/your/repo tick
python3 scripts/autoloop.py --repo /path/to/your/repo status
```

Where the bot looks for `SKILL.md` is a per-product convention. `~/.cursor/skills/`,
`~/.claude/skills/`, `~/.codex/skills/`, `~/.qoder-cn/skills/` are the shapes in use;
xAI shipped "Skills" for Grok in May 2026, but the directory Grok Bot's *cloud
computer* reads was not confirmable from this machine — check it on the box
(`ls` the obvious candidates) and, failing that, pass `--repo` explicitly and let the
bot read this file as plain instructions.

### The one thing most likely to break a night

`--runner` must be a command that actually runs headless **on that box, on that
version**. Placeholders the engine substitutes: `{prompt_file}` `{worktree}` `{prompt}`
`{log}` (the same values are also exported as `AUTOLOOP_PROMPT_FILE`,
`AUTOLOOP_WORKTREE`, `AUTOLOOP_TASK`, `AUTOLOOP_SLOT`, `AUTOLOOP_LOG`, `AUTOLOOP_BRANCH`).
Test it once directly:

```bash
printf 'Reply with exactly: OK\n' > /tmp/t.md
dsh headless /tmp/t.md        # whatever your runner.cmd says, run it by hand first
```

A wrong flag does not degrade — every agent job dies in exactly the same way, and the
loop reports red.

## Configuration

`.autoloop/config.json` is created by `init` from the detected repo; see
`config.example.json` for every field and why it exists. Environment overrides exist
for the same knobs (`AUTOLOOP_MAX_PARALLEL`, `AUTOLOOP_NIGHTLY_MAX_JOBS`,
`AUTOLOOP_PR_MAX_PER_DAY`, `AUTOLOOP_JOB_TIMEOUT_S`, `AUTOLOOP_REPO`,
`AUTOLOOP_WORKER_CMD`, `AUTOLOOP_HARD_STOP`, and debug-only `AUTOLOOP_NO_LIMIT`).

### Caps (defaults in `DEFAULT_CAPS`)

| cap | default | what it protects |
|---|---|---|
| `max_parallel` | 2 | A/B slots. 2 is the practical ceiling on one cloud box: two worktrees, two ports (`4100`/`4120`), two test DBs, two `TMPDIR`s |
| `pr_max_per_day` | 12 | how many PRs a human can actually review in a morning |
| `nightly_max_jobs` | 40 | worker jobs = the worker accounts' money |
| `max_turns_per_day` | 36 | supervisor turns (at a 10-min cadence that is most of a day, with room to spare) |
| `job_timeout_s` | 1800 | past this the worker's **process group is killed** (SIGTERM then SIGKILL) and its diff can never be judged green |
| `max_repair_rounds` | 2 | one repair attempt with the raw failure output, then one more, then blocked |
| `fail_gate` | 3 | consecutive reds ⇒ maintenance mode (tests/docs/lint only) |
| `min_free_disk_mb` | 2000 | below it the loop refuses to create worktrees |
| `max_business_lines` | 400 | churn cap: one task may not rewrite half the repo |
| `night_window` | 22:00-08:00 | night = `auto_apply` kinds only |
| `gh_cooldown_min` / `gh_daily_rate_hits` | 15 / 3 | a 429 backs the loop off GitHub for the rest of the round; three hits a day disables remote features |

Raise them by editing the file, which is a human act — not by asking the bot, and not
with `AUTOLOOP_NO_LIMIT` inside a scheduled task.

### Gate commands

`init` detects from the lockfile/manifests it finds (`package.json` scripts, pytest,
cargo, go, make) and writes `.autoloop/verify.sh` **plus** records them in
`config.json`. `verify.sh` in `.autoloop/` is the *control copy*: the engine always
executes the copy from the control checkout, fingerprints it (`sha256` in `doctor`),
and `.autoloop/**` is a forbidden path for workers. An empty command prints `[SKIP]`,
never `[PASS]` — and a tick whose every step is `[SKIP]` is **red**, because a loop
that runs nothing has no right to call anything verified.

### Policy and approval

`.autoloop/policy.json` holds `auto_apply` and `needs_approval` kind lists, extra
forbidden globs, and `default_kind_policy` (`needs_approval`). `candidates.json` is
*not* the queue: `queue.json` is work you or the user asked for, `candidates.json` is
what the loop discovered (`suggest` reads TODO/FIXME, skipped tests, lint output,
doc drift, and cites the evidence). Only `auto_apply` may self-start, and only during
night hours does the whitelist become the sole source. Morning triage:

```bash
autoloop.py queue                       # what is waiting
autoloop.py approve <id> [<id>…] --note "why"
autoloop.py reject <id> --note "no"
```

## The safety model, and how each rule is proven

| rule | mechanism | proven by (scenario) |
|---|---|---|
| worker cannot fake green | gate runs from the control copy; diff re-checked independently | `green`, `redlines` (8 misbehaviour modes) |
| no auto-merge / no deploy | engine never merges; `fake_gh.py` refuses `pr merge`; `auto/` never `main` | `green`, `cap`, `backup` |
| tests are not negotiable | M/D/R on an existing test path is red; additions are fine | `redlines`, `unit` |
| forbidden files untouched | `FORBIDDEN_GLOBS` (lockfiles, manifests, migrations, `.github/`, Dockerfiles, `.env*`, keys, `.autoloop/**`) | `redlines`, `unit` |
| scope discipline | `paths` whitelist, disjoint between slots, new test files exempt | `collision`, `green` |
| no secrets anywhere | `SECRET_LINE_RE` over the diff; state files and PR bodies are checked too | `redlines`, `unit` |
| a hung worker cannot burn the budget | timeout ⇒ kill process group ⇒ force red | `timeout` (also asserts a tick returns in <5 s while the worker sleeps 20 s) |
| nothing is judged on an empty gate | all-`[SKIP]` ⇒ red ⇒ blocked, never a PR | `emptygate` |
| the night cannot overspend | jobs/turns/PR/disk caps ⇒ stop, report, do not lower quality | `jobcap`, `cap`, `size`, `failgate` |
| two supervisors, one box | `fcntl` lock; loser changes nothing | `lock` |
| state survives a reset | worktrees rebuilt from branches, `recover` from git+gh truth, orphans reported | `recover`, `orphan` |
| history is not lost | day rollover archives counters to `history/<day>.json`, `ledger.jsonl`, journal | `rollover`, `ledger` |

Run it yourself (no network, no global state, throwaway repos in `$TMPDIR`):

```bash
python3 -m py_compile scripts/autoloop.py
python3 tests/selftest.py            # 26 scenarios; add --only <name> for one
```

Latest run on this machine: **282 passed, 0 failed (62.5 s)**.

## Deviations from the source prompt, on purpose

- **JSON, not YAML.** The prompt specified `*.yaml` state files. A cloud box does not
  guarantee PyYAML, and stdlib-only was a hard requirement, so state lives in
  `*.json`. Same file names, same meaning.
- **PR-only delivery.** The prompt allowed a merge path; this build has none. The
  worker's own prompt card forbids push/PR, and `fake_gh.py` exists partly to prove
  `pr merge` is never issued.
- **Verification numbers, not billing numbers.** Counters are volumes; the money is on
  the worker CLIs' accounts.
- **`gh` is optional.** Without it the loop pushes `auto/*` branches and prints the PR
  body, and says *why* in the log. It does not pretend a PR exists.

## Known limits

- The bot's reasoning tokens still come from the bot. What leaves that window is file
  reading, test watching, log paging, and the implementation itself — the bulk of a
  heavy turn, but not all of it.
- Repo-wide test suites that are slow dominate the wall-clock per round; the gate has
  its own timeout but a 20-minute suite is a 20-minute round.
- `paths` overlap detection is glob-based, so a task that legitimately needs to touch
  a shared file (`src/index.ts` re-exports) cannot run in parallel with one that owns
  `src/*`. The engine serialises them instead of guessing.
- Nothing here survives a hostile worker with write access to the *control* checkout.
  `.autoloop/**` being a forbidden path plus `git add -A` catching force-adds covers the
  accidental case; a box where the worker can edit `verify.sh` directly is outside the
  threat model.

## Not provable from a machine that is not the cloud box

1. `gh` authentication **there** (`autoloop.py doctor` reports it in one line).
2. The real headless flag of the worker CLI **there**, on that version.
3. Whether the scheduler actually wakes the bot — box cron on a hibernating cloud
   machine accepts lines it will never run.

Check those three by hand before the first unattended night. If any of them is false,
the honest output of this plugin is a stopped loop with a reason, not a quiet one.
