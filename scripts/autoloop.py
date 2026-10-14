#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoLoop — deterministic supervisor engine for unattended development on a
cloud-agent box (Grok Bot's cloud computer, or any Linux runner you can reach).

The contract in one paragraph
------------------------------
The model makes only the judgements a model should make: which task is worth
doing, what a PR is for, whether a candidate is safe. Every other decision is
program code with an exit code — branch creation, slot recovery, path-conflict
checks, the external acceptance gate, the red-line re-checks, the cost caps, the
ledger, the report. A prompt cannot be audited and an agent that lost its
context cannot be trusted to remember it is not allowed to push to `main`; a
script can be both.

All recoverable state lives in `.autoloop/` on the box, so any fresh agent
instance resumes from `status` / `tick` with no chat history.

Stdlib only. Subcommands:
  doctor init tick status plan dispatch reap verify prbody pr suggest report
  approve reject queue budget sync stop go halt resume recover backup ref
  watch round preflight
"""

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime

VERSION = "1.1.0"
ALO_DIR = ".autoloop"

DEFAULT_CAPS = {
    "max_parallel": 2,               # A / B slots
    "pr_max_per_day": 12,
    "nightly_max_jobs": 40,
    "max_turns_per_day": 36,
    "job_timeout_s": 1800,
    "max_repair_rounds": 2,
    "fail_gate": 3,                  # consecutive red verifies -> maintenance
    "min_free_disk_mb": 2000,
    "max_business_lines": 400,       # a single task may not exceed this churn
    "night_window": "22:00-08:00",
    "gh_cooldown_min": 15,
    "gh_daily_rate_hits": 3,
    # 空转 caps. v1.0.0 only looked at a worker when its process exited or when
    # job_timeout_s (30 min) expired, so a worker that finished the code and then
    # kept typing / waited on an approval prompt held its slot for half an hour.
    "finish_grace_s": 60,           # declared done -> session closed within a minute
    "stall_s": 420,                 # silent log + no commit -> the run is not coming back
    "watch_interval_s": 60,         # `watch` cadence; also the `next` hint for ticks
}

# Red lines that are mechanically checkable. Anything here is refused by code,
# not by asking the model nicely.
FORBIDDEN_GLOBS = [
    "*package-lock.json", "*pnpm-lock.yaml", "*yarn.lock", "*uv.lock",
    "*poetry.lock", "*Cargo.lock", "*composer.lock", "go.sum", "Gemfile.lock",
    "package.json", "pyproject.toml", "requirements*.txt", "go.mod", "Gemfile",
    "pom.xml", "build.gradle*", "Cargo.toml", "setup.py", "setup.cfg",
    "migrations/*", "**/migrations/*", "*migration*/*.sql",
    ".github/workflows/*", ".github/*", "Dockerfile*", "docker-compose*",
    ".env*", "*.pem", "*.key", "*.p12", "*.jks", "*secret*", "*credential*",
    "id_rsa*", ".npmrc", ".pypirc", ".netrc",
    ".autoloop/*", ".autoloop/**/*",
]
FORBIDDEN_GLOBS = sorted(set(FORBIDDEN_GLOBS))

# Scratch a dependency or test run leaves behind. These are not a worker's work:
# counting them as "uncommitted changes" would turn every otherwise green run red,
# because `pytest` itself writes __pycache__/ into the worktree before the red-line
# check looks at it. Only run artefacts — never a source directory.
RUN_NOISE_MARKERS = ("__pycache__/", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/",
                     ".nyc_output/", "/.tmp/", "/node_modules/", "/.turbo/",
                     "/.next/", "/venv/", "/.venv/", "/htmlcov/", "/.cache/")
RUN_NOISE_SUFFIXES = (".pyc", ".pyo", ".coverage", ".ds_store")


def is_run_noise(path):
    p = "/" + (path or "").replace(os.sep, "/").lower()
    if any(m in p for m in RUN_NOISE_MARKERS):
        return True
    return p.endswith(RUN_NOISE_SUFFIXES)


def status_path(line):
    """File name out of one `git status --porcelain` line (handles `old -> new`)."""
    field = line[3:].strip() if len(line) > 3 else line.strip()
    if " -> " in field:
        field = field.rsplit(" -> ", 1)[-1]
    return field.strip('"').strip()

# Test locations: modification/deletion is red, addition is fine.
TEST_PATH_GLOBS = [
    "tests/*", "test/*", "*/tests/*", "**/tests/*", "**/test/*", "specs/*",
    "*/spec/*", "**/__snapshots__/*", "*.test.*", "*.spec.*", "*_test.*",
    "test_*.py", "*snapshot*",
]

HIGH_RISK_NAME_RE = re.compile(
    r"(auth|pay|billing|crypto|token|secret|migrat|deploy|network|session|"
    r"password|privile|delete|purge)", re.I)
SECRET_LINE_RE = re.compile(
    r"(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|"
    r"(api[_-]?key|secret|passwd|password|access_token)[\"'\s]*[:=]"
    r"[\"'\s]+[A-Za-z0-9+/_-]{16,})", re.I)

SLOTS = ("A", "B")
ACTIVE_STATES = ("developing", "verifying", "repairing")
DONE_STATES = ("pr_open", "pr_ready", "blocked", "idle", None)

# The one line a worker must print when its diff is committed and its own checks
# have run. It is a *signal to the supervisor*, not a claim of correctness: the
# gate in .autoloop/verify.sh still decides green/red. Without it the supervisor
# can only tell "finished" from "silent" by waiting for the process to exit, which
# is exactly the 空转 that burns a slot for job_timeout_s.
DONE_MARKER = "AUTOLOOP_DONE"

# Which CLI is driving the worker, and what its "one task then exit" invocation
# looks like. Probed statically by `doctor` (help text + version), never by paying
# for a throwaway run.
WORKER_CLIS = {
    "dsh": {
        "probe": "dsh --profile headless --help",
        # Measured on the box this ships from: warm runs are 8-23 s, a cold Node start
        # took 56 s. A short timeout used to print "本机版本缺 --json" for a flag that
        # does exist, which is a fabricated diagnosis.
        "probe_timeout_s": 120,
        "must_support": ("--json", "--session-id"),
        "cmd": "dsh --profile headless --json < {prompt_file}",
        "resume_flag": "--session-id {session_id}",
        "note": "任务走 stdin（`-` 不能和别的词混用）；--json 的按行事件让监工能看到"
                "进度并在 60s 内判定完成；final 事件后进程自己退出，退出码 0=turn end。",
    },
    "codex": {"probe": "codex exec --help", "must_support": (),
              "cmd": "codex exec {prompt_file}", "resume_flag": "resume {session_id}",
              "note": "codex exec 读文件参数"},
    "claude": {"probe": "claude --help", "must_support": ("-p",),
               "cmd": "claude -p < {prompt_file}", "resume_flag": "--resume {session_id}",
               "note": "headless 打印结果即退出"},
    "qoder": {"probe": "qoder --help", "must_support": (),
              "cmd": "qoder -p < {prompt_file}", "resume_flag": "", "note": "同 claude"},
    "grok": {"probe": "grok --help", "must_support": (),
             "cmd": "grok -p < {prompt_file}", "resume_flag": "", "note": "同 claude"},
}

# ------------------------------------------------------------------ primitives


def now():
    return int(time.time())


def today():
    return time.strftime("%Y-%m-%d")


def stamp(ts=None):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def slug(text, maxlen=48):
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (text or "task")[:maxlen]


def log(msg):
    sys.stdout.write(str(msg) + "\n")


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def append_jsonl(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")


def run(argv, cwd=None, timeout=120, env=None, shell=False):
    """Return (rc, out, err). Never raises on non-zero."""
    try:
        proc = subprocess.run(
            argv if shell else [argv] if isinstance(argv, str) else argv,
            cwd=cwd, env=env, shell=shell,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, universal_newlines=True, errors="replace")
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", "command not found: %s" % (argv if isinstance(argv, str) else argv[0])
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after %ss" % timeout
    except Exception as exc:  # noqa: BLE001
        return 125, "", "%s: %s" % (type(exc).__name__, exc)


def git(args, cwd, timeout=120):
    return run("git " + args, cwd=cwd, timeout=timeout, shell=True)


def gh_bin():
    return os.environ.get("AUTOLOOP_GH", "gh")


def which(name):
    return shutil.which(name)


def probe_worker_cli(name):
    """Ask a worker CLI's own --help what it supports, and report honestly.

    One function because `doctor` and `preflight` must not drift: the same flag list, the
    same timeout, and the same three outcomes. A probe that dies (rc 124 = it was too slow,
    rc 127 = not on PATH) is *not* evidence the flag is missing — claiming it is turns an
    environment question into a fake finding about the CLI, and the loop would then drop
    `--json` and lose its 空转 detection for no reason.
    """
    spec = WORKER_CLIS[name]
    rc, out, err = run(spec["probe"], timeout=spec.get("probe_timeout_s", 30), shell=True)
    blob = (out or "") + (err or "")
    missing = [f for f in spec["must_support"] if f not in blob]
    if rc == 124:
        verdict = "探测超时（%ss），没有结论 —— 不是这个 CLI 缺 flag，是它启动慢" % (
            spec.get("probe_timeout_s", 30))
    elif rc != 0:
        verdict = "探测失败 rc=%s: %s" % (rc, (err or out)[:160] or "无输出")
    elif missing:
        verdict = "有 --help，但里面找不到 %s" % ", ".join(missing)
    else:
        verdict = "probe: %s；缺 %s" % (spec["probe"], "无")
    return {"rc": rc, "missing": missing, "probe": spec["probe"],
            "unproven": rc != 0, "ok": rc == 0 and not missing, "verdict": verdict}


def sha256_file(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


# ------------------------------------------------------------------------ ctx


class Ctx:
    """Paths, config and caps for one repository."""

    def __init__(self, repo=None):
        self.root = self._find_root(repo or os.environ.get("AUTOLOOP_REPO"))
        if not self.root:
            die("not a git repository: pass --repo /path/to/repo or cd into one")
        self.alo = os.path.join(self.root, ALO_DIR)
        self.cfg = self._load_cfg()
        self.state_path = os.path.join(self.alo, "state.json")
        self.state = read_json(self.state_path) or {}
        if self.state:
            self._normalize_state()
        self.log_dir = os.path.join(self.alo, "log")
        self.job_dir = os.path.join(self.alo, "jobs")

    @staticmethod
    def _find_root(repo):
        if repo:
            rc, out, _ = git("rev-parse --show-toplevel", cwd=repo)
            return out if rc == 0 else None
        rc, out, _ = git("rev-parse --show-toplevel", cwd=os.getcwd())
        return out if rc == 0 else None

    def _load_cfg(self):
        cfg = read_json(os.path.join(self.alo, "config.json"))
        if not cfg:
            cfg = {"caps": dict(DEFAULT_CAPS), "base_branch": None,
                   "runner": {}, "verify": {}, "reference_enabled": None}
        caps = dict(DEFAULT_CAPS)
        caps.update(cfg.get("caps") or {})
        cfg["caps"] = caps
        for key, val in (("max_parallel", "AUTOLOOP_MAX_PARALLEL"),
                         ("pr_max_per_day", "AUTOLOOP_PR_MAX_PER_DAY"),
                         ("nightly_max_jobs", "AUTOLOOP_NIGHTLY_MAX_JOBS"),
                         ("job_timeout_s", "AUTOLOOP_JOB_TIMEOUT_S"),
                         ("finish_grace_s", "AUTOLOOP_FINISH_GRACE_S"),
                         ("stall_s", "AUTOLOOP_STALL_S"),
                         ("watch_interval_s", "AUTOLOOP_WATCH_INTERVAL_S")):
            raw = os.environ.get(val)
            if raw and raw.isdigit():
                caps[key] = int(raw)
        if os.environ.get("AUTOLOOP_NO_LIMIT") == "1":
            caps["pr_max_per_day"] = 10 ** 6
            caps["nightly_max_jobs"] = 10 ** 6
            caps["max_turns_per_day"] = 10 ** 6
        cfg["base_branch"] = cfg.get("base_branch") or self.detect_base()
        return cfg

    def detect_base(self):
        rc, out, _ = git("symbolic-ref --short HEAD", cwd=self.root)
        if rc == 0 and out:
            return out
        rc, out, _ = git("rev-parse --abbrev-ref --symbolic-full-name origin/HEAD",
                         cwd=self.root)
        if rc == 0 and out:
            return out.split("/")[-1]
        return "main"

    def cap(self, key):
        return self.cfg["caps"][key]

    @property
    def base(self):
        return self.cfg["base_branch"]

    @property
    def origin_base(self):
        """Where a new auto/* branch starts. `origin/<base>` once the fetch has it;
        the local branch when the remote is still empty (first run against a freshly
        created repo). Never a reference that does not exist — otherwise every slot
        dies with `invalid reference: origin/main` instead of one usable sentence."""
        cached = getattr(self, "_origin_base_cache", None)
        if cached:
            return cached
        for ref in ("origin/" + self.base, self.base):
            rc, _, _ = git("rev-parse --verify --quiet %s" % shlex.quote(ref), cwd=self.root)
            if rc == 0:
                self._origin_base_cache = ref
                return ref
        return "origin/" + self.base

    def need_init(self):
        if not os.path.isdir(self.alo):
            die("no .autoloop/ here — run `autoloop.py init` first")

    # -- state ------------------------------------------------------------

    def _normalize_state(self):
        st = self.state
        st.setdefault("slots", {s: None for s in SLOTS})
        for slot in SLOTS:
            st["slots"].setdefault(slot, None)
        counters = st.setdefault("counters", {})
        for key in ("turns_today", "jobs_today", "pr_today", "fails_today",
                    "fails_consecutive", "gh_hits_today", "cands_added",
                    "prs_valid_today", "idle_ticks"):
            counters.setdefault(key, 0)
        st.setdefault("pending_pr", [])
        st.setdefault("notes", [])
        st.setdefault("ref", None)
        st.setdefault("gh_cooldown_until", 0)
        st.setdefault("day", today())
        st.setdefault("turn", 0)
        return st

    def ensure_state(self):
        if not self.state:
            self.state = {
                "version": 1, "engine": VERSION,
                "repo": self.remote_name(),
                "base_branch": self.base,
                "mode": "auto", "day": today(), "turn": 0,
                "counters": {"turns_today": 0, "jobs_today": 0,
                             "pr_today": 0, "fails_today": 0,
                             "fails_consecutive": 0, "gh_hits_today": 0,
                             "cands_added": 0, "prs_valid_today": 0,
                             "idle_ticks": 0},
                "slots": {s: None for s in SLOTS},
                "pending_pr": [], "gh_cooldown_until": 0,
                "idle_reason": None, "maintenance": False,
                "last_tick_at": 0, "last_report_at": 0,
                "ref": None, "notes": [],
            }
        return self._normalize_state()

    def save(self):
        self.state["updated_at"] = now()
        write_json(self.state_path, self.state)

    def roll_day(self):
        if self.state.get("day") == today():
            return False
        archive = os.path.join(self.alo, "history", self.state.get("day", "unknown") + ".json")
        snap = {k: v for k, v in self.state.items()}
        write_json(archive, snap)
        self.state["day"] = today()
        self.state["counters"].update({"turns_today": 0, "jobs_today": 0,
                                       "pr_today": 0, "fails_today": 0,
                                       "gh_hits_today": 0, "cands_added": 0,
                                       "prs_valid_today": 0})
        self.state["pending_pr"] = []
        self.note("day rollover")
        # the journal is what a fresh instance reads; the counter reset must be
        # visible there, not only inside state.json
        self.journal("- day rollover: counters reset for %s, yesterday kept in %s"
                     % (today(), os.path.basename(archive)))
        return True

    # -- collections ------------------------------------------------------

    def coll(self, name):
        return read_json(os.path.join(self.alo, name), []) or []

    def set_coll(self, name, items):
        write_json(os.path.join(self.alo, name), items)

    def note(self, text):
        self.state.setdefault("notes", []).append("%s %s" % (stamp(), text))
        self.state["notes"] = self.state["notes"][-25:]

    def ledger(self, rec):
        rec = dict(rec)
        rec.setdefault("ts", stamp())
        append_jsonl(os.path.join(self.alo, "ledger.jsonl"), rec)

    def journal(self, text):
        os.makedirs(self.log_dir, exist_ok=True)
        path = os.path.join(self.log_dir, today() + ".md")
        first = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as fh:
            if first:
                fh.write("# AutoLoop log %s\n" % today())
            fh.write("\n## %s\n\n%s\n" % (stamp(), text))

    def remote_name(self):
        rc, out, _ = git("config --get remote.origin.url", cwd=self.root)
        if rc != 0 or not out:
            return os.path.basename(self.root)
        hit = re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?$", out)
        return hit.group(1) if hit else out


# ----------------------------------------------------------------------- misc


def die(msg, code=2):
    log("autoloop: ERROR: %s" % msg)
    sys.exit(code)


def eprint(msg):
    sys.stderr.write(str(msg) + "\n")


class Lock:
    """Advisory lock: one cloud box, several bots sharing it (§ shared computer)."""

    def __init__(self, ctx, wait_s=0):
        self.path = os.path.join(ctx.alo, "loop.lock")
        self.wait_s = wait_s
        self.fh = None

    def __enter__(self):
        import fcntl
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fh = open(self.path + ".l", "a+", encoding="utf-8")
        deadline = time.time() + self.wait_s
        while True:
            try:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= deadline:
                    return None
                time.sleep(0.5)
        self.fh.truncate(0)
        self.fh.write(json.dumps({"pid": os.getpid(), "at": stamp(),
                                  "cmd": " ".join(sys.argv[1:2])}) + "\n")
        self.fh.flush()
        return self

    def __exit__(self, *exc):
        import fcntl
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            self.fh.close()
        except Exception:  # noqa: BLE001
            pass
        return False


def read_policy(ctx):
    pol = read_json(os.path.join(ctx.alo, "policy.json"), {}) or {}
    return {
        "auto_apply": list(pol.get("auto_apply") or []),
        "needs_approval": list(pol.get("needs_approval") or []),
        "extra_forbidden": list(pol.get("extra_forbidden_globs") or []),
        "kinds_default": pol.get("default_kind_policy", "needs_approval"),
    }


def kind_policy(kind, pol):
    """auto_apply / needs_approval for a task kind, per policy.json."""
    if kind in pol["auto_apply"]:
        return "auto_apply"
    if kind in pol["needs_approval"]:
        return "needs_approval"
    return pol["kinds_default"]


def is_forbidden(path, pol):
    norm = path.replace("\\", "/")
    base = os.path.basename(norm)
    for glob in FORBIDDEN_GLOBS + pol["extra_forbidden"]:
        if fnmatch.fnmatch(norm, glob) or fnmatch.fnmatch(base, glob):
            return glob
        if glob.startswith("**/") and fnmatch.fnmatch(norm, glob[3:]):
            return glob
        if "*/" in glob and fnmatch.fnmatch(norm, glob):
            return glob
    return None


def is_test_path(path):
    norm = path.replace("\\", "/")
    base = os.path.basename(norm)
    for glob in TEST_PATH_GLOBS:
        if fnmatch.fnmatch(norm, glob) or fnmatch.fnmatch(base, glob):
            return True
        if glob.startswith("**/") and fnmatch.fnmatch(norm, glob[3:]):
            return True
    return False


def path_matches(pattern, path):
    norm = path.replace("\\", "/")
    if fnmatch.fnmatch(norm, pattern):
        return True
    if pattern.endswith("/*") or pattern.endswith("/**"):
        head = pattern.rstrip("*").rstrip("/") + "/"
        if norm.startswith(head):
            return True
    if fnmatch.fnmatch(os.path.basename(norm), pattern):
        return True
    if "*/" in pattern and fnmatch.fnmatch(norm, pattern):
        return True
    if "**/" in pattern and fnmatch.fnmatch(norm, pattern.replace("**/", "*")):
        return True
    if "**/" in pattern and norm.split("/")[-1] and fnmatch.fnmatch(
            "*/".join(norm.split("/")[-2:]), pattern.replace("**/", "")):
        return True
    return False


def paths_overlap(left, right):
    for a in left or []:
        for b in right or []:
            if a == b:
                return True
            if ("/**" in a or "**" in b or "*" in a or "*" in b):
                pa = a.replace("/**", "*")
                pb = b.replace("/**", "*")
                if pa == pb or pa.rstrip("*").rstrip("/") == pb.rstrip("*").rstrip("/"):
                    return True
                if pa.startswith(pb.rstrip("*")) or pb.startswith(pa.rstrip("*")):
                    return True
    return False


def free_disk_mb(path):
    try:
        return int(shutil.disk_usage(path).free / (1024 * 1024))
    except OSError:
        return -1


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def mode_now(ctx):
    """day / night, from state override or the configured window."""
    forced = ctx.state.get("mode_forced")
    if forced in ("day", "night"):
        return forced
    win = ctx.cap("night_window") or ""
    hit = re.match(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})", win)
    if not hit:
        return "day"
    hh, mm, h2, m2 = (int(x) for x in hit.groups())
    cur = datetime.now().hour * 60 + datetime.now().minute
    start, end = hh * 60 + mm, h2 * 60 + m2
    night = cur >= start or cur < end if start > end else start <= cur < end
    return "night" if night else "day"


