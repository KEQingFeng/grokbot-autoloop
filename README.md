# grokbot-autoloop

A 7×24 development loop for an agent that has its own cloud computer. The bot
(Grok Bot, or any supervisor agent with a plan window worth protecting) stays the
**supervisor**: it starts, monitors, recovers and schedules work, picks tasks, fills in
the judgement fields of a PR, approves risky work. The **code** is written by headless
agent CLIs on that box, paid by their own keys, inside `git worktree` sandboxes, judged
by the repository's own test command — and in the default configuration each worker
pushes its own branch and opens its own PR. Everything stops at a branch plus a pull
request. The human merges in the morning — that is the shipped default, and the only way
off it is the user's own `init --auto-merge` plus a scheduled merge stage.

Built from two prompts: 《Guizang AutoLoop：7×24 自动开发监工与双分支调度系统》, which
described the contract in prose, and 《DeepSeekHarness 持续自主开发监督协议》, which is
shipped verbatim as [`PROTOCOL.md`](PROTOCOL.md). This repo is that contract as an
engine you can run, with every rule in it provable by a test.

Trigger it out loud — the skill description carries the keywords 「直接开发 xx 项目」
「全天候开发」「自动开发」「开始循环」「挂个循环」「明早看 PR」「监工」, so a user who
says *直接开发 myrepo* gets preflight → init → suggest → watch instead of a tutorial.

## What it is / what it is not

- **Is** a deterministic scheduler + acceptance gate + red-line checker with a
  JSON-file state machine. Stdlib only, no pip install, no daemon.
- **Is** the supervisor's own rulebook: `PROTOCOL.md` says 监工不写代码, and `round`
  re-injects it with its real sha256 plus a §十 state record rebuilt from git/gh, so a
  bot that lost its context resumes from files rather than from what it remembers saying.