# ---------------------------------------------------------------- git and gh


def fetch(ctx):
    rc, out, err = git("fetch origin --prune", cwd=ctx.root, timeout=180)
    if rc != 0:
        return False, (err or out)
    return True, ""


def auto_refs(ctx):
    rc, out, _ = git("for-each-ref --format='%(refname:short)' refs/remotes/origin/auto/",
                     cwd=ctx.root)
    return [x.strip() for x in out.splitlines() if x.strip()] if rc == 0 else []


def worktrees(ctx):
    rc, out, _ = git("worktree list --porcelain", cwd=ctx.root)
    paths, cur = [], None
    for line in (out or "").splitlines():
        if line.startswith("worktree "):
            cur = line[len("worktree "):].strip()
            paths.append(cur)
    return paths if rc == 0 else []


def has_origin(ctx):
    rc, out, _ = git("config --get remote.origin.url", cwd=ctx.root)
    return rc == 0 and bool((out or "").strip())


def repo_flag(ctx):
    """`--repo owner/name` when the recorded slug really is one; otherwise nothing,
    so gh infers it from the origin of the checkout it is run in. Passing a bare
    directory name as --repo makes gh fail for the wrong reason."""
    slug = (ctx.state.get("repo") or "").strip()
    if re.match(r"^[^/\s]+/[^/\s]+$", slug):
        return "--repo %s " % shlex.quote(slug)
    return ""


def pr_list(ctx):
    """Real PRs for auto/* branches: [{number,head,state,title,url}].
    Cached for 20s per Ctx: one tick asks from sync_prs, the adopt check and the
    red-path annotation, and `gh pr list` is the paid call the protocol tells the
    supervisor to stop repeating (低成本运行原则)."""
    hit = getattr(ctx, "_prs_cache", None)
    if hit and now() - hit[0] < 20:
        return hit[1], hit[2]
    cmd = ("pr list %s--state all --limit 100 --json number,headRefName,"
           "state,title,url,baseRefName" % repo_flag(ctx))
    rc, out, err = run(gh_bin() + " " + cmd, cwd=ctx.root, timeout=90, shell=True)
    if rc != 0:
        return None, (err or out)
    try:
        data = json.loads(out or "[]")
    except json.JSONDecodeError:
        return None, "unparsable gh output"
    rows = []
    for row in data:
        if str(row.get("headRefName", "")).startswith("auto/"):
            rows.append(row)
    ctx._prs_cache = (now(), rows, "")
    return rows, ""


def gh_authed(ctx):
    rc, _, _ = run(gh_bin() + " auth status", cwd=ctx.root, timeout=30, shell=True)
    return rc == 0


def adopt_worker_pr(ctx, rec, slot, pol, lines, verified):
    """`worker` delivery mode: the PR is the worker's own output, so the supervisor
    finds it on GitHub by head branch rather than trusting its chat log.
    Returns True when a real PR for this branch exists (protocol §四.7 — 检查 PR 是否
    真实存在、是否关联正确仓库和分支)."""
    if delivery_owner(ctx) != "worker":
        return False
    prs, err = pr_list(ctx)
    if prs is None:
        lines.append("- slot %s: cannot check the worker's PR (%s) — leaving the slot "
                     "open rather than claiming it delivered" % (slot, (err or "")[:120]))
        return False
    mine = [p for p in prs if p.get("headRefName") == rec.get("branch")]
    if not mine:
        lines.append("- slot %s: gate %s but NO PR exists on `%s` — the worker did not "
                     "deliver. Fallback: `pr %s` pushes + opens it from here."
                     % (slot, "green" if verified else "red", rec.get("branch"), slot))
        rec["next"] = "worker opened no PR — supervisor fallback `pr %s`, or re-dispatch" % slot
        return False
    pr = mine[0]
    wrong_base = pr.get("baseRefName") and pr.get("baseRefName") != ctx.base
    rec["pr"] = pr.get("number")
    rec["pr_url"] = pr.get("url")
    rec["pr_state"] = pr.get("state")
    rec["pr_opened_by"] = "worker"
    rec["state"] = "pr_open"
    rec["verify"] = "green" if verified else "red"
    if not verified:
        rec["pr_unverified"] = rec.get("red_reasons") or ["gate did not pass"]
    if wrong_base:
        rec["pr_wrong_base"] = pr.get("baseRefName")
    c = ctx.state["counters"]
    c["pr_today"] = c.get("pr_today", 0) + 1
    c["prs_valid_today"] = c.get("prs_valid_today", 0) + (1 if verified and not wrong_base else 0)
    ctx.state["slots"][slot] = None
    queue, cands = load_tasks(ctx)
    for pool in (queue, cands):
        for row in pool:
            if row.get("id") == rec.get("task"):
                row["status"] = "pr_open"
                row["pr"] = rec["pr"]
                row["branch"] = rec["branch"]
                row["pr_state"] = pr.get("state")
    ctx.set_coll("queue.json", queue)
    ctx.set_coll("candidates.json", cands)
    note = ""
    if wrong_base:
        note += "｜⚠ base 是 `%s` 不是 `%s`" % (pr.get("baseRefName"), ctx.base)
    if not verified:
        note += "｜⚠ 未过监工闸门，勿合并"
    claim = ""
    marker = rec.get("done_signal") or ""
    m = re.search(r"pr=(\S+)", marker)
    if m and m.group(1) not in ("none", "0", str(rec["pr"])):
        claim = "｜⚠ worker 自报 pr=%s，实际 #%s" % (m.group(1), rec["pr"])
    lines.append("- slot %s: worker PR #%s on `%s` (%s%s%s) — slot freed, 不合并"
                 % (slot, rec["pr"], rec["branch"], pr.get("state"), note, claim))
    ctx.ledger({"turn": ctx.state["turn"], "branch": rec["branch"], "task": rec.get("task"),
                "result": "pr_open", "pr": rec["pr"], "by": "worker",
                "verified": bool(verified), "slot": slot})
    return True


def mark_unverified_pr(ctx, rec, slot, lines, red):
    """Worker mode, red gate: the agent may already have pushed and opened its PR.
    Protocol §六 forbids closing or overwriting it, so the supervisor leaves it
    alone and annotates it instead — the human must be able to tell "PR exists but
    the gate is red" from "PR exists and is safe to review"."""
    prs, _ = pr_list(ctx)
    mine = [p for p in (prs or []) if p.get("headRefName") == rec.get("branch")]
    if not mine:
        return False
    pr = mine[0]
    rec["pr"] = pr.get("number")
    rec["pr_url"] = pr.get("url")
    rec["pr_opened_by"] = "worker"
    rec["pr_unverified"] = red[:3]
    lines.append("- slot %s: worker 已开 PR #%s 但未过闸门 —— 不合并、不关闭、不覆盖，"
                 "修复轮会更新同一条 PR（§六）" % (slot, pr.get("number")))
    return True


def changed_files(ctx, wt, base):
    rc, out, err = git("diff --name-status %s...HEAD" % base, cwd=wt)
    if rc != 0:
        rc, out, err = git("diff --name-status HEAD~1...HEAD", cwd=wt)
    rows = []
    for line in (out or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0][0]
        for path in parts[1:]:
            rows.append((status, path))
    return rows, (err or "")


def diff_stat(ctx, wt, base):
    rc, out, _ = git("diff --stat %s...HEAD" % base, cwd=wt)
    lines = 0
    hit = re.search(r"(\d+) insertions?.*?(\d+) deletions?", out or "")
    if hit:
        lines = int(hit.group(1)) + int(hit.group(2))
    elif out:
        tail = out.strip().splitlines()
        if tail and not hit:
            add = re.search(r"(\d+)\s+", tail[-1])
            lines = int(add.group(1)) if add else 0
    return out or "", lines


def added_lines(ctx, wt, base):
    rc, out, _ = git("diff -U0 %s...HEAD" % base, cwd=wt)
    text = []
    for line in (out or "").splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            text.append(line[1:])
    return "\n".join(text)


# ------------------------------------------------------------------ dispatch


def runner_cmd(ctx):
    run_cfg = ctx.cfg.get("runner") or {}
    cmd = os.environ.get("AUTOLOOP_WORKER_CMD") or run_cfg.get("cmd") or ""
    return cmd.strip()


def runner_cli(ctx):
    """Which headless CLI the configured command actually calls — used to decide
    whether `--json` events can be parsed and whether a repair round can resume the
    same session (protocol §六: the *original* agent fixes its own PR)."""
    cmd = runner_cmd(ctx)
    for name in WORKER_CLIS:
        if re.search(r"(^|[\s;&|/])%s(\s|$)" % re.escape(name), cmd):
            return name
    return ""


def delivery_owner(ctx):
    """Who pushes the branch and opens the PR.

    `worker` is the default and what the supervision protocol asks for: the开发
    Agent 自己建分支、提交、开 PR、写交付报告，监工只启动/监控/恢复/调度。
    `supervisor` is the v1.0.0 behaviour — the worker may not touch the remote and
    the engine pushes + opens the PR after the gate goes green. Keep it when the
    box has no `gh` auth for the worker, or when you want every PR to pass the gate
    before it becomes visible.
    """
    raw = os.environ.get("AUTOLOOP_DELIVERY_OWNER") or \
        (ctx.cfg.get("delivery") or {}).get("owner") or "worker"
    return raw.strip().lower()


def task_prompt(ctx, task, wt, slot, mode, extra="", session_id=""):
    pol = read_policy(ctx)
    owner = delivery_owner(ctx)
    cli = runner_cli(ctx)
    forbidden = ", ".join(FORBIDDEN_GLOBS[:12]) + ", …"
    repo_url = ctx.remote_name() or "(no origin — work stays local, branch only)"
    delivery = [
        "## Delivery (`%s` owns it)" % owner,
    ]
    if owner == "worker":
        delivery += [
            "- Commit on `%s`, then `git push -u origin %s` — this branch only."
            % (task["branch"], task["branch"]),
            "- Open the PR against `%s` with `gh pr create`, titled and body'ed from "
            "what you actually did: 改动意图 / 改了什么 / 验证情况（贴真实命令与退出码）/ "
            "已知问题。Never fabricate a check you did not run." % ctx.base,
            "- **Never** `gh pr merge`, never close or edit a PR you do not own, never "
            "push to `%s` or to another slot's branch. Merging is the human's call."
            % ctx.base,
            "- If the push or `gh pr create` fails, say so in your finish report and "
            "print the raw error — a missing PR reported as opened is the worst "
            "failure this loop can have.",
        ]
    else:
        delivery += [
            "- Do **not** push and do **not** open a PR. Commit on `%s` and stop; the "
            "supervisor runs the gate and delivers." % task["branch"],
        ]
    card = [
        "# AutoLoop task %s (slot %s)" % (task["id"], slot),
        "",
        "You are the implementation worker for one task, in this directory only.",
        "You decide *how* to implement; nobody else will decide it for you, and nobody "
        "will review your code before the human does — so verify your own work.",
        "",
        "- repo: %s (base branch `%s`)" % (repo_url, ctx.base),
        "- worktree: %s" % wt,
        "- branch: %s (already checked out; stay on it)" % task["branch"],
        "- mode: %s%s" % (mode, " — only low-risk auto_apply work is allowed"
                          if mode == "night" else ""),
        "- worker CLI: %s%s" % (cli or "unknown",
                                " (resumed session %s)" % session_id if session_id else ""),
        "",
        "## Goal",
        task.get("title", "").strip() or "(no title)",
        "",
        "## Files you may change (`paths`)",
        "\n".join("- `%s`" % p for p in task.get("paths") or []) or "- (none listed)",
        "",
        "## Acceptance (must actually run and pass)",
        "\n".join("- `%s`" % c for c in as_list(task.get("acceptance")))
        or "- repo-wide `.autoloop/verify.sh`",
        "",
        "## Rules (hard)",
        "- Read the repo's own AGENTS.md and follow it.",
        "- Change nothing outside `paths` above.",
        "- Work only inside the worktree above. Another agent owns a different "
        "worktree and branch; a third owns the human's checkout. Do not touch "
        "anything outside yours, and never reset/clean/`git worktree remove` "
        "someone else's.",
        "- Never touch: lockfiles, dependency manifests, migrations, "
        ".github/, Dockerfile*, docker-compose*, .env*, keys. Forbidden globs: %s" % forbidden,
        "- Never modify, weaken or delete an existing test assertion or snapshot. "
        "Adding tests is encouraged.",
        "- Never merge, deploy, run migrations, call paid APIs, or send anything to a "
        "real user. Never `git push --force`, never touch `%s`." % ctx.base,
        "- Never write tokens or secrets into code, commits, or logs.",
        "- Commit your work before finishing (`git add -A && git commit`) — uncommitted "
        "changes are rejected by the gate.",
        "- If a command is denied by the sandbox, do not ask for elevated permission — "
        "no human is watching this session and waiting on that prompt is the one way "
        "to stall the pipeline. Work around it inside the worktree and report what you "
        "could not do.",
    ]
    card += delivery + [
        "",
        "## Finish report (last lines of your output)",
        "Print: files changed, commands run with exit codes, what you did NOT manage, "
        "and any risk you noticed.",
        "Then print this line exactly, alone, as the very last line once your commits "
        "(%s) are in place — the supervisor closes your session on it and moves "
        "straight to the gate:" % ("the push and PR" if owner == "worker"
                                   else "the commit"),
        "",
        "```",
        DONE_MARKER + " pr=<number or none> files=<n> tests=<pass|fail|skip>",
        "```",
    ]
    if extra:
        card += ["", "## Previous verification failure (fix this)", "```",
                 extra[:6000], "```"]
    card += ["", "_policy kinds auto_apply: %s_" % (", ".join(pol["auto_apply"]) or "-")]
    return "\n".join(card) + "\n"


def launch(ctx, task, wt, prompt, slot, extra_env=None):
    """Detached worker run: survives the bot's turn ending, survives cron boxes."""
    cmd = runner_cmd(ctx)
    if not cmd:
        return None, "no worker configured (set runner.cmd in .autoloop/config.json " \
                     "or AUTOLOOP_WORKER_CMD)"
    os.makedirs(ctx.job_dir, exist_ok=True)
    jid = "%s-%s" % (task["id"][-24:], slot.lower())
    pf = os.path.join(ctx.job_dir, jid + ".prompt.md")
    lf = os.path.join(ctx.job_dir, jid + ".log")
    ef = os.path.join(ctx.job_dir, jid + ".exit")
    df = os.path.join(ctx.job_dir, jid + ".done")
    for stale in (ef, df):
        try:
            os.remove(stale)
        except OSError:
            pass
    with open(pf, "w", encoding="utf-8") as fh:
        fh.write(prompt)
    rendered = cmd
    rendered = rendered.replace("{prompt_file}", "$AUTOLOOP_PROMPT_FILE")
    rendered = rendered.replace("{worktree}", "$AUTOLOOP_WORKTREE")
    rendered = rendered.replace("{prompt}", "$AUTOLOOP_PROMPT_FILE")
    rendered = rendered.replace("{log}", "$AUTOLOOP_LOG")
    sid = (extra_env or {}).get("AUTOLOOP_SESSION_ID", "")
    if sid and "{session_id}" not in cmd:
        # Repair rounds resume the same agent session when the CLI can. Inserted
        # before a `< file` redirect so the redirect keeps pointing at the prompt.
        flag = (WORKER_CLIS.get(runner_cli(ctx), {}).get("resume_flag") or "") \
            .replace("{session_id}", shlex.quote(sid))
        if flag:
            head, sep, tail = rendered.partition(" < ")
            rendered = (head + " " + flag + sep + tail) if sep else (rendered + " " + flag)
    rendered = rendered.replace("{session_id}", "$AUTOLOOP_SESSION_ID")
    envs = {"AUTOLOOP_PROMPT_FILE": pf, "AUTOLOOP_WORKTREE": wt,
            "AUTOLOOP_SLOT": slot, "AUTOLOOP_TASK": task["id"],
            "AUTOLOOP_LOG": lf, "AUTOLOOP_BRANCH": task["branch"],
            "AUTOLOOP_BASE_BRANCH": ctx.base,
            "AUTOLOOP_REPO_URL": ctx.remote_name() or "",
            "AUTOLOOP_DELIVERY_OWNER": delivery_owner(ctx),
            "AUTOLOOP_SESSION_ID": "",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": os.path.join(wt, ".tmp")}
    envs.update(extra_env or {})
    exports = "".join("export %s=%s\n" % (k, shlex.quote(v)) for k, v in envs.items())
    script = (
        "cd %s || exit 98\n%ssh -c %s > %s 2>&1\nrc=$?\necho $rc > %s\ntouch %s\n"
        % (shlex.quote(wt), exports, shlex.quote(rendered), shlex.quote(lf),
           shlex.quote(ef), shlex.quote(df))
    )
    wrapper = os.path.join(ctx.job_dir, jid + ".sh")
    with open(wrapper, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n" + script)
    os.chmod(wrapper, 0o755)
    devnull = open(os.devnull, "w")
    try:
        proc = subprocess.Popen(["/bin/sh", wrapper], cwd=wt, stdin=devnull,
                                stdout=devnull, stderr=devnull,
                                start_new_session=True, env=os.environ.copy())
    except OSError as exc:
        return None, "launch failed: %s" % exc
    meta = {"jid": jid, "pid": proc.pid, "started": now(), "worktree": wt,
            "log": lf, "exit_file": ef, "done_file": df, "prompt": pf,
            "task": task["id"], "branch": task["branch"], "slot": slot,
            "owner": delivery_owner(ctx), "cli": runner_cli(ctx),
            "session_id": (extra_env or {}).get("AUTOLOOP_SESSION_ID", "")}
    write_json(os.path.join(ctx.job_dir, jid + ".json"), meta)
    return meta, ""


def job_status(ctx, meta):
    if not meta:
        return "none", None
    if os.path.exists(meta["exit_file"]):
        try:
            with open(meta["exit_file"], encoding="utf-8") as fh:
                code = int((fh.read() or "1").strip() or "1")
        except (OSError, ValueError):
            code = 1
        return "finished", code
    if pid_alive(meta.get("pid")):
        if now() - meta.get("started", 0) > ctx.cap("job_timeout_s"):
            return "timeout", None
        return "running", None
    if now() - meta.get("started", 0) > ctx.cap("job_timeout_s"):
        return "dead", None
    return "running", None


def stop_job(meta):
    """Stop a worker we gave up on. launch() starts each job in its own session,
    so the process group is ours alone and killing it cannot touch the bot, the
    supervisor or another slot. A hung worker that is left alive keeps spending the
    nightly budget and can still write into the worktree, so it must actually die."""
    pid = int(meta.get("pid") or 0)
    if not pid or not pid_alive(pid):
        return False
    for sig, grace in ((signal.SIGTERM, 4.0), (signal.SIGKILL, 2.0)):
        try:
            os.killpg(pid, sig)
        except (OSError, ValueError):
            return False
        deadline = now() + grace
        while now() < deadline:
            if not pid_alive(pid):
                return True
            time.sleep(0.2)
    return not pid_alive(pid)


def tail_log(ctx, meta, limit=6000):
    try:
        with open(meta["log"], "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read()
    except OSError:
        return ""
    return data[-limit:]


# A log that has stopped growing for this many seconds while its process is still
# alive, right after the worker declared itself finished, means the session is not
# going to exit on its own (it is waiting on an approval prompt, a sub-agent, or
# its own summary). Close it now — this is the "约一分钟内结束会话" the loop needs.
CLOSE_QUIET_S = 15


def log_activity(meta):
    """(bytes, seconds since last write) for a running worker's log."""
    path = (meta or {}).get("log") or ""
    try:
        st = os.stat(path)
    except OSError:
        return 0, now()
    return st.st_size, max(0.0, now() - st.st_mtime)


def done_marker(meta):
    """The worker's own AUTOLOOP_DONE line, read from the tail of its log.
    Only the last 8 KiB is read — a multi-hour log is not re-scanned every tick."""
    path = (meta or {}).get("log") or ""
    try:
        size = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            if size > 8192:
                fh.seek(size - 8192)
            data = fh.read()
    except OSError:
        return ""
    idx = data.rfind(DONE_MARKER)
    return data[idx:data.find("\n", idx)].strip() if idx >= 0 else ""


def commits_ahead(ctx, wt):
    """Commits on the slot's branch that are not on the base ref."""
    if not wt or not os.path.isdir(wt):
        return 0
    rc, out, _ = git("rev-list --count %s..HEAD" % shlex.quote(ctx.origin_base), cwd=wt)
    if rc != 0:
        return 0
    try:
        return int((out or "0").strip())
    except ValueError:
        return 0


def capture_session(ctx, meta):
    """`dsh --profile headless --json` opens its stream with a `session` event.
    Persisting that id is what lets a repair round resume the *same* agent instead
    of a stranger with no memory of the diff (protocol §六: 由原开发 Agent 继续修复)."""
    if not meta or meta.get("session_id"):
        return (meta or {}).get("session_id", "")
    path = meta.get("log") or ""
    sid = ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(4096)
        for line in head.splitlines():
            if '"session"' not in line:
                continue
            m = re.search(r'"(?:session_?[iI]d|id)"\s*:\s*"([^"]+)"', line)
            if m:
                sid = m.group(1)
                break
    except OSError:
        return ""
    if sid:
        meta["session_id"] = sid
        write_json(os.path.join(ctx.job_dir, meta["jid"] + ".json"), meta)
    return sid


def judge_running_job(ctx, rec, meta):
    """Is this still a worker, or is it 空转?

    Returns (verdict, detail). verdicts: `waiting` (leave it alone), `done`
    (it finished — close the session and run the gate now), `stalled` (no output
    and no commits for stall_s — stop it and repair). v1.0.0 had only the
    job_timeout_s ceiling, so both of those burned up to 30 minutes of a slot.
    """
    size, quiet = log_activity(meta)
    marker = done_marker(meta)
    ahead = commits_ahead(ctx, rec.get("worktree"))
    rec["log_bytes"] = size
    rec["log_quiet_s"] = int(quiet)
    rec["ahead"] = ahead
    grace = ctx.cap("finish_grace_s")
    quiet_needed = min(CLOSE_QUIET_S, grace)
    if marker:
        seen = rec.get("marker_seen_at") or now()
        rec["marker_seen_at"] = seen
        rec["done_signal"] = marker[:200]
        if quiet >= quiet_needed or now() - seen >= grace:
            return "done", "declared done %ss ago, log silent %ss — %s" % (
                int(now() - seen), int(quiet), marker[:120])
        return "waiting", "declared done, still writing (quiet %ss < %ss)" % (
            int(quiet), int(quiet_needed))
    if ahead and quiet >= grace:
        return "done", "committed %s commit(s) and silent %ss without a finish line " \
                       "— closing anyway" % (ahead, int(quiet))
    if quiet >= ctx.cap("stall_s"):
        return "stalled", "no log output for %ss and no commits — the run is not coming " \
                          "back (approval prompt, or a worker stuck retrying)" % int(quiet)
    return "waiting", "running %ss, %s bytes, silent %ss" % (
        int(now() - meta.get("started", now())), size, int(quiet))


# -------------------------------------------------------------------- verify


def run_verify(ctx, task, wt, slot):
    """External acceptance. Gate script is read from the CONTROL checkout, never
    from the worktree, so a worker cannot rewrite the rules it is judged by."""
    script = os.path.join(ctx.alo, "verify.sh")
    if not os.path.exists(script):
        return {"rc": None, "steps": [], "evidence": None,
                "error": "verify.sh missing — cannot judge anything"}
    env = os.environ.copy()
    env.update({"AUTOLOOP_BRANCH": task["branch"], "AUTOLOOP_WORKTREE": wt,
                "AUTOLOOP_TASK": task["id"], "AUTOLOOP_SLOT": slot,
                # keep the gate from littering the worktree with .pyc files, which
                # would otherwise look like uncommitted work a moment later
                "PYTHONDONTWRITEBYTECODE": "1",
                "AUTOLOOP_ACCEPTANCE": json.dumps(as_list(task.get("acceptance")))})
    rc, out, err = run("bash %s %s" % (shlex.quote(script),
                                       shlex.quote(task["branch"])),
                       cwd=wt, env=env, timeout=max(120, int(ctx.cap("job_timeout_s") or 0)),
                       shell=True)
    os.makedirs(ctx.log_dir, exist_ok=True)
    evid = os.path.join(ctx.log_dir, "verify-%s-%s.txt" % (task["id"][-30:], stamp().replace(" ", "_")))
    with open(evid, "w", encoding="utf-8") as fh:
        fh.write("$ bash .autoloop/verify.sh %s\n--- stdout ---\n%s\n--- stderr ---\n%s\nexit=%s\n"
                 % (task["branch"], out, err, rc))
    steps = []
    for line in (out or "").splitlines():
        hit = re.match(r"\[(PASS|FAIL|SKIP)\]\s*(.*)", line.strip())
        if hit:
            steps.append({"state": hit.group(1), "cmd": hit.group(2)})
    return {"rc": rc, "steps": steps, "evidence": evid,
            "error": None if rc == 0 else "gate script exit %s (see evidence)" % rc}


def redline_check(ctx, task, wt, pol):
    """Independent re-check of the red lines. Runs after the gate script, on the
    real diff — the worker's own claims are never evidence."""
    findings = []
    base = ctx.origin_base
    rows, err = changed_files(ctx, wt, base)
    if err and not rows:
        findings.append("cannot read diff vs %s: %s" % (base, err[:200]))
    if not rows:
        findings.append("no committed diff against %s — the worker produced nothing "
                        "to review" % base)
    rc, dirty, _ = git("status --porcelain", cwd=wt)
    # a green run must not be killed by the artefacts the gate itself wrote
    pending = [l for l in (dirty or "").splitlines()
               if l.strip() and not is_run_noise(status_path(l))]
    if pending:
        findings.append("worktree has uncommitted changes — nothing was committed:\n%s"
                        % "\n".join(pending)[:500])
    for status, path in rows:
        forb = is_forbidden(path, pol)
        if forb:
            findings.append("forbidden path touched: %s (matched %s)" % (path, forb))
        if is_test_path(path) and status in ("M", "D", "R"):
            findings.append("existing test changed (%s): %s" % (status, path))
        allowed = task.get("paths") or []
        # additions under a test path are exempt from the `paths` whitelist: the
        # contract says never weaken an existing test, but always allow adding one.
        if allowed and status == "A" and is_test_path(path):
            continue
        if allowed and not any(path_matches(p, path) for p in allowed):
            findings.append("outside task paths: %s" % path)
    hits = SECRET_LINE_RE.findall(added_lines(ctx, wt, base))
    if hits:
        findings.append("possible credential in added lines: %s"
                        % ", ".join(sorted({str(h)[:40] for h in hits})[:3]))
    _, churn = diff_stat(ctx, wt, base)
    if churn > ctx.cap("max_business_lines"):
        findings.append("diff too large for one task: %s lines > %s cap"
                        % (churn, ctx.cap("max_business_lines")))
    return findings, rows, churn


def as_list(val):
    if val is None:
        return []
    if isinstance(val, str):
        return [val]
    return list(val)


# ------------------------------------------------------------------ task pool


def load_tasks(ctx):
    return ctx.coll("queue.json"), ctx.coll("candidates.json")


def save_tasks(ctx, queue, cands):
    ctx.set_coll("queue.json", queue)
    ctx.set_coll("candidates.json", cands)


def busy_paths(ctx):
    used = []
    for slot in SLOTS:
        rec = ctx.state["slots"].get(slot)
        if rec and rec.get("state") in ACTIVE_STATES + ("pr_ready",):
            used.append({"slot": slot, "task": rec.get("task"),
                         "paths": rec.get("paths") or []})
    for ref in ctx.state.get("pending_pr", []):
        used.append({"slot": "-", "task": ref.get("task"), "paths": ref.get("paths") or []})
    return used


def norm_title(text):
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (text or "").lower())


def dedupe_key(task):
    return norm_title(task.get("title", "")) + "|" + ",".join(sorted(task.get("paths") or []))


def known_keys(ctx):
    keys = set()
    for name in ("done.json", "blocked.json", "candidates.json", "queue.json"):
        for row in ctx.coll(name):
            keys.add(dedupe_key(row))
    for slot in SLOTS:
        rec = ctx.state["slots"].get(slot)
        if rec and rec.get("title"):
            keys.add(norm_title(rec["title"]))
    return {k for k in keys if k}


def eligible(ctx, task, pol, mode):
    """Why this task may/may not start right now. Returns (ok, reason)."""
    if task.get("status") in ("done", "blocked", "pr_open", "rejected"):
        return False, "already %s" % task.get("status")
    for ref in ctx.state.get("pending_pr", []):
        if ref.get("task") == task.get("id"):
            return False, "PR pending"
    for rec in busy_paths(ctx):
        if paths_overlap(rec["paths"], task.get("paths")):
            return False, "paths overlap with slot %s" % rec["slot"]
    need = kind_policy(task.get("kind", "misc"), pol)
    if task.get("policy") in ("auto_apply", "needs_approval"):
        need = task["policy"]
    if need == "needs_approval" and not task.get("approved"):
        return False, "needs_approval"
    if mode == "night" and kind_policy(task.get("kind", "misc"), pol) != "auto_apply":
        return False, ("night mode: kind %s is not in the auto_apply whitelist "
                       "(a human approval does not make a change low-risk at night)"
                       % task.get("kind"))
    if ctx.state.get("maintenance"):
        return False, "maintenance mode (fail gate) — business work paused"
    return True, ""


def pick_task(ctx, pol, mode):
    queue, cands = load_tasks(ctx)
    last = "pool is empty"
    for pool, label in ((queue, "queue"), (cands, "candidate")):
        ranked = sorted([t for t in pool if t.get("status") not in ("done", "blocked",
                                                                    "rejected", "pr_open")],
                        key=lambda t: -int(t.get("priority") or 0))
        for task in ranked:
            ok, why = eligible(ctx, task, pol, mode)
            if ok:
                return task, label, ""
            last = why or last
    return None, "", last


def new_task_id(ctx, title):
    # branch-safe id: ascii slug + short hash, so Chinese titles stay readable
    # and still unique (slug() strips non-ascii, which would collapse them all
    # to "task").
    digest = hashlib.sha1((title or "").encode("utf-8")).hexdigest()[:6]
    base = "%s-%s-%s" % (time.strftime("%Y%m%d-%H%M"), slug(title), digest)
    cand, n = base, 2
    taken = set()
    for name in ("queue.json", "candidates.json", "done.json", "blocked.json"):
        for row in ctx.coll(name):
            taken.add(row.get("id"))
    for slot in SLOTS:
        rec = ctx.state["slots"].get(slot)
        if rec:
            taken.add(rec.get("task"))
    rc, refs, _ = git("for-each-ref --format='%(refname:short)' refs/heads/auto/ refs/remotes/origin/auto/",
                      cwd=ctx.root)
    while cand in taken or ("auto/" + cand) in (refs or ""):
        cand = "%s-%d" % (base, n)
        n += 1
    return cand


def active_branches(ctx):
    out = []
    for slot in SLOTS:
        rec = ctx.state["slots"].get(slot)
        if rec and rec.get("branch") and rec.get("state") in ACTIVE_STATES + ("pr_ready",):
            out.append(rec)
    return out


# --------------------------------------------------------------------- report


def slot_row(ctx, slot):
    rec = ctx.state["slots"].get(slot)
    if not rec:
        return {"slot": slot, "branch": "-", "task": "-", "state": "idle",
                "scope": "-", "last": "-", "verify": "n/a", "job": "-", "pr": "-",
                "next": "assign a task"}
    jstat, jcode = job_status(ctx, rec.get("job") and read_json(
        os.path.join(ctx.job_dir, rec["job"] + ".json")))
    return {"slot": slot, "branch": rec.get("branch", "-"),
            "task": rec.get("task", "-"), "state": rec.get("state", "?"),
            "scope": ", ".join((rec.get("paths") or [])[:3]) or "-",
            "last": rec.get("last_action", "-"),
            "verify": rec.get("verify", "pending"),
            "job": "%s%s" % (jstat, "" if jcode is None else "/exit%s" % jcode),
            "pr": rec.get("pr") or "-",
            "next": rec.get("next", "-")}


def report_text(ctx):
    st = ctx.state
    c = st["counters"]
    mode = mode_now(ctx)
    lines = ["## AutoLoop report %s — %s (%s档)" % (stamp(), st.get("repo", "?"), mode)]
    if st.get("idle_reason"):
        lines.append("idle_reason: %s" % st["idle_reason"])
    if st.get("maintenance"):
        lines.append("MAINTENANCE: consecutive failures hit the gate; only safe "
                     "housekeeping is allowed.")
    lines.append("")
    lines.append("| slot | branch | task | state | scope | last action | verify | job | PR | next |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for slot in SLOTS:
        r = slot_row(ctx, slot)
        lines.append("| %(slot)s | `%(branch)s` | %(task)s | %(state)s | %(scope)s | "
                     "%(last)s | %(verify)s | %(job)s | %(pr)s | %(next)s |" % r)
    queue, cands = load_tasks(ctx)
    pol = read_policy(ctx)
    pend = [t for t in queue + cands
            if (t.get("policy") or kind_policy(t.get("kind", "misc"), pol)) == "needs_approval"
            and not t.get("approved") and t.get("status") not in ("done", "blocked")]
    blocked = ctx.coll("blocked.json")
    lines.append("")
    lines.append("- PR today: %s/%s｜jobs today: %s/%s｜turns: %s/%s｜consecutive red: %s/%s"
                 % (c.get("pr_today", 0), ctx.cap("pr_max_per_day"),
                    c.get("jobs_today", 0), ctx.cap("nightly_max_jobs"),
                    c.get("turns_today", 0), ctx.cap("max_turns_per_day"),
                    c.get("fails_consecutive", 0), ctx.cap("fail_gate")))
    lines.append("- queue %d｜candidates %d｜needs_approval %d｜blocked %d｜done %d"
                 % (len([t for t in queue if t.get("status") not in ("done", "blocked")]),
                    len([t for t in cands if t.get("status") not in ("done", "blocked")]),
                    len(pend), len(blocked), len(ctx.coll("done.json"))))
    for ref in st.get("pending_pr", []):
        lines.append("- pending_pr (cap reached): `%s` — verify green, waiting for a PR slot"
                     % ref.get("branch"))
    for slot in SLOTS:
        rec = st["slots"].get(slot)
        if rec and rec.get("state") == "pr_ready":
            lines.append("- **ready to open**: slot %s `%s` — run `prbody %s > pr.md`, "
                         "fill the three judgement fields, then `pr %s`"
                         % (slot, rec["branch"], slot, slot))
    for item in blocked[-3:]:
        lines.append("- blocked: `%s` — %s" % (item.get("task") or item.get("branch"),
                                               str(item.get("reason"))[:160]))
    if pend:
        lines.append("- needs your decision (approve/reject): "
                     + ", ".join("`%s`" % t["id"] for t in pend[:8]))
    lines.append("")
    footer = ("autoloop %s %s｜分支 %s %s｜出PR %s/%s｜失败 %s｜候选 +%s｜当日轮数 %s/%s｜下一步 %s"
              % (stamp(), mode,
                 (st["slots"].get("A") or {}).get("branch") or "-",
                 (st["slots"].get("B") or {}).get("branch") or "-",
                 c.get("pr_today", 0), ctx.cap("pr_max_per_day"),
                 c.get("fails_consecutive", 0), c.get("cands_added", 0),
                 c.get("turns_today", 0), ctx.cap("max_turns_per_day"),
                 st.get("next") or "tick"))
    lines.append(footer)
    return "\n".join(lines)


# ----------------------------------------------------------------- subcommands


def cmd_doctor(args):
    ctx_root = args.repo or os.getcwd()
    root = Ctx._find_root(ctx_root)
    log("AutoLoop doctor %s" % VERSION)
    log("- python %s" % sys.version.split()[0])
    log("- repo: %s" % (root or "NOT A GIT REPO (%s)" % ctx_root))
    checks = []
    for tool in ("git", "bash", "tar", "rg"):
        checks.append((tool, which(tool)))
    for tool in ("gh", "node", "npm", "pnpm", "python3", "pip3", "pytest",
                 "make", "cargo", "go"):
        checks.append((tool, which(tool)))
    found = [n for n, p in checks if p]
    log("- tools: %s" % (", ".join(found) or "none"))
    log("- blocked-ish tools present (sudo/rm are always there): sudo=%s docker=%s"
        % (bool(which("sudo")), bool(which("docker"))))
    workers = {}
    for cand in ("dsh", "codex", "claude", "qoder", "grok", "aider", "opencode"):
        if which(cand):
            rc, out, _ = run(cand + " --version", timeout=20, shell=True)
            workers[cand] = (out or "").splitlines()[0][:60] if rc == 0 else "present"
    log("- worker CLIs on this box: %s"
        % (", ".join("%s(%s)" % kv for kv in workers.items()) or
           "NONE — set AUTOLOOP_WORKER_CMD / runner.cmd"))
    for name in workers:
        if name not in WORKER_CLIS:
            continue
        spec = WORKER_CLIS[name]
        p = probe_worker_cli(name)
        log("  - %s: `%s`%s" % (name, spec["cmd"],
                                "" if p["ok"] else "  (%s)" % p["verdict"]))
        if p["ok"]:
            log("    %s" % spec["note"])
        elif p["unproven"]:
            # Say what is unknown instead of guessing at what is missing.
            log("    %s｜flag 支持情况未证实：这条 runner 派工会照 config 走，"
                "先由你亲手跑一次 `%s` 确认可用 %s" % (
                    p["verdict"], spec["probe"],
                    ", ".join(spec["must_support"]) or "headless 写法"))
        else:
            log("    %s  ⚠ 本机版本缺 %s —— 别用 --json，"
                "空转检测只能靠日志静默" % (spec["note"], ", ".join(p["missing"])))
    if root:
        dctx = Ctx(root)
        log("- delivery owner: %s（%s）"
            % (delivery_owner(dctx),
               "worker 自己 push + 开 PR，监工核验 PR 真实性"
               if delivery_owner(dctx) == "worker"
               else "worker 只提交，监工跑闸门后 push + 开 PR"))
        log("- 空转 guard: declared-done→关闭会话 %ss｜静默无提交→停机 %ss｜"
            "硬超时 %ss｜watch 默认节拍 %ss"
            % (dctx.cap("finish_grace_s"), dctx.cap("stall_s"),
               dctx.cap("job_timeout_s"), dctx.cap("watch_interval_s")))
    if root:
        ctx = Ctx(root)
        log("- base branch: %s｜repo label: %s" % (ctx.base, ctx.state.get("repo") if ctx.state else ctx.remote_name()))
        log("- origin remote: %s" % ("configured (%s)" % ctx.remote_name() if has_origin(ctx)
                                     else "NONE — push/PR steps refuse until `git remote add origin <url>`"))
        log("- gh auth: %s" % ("ok" if gh_authed(ctx) else "NOT LOGGED IN — PR step will be skipped"))
        log("- free disk: %s MB (needs >= %s)" % (free_disk_mb(root), ctx.cap("min_free_disk_mb")))
        cron = which("crontab")
        rc, ct, _ = run("crontab -l", timeout=20, shell=True)
        log("- crontab: %s, entries %s｜tick line: %s"
            % ("present" if cron else "absent", len(ct.splitlines()) if cron and rc == 0 else "?",
               "yes" if "autoloop.py tick" in (ct or "") else "no"))
        log("- cron daemon: %s" % ("running" if pid_alive(
            _pgrep("cron")) else "not detected — box cron may not fire"))
        if not os.path.isdir(ctx.alo):
            log("- .autoloop/: missing → `autoloop.py init`")
        elif not os.path.exists(os.path.join(ctx.alo, "state.json")):
            # bootstrap writes boot.json before init runs; doctor must not assume
            # a half-populated .autoloop/ is a live loop.
            log("- .autoloop/: stamp only, not initialized → `autoloop.py init`")
            log("- verify.sh sha256: %s" % (sha256_file(os.path.join(ctx.alo, "verify.sh")) or "missing"))
        else:
            slots = (ctx.state or {}).get("slots") or {}
            log("- .autoloop/: turn %s, mode %s, slots %s"
                % ((ctx.state or {}).get("turn"), mode_now(ctx),
                   ", ".join("%s=%s" % (s, (slots.get(s) or {}).get("state") or "idle")
                             for s in SLOTS)))
            log("- verify.sh sha256: %s" % (sha256_file(os.path.join(ctx.alo, "verify.sh")) or "missing"))
            log("- bootstrap stamp: %s" % (read_json(os.path.join(ctx.alo, "boot.json"), {}) or {})
                )
    sched = ("Grok Bot scheduled task (native) — recommended: the box may hibernate "
             "when idle, so box cron is not guaranteed to fire")
    log("- wake-up driver: %s" % sched)
    log("- honest note: nothing here installs, restarts or backgrounds itself. "
        "Development advances only when `tick` runs.")
    return 0


def _pgrep(name):
    rc, out, _ = run("pgrep -f %s" % name, timeout=15, shell=True)
    for line in (out or "").splitlines():
        if line.strip().isdigit():
            return int(line.strip())
    return None


def cmd_init(args):
    ctx = Ctx(args.repo)
    ctx.ensure_state()
    os.makedirs(ctx.log_dir, exist_ok=True)
    os.makedirs(ctx.job_dir, exist_ok=True)
    created, present = [], []

    def seed(name, obj):
        path = os.path.join(ctx.alo, name)
        if os.path.exists(path):
            present.append(name)
        else:
            write_json(path, obj)
            created.append(name)

    seed("state.json", ctx.state)
    seed("queue.json", [])
    seed("candidates.json", [])
    seed("blocked.json", [])
    seed("done.json", [])
    seed("policy.json", _policy_template())
    vsh = os.path.join(ctx.alo, "verify.sh")
    if not os.path.exists(os.path.join(ctx.alo, "config.json")):
        det = detect_commands(ctx.root)
        if getattr(args, "test_cmd", None):
            det["test"] = args.test_cmd
        if getattr(args, "lint_cmd", None):
            det["lint"] = args.lint_cmd
        if getattr(args, "build_cmd", None):
            det["build"] = args.build_cmd
        cfg = ctx.cfg
        cfg["base_branch"] = ctx.base
        cfg["verify"] = det
        cfg["runner"] = {"cmd": args.runner or os.environ.get("AUTOLOOP_WORKER_CMD") or "",
                         "note": "verify the headless flag on THIS box before trusting it"}
        cfg["reference_enabled"] = gh_authed(ctx)
        write_json(os.path.join(ctx.alo, "config.json"), cfg)
        created.append("config.json")
    else:
        present.append("config.json")
        if getattr(args, "test_cmd", None) or getattr(args, "lint_cmd", None) or \
                getattr(args, "build_cmd", None):
            cfg = read_json(os.path.join(ctx.alo, "config.json"), {})
            det = cfg.setdefault("verify", {})
            for key in ("test", "lint", "build"):
                val = getattr(args, key + "_cmd", None)
                if val:
                    det[key] = val
            write_json(os.path.join(ctx.alo, "config.json"), cfg)
            if getattr(args, "force_gate", False):
                write_text(vsh, _verify_template(det, ctx.root))
                os.chmod(vsh, 0o755)
                log("- rewrote verify.sh from the detected commands (--force-gate)")
            else:
                log("- updated config.json commands; left verify.sh alone because it "
                    "may be hand-tuned. Pass --force-gate to regenerate it.")
    if not os.path.exists(vsh):
        cfg = read_json(os.path.join(ctx.alo, "config.json"), {})
        with open(vsh, "w", encoding="utf-8") as fh:
            fh.write(_verify_template(cfg.get("verify", {}), args.repo or ctx.root))
        os.chmod(vsh, 0o755)
        created.append("verify.sh")
    else:
        present.append("verify.sh")
    rc, gdir, _ = git("rev-parse --git-common-dir", cwd=ctx.root)
    common = (gdir or "").strip() or os.path.join(ctx.root, ".git")
    if not os.path.isabs(common):
        common = os.path.join(ctx.root, common)
    excl = os.path.join(common, "info", "exclude")
    # the loop's own state, plus the per-worktree scratch dir TMPDIR points at.
    # Both are the engine's litter, so the engine declares it instead of letting it
    # show up as an "uncommitted change" later.
    want = ["/" + ALO_DIR + "/", "**/.tmp/"]
    try:
        have = open(excl, encoding="utf-8").read() if os.path.exists(excl) else ""
        add = [l for l in want if l not in have]
        if add:
            with open(excl, "a", encoding="utf-8") as fh:
                if have and not have.endswith("\n"):
                    fh.write("\n")
                fh.write("# autoloop supervisor state + worktree scratch (local, never "
                         "committed)\n" + "\n".join(add) + "\n")
            created.append("info/exclude")
    except OSError as exc:
        eprint("warning: could not write %s: %s" % (excl, exc))

    ctx.state["mode_forced"] = args.mode if args.mode in ("day", "night") else None
    ctx.save()
    log("initialized %s" % ctx.alo)
    log("- created: %s" % (", ".join(created) or "-"))
    log("- kept: %s" % (", ".join(present) or "-"))
    det = (read_json(os.path.join(ctx.alo, "config.json"), {}) or {}).get("verify", {})
    log("- detected gate: build=%s test=%s lint=%s"
        % (det.get("build") or "-", det.get("test") or "-", det.get("lint") or "-"))
    if not det.get("test"):
        log("  ! no test command found. A loop without an external gate cannot "
            "prove anything. Edit .autoloop/config.json + verify.sh, or pass "
            "--test-cmd. Until then nothing will be judged green.")
    if not runner_cmd(ctx):
        log("  ! no worker CLI configured. tick will report `no-runner` instead of "
            "developing. Set runner.cmd (e.g. `dsh headless -` reading stdin) and "
            "verify the flag on this box.")
    log("next: `autoloop.py suggest` to fill candidates, then `autoloop.py tick`.")
    return 0


def _policy_template():
    return {
        "_comment": "auto_apply kinds may start themselves; everything else waits "
                    "for `approve`. Night mode runs only auto_apply.",
        "auto_apply": ["add_tests", "docs_align", "lint_fix", "typo",
                       "error_handling", "edge_case", "small_refactor",
                       "flaky_test_quarantine", "logging"],
        "needs_approval": ["new_feature", "ui", "dependency", "major_refactor",
                           "migration", "perf", "public_api", "removal",
                           "external_intel", "security"],
        "extra_forbidden_globs": [],
        "default_kind_policy": "needs_approval",
    }


def detect_commands(root):
    """Find trustworthy build/test/lint commands from the repo's own config."""
    out = {"build": "", "test": "", "lint": "", "install": ""}
    pkg = read_json(os.path.join(root, "package.json"), {}) or {}
    scripts = pkg.get("scripts", {}) or {}

    def pick(names):
        for name in names:
            if name in scripts:
                return "npm run %s" % name if name not in (
                    "test", "lint", "build") else "npm %s" % name
        return ""
    out["test"] = pick(["test", "test:unit", "vitest run", "jest"]) or (
        "npm test" if "test" in scripts else "")
    out["lint"] = pick(["lint", "lint:fix", "typecheck", "check"])
    out["build"] = pick(["build"])
    if os.path.exists(os.path.join(root, "pyproject.toml")) or \
       os.path.exists(os.path.join(root, "pytest.ini")) or \
       os.path.exists(os.path.join(root, "setup.cfg")):
        if not out["test"]:
            out["test"] = "python3 -m pytest -q"
        out["lint"] = out["lint"] or (
            "python3 -m ruff check ." if os.path.exists(os.path.join(root, "ruff.toml"))
            or "ruff" in (read_text(os.path.join(root, "pyproject.toml")) or "") else "")
    elif os.path.exists(os.path.join(root, "go.mod")):
        out["test"] = out["test"] or "go test ./..."
        out["build"] = out["build"] or "go build ./..."
        out["lint"] = out["lint"] or "gofmt -l ."
    elif os.path.exists(os.path.join(root, "Cargo.toml")):
        out["test"] = out["test"] or "cargo test"
        out["lint"] = out["lint"] or "cargo clippy -- -D warnings"
    elif os.path.exists(os.path.join(root, "pom.xml")):
        out["test"] = out["test"] or "mvn -q test"
    elif os.path.exists(os.path.join(root, "Makefile")):
        text = read_text(os.path.join(root, "Makefile")) or ""
        for key, pat in (("test", r"^test:"), ("lint", r"^lint:"), ("build", r"^build:")):
            if not out[key] and re.search(pat, text, re.M):
                out[key] = "make %s" % key
    if not out["install"]:
        if os.path.exists(os.path.join(root, "package-lock.json")):
            out["install"] = "npm ci --no-audit --no-fund"
        elif os.path.exists(os.path.join(root, "pnpm-lock.yaml")):
            out["install"] = "pnpm install --frozen-lockfile"
        elif os.path.exists(os.path.join(root, "yarn.lock")):
            out["install"] = "yarn install --frozen-lockfile"
        elif os.path.exists(os.path.join(root, "uv.lock")):
            out["install"] = "uv sync --frozen"
        elif os.path.exists(os.path.join(root, "requirements.txt")):
            out["install"] = "pip install -r requirements.txt"
    return out


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _verify_template(det, root):
    return """#!/usr/bin/env bash
# AutoLoop external acceptance gate.
#   usage: bash .autoloop/verify.sh <branch>
# This copy is the CONTROL copy. run_verify() always executes this file, never a
# copy inside a worktree, so a worker cannot edit the rules it is judged by.
# Fill the three commands from what this repo actually supports, then check:
#   bash .autoloop/verify.sh $(git rev-parse --abbrev-ref HEAD)
set -uo pipefail

WT="${AUTOLOOP_WORKTREE:-$(pwd)}"
BRANCH="${1:-$(git -C "$WT" rev-parse --abbrev-ref HEAD)}"
BUILD_CMD='%s'
TEST_CMD='%s'
LINT_CMD='%s'
INSTALL_CMD='%s'
ACCEPT="${AUTOLOOP_ACCEPTANCE:-[]}"

cd "$WT" || exit 3
FAILED=0
note() { printf '%%s\\n' "$*"; }
step() {
  local label="$1"; shift
  local cmd="$*"
  [ -z "$cmd" ] && { note "[SKIP] $label (no command configured)"; return 0; }
  note "[RUN ] $label: $cmd"
  if bash -c "$cmd" >/dev/null 2>&1; then note "[PASS] $label"; else
    note "[FAIL] $label"; FAILED=$((FAILED+1)); fi
}

note "== autoloop verify $BRANCH in $WT =="
# 0. dependencies must exist or every later step is a lie
if [ -n "$INSTALL_CMD" ] && [ ! -d node_modules ] && [ ! -d .venv ]; then
  step "install" "$INSTALL_CMD"
fi
# 1-3. the repo's own gates
step "build" "$BUILD_CMD"
step "full-test" "$TEST_CMD"
step "lint" "$LINT_CMD"
# 4. task-specific acceptance commands (JSON array handed over by the supervisor)
if command -v python3 >/dev/null 2>&1; then
  while IFS= read -r cmd; do
    [ -z "$cmd" ] && continue
    note "[RUN ] acceptance: $cmd"
    if bash -c "$cmd" >/dev/null 2>&1; then note "[PASS] acceptance";
    else note "[FAIL] acceptance"; FAILED=$((FAILED+1)); fi
  done < <(printf '%%s' "$ACCEPT" | python3 -c '
import json,sys
try:
    for cmd in json.load(sys.stdin) or []:
        print(cmd)
except Exception:
    pass
')
fi
# 5. no build artefact left behind that would be committed
if [ -f package.json ] && grep -q '"format"' package.json 2>/dev/null; then
  step "format-check" "npm run format -- --check 2>/dev/null || npx prettier --check ."
fi
# 6. safety re-read: the supervisor does the authoritative check on the diff
note "[INFO] forbidden-path and test-assertion checks run in autoloop.py (redline_check)"
if [ "$FAILED" -gt 0 ]; then
  note "== RESULT: RED ($FAILED step(s) failed) =="
  exit 1
fi
note "== RESULT: GREEN =="
exit 0
""" % (det.get("build", ""), det.get("test", ""), det.get("lint", ""),
       det.get("install", ""))


def cmd_tick(args):
    ctx = Ctx(args.repo)
    ctx.need_init()
    with Lock(ctx) as lk:
        if lk is None:
            log("another supervisor holds .autoloop/loop.lock — this tick did nothing")
            return 0
        ctx.state = read_json(ctx.state_path)
        ctx.ensure_state()
        ctx.roll_day()
        st = ctx.state
        c = st["counters"]
        c["turns_today"] = int(c.get("turns_today", 0)) + 1
        st["turn"] = int(st.get("turn", 0)) + 1
        mode = mode_now(ctx)
        pol = read_policy(ctx)
        lines = ["# tick %s turn %s mode %s" % (stamp(), st["turn"], mode)]

        # 1. hard stop files
        for flag, why in (("STOP", "STOP file present"), ("HALTED", "HALTED file present")):
            if os.path.exists(os.path.join(ctx.alo, flag)):
                st["idle_reason"] = "stopped"
                st["next"] = "remove .autoloop/%s when you want work to resume" % flag
                lines.append("- %s → no business work this tick (%s)" % (flag, why))
                _finish(ctx, lines, "stopped", args.quiet)
                return 0
        if os.environ.get("AUTOLOOP_HARD_STOP"):
            # the env-level kill switch has to actually kill, or the one command the
            # operator reaches for in an incident is the one that does nothing
            st["idle_reason"] = "stopped"
            st["next"] = "unset AUTOLOOP_HARD_STOP to let ticks work again"
            lines.append("- AUTOLOOP_HARD_STOP is set in this environment → no dispatch, "
                         "no repair, no PR this tick")
            _finish(ctx, lines, "stopped", args.quiet)
            return 0

        # 2. environment integrity (the box may auto-update when idle)
        boot = read_json(os.path.join(ctx.alo, "boot.json"), {}) or {}
        if not boot.get("ok"):
            lines.append("- env: never bootstrapped on this box → run "
                         "`bash bootstrap.sh` (idempotent) before expecting work")
        if not has_origin(ctx):
            # Delivery here is branch + PR, which needs a remote to base on and to
            # push to. Saying it once, in words, beats two slots worth of raw
            # "invalid reference: origin/main" that a fresh instance cannot act on.
            st["idle_reason"] = "no-origin"
            st["next"] = ("git remote add origin <url> in %s, then tick again" % ctx.root)
            lines.append("- remote: NO origin on %s → this loop delivers as branch + PR, so it "
                         "stops here rather than half-working" % ctx.root)
            _finish(ctx, lines, "no-origin", args.quiet)
            return 0
        if free_disk_mb(ctx.root) < ctx.cap("min_free_disk_mb"):
            st["idle_reason"] = "no-disk"
            lines.append("- disk: %s MB free < %s → refusing to create worktrees"
                         % (free_disk_mb(ctx.root), ctx.cap("min_free_disk_mb")))
            _finish(ctx, lines, "no-disk", args.quiet)
            return 0

        # 3. cost gates
        if c["jobs_today"] >= ctx.cap("nightly_max_jobs"):
            st["idle_reason"] = "cap-jobs"
            lines.append("- cap: %s jobs already today → no new dispatch "
                         "(do not lower quality to keep running)" % c["jobs_today"])
        elif c["turns_today"] > ctx.cap("max_turns_per_day"):
            st["idle_reason"] = "cap-turns"
            lines.append("- cap: turn budget %s/%s reached"
                         % (c["turns_today"], ctx.cap("max_turns_per_day")))

        # 4. sync reality: git + gh
        okf, err = fetch(ctx)
        if not okf:
            lines.append("- git fetch failed: %s (state may be stale)" % err[:160])
        sync_prs(ctx, lines)
        recover_slots(ctx, lines)

        # 5. reap running jobs → verify
        for slot in SLOTS:
            rec = st["slots"].get(slot)
            if not rec:
                continue
            _advance(ctx, rec, slot, pol, lines)

        # 6. fill empty slots
        if c["jobs_today"] < ctx.cap("nightly_max_jobs") and not st.get("maintenance"):
            for slot in SLOTS:
                if st["slots"].get(slot):
                    continue
                task, label, why = pick_task(ctx, pol, mode)
                if not task:
                    st["idle_reason"] = "no-work"
                    lines.append("- slot %s: no eligible task (%s)" % (slot, why or "pool empty"))
                    continue
                st["idle_reason"] = None
                start_task(ctx, task, label, slot, mode, lines)
        elif st.get("maintenance"):
            maintenance_tick(ctx, pol, lines)

        # 7. optional external reference (max one search per turn)
        if not args.no_ref:
            note_reference(ctx, lines)

        # 8. fail gate
        if c["fails_consecutive"] >= ctx.cap("fail_gate"):
            st["maintenance"] = True
            lines.append("- gate A: %s consecutive red verifications → maintenance mode "
                         "(docs/candidates/state repair only)" % c["fails_consecutive"])
        # 9. decide next action for the judgement layer
        st["next"] = next_action(ctx)
        _finish(ctx, lines, st.get("idle_reason"), args.quiet)
        return 0


def _finish(ctx, lines, idle, quiet=False):
    """Every tick ends here — including the ones that refuse to work. A tick that
    says nothing is indistinguishable from a tick that crashed, so the reason and
    the report go to stdout on every path."""
    ctx.state["last_tick_at"] = now()
    ctx.state["idle_reason"] = idle if idle else ctx.state.get("idle_reason")
    c = ctx.state["counters"]
    # Loop-level 空转: ticks that accomplish nothing are not free — they spend the
    # turn budget and they look alive in the log. After six of them the engine says
    # out loud that more ticking will not produce a PR.
    # A tick is only 空转 when *nothing is in flight either*: slot A can start a job
    # and slot B still reports "no-work" in the same tick, and counting that as a
    # wasted turn would make `watch` bail out while a worker is still developing.
    busy = any((ctx.state["slots"].get(s) or {}).get("state") in ACTIVE_STATES
               for s in SLOTS)
    c["idle_ticks"] = c.get("idle_ticks", 0) + 1 if (idle and not busy) else 0
    if c["idle_ticks"] == 6:
        lines.append("- 空转 %s 轮（原因 %s）：继续 tick 不会产出 PR。先 `suggest` 补池子、"
                     "`approve` 待审候选，或按 §七 异常模板汇报环境/额度问题。"
                     % (c["idle_ticks"], idle))
        ctx.state["next"] = "连续空转 %s 轮（%s）— 需要人工或 `suggest`/`approve`，别再 tick" \
                            % (c["idle_ticks"], idle)
    ctx.save()
    ctx.journal("\n".join(lines))
    ctx.ledger({"turn": ctx.state["turn"], "result": idle or "ok",
                "slots": {s: (ctx.state["slots"].get(s) or {}).get("state") for s in SLOTS}})
    if quiet:
        return
    log("\n".join(lines))
    log("")
    log(report_text(ctx))


def next_action(ctx):
    st = ctx.state
    owner = delivery_owner(ctx)
    for slot in SLOTS:
        rec = st["slots"].get(slot)
        if rec and rec.get("state") == "pr_ready":
            if owner == "worker":
                return ("slot %s green but no PR found — `sync` or fall back to `pr %s`"
                        % (slot, slot))
            return "open PR for slot %s (`prbody %s`)" % (slot, slot)
    if st.get("maintenance"):
        return "maintenance only: suggest/approve, then resume"
    pend = [t for t in ctx.coll("candidates.json") + ctx.coll("queue.json")
            if not t.get("approved") and (t.get("policy") == "needs_approval")]
    if not any(st["slots"].get(s) for s in SLOTS) and pend:
        return "approve or reject %d needs_approval candidates, then tick" % len(pend)
    if not ctx.coll("queue.json") and not ctx.coll("candidates.json"):
        return "pool empty: run `suggest`"
    if any((st["slots"].get(s) or {}).get("state") in ACTIVE_STATES for s in SLOTS):
        return "workers running; `watch` or tick again in %ss" % ctx.cap("watch_interval_s")
    got = int(st.get("counters", {}).get("prs_valid_today", 0))
    if got < 2:
        return "tick — 本轮有效 PR %s/2，继续派发" % got
    return "tick"


def sync_prs(ctx, lines):
    """Reconcile our PRs with GitHub: merged/closed → done.json."""
    prs, err = pr_list(ctx)
    if prs is None:
        if err:
            lines.append("- gh pr list unavailable: %s" % err[:120])
        return
    by_head = {p.get("headRefName"): p for p in prs}
    done = ctx.coll("done.json")
    keys = {d.get("branch") for d in done}
    changed = False
    for ref in list(ctx.state.get("pending_pr", [])):
        pr = by_head.get(ref.get("branch"))
        if pr:
            ref["pr"] = pr.get("number")
            ref["state"] = pr.get("state")
    for slot in SLOTS:
        rec = ctx.state["slots"].get(slot)
        if not rec or not rec.get("branch"):
            continue
        pr = by_head.get(rec["branch"])
        if pr and pr.get("state") in ("MERGED", "CLOSED") and rec.get("state") == "pr_open":
            if rec["branch"] not in keys:
                done.append({"id": rec["task"], "branch": rec["branch"], "title": rec.get("title"),
                             "state": pr["state"], "pr": pr.get("number"), "at": stamp()})
                changed = True
            ctx.state["slots"][slot] = None
            lines.append("- slot %s: PR #%s is %s → task moved to done.json, slot freed"
                         % (slot, pr.get("number"), pr.get("state").lower()))
        elif pr and rec.get("state") in ("pr_open", "pr_ready"):
            rec["pr"] = pr.get("number")
            rec["pr_state"] = pr.get("state")
    if changed:
        ctx.set_coll("done.json", done)
    # A PR's branch usually outlives its slot (the slot is freed the moment the PR
    # opens), so also reconcile the tracked rows in the pools: merged/closed must
    # land in done.json, otherwise the next round re-offers already-decided work.
    queue, cands = load_tasks(ctx)
    pool_changed = False
    for pool in (queue, cands):
        for row in pool:
            if row.get("status") != "pr_open":
                continue
            pr = by_head.get(row.get("branch"))
            if not pr:
                continue
            row["pr"] = pr.get("number")
            row["pr_state"] = pr.get("state")
            pool_changed = True
            if pr.get("state") in ("MERGED", "CLOSED"):
                row["status"] = "done" if pr["state"] == "MERGED" else "closed"
                row["triaged_at"] = stamp()
                pool_changed = True
                if row.get("branch") and row["branch"] not in keys:
                    done.append({"id": row.get("id"), "branch": row["branch"],
                                 "title": row.get("title"), "state": pr["state"],
                                 "pr": pr.get("number"), "at": stamp()})
                    keys.add(row["branch"])
                    changed = True
    if pool_changed:
        save_tasks(ctx, queue, cands)
    if changed:
        ctx.set_coll("done.json", done)
    ctx.state["prs_seen"] = {h: p.get("state") for h, p in by_head.items()}


def recover_slots(ctx, lines):
    """Rebuild slot truth from git + gh, not from memory. A fresh agent instance
    with no context must land on the same state machine."""
    st = ctx.state
    wt_paths = worktrees(ctx)
    refs = set(auto_refs(ctx))
    prs, _ = pr_list(ctx)
    pr_heads = {p.get("headRefName") for p in (prs or [])}
    for slot in SLOTS:
        rec = st["slots"].get(slot)
        if not rec:
            continue
        wt = rec.get("worktree")
        branch = rec.get("branch")
        if rec.get("state") == "pr_open":
            if branch in pr_heads or branch in refs:
                continue
        if wt and not os.path.isdir(wt):
            lines.append("- slot %s: worktree %s vanished (box reset/hibernate?) → "
                         "rebuilding from branch %s" % (slot, wt, branch))
            new_wt = os.path.join(os.path.dirname(ctx.root),
                                  "wt-%s" % rec["task"])
            if not os.path.isdir(new_wt):
                rc, out, err = git("worktree add %s %s" % (shlex.quote(new_wt),
                                                            shlex.quote(branch)),
                                   cwd=ctx.root, timeout=120)
                if rc != 0:
                    rc, out, err = git("worktree add -b %s %s %s" % (
                        shlex.quote(branch), shlex.quote(new_wt), ctx.origin_base),
                        cwd=ctx.root, timeout=120)
                if rc != 0:
                    rec["state"] = "blocked"
                    _block(ctx, rec, "cannot restore worktree: %s" % (err or out)[:200])
                    lines.append("- slot %s blocked: worktree restore failed" % slot)
                    continue
            rec["worktree"] = new_wt
            if rec.get("state") in ("developing", "verifying", "repairing"):
                rec["job"] = None
                rec["state"] = "verifying"
                rec["next"] = "verify what is on the branch"
                lines.append("- slot %s: job record lost → will verify the branch as-is"
                             % slot)
        if branch:
            rc, cur, _ = git("rev-parse --abbrev-ref HEAD", cwd=rec.get("worktree") or ctx.root)
            if rc == 0 and cur != branch and rec.get("state") in ACTIVE_STATES:
                lines.append("- slot %s: worktree is on %s not %s → forcing checkout"
                             % (slot, cur, branch))
                git("checkout %s" % shlex.quote(branch), cwd=rec.get("worktree") or ctx.root)
    # branches on origin that no slot claims: report, never auto-adopt
    held = {(ctx.state["slots"].get(s) or {}).get("branch")
            for s in SLOTS if ctx.state["slots"].get(s)}
    claimed = {"origin/" + h for h in held if h}
    orphans = sorted(r for r in refs if r not in claimed)
    if orphans:
        st["orphan_branches"] = orphans[:10]
        lines.append("- note: %d auto/* branch(es) on origin not in a slot (already "
                     "PR'd or abandoned): %s" % (len(orphans), ", ".join(o.split('/')[-1] for o in orphans[:5])))


def _block(ctx, rec, reason):
    blocked = ctx.coll("blocked.json")
    blocked.append({"task": rec.get("task"), "branch": rec.get("branch"),
                    "slot": rec.get("slot"), "reason": reason[:1200],
                    "at": stamp(), "evidence": rec.get("evidence"),
                    "tries": rec.get("tries", 0)})
    ctx.set_coll("blocked.json", blocked)
    rec["state"] = "blocked"
    rec["verify"] = "red"
    rec["next"] = "needs human or a new approach"
    ctx.state["slots"][rec.get("slot", "A")] = None
    ctx.ledger({"turn": ctx.state["turn"], "branch": rec.get("branch"),
                "task": rec.get("task"), "result": "blocked", "pr": None,
                "reason": reason[:200]})


def _advance(ctx, rec, slot, pol, lines):
    state = rec.get("state")
    if state in ("pr_open", "blocked"):
        return
    if state == "pr_ready":
        # In worker mode the PR can land after the marker tick: the push and
        # `gh pr create` are the worker's own commands, so keep looking for it
        # instead of parking a green slot forever.
        if delivery_owner(ctx) == "worker":
            adopt_worker_pr(ctx, rec, slot, pol, lines,
                            verified=(rec.get("verify") == "green"))
        return
    meta = read_json(os.path.join(ctx.job_dir, rec["job"] + ".json")) if rec.get("job") else None
    if state in ("developing", "repairing"):
        jstat, code = job_status(ctx, meta)
        if jstat == "none":
            rec["state"] = "verifying"
            lines.append("- slot %s: no job to wait for → verifying" % slot)
        elif jstat in ("running",):
            capture_session(ctx, meta)
            verdict, detail = judge_running_job(ctx, rec, meta)
            rec["last_action"] = "worker %s" % detail[:170]
            if verdict == "waiting":
                return
            stopped = stop_job(meta)
            lines.append("- slot %s: %s → session closed (%s) instead of waiting out "
                         "the %ss job timeout"
                         % (slot, verdict, "pid %s killed" % meta.get("pid") if stopped
                            else "process had already left", ctx.cap("job_timeout_s")))
            rec["worker_exit"] = None
            if verdict == "stalled":
                rec["stalled"] = True
                rec["tries"] = rec.get("tries", 0) + 1
                if rec["tries"] > ctx.cap("max_repair_rounds"):
                    _block(ctx, rec, "worker went silent %s times with no commits"
                           % rec["tries"])
                    lines.append("- slot %s blocked after %s silent runs"
                                 % (slot, rec["tries"]))
                    return
            rec["state"] = "verifying"
        else:
            # A worker that finishes inside one tick never takes the `running`
            # branch above, so grab its session id here — otherwise the repair
            # round has nothing to resume and sends a stranger to fix the diff.
            capture_session(ctx, meta)
            tail = tail_log(ctx, meta)
            rec["worker_report"] = tail[-2500:]
            rec["last_action"] = "job %s%s" % (jstat, "" if code is None else "/exit%s" % code)
            rec["worker_exit"] = code
            lines.append("- slot %s: job %s (exit %s)" % (slot, jstat, code))
            rec["state"] = "verifying"
            if jstat in ("timeout", "dead"):
                rec["tries"] = rec.get("tries", 0) + 1
                if jstat == "timeout":
                    stopped = stop_job(meta)
                    rec["timed_out"] = True
                    lines.append("- slot %s: worker ran past %ss → its process group %s; "
                                 "a run we stopped is never judged green"
                                 % (slot, ctx.cap("job_timeout_s"),
                                    "was stopped" if stopped else "had already left"))
                if rec["tries"] > ctx.cap("max_repair_rounds"):
                    _block(ctx, rec, "worker process died/timed out %s times" % rec["tries"])
                    lines.append("- slot %s blocked after %s dead runs"
                                 % (slot, rec["tries"]))
                    return
    if rec.get("state") == "verifying":
        verify_slot(ctx, rec, slot, pol, lines)


def rec_task(rec):
    """A slot record seen as a task card: the gate and the red-line check want the
    pool field names (id), while a record stores the id under `task`."""
    return dict(rec, id=rec.get("task") or rec.get("id") or "-")


def verify_slot(ctx, rec, slot, pol, lines):
    wt = rec["worktree"]
    task = rec_task(rec)
    vres = run_verify(ctx, task, wt, slot)
    findings, rows, churn = redline_check(ctx, task, wt, pol)
    rec["evidence"] = vres.get("evidence")
    rec["diff_files"] = [p for _, p in rows][:20]
    rec["churn"] = churn
    rec["verify_steps"] = vres.get("steps", [])
    red = list(findings)
    if vres.get("error"):
        red.append(vres["error"])
    elif vres["rc"] != 0:
        red.append("gate script exit %s" % vres["rc"])
    code = rec.get("worker_exit")
    if rec.get("timed_out"):
        red.append("worker exceeded job_timeout_s (%ss) and was stopped — the task was "
                   "never finished by the worker, so this diff cannot be called green"
                   % ctx.cap("job_timeout_s"))
    if rec.get("stalled"):
        red.append("worker went silent for %ss without printing %s and was stopped — "
                   "it never reported the task finished" % (ctx.cap("stall_s"), DONE_MARKER))
    if code not in (None, 0):
        red.append("worker process exited %s — the run reported its own failure, "
                   "so its output cannot be judged green" % code)
    # A gate whose every step was [SKIP] checked nothing. Reporting that as green
    # is the one failure mode that makes the whole loop worthless, so it is refused
    # by code rather than by hoping the model notices the empty test suite.
    ran = [s for s in (vres.get("steps") or []) if s.get("state") in ("PASS", "FAIL")]
    if not ran:
        red.append("no gate step executed (all [SKIP] or absent) — fill the verify "
                   "commands in .autoloop/config.json + verify.sh, or give the task an "
                   "acceptance[]; an unchecked diff is not green")
    if red:
        c = ctx.state["counters"]
        rec["tries"] = rec.get("tries", 0) + 1
        c["fails_today"] = c.get("fails_today", 0) + 1
        rec["verify"] = "red"
        rec["red_reasons"] = red[:6]
        if delivery_owner(ctx) == "worker":
            mark_unverified_pr(ctx, rec, slot, lines, red)
        lines.append("- slot %s VERIFY RED (%d reason(s)): %s"
                     % (slot, len(red), " | ".join(r.splitlines()[0][:90] for r in red[:3])))
        if rec["tries"] > ctx.cap("max_repair_rounds"):
            c["fails_consecutive"] = c.get("fails_consecutive", 0) + 1
            _block(ctx, rec, "acceptance failed after %s tries: %s"
                   % (rec["tries"], "; ".join(red)[:900]))
            lines.append("- slot %s blocked, reason recorded; slot freed for the next task"
                         % slot)
            return
        lines.append("- → dispatching repair round %s/%s with the raw failure output"
                     % (rec["tries"], ctx.cap("max_repair_rounds")))
        repair(ctx, rec, slot, pol, lines, "\n".join(
            ["verify: " + str(r) for r in red] + ["--- gate output tail ---",
                                                   tail_log_from(rec.get("evidence"))]))
        return
    c = ctx.state["counters"]
    c["fails_consecutive"] = 0
    rec["verify"] = "green"
    rec["state"] = "pr_ready"
    rec["next"] = "open PR"
    lines.append("- slot %s VERIFY GREEN (%s changed file(s), %s lines) → ready for PR"
                 % (slot, len(rows), churn))
    if adopt_worker_pr(ctx, rec, slot, pol, lines, verified=True):
        return
    if delivery_owner(ctx) == "worker":
        rec["next"] = "worker 未开 PR：`pr %s` 兜底交付，或重新派发" % slot


def tail_log_from(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()[-5000:]
    except OSError:
        return ""


def repair(ctx, rec, slot, pol, lines, failure):
    wt = rec["worktree"]
    task = rec_task(rec)
    if not task.get("paths"):
        task["paths"] = [p for p in rec.get("diff_files", [])] or ["*"]
    # The same agent, not a stranger: resume its session when the worker CLI can,
    # so the fix round keeps the context of the diff it wrote (protocol §六).
    prev = read_json(os.path.join(ctx.job_dir, (rec.get("job") or "") + ".json")) or {}
    sid = prev.get("session_id") or ""
    prompt = task_prompt(ctx, task, wt, slot, mode_now(ctx), extra=failure,
                         session_id=sid)
    meta, err = launch(ctx, task, wt, prompt, slot,
                       extra_env={"AUTOLOOP_SESSION_ID": sid})
    if err:
        _block(ctx, rec, "repair dispatch failed: %s" % err)
        lines.append("- slot %s: repair could not start (%s)" % (slot, err))
        return
    if sid:
        lines.append("- slot %s: repair resumes session %s" % (slot, sid[:24]))
    rec["job"] = meta["jid"]
    rec["state"] = "repairing"
    rec["worker_exit"] = None
    rec["timed_out"] = False
    ctx.state["counters"]["jobs_today"] = ctx.state["counters"].get("jobs_today", 0) + 1


def start_task(ctx, task, label, slot, mode, lines):
    tid = task.get("id") or new_task_id(ctx, task.get("title", ""))
    task["id"] = tid
    branch = "auto/" + tid
    wt = os.path.join(os.path.dirname(ctx.root), "wt-" + tid)
    dup = False
    for refspec in ("refs/heads/" + branch, "refs/remotes/origin/" + branch):
        rc, _, _ = git("show-ref --verify --quiet %s" % refspec, cwd=ctx.root)
        if rc == 0:
            dup = True
    if dup or ("origin/" + branch) in set(auto_refs(ctx)):
        lines.append("- slot %s: %s already exists → refusing a duplicate branch"
                     % (slot, branch))
        _persist_pool(ctx, task, "blocked")
        return
    if os.path.isdir(wt):
        shutil.rmtree(wt, ignore_errors=True)
        git("worktree prune", cwd=ctx.root)
    rc, out, err = git("worktree add %s -b %s %s" % (shlex.quote(wt), shlex.quote(branch),
                                                     shlex.quote(ctx.origin_base)),
                       cwd=ctx.root, timeout=180)
    if rc != 0:
        lines.append("- slot %s: worktree create failed: %s" % (slot, (err or out)[:200]))
        return
    port = 4100 + (0 if slot == "A" else 1) * 20
    os.makedirs(os.path.join(wt, ".tmp"), exist_ok=True)
    card = dict(task, branch=branch)
    prompt = task_prompt(ctx, card, wt, slot, mode)
    meta, lerr = launch(ctx, card, wt, prompt, slot,
                        extra_env={"PORT_BASE": str(port), "TEST_DB": "autoloop_w%s" % slot})
    if lerr:
        lines.append("- slot %s: cannot dispatch (%s) — nothing pushed" % (slot, lerr))
        git("worktree remove --force %s" % shlex.quote(wt), cwd=ctx.root)
        git("branch -D %s" % shlex.quote(branch), cwd=ctx.root)
        ctx.state["idle_reason"] = "no-runner" if "worker" in lerr else "dispatch-failed"
        ctx.state["next"] = "configure runner.cmd, then tick"
        return
    rec = {"slot": slot, "task": tid, "branch": branch, "title": task.get("title"),
           "paths": task.get("paths") or [], "kind": task.get("kind", "misc"),
           "acceptance": as_list(task.get("acceptance")), "worktree": wt,
           "job": meta["jid"], "state": "developing", "verify": "pending",
           "tries": 0, "started": now(), "port_base": port,
           "last_action": "dispatched", "next": "wait for worker, then verify",
           "from": label}
    ctx.state["slots"][slot] = rec
    ctx.state["counters"]["jobs_today"] = ctx.state["counters"].get("jobs_today", 0) + 1
    ctx.state["idle_reason"] = None
    lines.append("- slot %s: started %s (from %s) on %s [job %s, port %s]"
                 % (slot, tid, label, branch, meta["jid"], port))
    _persist_pool(ctx, task, "developing")
    ctx.ledger({"turn": ctx.state["turn"], "branch": branch, "task": tid,
                "result": "dispatched", "pr": None, "slot": slot})


def _persist_pool(ctx, task, status):
    """Record what happened to a pool entry. Never invents a new task."""
    for name in ("queue.json", "candidates.json"):
        pool = ctx.coll(name)
        hit = False
        for row in pool:
            if row.get("id") == task.get("id"):
                row["status"] = status
                row["last_action"] = "%s@%s" % (status, stamp())
                hit = True
        if hit:
            ctx.set_coll(name, pool)
            return
    # manual dispatch of a task that is not in any pool: register it in the queue
    if status == "developing":
        pool = ctx.coll("queue.json")
        pool.append(dict(task, status="developing"))
        ctx.set_coll("queue.json", pool)


def note_reference(ctx, lines):
    if not ctx.cfg.get("reference_enabled"):
        return
    if now() < int(ctx.state.get("gh_cooldown_until") or 0):
        return
    c = ctx.state["counters"]
    if c.get("gh_hits_today", 0) >= ctx.cap("gh_daily_rate_hits"):
        lines.append("- reference search disabled for the rest of today "
                     "(%s rate limit hits)" % c["gh_hits_today"])
        return
    queue, cands = load_tasks(ctx)
    words = []
    for t in queue + cands:
        if t.get("status") in ("done", "blocked"):
            continue
        words += re.findall(r"[A-Za-z]{4,}", t.get("title", ""))[:3]
    words += re.findall(r"[A-Za-z]{4,}", ctx.state.get("repo", ""))[:1]
    kw = " ".join(dict.fromkeys(words)).strip()[:60] or ctx.state.get("repo", "")
    if not kw:
        return
    rc, out, err = run("%s search repos %s --sort updated --limit 5 --json "
                       "fullName,stargazersCount,updatedAt,description"
                       % (gh_bin(), shlex.quote(kw)), cwd=ctx.root, timeout=45, shell=True)
    if rc != 0:
        blob = (err or "") + out
        if "429" in blob or "secondary rate limit" in blob.lower():
            c["gh_hits_today"] = c.get("gh_hits_today", 0) + 1
            ctx.state["gh_cooldown_until"] = now() + ctx.cap("gh_cooldown_min") * 60
            lines.append("- reference search rate-limited: cooldown until %s (hit %s today)"
                         % (stamp(ctx.state["gh_cooldown_until"]), c["gh_hits_today"]))
        else:
            lines.append("- reference search unavailable: %s" % blob[:120])
        return
    try:
        rows = json.loads(out or "[]")
    except json.JSONDecodeError:
        rows = []
    if rows:
        best = rows[0]
        ctx.state["ref"] = {"repo": best.get("fullName"),
                            "stars": best.get("stargazersCount"),
                            "updated": str(best.get("updatedAt", ""))[:10],
                            "desc": (best.get("description") or "")[:160],
                            "at": stamp(), "query": kw}
        lines.append("- reference: %s (%s★, updated %s)"
                     % (best.get("fullName"), best.get("stargazersCount"),
                        str(best.get("updatedAt", ""))[:10]))
    else:
        ctx.state["ref"] = None


def maintenance_tick(ctx, pol, lines):
    """Gate A: no business work. Rebase checks, re-verify, state repair."""
    done = 0
    for slot in SLOTS:
        rec = ctx.state["slots"].get(slot)
        if not rec or rec.get("state") != "pr_ready":
            continue
        wt = rec.get("worktree")
        if not wt or not os.path.isdir(wt):
            continue
        rc, dirty, _ = git("status --porcelain", cwd=wt)
        if dirty.strip():
            lines.append("- maintenance: slot %s worktree dirty, not touching it" % slot)
            continue
        rc, ahead, _ = git("rev-list --count %s..HEAD" % ctx.origin_base, cwd=wt)
        rc2, behind, _ = git("rev-list --count HEAD..%s" % ctx.origin_base, cwd=wt)
        if rc == 0 and rc2 == 0 and int(behind or 0) > 0 and int(ahead or 0) > 0:
            lines.append("- maintenance: slot %s `%s` diverged from %s by %s/%s commits "
                         "→ needs-human before any rebase" % (slot, rec["branch"],
                                                              ctx.base, ahead, behind))
            rec["next"] = "needs-human: branch diverged, rebase not attempted"
            done += 1
    if not done:
        lines.append("- maintenance: nothing safe to do. Candidates: run `suggest`; "
                     "to resume business work after a human look: `resume`.")
        ctx.state["idle_reason"] = "exhausted"


def cmd_status(args):
    ctx = Ctx(args.repo)
    ctx.need_init()
    ctx.ensure_state()
    log(report_text(ctx))
    return 0


def cmd_report(args):
    if getattr(args, "patrol", False):
        ctx = Ctx(args.repo)
        ctx.need_init()
        ctx.state = read_json(ctx.state_path) or ctx.state
        log(patrol_text(ctx))
        return 0
    rc = cmd_status(args)
    ctx = Ctx(args.repo)
    ctx.state["last_report_at"] = now()
    ctx.save()
    if args.journal:
        ctx.journal(report_text(ctx))
    return rc


def cmd_plan(args):
    ctx = Ctx(args.repo)
    ctx.need_init()
    pol = read_policy(ctx)
    mode = mode_now(ctx)
    task, label, why = pick_task(ctx, pol, mode)
    if not task:
        log("no eligible task: %s" % (why or "pool is empty"))
        return 1
    log(json.dumps(task, ensure_ascii=False, indent=2))
    log("# (from %s, mode %s)" % (label, mode))
    return 0


def cmd_dispatch(args):
    ctx = Ctx(args.repo)
    ctx.need_init()
    slot = (args.slot or "A").upper()
    if slot not in SLOTS:
        die("slot must be A or B")
    if ctx.state["slots"].get(slot):
        die("slot %s is busy: %s (%s)" % (slot, ctx.state["slots"][slot].get("task"),
                                         ctx.state["slots"][slot].get("state")))
    queue, cands = load_tasks(ctx)
    pool = queue + cands
    task = next((t for t in pool if t.get("id") == args.task), None)
    if not task and args.task:
        die("no task %s in queue.json/candidates.json (list with `queue`)" % args.task)
    if not task and args.title:
        task = {"id": new_task_id(ctx, args.title), "title": args.title,
                "paths": args.paths or ["*"], "kind": args.kind or "misc",
                "priority": args.priority or 3,
                "acceptance": args.accept or [], "policy": args.policy or "auto_apply",
                "deduplication": "created by hand"}
        queue.append(task)
        ctx.set_coll("queue.json", queue)
    if not task:
        die("give a task id or --title")
    ctx.state.setdefault("counters", {})
    start_task(ctx, task, "manual", slot, mode_now(ctx), [])
    ctx.save()
    log("slot %s → %s" % (slot, json.dumps(ctx.state["slots"].get(slot), ensure_ascii=False)[:400]))
    return 0


def cmd_reap(args):
    ctx = Ctx(args.repo)
    ctx.need_init()
    for slot in SLOTS:
        rec = ctx.state["slots"].get(slot)
        if not rec or not rec.get("job"):
            continue
        meta = read_json(os.path.join(ctx.job_dir, rec["job"] + ".json"))
        st, code = job_status(ctx, meta)
        log("slot %s job %s: %s%s" % (slot, rec["job"], st,
                                      "" if code is None else " exit=%s" % code))
        if args.tail and meta:
            log(tail_log(ctx, meta)[-2000:])
    return 0


def cmd_verify(args):
    ctx = Ctx(args.repo)
    ctx.need_init()
    slot = (args.slot or "").upper()
    rec = ctx.state["slots"].get(slot) if slot in SLOTS else None
    if not rec and args.task:
        rec = _find_rec(ctx, args.task)
    if not rec:
        die("give --slot A|B or --task <id> of a tracked branch")
    pol = read_policy(ctx)
    verify_slot(ctx, rec, slot or "?", pol, [])
    ctx.save()
    log("verify: %s｜reasons: %s" % (rec.get("verify"),
                                    " | ".join(rec.get("red_reasons", []))[:600] or "-"))
    log("evidence: %s" % rec.get("evidence"))
    return 0 if rec.get("verify") == "green" else 1


def _find_rec(ctx, tid):
    for name in ("queue.json", "candidates.json"):
        for t in ctx.coll(name):
            if t.get("id") == tid:
                wt = os.path.join(os.path.dirname(ctx.root), "wt-" + tid)
                if os.path.isdir(wt):
                    return {"slot": "?", "task": tid, "branch": "auto/" + tid,
                            "title": t.get("title"), "paths": t.get("paths"),
                            "kind": t.get("kind"), "worktree": wt, "tries": 0}
    return None


def risk_score(ctx, rec, rows, churn):
    score, why = 10, ["base 10"]
    score += min(30, int(churn / 20))
    if churn:
        why.append("+%d for %s changed lines" % (min(30, int(churn / 20)), churn))
    mods = len({p.split("/")[0] for _, p in rows})
    if mods > 3:
        score += 15
        why.append("+15 for %d top-level modules" % mods)
    if not any(is_test_path(p) for _, p in rows):
        score += 15
        why.append("+15 for no test file in the diff")
    score += 10 * int(rec.get("tries", 0))
    if rec.get("tries"):
        why.append("+%d for %d repair round(s)" % (10 * rec["tries"], rec["tries"]))
    if (rec.get("kind") or "") in ("small_refactor",):
        score += 5
        why.append("+5 refactor")
    if any(HIGH_RISK_NAME_RE.search(p) for _, p in rows):
        score += 25
        why.append("+25 file name matches auth/pay/crypto/…")
    if (rec.get("kind") or "") in ("lint_fix", "typo", "docs_align"):
        score = max(5, score - 10)
        why.append("-10 mechanical change")
    return min(100, score), why


def pr_body(ctx, rec, auto=False):
    base = ctx.origin_base
    rows, _ = changed_files(ctx, rec["worktree"], base)
    stat, churn = diff_stat(ctx, rec["worktree"], base)
    score, why = risk_score(ctx, rec, rows, churn)
    tag = "auto-ready" if (score < 20 and churn < 50) else "auto-needs-review"
    business = [(s, p) for s, p in rows if not is_test_path(p)]
    ref = ctx.state.get("ref")
    rc, commits, _ = git("log --oneline %s..HEAD" % base, cwd=rec["worktree"])
    focus = sorted(business, key=lambda r: -len(r[1]))[:3] or rows[:3]
    steps = rec.get("verify_steps") or []
    ref_txt = ("`%s` — %s★, last update %s. 借鉴点：<填：接口/分层/测试组织/边界处理>"
               % (ref["repo"], ref.get("stars"), ref.get("updated"))
               if ref else "本轮未查询参考项目（未触发或 gh 不可用），不得编造。")
    body = ["## 1. 改动意图",
            "<填一句：解决什么具体问题>%s" % ("" if not auto else
             "  (auto: %s)" % (rec.get("title") or "")),
            "",
            "## 2. 影响模块",
            "```", (stat or "(no diff)")[-1800:], "```",
            "",
            "## 3. 建议重点审查",
            "\n".join("- `%s` (%s)" % (p, s) for s, p in focus) or "- (none)",
            "",
            "## 4. 参考的 GitHub 项目",
            ref_txt,
            "",
            "## 5. verify 输出摘要",
            "- 分支 `%s`，基线 `%s`" % (rec["branch"], base),
            "- 步骤: " + (", ".join("%s:%s" % (s["state"].lower(), s["cmd"][:40])
                                    for s in steps) or "(见证据文件)"),
            "- 证据: `%s`" % (rec.get("evidence") or "-"),
            "- 红线复查: 通过（禁区/测试断言/paths/凭证/规模 全部由引擎独立复核）",
            "- worker 自述不作为通过依据",
            "",
            "## 6. 风险分 %d/100" % score,
            "依据: " + "; ".join(why),
            "",
            "---",
            "labels: %s｜created by autoloop %s%s"
            % (tag, VERSION, " (body auto-filled, judgement fields left as `<填>`)"
               if auto else ""),
            "PR created by the AutoLoop supervisor. Never merged automatically.",
            ]
    return "\n".join(body) + "\n", tag, score


def cmd_prbody(args):
    ctx = Ctx(args.repo)
    ctx.need_init()
    rec = _slot_rec(ctx, args.slot)
    body, tag, score = pr_body(ctx, rec, auto=args.auto)
    sys.stdout.write(body)
    eprint("suggested label: %s (risk %s)" % (tag, score))
    return 0


def _slot_rec(ctx, slot):
    slot = (slot or "A").upper()
    rec = ctx.state["slots"].get(slot)
    if not rec:
        die("slot %s is empty" % slot)
    if rec.get("verify") != "green":
        die("slot %s is not green (verify=%s) — no PR without a green external gate"
            % (slot, rec.get("verify")))
    if not os.path.isdir(rec.get("worktree") or ""):
        die("slot %s worktree is gone" % slot)
    return rec


def cmd_pr(args):
    ctx = Ctx(args.repo)
    ctx.need_init()
    with Lock(ctx) as lk:
        if lk is None:
            die("another supervisor holds the lock")
        rec = _slot_rec(ctx, args.slot)
        pol = read_policy(ctx)
        hard, soft = [], []
        if os.path.exists(os.path.join(ctx.alo, "STOP")) or \
           os.path.exists(os.path.join(ctx.alo, "HALTED")):
            hard.append("STOP/HALTED present")
        if not str(rec.get("branch", "")).startswith("auto/"):
            hard.append("branch is not auto/*")
        if not has_origin(ctx):
            hard.append("no origin remote on this repo — `git remote add origin <url>` "
                        "first, otherwise there is nothing to push a branch to")
        if rec.get("state") == "pr_open":
            hard.append("PR already open for this branch")
        findings, rows, churn = redline_check(ctx, rec_task(rec), rec["worktree"], pol)
        if findings:
            rec["verify"] = "red"
            rec["red_reasons"] = findings[:6]
            hard.append("redline re-check now fails: " +
                        "; ".join(f[:120] for f in findings[:3]))
        if int(ctx.state["counters"].get("pr_today", 0)) >= ctx.cap("pr_max_per_day"):
            soft.append("daily PR cap %s reached" % ctx.cap("pr_max_per_day"))
        if hard:
            die("refused: %s" % " / ".join(hard))
        if soft:
            st = ctx.state
            st.setdefault("pending_pr", []).append(
                {"task": rec["task"], "branch": rec["branch"], "paths": rec.get("paths"),
                 "at": stamp(), "reason": soft[0]})
            rec["state"] = "pr_open"
            rec["next"] = "cap reached — branch pushed only, PR waits for a slot tomorrow "
            _push_only(ctx, rec, soft)
            ctx.state["slots"][rec["slot"]] = None
            ctx.ledger({"turn": st.get("turn"), "branch": rec["branch"],
                        "task": rec["task"], "result": "pending_pr", "pr": None})
            ctx.save()
            log("%s — branch pushed, PR queued (slot %s freed)" % (soft[0], rec["slot"]))
            return 1
        body, tag, score = pr_body(ctx, rec, auto=args.auto_body or not args.body)
        if args.body:
            body = read_text(args.body) or body
        if not gh_authed(ctx):
            _push_only(ctx, rec, ["gh not authenticated"])
            ctx.state["slots"][rec["slot"]] = rec
            rec["next"] = "branch pushed; PR needs `gh auth login`"
            log("pushed but cannot open a PR: gh is not authenticated on this box")
            ctx.save()
            return 1
        rc, out, err = git("push -u origin %s" % shlex.quote(rec["branch"]),
                           cwd=rec["worktree"], timeout=300)
        if rc != 0:
            die("push failed: %s" % (err or out)[:400])
        tmp = os.path.join(ctx.alo, "pr-body-%s.md" % rec["task"])
        write_text(tmp, body)
        cmd = ("%s pr create %s--base %s --head %s --title %s --body-file %s"
               % (gh_bin(), repo_flag(ctx), ctx.base,
                  shlex.quote(rec["branch"]), shlex.quote("autoloop: " + (rec.get("title") or rec["task"])),
                  shlex.quote(tmp)))
        rc, out, err = run(cmd, cwd=ctx.root, timeout=120, shell=True)
        if rc != 0:
            die("gh pr create failed (branch is pushed): %s" % (err or out)[:400])
        num = re.search(r"(\d+)$", out or "")
        run("%s pr edit %s%s --add-label %s"
            % (gh_bin(), repo_flag(ctx), num.group(1) if num else "",
               shlex.quote(tag)), cwd=ctx.root, timeout=60, shell=True)
        rec["state"] = "pr_open"
        rec["pr"] = num.group(1) if num else out.strip()
        rec["risk"] = score
        rec["label"] = tag
        rec["next"] = "slot released"
        ctx.state["counters"]["pr_today"] = ctx.state["counters"].get("pr_today", 0) + 1
        # §五 counts *valid* PRs, and this path only opens after a green gate plus a
        # fresh red-line re-check, so a supervisor-opened PR is valid by construction.
        # Without this the patrol template under-reports in delivery.owner=supervisor.
        ctx.state["counters"]["prs_valid_today"] = \
            ctx.state["counters"].get("prs_valid_today", 0) + 1
        ctx.state["slots"][rec["slot"]] = None
        queue, cands = load_tasks(ctx)
        for pool in (queue, cands):
            for row in pool:
                if row.get("id") == rec["task"]:
                    row["status"] = "pr_open"
                    row["pr"] = rec["pr"]
                    row["branch"] = rec["branch"]
                    row["pr_state"] = "OPEN"
        ctx.set_coll("queue.json", queue)
        ctx.set_coll("candidates.json", cands)
        ctx.ledger({"turn": ctx.state["turn"], "branch": rec["branch"], "task": rec["task"],
                    "result": "pr_open", "pr": rec["pr"], "risk": score, "label": tag})
        log("PR %s opened for %s (risk %s, label %s). Slot %s released — tick to refill."
            % (rec["pr"], rec["branch"], score, tag, rec["slot"]))
        ctx.save()
        return 0


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _push_only(ctx, rec, why):
    """Push the branch without opening a PR. Returns True only when git actually
    took it — logging 'pushed' over a failed push would be a lie the morning
    review has no way to detect."""
    rc, out, err = git("push -u origin %s" % shlex.quote(rec["branch"]),
                       cwd=rec["worktree"], timeout=300)
    if rc == 0:
        log("branch pushed, no PR (%s)" % "; ".join(why)[:120])
        return True
    log("PUSH FAILED (%s): %s — the work exists only as a local branch"
        % ("; ".join(why)[:120], (err or out)[:200]))
    return False


def cmd_suggest(args):
    ctx = Ctx(args.repo)
    ctx.need_init()
    pol = read_policy(ctx)
    found = []
    rc, out, _ = git("grep -nE \"TODO|FIXME|XXX|HACK\" -- '*.py' '*.js' '*.ts' '*.tsx' "
                     "'*.vue' '*.java' '*.go' '*.rs' '*.md' ':!*.lock' || true",
                     cwd=ctx.root, timeout=120)
    marks = []
    for line in (out or "").splitlines():
        head = line.split(":", 1)[0]
        if re.search(r"node_modules|dist|build|\.min\.|__pycache__", head):
            continue
        marks.append(line)
    by_file = {}
    for line in marks:
        head = line.split(":")[0]
        by_file.setdefault(head, line)
    for path, line in sorted(by_file.items())[: args.limit]:
        text = line.split(":", 2)[-1].strip() if ":" in line else path
        found.append({"title": "清理遗留标记: %s" % text[:60],
                      "paths": [path], "kind": "docs_align",
                      "acceptance": ["git grep -c -E 'TODO|FIXME|XXX|HACK' -- %s || true" % path],
                      "evidence": line[:200], "priority": 2})
    rc, out, _ = git("grep -nE '(pytest\\.mark\\.skip|it\\.skip|test\\.skip|@skip"
                     "|xfail)' -- 'tests/*' 'test/*' '*.test.*' '*.spec.*' "
                     "'*test*.py' || true", cwd=ctx.root)
    skipped = {}
    for line in (out or "").splitlines():
        skipped.setdefault(line.split(":")[0], line)
    for path, line in sorted(skipped.items())[: max(1, args.limit // 3)]:
        found.append({"title": "恢复被跳过的测试: %s" % path,
                      "paths": [path], "kind": "add_tests", "priority": 4,
                      "acceptance": [], "evidence": line[:200]})
    if args.lint:
        lint = (ctx.cfg.get("verify") or {}).get("lint")
        if lint:
            rc, out, err = run(lint, cwd=ctx.root, timeout=300, shell=True)
            if rc != 0:
                bad = {}
                for line in (out + "\n" + err).splitlines():
                    hit = re.match(r".*?([./A-Za-z0-9_\-/]+\.[A-Za-z]{1,5}):(\d+)", line)
                    if hit:
                        bad.setdefault(hit.group(1), line)
                for path, line in sorted(bad.items())[: max(1, args.limit // 3)]:
                    found.append({"title": "修 lint: %s" % path, "paths": [path],
                                  "kind": "lint_fix", "priority": 3,
                                  "acceptance": [lint], "evidence": line[:200]})
    docs = []
    for cand in ("AGENTS.md", "README.md", "docs"):
        pth = os.path.join(ctx.root, cand)
        if os.path.isfile(pth):
            docs.append(cand)
        elif os.path.isdir(pth):
            docs += [os.path.join(cand, f) for f in sorted(os.listdir(pth))
                     if f.endswith(".md")][:5]
    text = "".join(read_text(os.path.join(ctx.root, d)) for d in docs[:6])
    claims = re.findall(r"(?:支持|实现|TODO[:：]|待办|roadmap)[^\n。]{4,60}", text, re.I)[:4]
    for claim in claims:
        found.append({"title": "核对文档承诺与实现: %s" % claim[:50],
                      "paths": docs[:3], "kind": "docs_align", "priority": 1,
                      "acceptance": [], "evidence": claim[:200]})
    cands = ctx.coll("candidates.json")
    known = known_keys(ctx) | {dedupe_key(c) for c in cands}
    added = []
    for row in found:
        row["id"] = new_task_id(ctx, row["title"])
        row["policy"] = kind_policy(row["kind"], pol)
        row["status"] = "open"
        row["deduplication"] = "checked against done/blocked/candidates/active slots; " \
                               "title+paths key not present"
        key = dedupe_key(row)
        if key in known:
            continue
        known.add(key)
        cands.append(row)
        added.append(row)
    ctx.set_coll("candidates.json", cands)
    ctx.state["counters"]["cands_added"] = ctx.state["counters"].get("cands_added", 0) + len(added)
    ctx.save()
    log("suggest: %d new candidate(s), pool now %d" % (len(added), len(cands)))
    for row in added:
        log("- [%s] %s  (%s) %s" % (row["policy"], row["title"], row["kind"],
                                     (row.get("evidence") or "")[:70]))
    if not added:
        log("- nothing new: pool is already covered. Do not invent work to fill the "
            "slot — say idle_reason: exhausted.")
    return 0


def cmd_approve(args):
    _triage(Ctx(args.repo), args.id, True, args.note)
    return 0


def cmd_reject(args):
    _triage(Ctx(args.repo), args.id, False, args.note)
    return 0


def _triage(ctx, ids, approve, note=""):
    ctx.need_init()
    queue, cands = load_tasks(ctx)
    hit = 0
    for pool in (queue, cands):
        for row in pool:
            if row.get("id") in ids:
                row["approved"] = bool(approve)
                row["policy"] = "auto_apply" if approve else row.get("policy")
                row["triaged_at"] = stamp()
                row["triage_note"] = note
                if not approve:
                    row["status"] = "rejected"
                hit += 1
    save_tasks(ctx, queue, cands)
    ctx.ledger({"turn": ctx.state.get("turn"), "result": "triage",
                "approved": approve, "tasks": list(ids)})
    log("%s %d task(s)%s" % ("approved" if approve else "rejected", hit,
                             "" if hit else " — id not found in queue/candidates"))
    if approve and hit:
        log("they may now start on the next tick (subject to paths/night/policy).")


def cmd_queue(args):
    ctx = Ctx(args.repo)
    ctx.need_init()
    if args.add:
        task = {"id": new_task_id(ctx, args.add), "title": args.add,
                "paths": args.paths or ["*"], "kind": args.kind or "new_feature",
                "priority": args.priority if args.priority is not None else 3,
                "acceptance": args.accept or [],
                "policy": args.policy or kind_policy(args.kind or "new_feature",
                                                     read_policy(ctx)),
                "status": "open", "approved": bool(args.approve),
                "deduplication": args.dedup or "user-supplied task"}
        if task["approved"]:
            task["policy"] = "auto_apply"
        queue = ctx.coll("queue.json")
        if dedupe_key(task) in {k for k in known_keys(ctx)}:
            log("skipped: looks like an existing/done/blocked task")
            return 1
        queue.append(task)
        queue.sort(key=lambda t: -int(t.get("priority") or 0))
        ctx.set_coll("queue.json", queue)
        ctx.save()
        log("queued %s [%s/%s]" % (task["id"], task["kind"], task["policy"]))
        return 0
    queue, cands = load_tasks(ctx)
    log("# queue (%d)" % len(queue))
    for t in queue:
        log("- %s p%s [%s/%s] %s %s" % (t.get("id"), t.get("priority", "-"),
                                         t.get("kind"), t.get("policy"),
                                         t.get("title", "")[:60], t.get("status", "open")))
    log("# candidates (%d)" % len(cands))
    for t in cands:
        log("- %s p%s [%s/%s] %s %s" % (t.get("id"), t.get("priority", "-"),
                                         t.get("kind"), t.get("policy"),
                                         t.get("title", "")[:60], t.get("status", "open")))
    return 0


def cmd_budget(args):
    ctx = Ctx(args.repo)
    ctx.need_init()
    c = ctx.state.get("counters", {})
    rows = [("turns today", c.get("turns_today", 0), ctx.cap("max_turns_per_day")),
            ("worker jobs today", c.get("jobs_today", 0), ctx.cap("nightly_max_jobs")),
            ("PRs today", c.get("pr_today", 0), ctx.cap("pr_max_per_day")),
            ("consecutive red", c.get("fails_consecutive", 0), ctx.cap("fail_gate")),
            ("gh rate hits today", c.get("gh_hits_today", 0), ctx.cap("gh_daily_rate_hits")),
            ("free disk MB", free_disk_mb(ctx.root), ctx.cap("min_free_disk_mb"))]
    log("| counter | now | cap |")
    log("|---|---|---|")
    for name, val, cap in rows:
        log("| %s | %s | %s |" % (name, val, cap))
    stop = int(c.get("turns_today", 0)) >= int(ctx.cap("max_turns_per_day"))
    if stop:
        log("turn budget spent: tick will not dispatch until the day rolls over "
            "(or raise caps in .autoloop/config.json — that is a human edit)")
    if os.environ.get("AUTOLOOP_HARD_STOP"):
        log("AUTOLOOP_HARD_STOP is set: nothing will be dispatched tonight.")
        return 3
    return 0


def cmd_stop(args):
    ctx = Ctx(args.repo)
    ctx.need_init()
    path = os.path.join(ctx.alo, "HALTED" if args.hard else "STOP")
    write_text(path, "%s\n%s\n" % (stamp(), args.reason or "stopped by operator"))
    log("wrote %s — tick will refuse business work until removed" % path)
    return 0


def cmd_go(args):
    ctx = Ctx(args.repo)
    ctx.need_init()
    removed = []
    for flag in ("STOP", "HALTED"):
        path = os.path.join(ctx.alo, flag)
        if os.path.exists(path):
            os.remove(path)
            removed.append(flag)
    ctx.state["maintenance"] = False
    ctx.state["counters"]["fails_consecutive"] = 0
    ctx.state["mode_forced"] = args.mode if args.mode in ("day", "night") else ctx.state.get("mode_forced")
    ctx.save()
    log("removed %s; maintenance flag cleared; mode_forced=%s"
        % (", ".join(removed) or "nothing", ctx.state.get("mode_forced") or "auto"))
    return 0


def cmd_halt(args):
    return _resume_or_halt(args, halt=True)


def cmd_resume(args):
    return _resume_or_halt(args, halt=False)


def _resume_or_halt(args, halt):
    ctx = Ctx(args.repo)
    ctx.need_init()
    if halt:
        write_text(os.path.join(ctx.alo, "HALTED"), "%s %s" % (stamp(), args.reason or ""))
        log("HALTED written: only `resume` clears it")
    else:
        for flag in ("HALTED",):
            path = os.path.join(ctx.alo, flag)
            if os.path.exists(path):
                os.remove(path)
        ctx.state["maintenance"] = False
        ctx.state["counters"]["fails_consecutive"] = 0
        log("HALTED removed, maintenance cleared, loop may take business work again")
    ctx.save()
    return 0


def cmd_recover(args):
    ctx = Ctx(args.repo)
    ctx.need_init()
    with Lock(ctx) as lk:
        if lk is None:
            die("locked")
        lines = []
        fetch(ctx)
        sync_prs(ctx, lines)
        recover_slots(ctx, lines)
        ctx.save()
        log("\n".join(lines) or "- nothing to recover: state matches git/gh")
        log(report_text(ctx))
    return 0


def cmd_sync(args):
    ctx = Ctx(args.repo)
    ctx.need_init()
    lines = []
    sync_prs(ctx, lines)
    ctx.save()
    log("\n".join(lines) or "- no PR changes")
    return 0


def cmd_backup(args):
    """Snapshot the supervisory state to a dedicated branch. Never main, never
    merges, and it refuses if the branch is not autoloop/state."""
    ctx = Ctx(args.repo)
    ctx.need_init()
    branch = "autoloop/state"
    if any(str(rec.get("branch", "")).startswith("auto/") for rec in
           [ctx.state["slots"].get(s) for s in SLOTS] if rec):
        log("note: task branches are untouched by this command")
    tmp = os.path.join("/tmp", "autoloop-state-%s" % today())
    os.makedirs(tmp, exist_ok=True)
    for name in os.listdir(ctx.alo):
        src = os.path.join(ctx.alo, name)
        dst = os.path.join(tmp, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
    git("init -q", cwd=tmp)
    git("checkout -q -b %s" % branch, cwd=tmp)
    git("add -A", cwd=tmp)
    git("commit -qm %s" % shlex.quote("autoloop state snapshot %s" % stamp()), cwd=tmp)
    if args.push:
        rc, out, err = git("remote add origin %s" % shlex.quote(args.push), cwd=tmp)
        rc2, out2, err2 = git("push -f origin %s" % branch, cwd=tmp, timeout=180)
        log("push %s → %s: %s" % (tmp, branch, "ok" if rc2 == 0 else (err2 or out2)[:200]))
        return 0 if rc2 == 0 else 1
    log("snapshot at %s (branch %s). Pass --push <remote-url> to ship it." % (tmp, branch))
    return 0


def cmd_ref(args):
    ctx = Ctx(args.repo)
    ctx.need_init()
    if args.clear:
        ctx.state["ref"] = None
        ctx.save()
        log("reference cleared")
        return 0
    note_reference(ctx, [])
    ctx.save()
    log(json.dumps(ctx.state.get("ref"), ensure_ascii=False, indent=2))
    return 0


# ------------------------------------------------------- patrol report (§七)


def anomalies_of(ctx):
    """Everything the human needs to be told about, in one list."""
    st = ctx.state
    c = st.get("counters", {})
    out = []
    flags = [f for f in ("HALTED", "STOP") if os.path.exists(os.path.join(ctx.alo, f))]
    if flags:
        out.append("%s 在场：派发已被人工停止，恢复要 `resume`/`go`" % "/".join(flags))
    if st.get("maintenance"):
        out.append("维护模式（连续红 %s 次），只跑安全家务活" % c.get("fails_consecutive"))
    blocked = ctx.coll("blocked.json")
    for item in blocked[-2:]:
        out.append("阻塞 `%s`：%s" % (item.get("task") or item.get("branch"),
                                     str(item.get("reason"))[:90]))
    for slot in SLOTS:
        rec = st["slots"].get(slot) or {}
        if rec.get("pr_unverified"):
            out.append("PR #%s 已建但未过监工闸门，勿合并（%s）"
                       % (rec.get("pr"), "; ".join(str(r).splitlines()[0]
                                                  for r in (rec.get("red_reasons") or [])[:2])[:120]))
        if rec.get("pr_wrong_base"):
            out.append("PR #%s 的基分支是 %s，不是 %s"
                       % (rec.get("pr"), rec["pr_wrong_base"], ctx.base))
        if rec.get("stalled"):
            out.append("slot %s worker 静默 %ss 被停（未打印 %s）"
                       % (slot, ctx.cap("stall_s"), DONE_MARKER))
        if rec.get("timed_out"):
            out.append("slot %s worker 超时 %ss 被停" % (slot, ctx.cap("job_timeout_s")))
    if st.get("idle_reason") in ("no-origin", "no-runner", "no-disk"):
        # an empty pool or a spent budget is §八's "可自动处理" territory — the answer
        # is `suggest` or the next day's reset, not an hourly alert the human ignores.
        out.append("循环停摆：%s" % st["idle_reason"])
    if int(c.get("idle_ticks") or 0) >= 6:
        out.append("连续空转 %s 轮，tick 本身已无法推进" % c["idle_ticks"])
    return out


def patrol_text(ctx):
    """§七 的两个固定模板，用真实状态填好 —— 监工只需要把它贴给用户，不必自己组织
    语言，也不该往里加每个 agent 在开发什么功能（协议明确禁止）。"""
    st = ctx.state
    c = st.get("counters", {})
    bad = anomalies_of(ctx)
    valid = int(c.get("prs_valid_today", 0))
    stopped = os.path.exists(os.path.join(ctx.alo, "STOP")) or \
        os.path.exists(os.path.join(ctx.alo, "HALTED"))
    if stopped:
        loop = "已暂停"
    elif st.get("maintenance") or st.get("idle_reason") in ("no-origin", "no-runner", "no-disk"):
        loop = "已阻塞"
    else:
        loop = "运行中"
    recover = st.get("last_recovery") or "无"
    need_me = "否"
    if bad:
        for kind in ("维护模式", "阻塞", "勿合并", "基分支", "人工停止",
                     "停摆：no-origin", "停摆：no-runner", "停摆：no-disk", "连续空转"):
            if any(kind in b for b in bad):
                need_me = "是，见异常情况"
                break
    head = "【%s｜%s】" % ("异常告警" if bad else "定时巡检", stamp())
    lines = [head]
    if bad:
        lines += ["- 异常类型：" + "；".join(b[:90] for b in bad[:3]),
                  "- 影响范围：slot %s（A=%s / B=%s），当日有效 PR %s 条"
                  % ("、".join(s for s in SLOTS if st["slots"].get(s)) or "无",
                     (st["slots"].get("A") or {}).get("state") or "idle",
                     (st["slots"].get("B") or {}).get("state") or "idle", valid),
                  "- 已执行的恢复措施：" + str(recover)[:200],
                  "- 当前状态：%s（%s档）" % (loop, mode_now(ctx)),
                  "- 是否需要我授权或处理：" + need_me,
                  "- 下一步监督动作：" + str(st.get("next") or "tick")[:160]]
    else:
        quiet_note = {"no-work": "待办池空，需要 `suggest` 补任务",
                      "exhausted": "候选池已试过一轮，需要新任务",
                      "cap-jobs": "当晚 job 额度已用完",
                      "cap-turns": "当日轮数额度已用完"}.get(st.get("idle_reason") or "", "")
        lines += ["- 总体状态：%s" % ("正常" + ("（%s）" % quiet_note if quiet_note else "")),
                  "- 开发循环：%s（%s档，turn %s，当日轮数 %s/%s）"
                  % (loop, mode_now(ctx), st.get("turn"), c.get("turns_today", 0),
                     ctx.cap("max_turns_per_day")),
                  "- 本轮有效 PR：%d 条" % valid,
                  "- PR 目标：至少 2 条" + ("（已达）" if valid >= 2 else "（还差 %d 条）"
                                            % (2 - valid)),
                  "- 异常情况：无",
                  "- 采取的恢复措施：" + str(recover)[:200],
                  "- 是否需要我介入：" + need_me]
    lines.append("")
    lines.append("autoloop %s｜PR 今日 %s/%s｜jobs %s/%s｜队列 %d/%d"
                 % (stamp(), c.get("pr_today", 0), ctx.cap("pr_max_per_day"),
                    c.get("jobs_today", 0), ctx.cap("nightly_max_jobs"),
                    len([t for t in ctx.coll("queue.json")
                         if t.get("status") not in ("done", "blocked")]),
                    len([t for t in ctx.coll("candidates.json")
                         if t.get("status") not in ("done", "blocked")])))
    return "\n".join(lines)


# ---------------------------------------------------- state record + injection


def protocol_path(ctx=None):
    """PROTOCOL.md ships next to the engine, but a box that cloned the repo into a
    skill directory resolves it from the script's own location."""
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(os.path.dirname(here), "PROTOCOL.md"),
                 os.path.join(here, "PROTOCOL.md"),
                 os.path.join(here, "..", "PROTOCOL.md")):
        real = os.path.realpath(cand)
        if os.path.exists(real):
            return real
    return ""


def state_record(ctx):
    """§十 的持久化状态记录，全部来自真实文件/git/gh，不是聊天记忆。"""
    st = ctx.state
    c = st.get("counters", {})
    rec = {
        "engine": VERSION,
        "目标仓库地址": ctx.remote_name() or "(no origin)",
        "基分支": ctx.base,
        "插件技能安装及验证状态": {
            "protocol": protocol_path(ctx) or "MISSING",
            "protocol_sha256": (sha256_file(protocol_path(ctx)) if protocol_path(ctx) else "")[:12],
            "verify.sh_sha256": sha256_file(os.path.join(ctx.alo, "verify.sh"))[:12],
            "preflight": read_json(os.path.join(ctx.alo, "preflight.json"), {}) or {},
        },
        "运行中的 Agent 和任务": {
            s: {"task": (st["slots"].get(s) or {}).get("task"),
                "state": (st["slots"].get(s) or {}).get("state"),
                "job": (st["slots"].get(s) or {}).get("job"),
                "pid": (read_json(os.path.join(ctx.job_dir,
                          (st["slots"].get(s) or {}).get("job", "") + ".json"), {}) or {}).get("pid"),
                "session_id": (read_json(os.path.join(ctx.job_dir,
                          (st["slots"].get(s) or {}).get("job", "") + ".json"), {}) or {}).get("session_id"),
                }
            for s in SLOTS if st["slots"].get(s)
        },
        "各 Agent 的分支与工作目录": {
            s: {"branch": (st["slots"].get(s) or {}).get("branch"),
                "worktree": (st["slots"].get(s) or {}).get("worktree")}
            for s in SLOTS if st["slots"].get(s)
        },
        "已创建 PR（编号/链接/状态）": [
            {"pr": r.get("pr"), "url": r.get("pr_url"), "state": r.get("pr_state"),
             "branch": r.get("branch"), "verified": r.get("verify")}
            for r in (st["slots"].values() if isinstance(st.get("slots"), dict) else [])
            if r and r.get("pr")
        ] + [{"pr": p.get("pr"), "branch": p.get("branch"), "state": p.get("state")}
             for p in ctx.coll("done.json")[-5:]],
        "本轮有效 PR 数量": int(c.get("prs_valid_today", 0)),
        "PR 目标": 2,
        "上次巡检时间": stamp(st.get("last_tick_at")) if st.get("last_tick_at") else "never",
        "最近异常与恢复结果": (st.get("last_recovery") or "无"),
        "待处理阻塞": [b.get("task") for b in ctx.coll("blocked.json")[-5:]],
        "待我授权": [t.get("id") for t in ctx.coll("candidates.json")
                    if t.get("policy") == "needs_approval" and not t.get("approved")][:8],
        "下一次巡检与调度状态": {
            "next": st.get("next") or "tick",
            "idle_reason": st.get("idle_reason"),
            "idle_ticks": c.get("idle_ticks", 0),
            "cadence_s": ctx.cap("watch_interval_s"),
            "wake": "watch（前台循环）或 定时任务跑 tick",
        },
        "成本闸": {"当日 jobs": c.get("jobs_today", 0), "上限": ctx.cap("nightly_max_jobs"),
                   "当日轮数": c.get("turns_today", 0), "上限": ctx.cap("max_turns_per_day"),
                   "当日 PR": c.get("pr_today", 0), "上限": ctx.cap("pr_max_per_day")},
    }
    return rec


def cmd_round(args):
    """One command a supervisor runs before every development round: the state
    record to restore from, the protocol to inject, and the exact next commands.
    Reads files, never the chat history — so a compacted or brand-new instance
    resumes correctly (§二.6 / §十)."""
    ctx = Ctx(args.repo)
    ctx.need_init()
    ctx.state = read_json(ctx.state_path) or ctx.state
    pp = protocol_path(ctx)
    if args.json:
        log(json.dumps(state_record(ctx), ensure_ascii=False, indent=2))
        return 0
    lines = ["# AutoLoop 轮前状态 %s（%s）" % (stamp(), ctx.remote_name() or ctx.root)]
    lines.append("")
    lines.append("1. 注入监督协议：%s%s" % (pp or "PROTOCOL.md MISSING — 重新 clone 插件仓库",
                                          "（sha256 %s…）" % sha256_file(pp)[:12] if pp else ""))
    lines.append("2. 用下面的状态记录恢复监督，不要相信旧对话里的记忆：")
    rec = state_record(ctx)
    for key in ("目标仓库地址", "基分支", "运行中的 Agent 和任务", "各 Agent 的分支与工作目录",
                "本轮有效 PR 数量", "上次巡检时间", "最近异常与恢复结果", "待处理阻塞",
                "待我授权", "下一次巡检与调度状态", "成本闸"):
        val = rec.get(key)
        if isinstance(val, (dict, list)) and not val:
            val = "-"
        lines.append("   - %s：%s" % (key, json.dumps(val, ensure_ascii=False)
                                     if isinstance(val, (dict, list)) else val))
    lines.append("3. 本轮动作：%s" % (rec["下一次巡检与调度状态"]["next"]))
    lines.append("4. 巡检用 `report --patrol`（§七 模板），别自己组织语言；"
                 "不要汇报每个 agent 在开发什么功能。")
    lines.append("5. 交付由 %s 负责开 PR（改 .autoloop/config.json 的 delivery.owner 切换）。"
                 % delivery_owner(ctx))
    log("\n".join(lines))
    return 0


# ----------------------------------------------------------- preflight (§二)


def cmd_preflight(args):
    """Everything that has to be true before a loop can deliver a PR, checked in
    one read-only pass: identity, gh, origin, the worker CLI, the skill install.
    Prints the exact command to fix each gap and never runs it (§八 高风险)."""
    root = Ctx._find_root(args.repo or os.getcwd())
    requested = args.repo or os.getcwd()
    checks = []

    def add(name, ok, detail, fix, level="required"):
        checks.append({"name": name, "ok": bool(ok), "detail": (detail or "")[:220],
                       "fix": fix or "", "level": level})

    add("git", bool(which("git")), which("git") or "missing",
        "apt-get install -y git")
    rc, name, _ = run("git config --get user.name", cwd=root or os.getcwd(), shell=True)
    rc2, mail, _ = run("git config --get user.email", cwd=root or os.getcwd(), shell=True)
    have_id = (rc == 0 and name.strip()) or os.environ.get("GIT_AUTHOR_NAME")
    add("git identity", have_id, "user.name=%s user.email=%s" % (name.strip() or "-",
                                                                 mail.strip() or "-"),
        'git config --global user.name "KEQingFeng" && git config --global '
        'user.email "you@example.com"')
    add("repository", bool(root), root or "不是 git 仓库: %s" % requested,
        "git clone <your repo> && cd into it，或 git init")
    ctx = Ctx(root) if root else None
    if ctx:
        add("origin remote", has_origin(ctx), ctx.remote_name() or "none",
            "git remote add origin git@github.com:<owner>/<repo>.git")
        if has_origin(ctx):
            rc, out, err = git("ls-remote --heads origin", cwd=ctx.root, timeout=40)
            reachable = rc == 0 and bool((out or "").strip())
            add("origin reachable", reachable, (out or err)[:200] if not reachable
                else "%d branch(es) on origin" % len((out or "").splitlines()),
                "ssh -T git@github.com 先看认证；无 key 就 ssh-keygen -t ed25519 "
                "-C you@example.com -f ~/.ssh/id_ed25519_github 再把公钥加到 GitHub "
                "Settings→SSH keys（或改用 gh auth 后的 https）", level="required")
            add("base branch on origin", any(ctx.base in l for l in (out or "").splitlines()),
                "expecting %s" % ctx.base,
                "空仓库先推一次：git push -u origin %s" % ctx.base, level="warn")
        # probe the gh this box actually uses: AUTOLOOP_GH may point at a wrapper or a
        # stand-in, and `which("gh")` would report "not installed" on a box that works.
        rc, out, err = run(gh_bin() + " auth status", cwd=ctx.root, shell=True, timeout=30)
        blob = (out or "") + (err or "")
        absent = rc == 127 or "command not found" in blob
        add("gh CLI", not absent, ("%s ← %s" % (blob.splitlines()[0][:120] if blob else "ok",
                                                 gh_bin())) if not absent
            else "%s 不存在" % gh_bin(),
            "无 gh 也能跑：worker 模式改为 delivery.owner=supervisor 并手开 PR，"
            "或按 README 安装 gh", level="pr")
        add("gh auth", rc == 0 and not absent,
            "logged in" if rc == 0 else (blob[:200] or "gh 不可用"),
            "gh auth login --scopes repo（交互式，需要你本人做）"
            if not absent else "先装 gh，再 gh auth login", level="pr")
    add("worker CLI", bool(runner_cmd(ctx)) if ctx else False,
        (runner_cmd(ctx) or "unset")[:200] if ctx else "no repo",
        'init --runner \'dsh --profile headless --json < {prompt_file}\'')
    cli = runner_cli(ctx) if ctx else ""
    if cli and cli in WORKER_CLIS:
        spec = WORKER_CLIS[cli]
        p = probe_worker_cli(cli)
        add("%s headless 接口" % cli, p["ok"], p["verdict"],
            ("本机 %s 版本可能不同：用 `AUTOLOOP_WORKER_CMD` 换成实测可用的一条，"
             "README 的 runner 表有候选" % cli) if not p["unproven"] else
            ("由你亲手跑一次 `%s`（冷启动可能要一分钟）；确认能出 help 再回来重跑 preflight，"
             "这一步不是 GitHub 缺口，循环可以先只出分支" % spec["probe"]),
            level="warn")
        add("%s 已登录模型端点" % cli, bool(os.environ.get("DSH_HOME") or
            os.path.exists(os.path.expanduser("~/.dsh"))), "~/.dsh 存在"
            if os.path.exists(os.path.expanduser("~/.dsh")) else "未见 ~/.dsh",
            "先跑一次 `dsh --profile headless \"reply ok\"` 确认 key 与 profile 可用",
            level="warn")
    here = os.path.dirname(os.path.abspath(__file__))
    skill_home = os.path.expanduser("~/.dsh/skills/grokbot-autoloop/SKILL.md")
    add("技能已装入 dsh", os.path.exists(skill_home), skill_home if os.path.exists(skill_home)
        else "未安装（源码副本在 %s/../SKILL.md）" % here,
        "cp -R %s/.. %s（不要覆盖已有同名目录）" % (here, os.path.dirname(skill_home)),
        level="warn")
    add("监督协议", bool(protocol_path()), protocol_path() or "PROTOCOL.md missing",
        "git clone https://github.com/KEQingFeng/grokbot-autoloop", level="warn")
    if ctx:
        add(".autoloop 已初始化", os.path.exists(os.path.join(ctx.alo, "state.json")),
            ctx.alo if os.path.isdir(ctx.alo) else "missing",
            "python3 %s init --test-cmd '<本仓库真实测试命令>'" % os.path.join(here, "autoloop.py"),
            level="warn")
        det = (read_json(os.path.join(ctx.alo, "config.json"), {}) or {}).get("verify", {}) \
            if os.path.isdir(ctx.alo) else {}
        add("闸门有可跑的命令", bool(det.get("test")),
            "test=%s build=%s" % (det.get("test") or "-", det.get("build") or "-"),
            "init --test-cmd '…'，否则任何 diff 都不会变绿", level="warn")
        add("磁盘余量", free_disk_mb(ctx.root) >= ctx.cap("min_free_disk_mb"),
            "%s MB free / 需要 %s" % (free_disk_mb(ctx.root), ctx.cap("min_free_disk_mb")),
            "清理或调低 caps.min_free_disk_mb", level="warn")
    bad = [c for c in checks if not c["ok"] and c["level"] in ("required", "pr")]
    out = {"at": stamp(), "repo": root or "", "ok": not bad,
           "checks": checks,
           "blockers": [c["name"] for c in bad],
           "next": ("可以开循环：init → suggest → tick/watch" if not bad
                    else "先修：%s" % "、".join(c["name"] for c in bad))}
    if ctx and os.path.isdir(ctx.alo):
        # Persist only inside a state directory that already exists: a preflight on a
        # repo nobody has `init`ed yet must not leave an untracked `.autoloop/` behind —
        # this command is advertised as read-only, and §八 says the supervisor does not
        # rearrange the user's checkout as a side effect of asking a question.
        write_json(os.path.join(ctx.alo, "preflight.json"),
                   {"ok": out["ok"], "at": out["at"], "blockers": out["blockers"]})
        ctx.state["last_recovery"] = "preflight: %s" % out["next"]
        ctx.save()
    if args.json:
        log(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        log("AutoLoop preflight %s" % out["at"])
        for c in checks:
            log("%s %-22s %s%s"
                % ("[ok]  " if c["ok"] else ("[!!]  " if c["level"] in ("required", "pr")
                                            else "[warn]"),
                   c["name"], c["detail"], "" if c["ok"] else "\n        → %s" % c["fix"]))
        log("")
        log("结论：%s" % out["next"])
    return 0 if not bad else 3


def cmd_watch(args):
    """Foreground supervisor loop: ticks every `--interval` seconds so a worker that
    declares 完成 gets its session closed and delivered inside about a minute,
    instead of waiting for the next cron line. Nothing here is installed or detached
    — stop it with Ctrl-C, or `stop`/`halt` writes a flag file and the next cycle
    reports and exits if `--until-stop` is on."""
    interval = max(5, int(args.interval or Ctx(args.repo).cap("watch_interval_s")))
    deadline = now() + args.minutes * 60 if args.minutes > 0 else 0
    cycles = 0
    log("watch: every %ss, %s. Ctrl-C 或 .autoloop/STOP 停止（本命令不装任何后台进程）"
        % (interval, ("for %s min" % args.minutes) if args.minutes else "until stopped"))
    while True:
        cycles += 1
        try:
            rc = cmd_tick(argparse.Namespace(repo=args.repo, quiet=True, no_ref=not args.ref))
        except SystemExit as exc:      # die() inside a tick must not kill the watch
            rc = exc.code or 2
        ctx = Ctx(args.repo)
        st = read_json(ctx.state_path) or {}
        c = st.get("counters", {})
        slots = ", ".join("%s=%s%s" % (s, (st.get("slots", {}).get(s) or {}).get("state")
                                       or "idle",
                                       "/PR#%s" % (st.get("slots", {}).get(s) or {}).get("pr")
                                       if (st.get("slots", {}).get(s) or {}).get("pr") else "")
                          for s in SLOTS)
        log("watch %s｜turn %s｜%s｜有效PR %s/2｜空转 %s｜%s"
            % (stamp(), st.get("turn"), slots, c.get("prs_valid_today", 0),
               c.get("idle_ticks", 0), st.get("next") or "-"))
        if rc not in (0,):
            log("watch: tick 退出码 %s（环境或红线问题，见上面输出）" % rc)
        if args.until_stop and (os.path.exists(os.path.join(ctx.alo, "STOP"))
                                or os.path.exists(os.path.join(ctx.alo, "HALTED"))):
            log("watch: 检测到 STOP/HALTED，退出前台循环")
            return 0
        if int(c.get("idle_ticks") or 0) >= 6 and not args.keep_spinning:
            log("watch: 连续空转 %s 轮 —— 再 tick 也不会产出 PR。停在这里，"
                 "按 §七 异常模板汇报（suggest/approve/修环境后再启动）。"
                 % c["idle_ticks"])
            return 4
        if deadline and now() >= deadline:
            log("watch: 到达 %s 分钟上限，退出（定时任务可再接棒 tick）" % args.minutes)
            return 0
        left = interval
        while left > 0:
            time.sleep(min(2, left))
            left -= 2


# --------------------------------------------------------------------- wiring


def build_parser():
    p = argparse.ArgumentParser(
        prog="autoloop.py", description=__doc__.splitlines()[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", help="path to the repository (default: cwd)")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_text, *args):
        sp = sub.add_parser(name, help=help_text)
        for arg in args:
            sp.add_argument(*arg[0], **arg[1])
        sp.set_defaults(fn=fn)

    add("doctor", cmd_doctor, "probe the box: tools, gh, cron, workers, disk, caps")
    add("init", cmd_init, "bootstrap .autoloop/ and detect the gate commands",
        (("--repo",), {"default": argparse.SUPPRESS}), (("--runner",), {"help": "headless worker CLI, "
                                            "use {prompt_file}/{worktree} placeholders"}),
        (("--mode",), {"choices": ["day", "night"]}),
        (("--test-cmd",), {}), (("--lint-cmd",), {}), (("--build-cmd",), {}),
        (("--force-gate",), {"action": "store_true",
                             "help": "regenerate verify.sh from the commands above"}))
    add("tick", cmd_tick, "one supervisor cycle: recover, reap, verify, dispatch, report",
        (("--quiet",), {"action": "store_true"}),
        (("--no-ref",), {"action": "store_true", "help": "skip the GitHub reference search"}))
    add("status", cmd_status, "slot table and counters, no side effects")
    add("report", cmd_report, "the progress report + fixed footer line",
        (("--journal",), {"action": "store_true"}),
        (("--patrol",), {"action": "store_true",
                         "help": "§七 的定时巡检/异常告警模板，用真实状态填好"}))
    add("watch", cmd_watch, "foreground loop: tick every 60s so a finished worker is "
                            "closed and delivered inside about a minute (no daemon)",
        (("--interval",), {"type": int, "help": "seconds between ticks "
                                                "(default caps.watch_interval_s)"}),
        (("--minutes",), {"type": int, "default": 0,
                          "help": "exit after this long (0 = until interrupted)"}),
        (("--ref",), {"action": "store_true", "help": "keep the GitHub reference search "
                                                      "on every cycle"}),
        (("--until-stop",), {"action": "store_true",
                             "help": "exit when .autoloop/STOP or HALTED appears"}),
        (("--keep-spinning",), {"action": "store_true",
                                "help": "do not exit on 6 idle ticks (default: exit and report)"}))
    add("round", cmd_round, "轮前注入：监督协议路径 + §十 状态记录 + 本轮动作（只读文件，不读聊天记忆）",
        (("--json",), {"action": "store_true"}))
    add("preflight", cmd_preflight,
        "开循环前的只读体检：git 身份/ssh/gh auth/origin/worker CLI/技能安装，缺什么给什么命令",
        (("--json",), {"action": "store_true"}))
    add("plan", cmd_plan, "print the task the scheduler would pick next")
    add("dispatch", cmd_dispatch, "start a specific task in a slot",
        (("task",), {"nargs": "?"}), (("--slot",), {}), (("--title",), {}),
        (("--paths",), {"action": "append"}), (("--kind",), {}),
        (("--priority",), {"type": int}), (("--accept",), {"action": "append"}),
        (("--policy",), {"choices": ["auto_apply", "needs_approval"]}))
    add("reap", cmd_reap, "job status for both slots",
        (("--tail",), {"action": "store_true"}))
    add("verify", cmd_verify, "run the gate + red-line re-check on a slot",
        (("--slot",), {}), (("--task",), {}))
    add("prbody", cmd_prbody, "print the six-section PR body with facts filled",
        (("slot",), {}), (("--auto",), {"action": "store_true"}))
    add("pr", cmd_pr, "push + open a PR, only if green and all guards pass "
                      "(the daily cap is not bypassable from here)",
        (("slot",), {}), (("--body",), {"help": "file with the final body"}),
        (("--auto-body",), {"action": "store_true"}))
    add("suggest", cmd_suggest, "self-discovery: TODO/skipped-tests/lint/doc claims → candidates",
        (("--limit",), {"type": int, "default": 5}),
        (("--lint",), {"action": "store_true"}))
    add("queue", cmd_queue, "list or add tasks",
        (("--add",), {}), (("--paths",), {"action": "append"}), (("--kind",), {}),
        (("--priority",), {"type": int}), (("--accept",), {"action": "append"}),
        (("--policy",), {"choices": ["auto_apply", "needs_approval"]}),
        (("--approve",), {"action": "store_true"}), (("--dedup",), {}))
    for name, fn in (("approve", cmd_approve), ("reject", cmd_reject)):
        sp = sub.add_parser(name, help="morning triage of needs_approval tasks")
        sp.add_argument("id", nargs="+")
        sp.add_argument("--note", default="")
        sp.set_defaults(fn=fn)
    add("budget", cmd_budget, "counters against caps")
    add("stop", cmd_stop, "soft stop: ticks refuse business work",
        (("--reason",), {}), (("--hard",), {"action": "store_true"}))
    add("go", cmd_go, "remove STOP/HALTED and clear maintenance mode",
        (("--mode",), {"choices": ["day", "night"]}))
    add("halt", cmd_halt, "hard stop file (only `resume` clears it)",
        (("--reason",), {}))
    add("resume", cmd_resume, "clear HALTED + maintenance")
    add("recover", cmd_recover, "rebuild slots from real git + gh state")
    add("sync", cmd_sync, "reconcile PR states (merged/closed → done.json)")
    add("backup", cmd_backup, "snapshot .autoloop/ (branch autoloop/state)",
        (("--push",), {"help": "remote url to push the snapshot branch to"}))
    add("ref", cmd_ref, "GitHub reference project for this round (max one search)",
        (("--clear",), {"action": "store_true"}))
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        eprint("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