- **Is not** an autonomous merge bot. No deploy, no migration, no paid API, no outbound
  mail. It will refuse, and the refusal is in the log. In the default
  `delivery.owner=worker` mode the *worker* opens its PR, and a PR whose gate is red gets
  annotated `pr_unverified` and left alone (§六). The engine never issues `pr close` or
  `pr review`; it *can* issue `pr merge`, but only through the merge line below, which
  ships off and needs a config switch plus a per-invocation confirmation — see
  [Reviewing the queue, and the merge line](#reviewing-the-queue-and-the-merge-line-review-merge),
  and for putting that same line on a schedule
  [Nightly auto-merge](#nightly-auto-merge-the-opt-in-tier). The loop itself never merges:
  a tick does not touch the base branch with the cap on, which is asserted, not promised.
- **Is not** a daemon. Nothing schedules itself: `install-cron.sh` prints the options
  and only writes a crontab with `--write`, and box cron may never fire on a sleeping
  cloud box — the bot's own scheduled task, or `watch` in a session you already have,
  is the driver.
- **Is not** a token meter. It changes *which account does the work*; it cannot read
  or rewrite xAI's server-side plan window.

## Layout

| file | lines | what it does |
|---|---|---|
| `PROTOCOL.md` | 278 | the supervision protocol, verbatim: roles, loop cadence, delivery, red lines, the two hourly report templates, the state record |
| `SKILL.md` | 436 | the supervisor-side policy: trigger keywords, preflight-first onboarding, tick contract, red lines |
| `scripts/autoloop.py` | 5870 | the engine: 35 subcommands, state machine, gate, red lines, PR, `watch`/`round`/`preflight`, `watchdog`/`alerts`/`budget`, `lifecycle`/`metrics` |
| `bootstrap.sh` | 158 | install into the configured supervisor skill directory, probe `env.manifest`, then preflight + init |
| `scripts/feedback.py` | 345 | local feedback classification, pending proposals, failure lessons and bounded intake rounds |
| `tests/test_*.py` | — | 69 additional tests for portable bootstrap, the nightly merge driver, PI adapter, launch guards, feedback CLI, progress clock and reporting |
| `env.manifest` | 23 | TAB-separated tool list (required / sched / pr / optional / worker) |
| `install-cron.sh` | 232 | prints the three wake-up drivers — with the `review` → `merge` stage attached when the merge line is on — `--write` to install box cron, verifies |
| `config.example.json` | 143 | every knob, with the reason each cap exists |
| `tests/selftest.py` | 2859 | 47 scenarios / 721 assertions against throwaway real git repos |
| `tests/fake_worker.py` | 172 | stand-in worker, 16 modes: `good`/`slow`/`deliver` plus 13 misbehaviours |
| `tests/fake_gh.py` | 244 | `gh` stand-in; records every call, always refuses `--admin`/`--force`, refuses `pr merge` unless a scenario opts in, file-locked |
| `tests/bin/dsh` | 90 | `dsh` stand-in, so the DeepSeekHarness adapter (stdin prompt, `--json` events, `--session-id` resume) is really exercised |

## GrokBot supervising PI / DeepSeek Harness

GrokBot reads this skill and invokes the engine **on its cloud computer**. PI or
DeepSeek Harness runs there as the coding worker; the plugin does not call a Grok
API or require the supervisor and worker to share an account. A local installation
on your laptop does not monitor a separate cloud computer. Transfer the plugin and
run preflight on the machine that owns the repository and workers.

Choose the skill directory that your GrokBot environment actually reads:
`bash bootstrap.sh --skills-dir /actual/supervisor/skills --repo /actual/project`.
`AUTOLOOP_SKILLS_DIR` provides the same setting; `DSH_SKILLS` remains compatible.
No GrokBot private directory or scheduler API is assumed. Without a discoverable
skill directory, have GrokBot read `SKILL.md` and `PROTOCOL.md` from the clone and
invoke `python3 /path/to/plugin/scripts/autoloop.py --repo /actual/project …`.

Worker candidates (verify against the cloud computer's installed version):

| worker | candidate runner | verification status |
|---|---|---|
| DeepSeek Harness | `dsh --profile headless --json < {prompt_file}` | existing adapter regression tests; cloud credentials still need checking |
| PI | `pi -p --mode json < {prompt_file}` | print/JSON documented at https://pi.dev; stdin and `--session` recovery require a real cloud run |

PI detection uses `pi --help`; missing or failed probes are reported honestly. The
PI executable on the development machine is a broken symlink, so this release does
not claim a successful PI end-to-end run. Use a full executable path in `runner.cmd`
if the cloud scheduler has a different PATH. Run two tasks and a repair before
leaving it unattended; confirm session recovery uses the intended task's session.

## Three input loops for GrokBot

The supervisor collects feedback using its own authorized tools and saves real
records as a JSON array. The engine provides no implicit X/Discord login, paid
classifier, reply sender or release publisher. Example input:

```json
[{"source":"github","source_id":"owner/repo#42","title":"Crash on export",
  "body":"Export crashes on Linux", "url":"https://github.com/owner/repo/issues/42",
  "author":"reporter", "paths":["src/export.py"],
  "acceptance":["Reproduce and verify export on Linux"]}]
```

Use a source ID containing the repository/channel/account scope, not a bare local
counter. Sources are `github`, `discord`, `x`, `local`; use actual repository-relative
file paths. Missing, nonexistent or unsafe paths never grant permission to work.
Set `feedback.project_terms` in `.autoloop/config.json` to the project's name and
aliases before social filtering. Without terms social items are filtered out;
inspect `feedback list` and configure terms before relying on the result.

```bash
python3 scripts/autoloop.py --repo /project feedback cycle --lane github --file /input/github.json
python3 scripts/autoloop.py --repo /project feedback cycle --lane social --file /input/social.json
python3 scripts/autoloop.py --repo /project feedback cycle --lane maintenance
python3 scripts/autoloop.py --repo /project feedback list
python3 scripts/autoloop.py --repo /project feedback promote --id fb-REPLACE_WITH_REAL_ID
python3 scripts/autoloop.py --repo /project approve feedback-REPLACE_WITH_REAL_ID
```

Each invocation performs one bounded round. GitHub/social without an input file
reports `source_not_configured`. Maintenance runs the existing bounded candidate
scanner and extracts evidence from real failure events into `lessons.json`; these
remain `pending_confirmation`, never automatic permanent rules or contributor bans.
`feedback.json` deduplicates source + source ID, preserves a previous proposal on
update, and records classification and any reply draft. Eligible bug/feature items
become `needs_approval` tasks; an imported classification cannot approve a task.
Advertisement replies stay drafts, including the specified intermediary-ad template.
Rules make no paid model call; uncertain items require supervisor judgement. No Jev
integration is claimed. GrokBot can use a separately authorized classifier, but its
labels remain untrusted input and do not bypass review.

Schedule these three commands through the GrokBot scheduler the user actually
enabled, or invoke them manually. They share the original A/B worker slots and
budgets; they do not create three workers or install a scheduler. New intake commands
use the engine lock. This is not evidence that a remote scheduler wakes successfully.

## Install on the cloud box

```bash
# 1. get the plugin onto the box (clone, scp, or drop it in the bot's skill directory)
git clone https://github.com/aritosi/grokbot-autoloop.git ~/grokbot-autoloop && cd ~/grokbot-autoloop

# 2. ask the box what it can actually do before anything is configured (§二 前置流程).
#    Read-only: identity, ssh→GitHub, gh auth, origin, the worker CLI, the protocol file.
#    Every gap comes back with the exact command; the engine never runs them for you.
python3 scripts/autoloop.py --repo /path/to/your/repo preflight

# 3. probe tools and install into the supervisor's confirmed skill directory.
#    --install additionally prints the install command for each missing tool — and
#    stops there: this script never runs apt/brew for you (§八 forbids a script
#    reinstalling the environment to keep a loop going).
bash bootstrap.sh --skills-dir /actual/supervisor/skills --repo /path/to/your/repo

# 4. teach the loop how to prove its work, and who does it
python3 scripts/autoloop.py --repo /path/to/your/repo init \
    --runner 'dsh --profile headless --json < {prompt_file}' \
    --test-cmd 'python3 -m pytest -q' \
    --lint-cmd 'ruff check .'

# 5. one round, by hand, before you trust a night with it
python3 scripts/autoloop.py --repo /path/to/your/repo suggest --limit 5
python3 scripts/autoloop.py --repo /path/to/your/repo round     # 轮前注入：协议 + §十 状态记录
python3 scripts/autoloop.py --repo /path/to/your/repo watch --minutes 30
```

If `preflight` returns exit 3, fix what it names before `init` — a loop started on a box
without an accepted SSH key produces branches nobody can see and red ticks that look like
worker failures. Where the bot looks for `SKILL.md` is a per-product convention:
`~/.dsh/skills/` is what `bootstrap.sh` installs into for DeepSeekHarness, and
`~/.cursor/skills/`, `~/.claude/skills/`, `~/.codex/skills/`, `~/.qoder-cn/skills/` are
the shapes in use elsewhere. xAI shipped "Skills" for Grok in May 2026, but the directory
Grok Bot's *cloud computer* reads was not confirmable from this machine — check it on the
box (`ls` the obvious candidates) and, failing that, pass `--repo` explicitly and let the
bot read this file as plain instructions.

### The one thing most likely to break a night

`--runner` must be a command that actually runs headless **on that box, on that
version**. Placeholders the engine substitutes: `{prompt_file}` `{worktree}` `{prompt}`
`{log}` (the same values are also exported as `AUTOLOOP_PROMPT_FILE`,
`AUTOLOOP_WORKTREE`, `AUTOLOOP_TASK`, `AUTOLOOP_SLOT`, `AUTOLOOP_LOG`, `AUTOLOOP_BRANCH`).
Test it once directly:

```bash
printf 'Reply with exactly: OK\n' > /tmp/t.md
dsh --profile headless --json < /tmp/t.md   # whatever your runner.cmd says, run it by hand
```

The DeepSeekHarness adapter contract in the shipped default is deliberate: the positional
argument is the *task text*, not a file path, so the prompt must arrive through `<`;
`--json` gives line events (`session` → `status`/`text`/`tool_call`/`tool_result` →
`final`) so the supervisor can read a session id and measure silence; exit 0 means the
turn ended; and `--session-id <id>` continues that session — which is how §六 gets the
*original* agent back to fix its own red gate. A wrong flag does not degrade: every agent
job dies in exactly the same way, and the loop reports red.

Measured on the box this ships from (dsh 0.2.0-rc.2, 2026-10-14): its own
`--profile headless --help` lists `--json` and `--session-id` and says `-` reads stdin, so
the shipped default is the CLI's documented shape rather than a guess. That probe is also
why `preflight` can take a minute on a cold box — the call cost 56 s cold and 8-23 s warm
here, so `dsh` is given a 120 s probe budget, and a probe that dies is reported as
*unproven* (`探测失败 rc=…`, run it by hand) instead of being invented into "this version
has no `--json`". A loop that silently drops `--json` loses its 空转 detection, so the
distinction is the point.

## Configuration

`.autoloop/config.json` is created by `init` from the detected repo; see
`config.example.json` for every field and why it exists. Environment overrides exist for
the same knobs (`AUTOLOOP_REPO`, `AUTOLOOP_WORKER_CMD`, `AUTOLOOP_DELIVERY_OWNER`,
`AUTOLOOP_MAX_PARALLEL`, `AUTOLOOP_NIGHTLY_MAX_JOBS`, `AUTOLOOP_PR_MAX_PER_DAY`,
`AUTOLOOP_JOB_TIMEOUT_S`, `AUTOLOOP_FINISH_GRACE_S`, `AUTOLOOP_STALL_S`,
`AUTOLOOP_WATCH_INTERVAL_S`, `AUTOLOOP_HARD_STOP`, `AUTOLOOP_GH` for a stand-in, and
debug-only `AUTOLOOP_NO_LIMIT`).

### Caps (defaults in `DEFAULT_CAPS`)

| cap | default | what it protects |
|---|---|---|
| `max_parallel` | 2 | A/B slots. 2 is the practical ceiling on one cloud box: two worktrees, two ports (`4100`/`4120`), two test DBs, two `TMPDIR`s |
| `pr_max_per_day` | 12 | how many PRs a human can actually review in a morning |
| `nightly_max_jobs` | 40 | worker jobs = the worker accounts' money |
| `max_turns_per_day` | 36 | supervisor turns (at a 10-min cadence that is most of a day, with room to spare) |
| `finish_grace_s` | 60 | 空转 guard 1 — the worker declared `AUTOLOOP_DONE` (or committed and went quiet): close its session and run the gate inside this many seconds. This is the "写完约一分钟内结束会话、接着交付 PR" behaviour |
| `stall_s` | 420 | 空转 guard 2 — no log bytes *and* no commits for this long ⇒ the run is not coming back (sandbox approval prompt, waiting on a sub-agent): kill the process group, count the try, repair |
| `watch_interval_s` | 60 | cadence of `watch`, and the number the `下一步` hint quotes while workers run |
| `job_timeout_s` | 1800 | the outer bound, not the normal path: past it the worker's **process group is killed** (SIGTERM then SIGKILL) and its diff can never be judged green |
| `max_repair_rounds` | 2 | one repair attempt with the raw failure output, then one more, then blocked |
| `fail_gate` | 3 | consecutive reds ⇒ maintenance mode (tests/docs/lint only) |
| `min_free_disk_mb` | 2000 | below it the loop refuses to create worktrees |
| `max_business_lines` | 400 | churn cap: one task may not rewrite half the repo |
| `night_window` | 22:00-08:00 | night = `auto_apply` kinds only |
| `gh_cooldown_min` / `gh_daily_rate_hits` | 15 / 3 | a 429 backs the loop off GitHub for the rest of the round; three hits a day disables remote features |
| `review_threshold` / `review_batch_max` / `review_max_age_h` | 3 / 5 / 2 | 批量审查 (§PR-1): three pending PRs make a batch due, a batch reads at most five, and the oldest pending PR is reviewed after two hours rather than waiting for the batch to fill |
| `merge_threshold` | 5 | 「PR 阈值 5」(§PR-5): below this many *eligible* PRs the merge line waits for the next round. `--now` skips the wait, never the per-PR checks |
| `risk_low_max` / `risk_mid_max` | 20 / 45 | the 0-100 grade bands: ≤ 20 and otherwise green ⇒ `auto_merge_ok` (the only verdict merge acts on); ≤ 45 ⇒ 需加强审查; above ⇒ `hold_high_risk`, which waits for a human even when every check is green |
| `merge_enabled` | **false** | PROTOCOL.md §八 keeps the merge decision with the user. Config switch *and* `--yes-i-will-merge`, both required, every time. `init --auto-merge` is the only CLI that writes it, and it enables the merge stage in the wake-up driver — `tick`/`watch` still never merge |
| `watchdog_stale_s` | 1800 | no `last_tick_at` for this long ⇒ `watchdog` calls the loop **dead** (critical, exit 4), not idle. Keep it ≥ 2× your tick cadence. A deliberately stopped loop is reported as stopped, never as dead |
| `no_progress_gate` | 3 | consecutive tasks that end blocked having delivered nothing ⇒ **hard stop** (`HALTED`, only `resume` closes it). Counts deliverables, not exited processes |
| `idle_gate` | 6 | consecutive empty ticks ⇒ **soft stop** (`STOP`, only `go` closes it). Adding a task does *not* resume it — closing a breaker is a human decision |
| `backoff_at` / `backoff_max_s` | 0.8 / 900 | at this fraction of a daily budget the loop slows down (parallel → 1, interval ×2) instead of running full speed into the ceiling; the backed-off interval never exceeds the ceiling. Backoff never *raises* a cap |
| `alert_cooldown_min` | 30 | minutes during which a repeat of the same problem folds into the open alert as `count += 1` instead of opening a new one |
| `progress_stale_s` | 900 | real-progress guard: only a new commit or a changed gate result resets this clock. A chatty log, repeated heartbeat, or repeated tool output alone does not count as progress |
| `same_error_gate` | 3 | the same normalized verification error this many times ⇒ `same_error` breaker (`HALTED`, only `resume` closes it). Signature normalizes digits and SHAs so line-number churn is not a new error |
| `error_retry_base_s` | 60 | repair backoff base: try *n* waits `base × 2^(n−1)` seconds, capped at 900. Backoff slows retries; it never deletes worktrees or uncommitted work |
| `report_interval_s` | 7200 | minimum interval for `report --outbox`. Reports are written locally to `report_outbox.jsonl` with `status=pending`; the sender calls `report --mark-sent <id>` after a successful delivery so a restart cannot send it twice |
| `grok_daily_max` | 0 | supervisor-model escalations per day. Default **0** = local monitoring never calls a model. Raising it does not create an outbound channel by itself; it only caps how many escalation records a caller may generate |

Raise them by editing the file, which is a human act — not by asking the bot, and not
with `AUTOLOOP_NO_LIMIT` inside a scheduled task.

### Who opens the PR

`delivery.owner` in `config.json` (or `AUTOLOOP_DELIVERY_OWNER`), and the default is
**`worker`** because that is what the supervision protocol asks for: 开发 Agent 自己建分支、
提交、开 PR、写交付报告，监工只启动 / 监控 / 恢复 / 调度. The supervisor's job is to not
believe the chat log — after a job ends it re-reads `gh pr list` and matches the PR by
**head branch**, so "I opened PR #7" that GitHub cannot find is treated as no PR, and a
real PR on the right branch is adopted into the slot instead of being duplicated.

What that does *not* relax: the gate still runs from the control copy, the red lines are
re-checked on the pushed diff, and a PR whose gate is red is annotated `pr_unverified`
and reported as 已建但未过监工闸门，勿合并 — the engine never closes or force-pushes over it,
and a red gate is precisely what stops the merge line's queue behind that PR (§六). Set
`delivery.owner=supervisor` for the v1.0.0 behaviour: workers may not
touch the remote at all, and the engine pushes + opens after green. Use it when the
worker has no `gh` authorisation, or when an unverified diff must not become public.

### Gate commands

`init` detects from the lockfile/manifests it finds (`package.json` scripts, pytest,
cargo, go, make) and writes `.autoloop/verify.sh` **plus** records them in
`config.json`. `verify.sh` in `.autoloop/` is the *control copy*: the engine always
executes the copy from the control checkout, fingerprints it (`sha256` in `doctor`),
and `.autoloop/**` is a forbidden path for workers — at *any* depth, so a nested
`vendor/.autoloop/verify.sh` is a second copy of the rules and is refused too. An empty
command prints `[SKIP]`, never `[PASS]` — and a tick whose every step is `[SKIP]` is
**red**, because a loop that runs nothing has no right to call anything verified.

### Policy and approval

`.autoloop/policy.json` holds `auto_apply` and `needs_approval` kind lists, extra
forbidden globs, and `default_kind_policy` (`needs_approval`). `candidates.json` is
*not* the queue: `queue.json` is work you or the user asked for, `candidates.json` is
what the loop discovered (`suggest` reads TODO/FIXME, skipped tests, lint output,
doc drift, and cites the evidence). A marker counts only when it *starts a note* —
a marker followed by a colon, a bracketed owner, a dash, or nothing at all — so a
word inside prose, or an alternation arm of a regex, is not a marker; a marker
inside a test file is left to the skipped-test scan (editing an existing test is
red), and the task's `acceptance[]` is anchored on that exact line, so it fails
while the line is there and passes once it is gone instead of passing either way.
Only `auto_apply` may self-start, and only during night hours does the whitelist
become the sole source. Morning triage:

```bash
autoloop.py queue                       # what is waiting
autoloop.py approve <id> [<id>…] --note "why"
autoloop.py reject <id> --note "no"
```

`reject` writes `status=rejected`; `approve` reverses that same status back to `open`,
because triage state is the human's to change. Rows that are already `done`, `blocked` or
`pr_open` are *not* revived — the approval is recorded, and the command says so instead of
promising a start the next tick would refuse.

### Reviewing the queue, and the merge line (`review`, `merge`)

Both stages are **script-only**: no model call, no worker, `counters.jobs_today` does not
move. A morning's worth of PRs is a queue problem, not a coding problem — and "the bot
thought it was fine" is not a reason to merge anything.

```bash
autoloop.py review [--json] [--all] [--force] [--threshold N] [--batch N]
autoloop.py merge --dry-run                  # what it would do — allowed while merge is off
autoloop.py merge [--now] [--limit N] [--method merge|squash|rebase] [--json] --yes-i-will-merge
```

`review` asks gh once for the fields a decision actually needs (CI rollup, mergeability,
draft state, churn) and grades each PR with a 0-100 `risk_score` computed from the diff:
churn, how many top-level modules it touches, whether it carries a test at all, how many
tries it took, whether a path matches the high-risk name list, minus a little for purely
mechanical kinds. The verdict is `auto_merge_ok` / `needs_review` / `hold_high_risk` /
`hold`, each with the blockers and notes that produced it, plus the order to merge in —
oldest first, and a PR sharing a file with an earlier one waits behind it rather than
racing it. All of it lands in `.autoloop/review.json`.

`merge` is off by default and needs two independent switches to do anything:
`caps.merge_enabled=true` in config **and** `--yes-i-will-merge` on the invocation
(PROTOCOL.md §八: 不擅自合并 PR，所有 PR 合并决定交给我). With both thrown it re-reads each PR
from gh *immediately* before acting — `review` may be ten minutes stale, and a stale
conclusion is not a reason to touch the base branch — refuses anything that is not
`auto_merge_ok` and green, refuses a PR whose head commit is not the SHA the local gate
was measured on (a push or a rebase after the gate makes it unverified, however clean the
diff reads), merges serially in the reviewed order, fast-forwards its own
checkout so the regression sees the merged tree, and runs `merge_regression_cmd` after each
merge. `--admin` and `--force` are never passed: bypassing branch protection is not a
feature, and the test suite fails a run that emits either.

What it reports, and what it refuses to round up (§PR-6):

| situation | recorded result |
|---|---|
| gate green, regression green | `merged` |
| merged, regression red | `regression_red` — the queue stops *behind* it, the base SHA it stopped at is printed, and the rollback is **yours**. The engine does not revert and does not rewrite shared history |
| merged, but no `merge_regression_cmd` (or the checkout could not be fast-forwarded) | `pending_verification` — reported as 待验证 with the reason, never as success |
| PR already MERGED when re-read | `already_merged`, skipped. Not an error, so `--limit` can still make progress on the next tick |
| anything else failing the re-read (CI red, conflict, draft, closed, base mismatch, head moved since the gate, forbidden path, secret line) | the queue stops at that PR; the ones behind it are neither skipped nor merged (§PR-4) |

Exit code is 1 when the queue stopped or a regression was red, 0 otherwise, and
`merge --json` prints parseable JSON on *every* path including refusals — a cron line that
reads it can never mistake progress prose for a result. Each merge also writes `done.json`
(with the base SHA before and after), flips the pool row to `status=merged` /
`task_state=DONE`, and appends a `pr_merged` event, so `timeline` can answer "what landed,
when, on which commit, and was it verified" without asking a model.

To put this line on a schedule instead of running it by hand, see **Nightly auto-merge** below.

#### Nightly auto-merge (the opt-in tier)

The default tier is 分支+PR：work stops at a PR and a human merges in the morning. The nightly
tier is *the same line* with a schedule attached to it — still two switches deep:

```bash
autoloop.py init --auto-merge [--merge-regression-cmd '<该仓库自己的回归命令>']
bash install-cron.sh --repo <repo>            # prints the driver with the merge stage attached
bash install-cron.sh --repo <repo> --write    # only if box cron is the driver you chose
```

`init --auto-merge` is the **only** command line that writes `caps.merge_enabled`. It writes
config and appends a `merge_line_enabled` event; it reviews nothing and merges nothing, and
running it twice reports 本来就开着 instead of pretending to be a fresh decision.
`--merge-regression-cmd` can be set on its own — configuring a regression command does not
open the merge line, and opening the line without one warns you on the spot that every merge
will be booked as `pending_verification`.

Each round the driver then runs `tick`, then `review --json`, then
`merge --yes-i-will-merge --now --json`. Three properties the suite pins down (`nightmerge`):

- **`tick` and `watch` never merge**, cap on or off. The merge is a separate command in a
  separate stage, so "the loop ran 200 times tonight" is never evidence that anything landed.
  `watch` is deliberately left out of the merge stage: re-reading gh every 60 s would burn the
  rate limit and make PRs disappear faster than anyone can read the report.
- **`--yes-i-will-merge` is still required on every invocation.** The cap enables the stage; it
  does not pre-answer the confirmation for the hours that follow.
- **`--now` skips only the 凑批 wait** (`caps.merge_threshold`), never a per-PR check. At most
  `caps.review_batch_max` PRs (default 5) get a conclusion per round, so that number also caps
  how many can merge — a night's queue drains over rounds rather than in one command.

A round with nothing to merge exits **0** and writes `{"idle": true}`: with a driver firing
every 15 minutes an empty queue is the normal case, and making it exit 1 would paint a failure
into `merge.out` every quiet hour until nobody reads it any more. `merge` still exits 1 when the
queue *stopped* on a PR and 3 when a gate refuses it, and a human running `merge` with no
conclusion behind it still gets the refusal.

What the night leaves behind: `.autoloop/merge.out` (raw JSON, per round),
`.autoloop/merge.json` (the last round that got far enough to have a conclusion) and
`events.jsonl` (`pr_merged` / `merge_idle` / `merge_queue_stopped` / `merge_line_enabled`).
`status` and `tick` add a `- 合并线：已启用 …` line **only while the cap is on**, so the hourly
read tells you whether the PRs listed there are waiting for you or already on main.

PROTOCOL.md says 不得将 PR 自动合并 and 所有 PR 合并决定交给我 — that text is the user's own and
stays verbatim; this tier does not amend it. It is that sentence *executed* rather than
overridden: the user made the merge decision when they turned the tier on, and every round
after just carries it out, which is why the engine still stops on anything it cannot prove
green and why a `regression_red` gets no automatic revert — reverting shared history is
another one of their decisions. The protocol injected into a worker round is unchanged, and
a worker never gains a merge verb.

### Cadence: `watch`, `tick`, and the hourly report

The v1.0.0 loop asked "is the worker done?" only when the bot happened to run a `tick`,
and asked it of the *process* — so an agent that had already written its code and gone
quiet held its slot until `job_timeout_s` (30 min) expired. That gap was the 空转 the user
reported: minutes of wall-clock spent looking alive while producing nothing, and a PR that
appeared half an hour after the work was finished.

Three pieces close it. `judge_running_job()` now classifies a *running* job as
`waiting | done | stalled` from its own log and commits (`finish_grace_s` after an
`AUTOLOOP_DONE` line or a commit + silence; `stall_s` after silence with no commits).
`watch` runs that judgement every `watch_interval_s` = 60 s in the foreground of a session
you already have, so detection latency is about a minute, and it exits 4 once `idle_ticks`
reaches `idle_gate` instead of spinning till dawn. And a tick that dispatched work is not
counted as
idle — `idle_ticks` only climbs while nothing is in flight, which is what makes the
"stop ticking, go refill the pool" message arrive at the right time rather than after the
sixth tick of a productive round.

```bash
autoloop.py watch --minutes 240     # 60s cadence; no daemon, Ctrl-C stops it
autoloop.py report --patrol         # §七's two templates, filled from real state
autoloop.py round                   # 轮前注入：协议路径 + sha256 + §十 状态记录
```

`report --patrol` chooses 定时巡检 when the loop is healthy and 异常告警 when something is
halted, blocked, spinning, out of budget, or holding a PR that failed the gate. It reports
counts and states, never what each agent is developing — §七 says the hourly report must
not be a feature list, and a patrol that reads like a changelog is the one that gets
ignored. The §五 floor (≥2 有效 PR per round) is printed as a shortfall when a round
delivers one, with the reason.

### Who watches the watcher: `watchdog`, breakers, backoff, `alerts`

Three questions that fail separately, so they get three separate mechanisms rather than one
`tick` that claims to cover all of them.

**Is the loop still alive?** `tick` cannot answer that — a tick that never runs reports
nothing — and `watch` cannot either, because it dies with the thing it watches. So
`watchdog` is a command for a *different* scheduler (cron, launchd, your monitor's own
probe): it reads state, disk and the workers' own log files, exits `0` healthy / `3` warn /
`4` critical, and writes a heartbeat to `.autoloop/watchdog.json`. It never runs `git`,
never calls `gh`, and never takes `loop.lock` — the `watchdog` scenario proves all three by
holding the lock with a real `fcntl.flock`, by byte-comparing `fake_gh.py`'s recorded argv
and `origin/main`'s SHA across a watchdog run, and by showing `tick` refusing to enter
while `watchdog` still answers. `--act` is the only verb it has, and it may only stop a
worker it can prove is silent; by default a stuck worker is *reported* with its slot and pid
and left alive.

```bash
autoloop.py watchdog --json          # rc 0 / 3 / 4; safe to run from any monitor
autoloop.py watchdog --act --json    # additionally: stop a provably silent worker
autoloop.py alerts --json            # open alerts, folded, acks applied
autoloop.py alerts --ack <id> --note "为什么可以不管"
autoloop.py budget --json            # counters vs caps + the backoff plan the next tick uses
autoloop.py lifecycle --json         # read-only issue/PR reconciliation: reopened, unverified-closed, CI, conflicts, review decision
autoloop.py metrics --json           # local load/quality/progress/budget summary; no model call
autoloop.py report --outbox --force  # generate a durable pending report; never sends anything
autoloop.py report --mark-sent <id>  # sender confirms delivery so a restart cannot send twice
```

**Has it stopped delivering?** Three breakers, at three rungs, each writing a real flag file
because a breaker that only logs is a suggestion:

| breaker | trips on | writes | closed by |
|---|---|---|---|
| `idle` | `idle_gate` consecutive empty ticks | `STOP` | `go` |
| `same_error` | `same_error_gate` repetitions of one normalized verification error | `HALTED` | `resume` |
| `no_progress` | `no_progress_gate` consecutive tasks that ended blocked with nothing delivered | `HALTED` | `resume` |

`state.json["breakers"]` records which one, when, and why, so the patrol report can say
「熔断已断开（no_progress）」 instead of 「人工停止」 — blaming a human who never touched the
box sends the operator looking for a person instead of a log line. The rungs are sequential,
not simultaneous: a gate that is red for everything pushes `fails_consecutive`,
`same_error` and `no_progress_streak` up together, so while maintenance mode is already
handling that same broken gate the hard stops stay shut (`same_error_deferred`,
`no_progress_deferred`). Without that rule, maintenance mode is unreachable in exactly the
situation it was written for. Neither breaker re-opens itself: adding a task does not resume
an idle-tripped loop, because closing a breaker is a human decision.

**How hard should it push with what is left?** `backoff_plan()` is a pure function of the
counters and the caps, and it answers with `{parallel, interval_s, throttled, reasons}`: at
`backoff_at` of a daily budget the loop drops to one concurrent job and doubles its
interval, and an idle night stretches its cadence as `base × 2^idle_ticks` up to
`backoff_max_s`. Maintenance mode or an open breaker takes parallel to `0`. It never raises
a cap — slowing down buys back the budget that is left; raising the ceiling would just be a
loophole around §四. `watch` follows the plan when `--interval` was not given, because an
explicit `--interval` is a human setting a cadence and humans outrank heuristics.

**One problem = one alert.** `alerts.jsonl` is append-only history and `alert_acks.json` is
separate, so acknowledging a page never edits the record of what happened. Repeats inside
`alert_cooldown_min` fold on *read* (`count += 1`, `first_seen_epoch` preserved) rather than
being dropped on write, which is what keeps the file provable: two runs of a stale loop are
two lines on disk and one open alert. An ack silences the *episode*, so a persistent problem
cannot re-page every minute, and a recurrence after the cooldown opens a new one. Every
record goes through `scrub_secrets` before it lands, and the `alerts` scenario asserts a
token in `state.json["next"]` comes out as `<redacted:credential-pattern>` — an alerting
channel that copies the secret it is alerting about is a second incident.

## The safety model, and how each rule is proven

| rule | mechanism | proven by (scenario) |
|---|---|---|
| worker cannot fake green | gate runs from the control copy; diff re-checked independently | `green`, `redlines` |
| no auto-merge / no deploy | merging is off by default and needs `caps.merge_enabled` **and** `--yes-i-will-merge`; no deploy, no migration; `--admin`/`--force` are never passed — and `fake_gh.py` refuses them even in a scenario that allows merge, so a bypass cannot be tested into existence; `auto/` never `main`. Opening the nightly cap changes no behaviour inside the loop: `nightmerge` asserts a `tick` with `merge_enabled=true` still emits zero `gh pr merge` | `green`, `cap`, `backup`, `workerpr`, `mergegate`, `nightmerge` |
| a merge decision is made against *now* | `_pr_fresh` re-reads each PR from gh immediately before acting — including its head commit, which must be the SHA the local gate verified (§七) — and a disagreement (CI went red, conflict, draft, closed, already merged, head moved since the gate) stops the queue at that PR and the ones behind it wait | `mergeserial`, `mergegate`, `test_stale_gate_merge.py` |
| a schedule adds no new authority | the nightly driver is `tick` → `review` → `merge --yes-i-will-merge --now`, generated from whatever `config.json` says, so `init --auto-merge` and a hand-edited cap produce the same line; `--yes-i-will-merge` is still required every invocation, `--now` skips only the batching cap, and an empty queue reports `{"idle": true}` and exits 0 instead of dressing a quiet round up as a failure | `nightmerge`, `test_nightly_auto_merge.py` |
| merged ≠ verified | `merge_regression_cmd` runs after each merge, on a checkout fast-forwarded to it; red ⇒ `regression_red`, queue stopped, base SHA printed, **no auto-rollback**; unconfigured ⇒ `pending_verification`, never success | `mergered` |
| the queue cannot lie about what is left | `remaining` is computed against the whole reviewed order, so `--limit` cannot hide work; an already-MERGED PR is recorded `already_merged` — not a failure, and not a second merge | `mergeserial` |
| a machine-read result is machine-readable | `merge --json` emits parseable JSON on every path, refusals included (`{"refused": true, "reason": …, "merged": []}`) | `mergegate` |
| a risk grade is evidence, not a vibe | 0-100 `risk_score` from churn / modules touched / tests present / tries / high-risk paths; ≤ `risk_low_max` with everything else green ⇒ `auto_merge_ok`, the only verdict the merge line will act on | `reviewqueue` |
| a script-only stage stays script-only | `review` and `merge` make zero model calls — asserted, not promised, via `counters.jobs_today == 0` | `reviewqueue`, `mergeserial`, `mergered` |
| the supervisor's chat log is not evidence | PR adoption re-reads `gh` by head branch; a self-reported PR that does not exist counts as zero | `workerpr`, `nomarker` |
| a red PR is annotated, never destroyed | `pr_unverified` + 勿合并 in the patrol; no close, no force-push, the repair round updates the same PR | `badpr` (§六) |
| tests are not negotiable | M/D/R on an existing test path is red; additions are fine | `redlines`, `unit` |
| forbidden files untouched | `FORBIDDEN_GLOBS` (lockfiles, manifests, migrations, `.github/`, Dockerfiles, `.env*`, keys, `.autoloop/**`) — control dirs and CI config are refused at any depth, not only at the repo root | `redlines`, `unit` |
| scope discipline | `paths` whitelist, disjoint between slots, new test files exempt | `collision`, `green` |
| no secrets anywhere | `SECRET_LINE_RE` over the diff; state files and PR bodies are checked too | `redlines`, `unit` |
| a finished worker does not squat in a slot | `AUTOLOOP_DONE` (or commits + silence) ⇒ session closed, gate run, delivery inside the same tick | `finish`, `nomarker` |
| a silent worker is not waited on | `stall_s` ⇒ process group killed, reason recorded, repair round resumes the *same* agent session — and that round is judged on its own run, so one silence cannot block the task | `stall`, `dsh`, `test_stall_repair_recovers` |
| a hung worker cannot burn the budget | timeout ⇒ kill process group ⇒ force red | `timeout` (also asserts a tick returns in <5 s while the worker sleeps 20 s) |
| a spinning loop says so out loud | `idle_gate` idle ticks ⇒ writes `STOP` (an open breaker, not a log line) + `watch` exits 4; a tick with work in flight never counts as idle; adding a task does **not** auto-resume | `idlespin` |
| a loop that delivers nothing stops itself | `no_progress_gate` tasks ending blocked with nothing delivered ⇒ `HALTED` + a critical alert; a real PR resets the streak; `resume` clears file, record and streak | `noprog` |
| escalation is a ladder, not a race | while maintenance mode is handling the same broken gate the hard stop stays shut and says so (`no_progress_deferred`), so maintenance remains reachable | `failgate`, `noprog` |
| the watchdog is independent of the thing it watches | no `git`, no `gh`, no `loop.lock`, and `--act` may only stop a provably silent worker — proven by holding a real flock and byte-comparing gh's argv and `origin/main` across a run | `watchdog` |
| a stopped loop is not a dead loop | `loop_stale` is suppressed when STOP/HALTED or a breaker explains the silence, so your own shutdown does not page you | `watchdog` |
| a chatty worker is not a progressing worker | only a commit-count change or a gate-state change resets `last_real_progress_at`; after `progress_stale_s` it is treated as stalled and stops burning the slot | `test_supervision_progress` |
| the same error is not retried forever | error signature normalizes digits/SHAs; the `same_error` breaker trips at `same_error_gate`, blocks the task and writes `HALTED`, preserving the worktree | `test_supervision_progress`, `failgate` |
| an issue's remote state is reconciled, not trusted | `lifecycle` flags reopened, unverified-closed and unclaimed issues from read-only `gh` data; it never closes, comments, or reopens anything itself | `test_lifecycle_reporting` |
| report delivery is not duplicated by a restart | `report_outbox.jsonl` keeps a digest and `pending`; the sender's `--mark-sent` is what flips it, and an unchanged digest is never re-emitted | `test_supervision_cli` |
| one problem is one alert | repeats fold inside `alert_cooldown_min` on read (append-only history intact), an ack silences an episode, a post-cooldown recurrence re-opens, and secrets are scrubbed on the way in | `alerts` |
| backoff slows down, it never raises a cap | `backoff_plan()` cuts parallel and doubles the interval near a budget, caps idle growth at `backoff_max_s`, and a spent cap still dispatches nothing | `backoff` |
| nothing is judged on an empty gate | all-`[SKIP]` ⇒ red ⇒ blocked, never a PR | `emptygate` |
| the night cannot overspend | jobs/turns/PR/disk caps ⇒ stop, report, do not lower quality | `jobcap`, `cap`, `size`, `failgate` |
| the report is the protocol's, not prose | `report --patrol` fills the two §七 templates from real state and omits per-agent feature detail | `patrol`, `workerpr` |
| the protocol is injected, not remembered | `round` prints PROTOCOL.md's path + real sha256 + the §十 record rebuilt from git/gh | `protocol` |
| an un-linked GitHub is a message, not a workaround | `preflight` names each gap with its command, exits 3, dispatches nothing, and writes nothing outside `.autoloop/` | `preflight` |
| a probe that died is not a finding | `probe_worker_cli` reports rc≠0 as 未证实 with its rc and tells you to run it by hand; only a help text that truly omits a flag says 找不到 | `dsh` |
| two supervisors, one box | `fcntl` lock; loser changes nothing | `lock` |
| state survives a reset | worktrees rebuilt from branches, `recover` from git+gh truth, orphans reported | `recover`, `orphan` |
| history is not lost | day rollover archives counters to `history/<day>.json`, `ledger.jsonl`, journal | `rollover`, `ledger` |

Run it yourself (no network, no global state, throwaway repos in `$TMPDIR`):

```bash
python3 -m py_compile scripts/autoloop.py
python3 tests/selftest.py             # 47 scenarios; add --only <name> for one
python3 -m unittest discover -s tests  # 69 tests: portable bootstrap + the merge drivers
```

The `unittest` suite runs the real `install-cron.sh` against a `crontab` shim in `$TMPDIR`,
so no version of this suite has ever touched the box's crontab — including the tests that
install and remove a nightly entry.

Latest full run on this machine: **721 passed, 0 failed (134 s)** and **69 tests, OK
(46 s)** — Python 3.14.6,
git 2.50.1, macOS, load average ~7. The *same* selftest took 391 s one hour earlier on the
same box at load 16, and 1063 s at load average 213
(`python3 -c pass` alone cost 3-9 s there), so the seconds are a property of the box, not
a regression signal; the pass count is. The engine never touches the network in tests:
`tests/fake_gh.py`
stands in for `gh` (it records every argv, refuses `--admin`/`--force` unconditionally, and
only performs `pr merge` in a scenario that opts in), `tests/fake_worker.py` for the coding
agent, `tests/bin/dsh` for the DeepSeekHarness CLI.

## Deviations from the source prompts, on purpose

- **JSON, not YAML.** The prompt specified `*.yaml` state files. A cloud box does not
  guarantee PyYAML, and stdlib-only was a hard requirement, so state lives in
  `*.json`. Same file names, same meaning.
- **PR-only delivery, and in the default mode the worker opens it.** Neither prompt has a
  merge path; this build grew one later (the 「PR 批量审查 + 受控合并」 instruction, §PR-1…§PR-6)
  and it ships **off**. `workerpr` still asserts the forbidden verb count is zero on the
  default path, where two agents deliver two PRs in one round and nothing merges;
  `mergegate` and `mergeserial` exercise the opt-in line, `nightmerge` exercises the nightly
  tier on top of it (the same two switches, put in a schedule — not a different code path),
  and `fake_gh.py` refuses `--admin`/`--force` even there, so a bypass cannot be tested into
  existence. §四 puts branching, committing, pushing and the PR on the 开发 Agent;
  `delivery.owner=supervisor` keeps the older split where no worker may
  touch the remote.
- **Verification numbers, not billing numbers.** Counters are volumes; the money is on
  the worker CLIs' accounts.
- **One URL in the protocol is older than the account.** PROTOCOL.md §二 names
  `github.com/KEQingFeng/grokbot-autoloop`; the account is now `aritosi`, GitHub still
  redirects the old path, and the protocol text stays byte-for-byte the user's own — so the
  README and `preflight` print the current URL while §二 keeps the original. Nothing in this
  build edits PROTOCOL.md to make a link look tidy.
- **`gh` is optional.** Without it the loop pushes `auto/*` branches and prints the PR
  body, and says *why* in the log. It does not pretend a PR exists.
- **No scheduler is installed by default.** §十 forbids claiming a background timer from a
  prompt alone, so `install-cron.sh` prints its options and writes only with `--write`,
  and `watch` is offered as the in-session alternative you can actually see. With
  `caps.merge_enabled=true` the driver it prints and installs gains the `review` → `merge`
  stage; with it false — the shipped default — the driver is tick-only and the merge line
  stays wherever the user left it.

## Known limits

- The bot's reasoning tokens still come from the bot. What leaves that window is file
  reading, test watching, log paging, and the implementation itself — the bulk of a
  heavy turn, but not all of it.
- Repo-wide test suites that are slow dominate the wall-clock per round; the gate has
  its own timeout but a 20-minute suite is a 20-minute round.
- `paths` overlap detection is glob-based, so a task that legitimately needs to touch
  a shared file (`src/index.ts` re-exports) cannot run in parallel with one that owns
  `src/*`. The engine serialises them instead of guessing.
- The `stall_s` judge is silence + no commits, which is also what a legitimately slow
  step (a 10-minute install) looks like. It is capped by `job_timeout_s` and it kills
  only its own process group, so the failure mode is a wasted round, not a lost repo —
  but a repo with one very long build wants a higher `stall_s`, decided by a human.
- Nothing here survives a hostile worker with write access to the *control* checkout.
  `.autoloop/**` being a forbidden path — at any depth, so a force-added
  `vendor/.autoloop/verify.sh` is refused rather than silently becoming a second copy of
  the gate rules — plus `git add -A` catching force-adds covers the accidental case; a
  box where the worker can edit `verify.sh` directly is outside the threat model.
- The 空转 judges are wall-clock timers, so they assume a box that is not oversubscribed.
  Measured here on 2026-10-14 with load average 213: `python3 -c pass` took 3-9 s, so a
  `watch_interval_s: 60` sweep could be a few seconds late and `preflight`'s CLI probe
  dominated the suite's runtime. Nothing broke — the guards fire on 60/420/1800 s windows —
  but a cloud box that is *shared* wants `stall_s` and `job_timeout_s` reviewed, not trusted.
- The post-merge regression runs in the supervisor's own checkout, fast-forward only. A
  checkout that is dirty, on another branch, or diverged from `origin/<base>` is not moved —
  the regression is skipped and the merge is recorded `pending_verification` with the reason.
  A box where the supervisor checkout is also somebody's working copy therefore verifies
  nothing; give the merge line its own clone.
- `review` grades risk from the *shape* of a diff, not by reading it: churn, modules
  touched, tests present, tries, high-risk path names. Twelve lines in a payment path can
  score low if the name does not match the list, which is why `auto_merge_ok` also requires
  a green local gate and green CI — and why enabling the merge line at all stays a human
  decision rather than a default.
- Merging serially costs one `gh pr view` per PR right before it is merged (§PR-3), on top
  of the merge itself. That is deliberate (a stale conclusion must not touch the base
  branch) but it is rate-limit-relevant on a big queue — which is what `merge_threshold`
  and `--limit` are for.
- The nightly tier merges what it can *prove*, so on a night where the local gate never ran
  for a branch, `review` holds that PR and the queue drains to zero merges. That is the
  intended failure: a quiet `merge.out` is honest, a wrong merge is not. Read the tier's real
  risk the other way too — a `regression_red` at 03:00 stops the queue and leaves the base
  branch red until a human reads the report, because this engine never reverts and never
  rewrites shared history. Choosing 夜间自己合 buys latency and pays for that window; the
  早上人工合 tier buys the review and pays for the delay.
- A quiet nightly round exits 0 (`{"idle": true}`), so `merge.out` is not an alert channel.
  The signals that mean something are `merge.json`'s `stopped`, the `merge_queue_stopped` /
  `pr_merged` events, and `alerts` — an exit code alone tells you only that the round did not
  stop the queue.
- The merge stage only runs if the *driver* runs. `install-cron.sh --write` verifies the line
  is readable back in a crontab; it cannot verify a cron daemon fired it, and a sleeping box
  merges nothing all night while every file above stays perfectly green. Same caveat as the
  watchdog: the schedule is the thing you have to check, not the script.
- `watchdog` is only as independent as the scheduler running it. If you schedule it from the
  same cron line as `tick`, a box whose cron is dead loses both at once and nothing pages
  you; the honest setup is a second scheduler, or a monitor that probes from outside the box.
  Its `worker_stuck` finding inherits `stall_s`'s weakness (a legitimately slow step looks
  like silence), and `--act` kills a process group it believes is silent — so `--act` belongs
  on a schedule you have looked at, not on a guess.
- Alerts fold on `dedupe_key`, which is derived from the finding kind (plus slot for
  per-worker findings). Two *different* problems that produce the same kind therefore share
  one open alert with a growing `count`; the detail field carries the latest one, and
  `alerts.jsonl` keeps both lines. A monitoring setup that needs them separate has to read
  the history file, not the folded view.
- `alert_cooldown_min` is a wall-clock window measured against the previous record of the
  same key, so a box whose clock jumps (NTP correction, snapshot restore) can fold or
  re-open an episode early. Nothing here is safe against a clock that runs backwards; the
  event log's `epoch` fields are the only way to audit that after the fact.
- Backoff reacts to *daily* counters, so it resets at the day rollover. A loop that spends
  its budget in the first two hours runs full speed until it does, then slows — it does not
  plan the night in advance.

## Not provable from a machine that is not the cloud box

1. `gh` authentication **there** — `preflight` and `doctor` both report it, in one line.
2. The real headless flag of the worker CLI **there**, on that version — `preflight`
   probes the configured runner for the flags its adapter needs (`--json`,
   `--session-id` for dsh) and prints what to change when the probe disagrees.
3. Whether the scheduler actually wakes the bot — box cron on a hibernating cloud
   machine accepts lines it will never run. No test on this machine can prove it.

`preflight` closes 1 and 2 *on the box* and hands you the commands instead of running
them (§八). Check 3 by hand before the first unattended night. If any of them is false,
the honest output of this plugin is a stopped loop with a reason, not a quiet one.
