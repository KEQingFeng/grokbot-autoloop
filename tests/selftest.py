#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""End-to-end self-test for the AutoLoop supervisor.

Builds throwaway git repositories on disk, points the engine at `fake_worker.py`
(as the headless coding agent) and `fake_gh.py` (as the GitHub CLI), and drives
real ticks through it. Every assertion is about an observable effect: a file on
disk, a git ref, a PR the shim actually received, or a refusal the engine printed.

    python3 tests/selftest.py            # everything
    python3 tests/selftest.py --only red # scenarios whose name matches

Nothing here touches a real repository, a real GitHub, or your own machine state
beyond a temp directory it deletes again.
"""

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENGINE = os.path.join(ROOT, "scripts", "autoloop.py")
WORKER = os.path.join(HERE, "fake_worker.py")
FAKEGH = os.path.join(HERE, "fake_gh.py")
DSH = os.path.join(HERE, "bin", "dsh")
PROTOCOL = os.path.join(ROOT, "PROTOCOL.md")
# the adapter contract the engine was written against; `dsh --help` must print both
DSH_CMD = "dsh --profile headless --json < {prompt_file}"

# Shadow the worker CLIs for *every* scenario, not just the dsh one. `preflight` and
# `doctor` really run `dsh --profile headless --help` — that is the whole point of probing
# instead of trusting a table — and on this box that call costs 8-56 s and reports a binary
# the test does not control. With the stand-in first on PATH, the probe reads a help text
# this file owns, and the suite stays a test rather than a benchmark of someone's Node CLI.
os.environ["PATH"] = os.path.join(HERE, "bin") + os.pathsep + os.environ.get("PATH", "")

PASS = []
FAIL = []
KEEP = []


def _flat(detail):
    text = str(detail).replace("\n", " ⏎ ")
    # a traceback is only useful at the end; a wall of log lines at the front
    if "Traceback" in text:
        i = text.index("Traceback")
        return "…" + text[i:]
    return text[:400]


def check(cond, label, detail=""):
    if cond:
        PASS.append(label)
        return True
    FAIL.append("%s  ⟵ %s" % (label, _flat(detail)))
    print("  FAIL  %s\n        %s" % (label, _flat(detail)))
    return False


def head(title):
    print("\n── %s" % title)


def read_text(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


# --------------------------------------------------------------- fixture repo

SRC_APP = """# the app under test
def add(a, b):
    return a + b  # TODO: handle overflow


def sub(a, b):
    return a - b  # FIXME: negative results untested
"""

TEST_APP = """from src.app import add


def test_add():
    assert add(1, 2) == 3
"""

RUN_SH = """#!/usr/bin/env bash
# minimal stand-in for the repo's real test command
set -uo pipefail
if [ -e src/BROKEN ]; then echo "BROKEN marker present"; exit 1; fi
python3 - <<'PY'
import ast, glob, sys
bad = []
for path in glob.glob('src/**/*.py', recursive=True):
    try:
        ast.parse(open(path, encoding='utf-8').read())
    except SyntaxError as exc:
        bad.append('%s: %s' % (path, exc))
if bad:
    print('\\n'.join(bad))
    sys.exit(1)
print('syntax ok')
PY
"""

AGENTS_MD = """# Development guide

- Run `bash tests/run.sh` before finishing.
- Keep changes small and inside the task's declared paths.
"""

README_MD = """# Demo app

roadmap: 支持导出 PDF 与批量转换
"""


class Box:
    """One throwaway repository + initialised .autoloop/ control directory."""

    counter = 0

    def __init__(self, name, reference=False, caps=None, base_branch=None,
                 init_args=(), test_cmd="bash tests/run.sh", owner="supervisor"):
        Box.counter += 1
        raw = tempfile.mkdtemp(prefix="autoloop-%s-%d-" % (re.sub(r"\W+", "", name),
                                                           Box.counter))
        self.dir = os.path.realpath(raw)
        self.name = name
        self.origin = os.path.join(self.dir, "origin.git")
        self.repo = os.path.join(self.dir, "work")
        os.makedirs(self.repo)
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": self.dir,
            "GIT_AUTHOR_NAME": "seed", "GIT_AUTHOR_EMAIL": "seed@example.com",
            "GIT_COMMITTER_NAME": "seed", "GIT_COMMITTER_EMAIL": "seed@example.com",
            "AUTOLOOP_GH": "%s %s" % (sys.executable, FAKEGH),
            "FAKE_GH_STATE": os.path.join(self.dir, "ghstate.json"),
            "AUTOLOOP_WORKER_CMD": "%s %s" % (sys.executable, WORKER),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self._seed_repo()
        self.gh_state({"prs": [], "next": 1, "auth": True, "search": "ok", "calls": []})
        # test_cmd=None builds a box whose gate has nothing to run — that is a
        # scenario, not the default
        init = ["init"]
        if test_cmd:
            init += ["--test-cmd", test_cmd]
        init += list(init_args)
        rc, out = self.run(*init)
        if rc != 0:
            raise RuntimeError("init failed for %s: %s" % (name, out))
        cfg = self.config()
        cfg["reference_enabled"] = reference
        cfg.setdefault("runner", {})["cmd"] = ""
        # the v1 suite was written when the supervisor pushed and opened every PR;
        # keep that contract under test and let the worker-delivery scenarios opt in
        cfg["delivery"] = {"owner": owner}
        self.owner = owner
        # the throwaway box is on a real disk: keep the floor out of the way unless
        # the scenario is precisely about the floor
        wanted = dict(caps or {})
        wanted.setdefault("min_free_disk_mb", 1)
        cfg.setdefault("caps", {}).update(wanted)
        self.write_config(cfg)

    # -- plumbing -----------------------------------------------------------

    def git(self, *args, cwd=None, env=None):
        merged = dict(self.env)
        merged.update(env or {})
        proc = subprocess.run(["git"] + list(args), cwd=cwd or self.repo, env=merged,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True)
        return proc.returncode, proc.stdout

    def run(self, *args, **kw):
        merged = dict(self.env)
        merged.update(kw.get("env") or {})
        if kw.get("mode"):
            merged["FAKE_WORKER_MODE"] = kw["mode"]
        cmd = [sys.executable, ENGINE, "--repo", self.repo] + [str(a) for a in args]
        proc = subprocess.run(cmd, cwd=self.dir, env=merged, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, universal_newlines=True,
                              timeout=kw.get("timeout", 240))
        return proc.returncode, proc.stdout

    def tick(self, mode="good", *extra, **kw):
        rc, out = self.run("tick", *extra, mode=mode, **kw)
        if kw.get("wait", True):
            self.wait_jobs()
        return rc, out

    def wait_jobs(self, timeout=40):
        deadline = time.time() + timeout
        while time.time() < deadline:
            pending = []
            for slot in ("A", "B"):
                rec = self.state()["slots"].get(slot) or {}
                jid = rec.get("job")
                if not jid or rec.get("state") not in ("developing", "repairing"):
                    continue
                if not os.path.exists(os.path.join(self.alo, "jobs", jid + ".exit")):
                    pending.append(jid)
            if not pending:
                return True
            time.sleep(0.05)
        return False

    # -- state access --------------------------------------------------------

    @property
    def alo(self):
        return os.path.join(self.repo, ".autoloop")

    def state(self):
        with open(os.path.join(self.alo, "state.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def write_state(self, st):
        with open(os.path.join(self.alo, "state.json"), "w", encoding="utf-8") as fh:
            json.dump(st, fh, indent=2)

    def patch_state(self, **kw):
        st = self.state()
        st.update(kw)
        self.write_state(st)
        return st

    def coll(self, name):
        path = os.path.join(self.alo, name)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def write_coll(self, name, rows):
        with open(os.path.join(self.alo, name), "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2)

    def config(self):
        with open(os.path.join(self.alo, "config.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def write_config(self, cfg):
        with open(os.path.join(self.alo, "config.json"), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)

    def gh(self):
        with open(self.env["FAKE_GH_STATE"], encoding="utf-8") as fh:
            return json.load(fh)

    def gh_state(self, data):
        with open(self.env["FAKE_GH_STATE"], "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def base(self):
        rc, out = self.git("rev-parse", "--abbrev-ref", "HEAD")
        return (out or "main").strip()

    # -- fixtures ------------------------------------------------------------

    def _seed_repo(self):
        for path, body in (("src/app.py", SRC_APP), ("tests/test_app.py", TEST_APP),
                           ("tests/run.sh", RUN_SH), ("AGENTS.md", AGENTS_MD),
                           ("README.md", README_MD)):
            full = os.path.join(self.repo, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(body)
            if path.endswith(".sh"):
                os.chmod(full, 0o755)
        self.git("init", "-q")
        self.git("add", "-A")
        rc, out = self.git("commit", "-qm", "seed")
        if rc != 0:
            raise RuntimeError("seed commit failed: %s" % out)
        self.git("init", "-q", "--bare", self.origin)
        self.git("remote", "add", "origin", self.origin)
        rc, out = self.git("push", "-q", "-u", "origin", "HEAD")
        if rc != 0:
            raise RuntimeError("seed push failed: %s" % out)

    def add_task(self, title, paths, kind="edge_case", priority=5, accept=None,
                 pool="queue.json", policy=None, status="open", tid=None):
        rows = self.coll(pool)
        tid = tid or "t%02d-%s" % (len(rows) + 1, re.sub(r"\W+", "", title)[:18].lower())
        task = {"id": tid, "title": title, "paths": list(paths), "kind": kind,
                "priority": priority, "acceptance": accept if accept is not None else [],
                "policy": policy or ("auto_apply" if kind == "edge_case" else None),
                "status": status, "deduplication": "self-test fixture"}
        if task["policy"] is None:
            del task["policy"]
        rows.append(task)
        self.write_coll(pool, rows)
        return tid

    def cleanup(self):
        self.kill_jobs()
        shutil.rmtree(self.dir, ignore_errors=True)

    def kill_jobs(self):
        """The 空转 cases keep their worker alive for a minute on purpose; a scenario
        that stops mid-run must not leave that process behind for the next one."""
        jobs = os.path.join(self.alo, "jobs")
        if not os.path.isdir(jobs):
            return
        for name in sorted(os.listdir(jobs)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(jobs, name), encoding="utf-8") as fh:
                    pid = int(json.load(fh).get("pid") or 0)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if pid:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    pass

    def pid_alive(self, pid):
        try:
            os.kill(int(pid), 0)
        except OSError:
            return False
        return True


# ------------------------------------------------------- shared PR/merge fixtures
#
# `review` and `merge` both need the same three things: a real branch on a real
# remote, a `gh pr list` row describing it, and a record that *this box* verified
# it. They live here so the shape of a PR row cannot drift between the scenarios,
# and so neither scenario can pass by seeding a conclusion the engine never wrote.

GREEN_CI = [{"name": "tests", "conclusion": "SUCCESS"}]
RED_CI = [{"name": "tests", "conclusion": "FAILURE"}]


def push_branch(box, branch, files):
    """Commit `files` onto a new branch off origin/<base> and push it."""
    base = box.base()
    box.git("checkout", "-q", "-B", branch, "origin/" + base)
    for path, body in files.items():
        full = os.path.join(box.repo, path)
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(body)
    box.git("add", "-A")
    box.git("commit", "-qm", "work on " + branch)
    rc, out = box.git("push", "-q", "-u", "origin", branch)
    check(rc == 0, "pushed %s" % branch, out)
    box.git("checkout", "-q", base)
    return rc == 0


def pr_row(base, num, branch, created_h_ago, mergeable="MERGEABLE", ci=None,
           state="OPEN", draft=False, base_ref=None):
    """One `gh pr list --json …` row, shaped like the real CLI's."""
    when = time.time() - created_h_ago * 3600
    row = {"number": num, "headRefName": branch,
           "baseRefName": base_ref or base, "state": state,
           "title": "autoloop: " + branch,
           "url": "https://github.com/fake/repo/pull/%s" % num,
           "isDraft": draft, "mergeable": mergeable,
           "mergeStateStatus": "CLEAN" if mergeable == "MERGEABLE" else "DIRTY",
           "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when)),
           "additions": 5, "deletions": 1, "changedFiles": 1, "labels": []}
    if ci is not None:
        row["statusCheckRollup"] = ci
    return row


def seed_gate(box, branch, task_id, verdict="green"):
    """Append a gate event naming the SHA origin really has for `branch`.

    `review` refuses to auto-merge a branch this box never verified. A scenario
    that wants a mergeable PR therefore has to produce that record the way the
    engine does — an event pointing at a real commit — not a config flag."""
    _, sha = box.git("rev-parse", "origin/" + branch)
    sha = (sha or "").strip()
    path = os.path.join(box.alo, "events.jsonl")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts": "seed", "epoch": int(time.time()),
            "event_id": "seed-%s-%s" % (branch, verdict), "phase": "verify",
            "kind": "gate_green" if verdict == "green" else "gate_red",
            "task_id": task_id, "branch": branch, "sha": sha,
            "red": [] if verdict == "green" else ["seeded red for the self-test"],
        }, ensure_ascii=False) + "\n")
    return sha


def seed_verified_task(box, tid, title, branch, pool="queue.json"):
    """A pool row shaped like one the engine left behind after a green local gate.

    Deliberately has no `task_state`: the merge is what has to write DONE, so a
    scenario can tell "the engine recorded it" from "the fixture pre-filled it"."""
    box.add_task(title, ["src/"], tid=tid, pool=pool)
    sha = seed_gate(box, branch, tid)
    rows = box.coll(pool)
    for row in rows:
        if row.get("id") == tid:
            row.update({"branch": branch, "verify": "green", "verified_sha": sha,
                        "status": "pr_open"})
    box.write_coll(pool, rows)
    return sha


def events_of(box, kind=None):
    path = os.path.join(box.alo, "events.jsonl")
    if not os.path.exists(path):
        return []
    out = []
    for line in read_text(path).strip().splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if kind is None or ev.get("kind") == kind:
            out.append(ev)
    return out


def alerts_of(box):
    """Every line of alerts.jsonl, in write order. Append-only, so this is history."""
    path = os.path.join(box.alo, "alerts.jsonl")
    if not os.path.exists(path):
        return []
    out = []
    for line in read_text(path).strip().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def open_alerts(box, *extra):
    """What `alerts --json` reports as open right now (deduped, acks applied)."""
    rc, out = box.run("alerts", "--json", *extra)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"rc": rc, "raw": out[-400:], "open": None}


def origin_sha(box, base):
    """What origin/<base> points at right now — fetched, never cached."""
    box.git("fetch", "-q", "origin")
    _, out = box.git("rev-parse", "origin/" + base)
    return (out or "").strip()


def is_ancestor(box, ref, target):
    box.git("fetch", "-q", "origin")
    rc, _ = box.git("merge-base", "--is-ancestor", ref, target)
    return rc == 0


def merge_calls(box):
    """Every `gh pr merge` the engine actually issued, in order."""
    return [c for c in box.gh()["calls"] if "pr merge" in c]


def set_caps(box, **kw):
    cfg = box.config()
    cfg.setdefault("caps", {}).update(kw)
    box.write_config(cfg)
    return cfg


def set_cfg(box, **kw):
    """Top-level config keys — the merge line's command and method live here, not in
    caps, because a cap is a budget and these are neither."""
    cfg = box.config()
    cfg.update(kw)
    box.write_config(cfg)
    return cfg


# -------------------------------------------------------------------- scenarios

def s_init_doctor():
    head("init + doctor")
    box = Box("init")
    try:
        for name in ("state.json", "queue.json", "candidates.json", "blocked.json",
                     "done.json", "policy.json", "config.json", "verify.sh"):
            check(os.path.exists(os.path.join(box.alo, name)), "init wrote %s" % name,
                  os.listdir(box.alo))
        vsh = os.path.join(box.alo, "verify.sh")
        check(os.access(vsh, os.X_OK), "verify.sh is executable")
        body = open(vsh, encoding="utf-8").read()
        check("bash tests/run.sh" in body, "gate picked up --test-cmd", body[:400])
        excl = open(os.path.join(box.repo, ".git", "info", "exclude"),
                    encoding="utf-8").read()
        check("/.autoloop/" in excl, "state excluded locally, not committed", excl)
        rc, st = box.git("status", "--porcelain")
        check(".autoloop" not in st, ".autoloop invisible to git", st)
        rc, out = box.run("doctor")
        check(rc == 0, "doctor exits 0", out)
        for probe in ("git", "gh", "worker", "disk"):
            check(probe in out.lower(), "doctor probes %s" % probe, out)
        check("install" in out.lower() and "not" in out.lower(),
              "doctor states it installs nothing itself", out)
        check("verify.sh" in out and re.search(r"sha256", out),
              "doctor fingerprints the gate", out)
    finally:
        box.cleanup()


def s_green_pr():
    head("green gate → PR, then slot released")
    box = Box("green")
    try:
        tid = box.add_task("Handle overflow in add", ["src/*"],
                           accept=["test -f src/autoloop_helper.py"])
        rc, out = box.tick(mode="good")
        check("slot A: started" in out, "dispatched into slot A", out)
        rec = box.state()["slots"]["A"]
        check(rec["state"] == "developing" and rec["branch"] == "auto/" + tid,
              "slot holds the task branch", rec)
        check(rec["port_base"] == 4100 and rec["worktree"].endswith("wt-" + tid),
              "slot got its own port + worktree", rec)
        check(box.state()["counters"]["jobs_today"] == 1, "one job counted")

        rc, out = box.tick(mode="good")
        check("VERIFY GREEN" in out, "gate + redline both clear", out)
        rec = box.state()["slots"]["A"]
        check(rec["state"] == "pr_ready" and rec["verify"] == "green", "ready for PR", rec)
        steps = " ".join(s["state"] for s in rec["verify_steps"])
        check("FAIL" not in steps and "PASS" in steps, "gate steps all pass", steps)
        check(os.path.isfile(rec["evidence"] or ""), "evidence file exists",
              rec.get("evidence"))

        rc, out = box.run("prbody", "A")
        for section in ("## 1. 改动意图", "## 2. 影响模块", "## 3. 建议重点审查",
                        "## 4. 参考的 GitHub 项目", "## 5. verify 输出摘要", "## 6. 风险分"):
            check(section in out, "PR body has %s" % section, out)
        check("不得编造" in out, "missing reference is declared, not invented", out)
        check(rec["branch"] in out and tid in out, "PR body cites the real branch")

        rc, out = box.run("pr", "A", "--auto-body")
        check(rc == 0, "pr exits 0", out)
        gh = box.gh()
        check(len(gh["prs"]) == 1, "exactly one PR opened", gh["prs"])
        pr = gh["prs"][0]
        check(pr["headRefName"] == "auto/" + tid and pr["baseRefName"] == box.base(),
              "PR is auto/* → base", pr)
        check(pr["labels"] in (["auto-ready"], ["auto-needs-review"]),
              "risk label applied", pr["labels"])
        check("Never merged automatically" in pr["body"], "body disclaims merging",
              pr["body"][-200:])
        check("pr merge" not in " ".join(gh["calls"]), "the engine never merges",
              gh["calls"])
        check(box.state()["slots"]["A"] is None, "slot released right after the PR")
        check(box.state()["counters"]["pr_today"] == 1, "PR counted")
        queued = box.coll("queue.json")[0]
        check(queued["status"] == "pr_open" and queued.get("branch"),
              "pool row tracks the open PR", queued)
        rc, out = box.git("rev-parse", "-q", "--verify", "refs/remotes/origin/auto/" + tid)
        check(rc == 0, "branch really landed on origin", out)
        rc, out = box.git("rev-parse", "-q", "--verify", "refs/remotes/origin/" + box.base())
        head_sha = box.git("rev-parse", "origin/" + box.base())[1].strip()
        rc2, pushed = box.git("rev-list", "--count", "origin/" + box.base() + ".."
                              + "auto/" + tid)
        check(int(pushed.strip() or 0) >= 1, "work is on top of base, not in it", pushed)
        check(box.git("log", "--oneline", "-1", "origin/" + box.base())[1].strip()
              .startswith(head_sha[:7]), "main never moved", head_sha)

        # merged PR → done.json, and the pool row stops being offered
        state = gh["prs"][0]
        state["state"] = "MERGED"
        box.gh_state(gh)
        rc, out = box.run("sync")
        done = box.coll("done.json")
        check(any(d.get("branch") == "auto/" + tid for d in done),
              "merged PR moved to done.json", done)
        check(box.coll("queue.json")[0]["status"] == "done",
              "pool row closed so the next round cannot repeat it",
              box.coll("queue.json")[0])
    finally:
        box.cleanup()


RED_CASES = [
    ("forbidden", ["src/*"], "forbidden path touched"),
    ("outside", ["src/*"], "outside task paths"),
    ("testbreak", ["tests/*"], "existing test changed"),
    ("secret", ["src/*"], "credential"),
    ("autoloop", ["src/*"], "forbidden path touched"),
    ("dirty", ["src/*"], "uncommitted"),
    ("failtest", ["src/*"], "gate script exit"),
    ("crash", ["src/*"], "worker process exited 7"),
]


def s_redlines():
    head("red lines refused by code, not by asking")
    for mode, paths, phrase in RED_CASES:
        box = Box("red-" + mode)
        try:
            box.add_task("Redline %s" % mode, paths)
            box.tick(mode=mode)
            rc, out = box.tick(mode=mode)
            rec = box.state()["slots"]["A"] or {}
            reasons = " | ".join(rec.get("red_reasons", []))
            check(phrase in reasons, "%s → red: %s" % (mode, phrase), reasons or out)
            check(rec.get("verify") == "red", "%s marked red" % mode, rec)
            rc2, out2 = box.run("pr", "A")
            check(rc2 != 0 and "not green" in out2,
                  "%s: PR refused without a green gate" % mode, out2)
            gh = box.gh()
            check(gh["prs"] == [], "%s: nothing was pushed as a PR" % mode, gh["prs"])
        finally:
            box.cleanup()


def s_blocked_after_rounds():
    head("two repair rounds, then blocked and the slot is freed")
    box = Box("rounds")
    try:
        tid = box.add_task("Always red", ["src/*"])
        box.tick(mode="forbidden")
        seen = []
        for _ in range(4):
            box.wait_jobs()
            rc, out = box.tick(mode="forbidden")
            rec = box.state()["slots"]["A"] or {}
            seen.append((rec.get("state"), rec.get("tries")))
        blocked = box.coll("blocked.json")
        check(len(blocked) == 1 and blocked[0]["task"] == tid,
              "blocked.json records the task", blocked)
        check("forbidden path touched" in (blocked[0]["reason"] or ""),
              "blocked reason carries the raw finding", blocked[0]["reason"])
        check(blocked[0].get("tries", 0) > 2, "repair rounds capped at 2", blocked[0])
        check(blocked[0].get("evidence") and os.path.isfile(blocked[0]["evidence"]),
              "blocked entry points at the evidence file", blocked[0].get("evidence"))
        check(box.state()["slots"]["A"] is None, "slot freed after blocking", seen)
        check(box.state()["counters"]["jobs_today"] == 3,
              "each repair round counted as a job", box.state()["counters"])
        check(box.state()["counters"]["fails_consecutive"] == 1,
              "consecutive-failure gate advanced")
    finally:
        box.cleanup()


def s_parallel_refill():
    head("two slots fill, refill after PR, third task waits")
    box = Box("parallel")
    try:
        a = box.add_task("Alpha work", ["src/alpha/*"], priority=9)
        b = box.add_task("Beta work", ["src/beta/*"], priority=8)
        c = box.add_task("Gamma work", ["src/gamma/*"], priority=7)
        rc, out = box.tick(mode="good")
        st = box.state()["slots"]
        check(st["A"] and st["B"] and st["A"]["task"] == a and st["B"]["task"] == b,
              "both slots started, highest priority first",
              {k: (v or {}).get("task") for k, v in st.items()})
        check(box.state()["counters"]["jobs_today"] == 2, "two jobs in flight")
        check(a in [t["id"] for t in box.coll("queue.json") if
                    t.get("status") == "developing"], "pool marks them developing")
        box.wait_jobs()
        rc, out = box.tick(mode="good")
        check(out.count("VERIFY GREEN") == 2, "both slots verify green", out)
        check(box.state()["slots"]["A"]["port_base"] !=
              box.state()["slots"]["B"]["port_base"], "slots use different ports")
        rc, out = box.run("pr", "A", "--auto-body")
        check(rc == 0, "PR from slot A", out)
        rc, out = box.run("pr", "B", "--auto-body")
        check(rc == 0, "PR from slot B", out)
        check(len(box.gh()["prs"]) == 2, "two PRs out, morning review decides",
              box.gh()["prs"])
        rc, out = box.tick(mode="good")
        st = box.state()["slots"]
        check(st["A"] and st["A"]["task"] == c and st["B"] is None,
              "freed slot refilled with the third task",
              {k: (v or {}).get("task") for k, v in st.items()})
    finally:
        box.cleanup()


def s_path_collision():
    head("overlapping paths never run in two slots at once")
    box = Box("collision")
    try:
        a = box.add_task("Shared area one", ["src/*"], priority=9)
        b = box.add_task("Shared area two", ["src/*"], priority=8)
        rc, out = box.tick(mode="good")
        st = box.state()["slots"]
        check(st["A"] and st["A"]["task"] == a, "first claim took slot A", st["A"])
        check(st["B"] is None, "second task did not start", st["B"])
        check("paths overlap" in out, "engine says why", out)
    finally:
        box.cleanup()


def s_pr_cap():
    head("daily PR cap: branch pushed, PR queued, never a silent extra PR")
    box = Box("cap")
    try:
        tid = box.add_task("Capped work", ["src/*"])
        env = {"AUTOLOOP_PR_MAX_PER_DAY": "0"}
        box.tick(mode="good", env=env)
        box.wait_jobs()
        rc, out = box.tick(mode="good", env=env)
        check("VERIFY GREEN" in out, "gate green", out)
        rc, out = box.run("pr", "A", "--auto-body", env=env)
        check(rc == 1, "pr returns non-zero when the cap is spent", out)
        check("cap" in out.lower(), "cap stated in the refusal", out)
        check(box.gh()["prs"] == [], "no PR created past the cap", box.gh()["prs"])
        pend = box.state()["pending_pr"]
        check(len(pend) == 1 and pend[0]["branch"] == "auto/" + tid,
              "pending_pr recorded for tomorrow", pend)
        rc, out = box.git("rev-parse", "-q", "--verify", "refs/remotes/origin/auto/" + tid)
        check(rc == 0, "the green branch was still pushed", out)
        check(box.state()["slots"]["A"] is None, "slot freed anyway")
        rc, out = box.run("report")
        check("pending_pr" in out, "report shows the queued branch", out)
    finally:
        box.cleanup()


def s_policy_and_approval():
    head("needs_approval waits for a human; night only runs the whitelist")
    box = Box("policy")
    try:
        tid = box.add_task("New exporter", ["src/*"], kind="new_feature",
                           pool="candidates.json")
        rc, out = box.tick(mode="good")
        check(box.state()["slots"]["A"] is None, "nothing started without approval")
        check("needs_approval" in out, "engine says needs_approval", out)
        rc, out = box.run("plan")
        check("needs_approval" in out, "plan explains the refusal", out)
        rc, out = box.run("approve", tid, "--note", "morning review")
        check("approved 1 task" in out, "approve reports the hit", out)
        row = box.coll("candidates.json")[0]
        check(row.get("approved") is True and row.get("triage_note") == "morning review",
              "approval is written to the pool", row)
        rc, out = box.tick(mode="good")
        check(out.count("started") == 0 or box.state()["slots"]["A"],
              "approved task can now start", out)
    finally:
        box.cleanup()

    box2 = Box("night")
    try:
        box2.add_task("Approved feature at night", ["src/*"], kind="new_feature",
                      pool="candidates.json", policy="auto_apply")
        rows = box2.coll("candidates.json")
        rows[0]["approved"] = True
        box2.write_coll("candidates.json", rows)
        rc, out = box2.run("go", "--mode", "night")
        check("night" in out, "mode forced to night", out)
        rc, out = box2.tick(mode="good")
        check(box2.state()["slots"]["A"] is None, "night refuses it even approved", out)
        check("night mode" in out, "night refusal stated", out)
        box2.add_task("Night-safe test gap", ["tests/*"], kind="add_tests")
        rc, out = box2.tick(mode="good")
        check(box2.state()["slots"]["A"] is not None,
              "night still runs whitelist kinds", out)
    finally:
        box2.cleanup()


def s_stop_halt():
    head("STOP and HALTED files stop business work")
    box = Box("stop")
    try:
        box.add_task("Will not run", ["src/*"])
        rc, out = box.run("stop", "--reason", "operator asleep")
        check("STOP" in out and rc == 0, "stop wrote the flag", out)
        rc, out = box.tick(mode="good")
        check(box.state()["slots"]["A"] is None, "STOP blocked dispatch")
        check(box.state()["idle_reason"] == "stopped", "idle_reason=stopped",
              box.state().get("idle_reason"))
        check("no business work" in out, "tick says it refused", out)
        check(box.state()["counters"]["jobs_today"] == 0, "no jobs spent while stopped")
        rc, out = box.run("go")
        check("removed STOP" in out, "go clears STOP", out)
        rc, out = box.tick(mode="good")
        check(box.state()["slots"]["A"] is not None, "work resumes after `go`")
        box.run("stop", "--hard", "--reason", "freeze")
        check(os.path.exists(os.path.join(box.alo, "HALTED")), "--hard writes HALTED")
        rc, out = box.tick(mode="good")
        check(box.state()["idle_reason"] == "stopped", "HALTED blocks too")
        rc, out = box.run("resume")
        check("HALTED removed" in out, "resume clears HALTED", out)
        check(not os.path.exists(os.path.join(box.alo, "HALTED")), "file gone")
        # the env-level kill switch is what an operator reaches for mid-incident, so
        # it has to bite in tick, not only be reported by `budget`
        rc, out = box.tick(mode="good")
        before = box.state()["counters"]["jobs_today"]
        check(before >= 1, "a normal tick does dispatch", box.state()["counters"])
        rc, out = box.tick(mode="good", env={"AUTOLOOP_HARD_STOP": "1"})
        check("AUTOLOOP_HARD_STOP" in out, "tick names the env switch it obeyed", out[-400:])
        check(box.state()["counters"]["jobs_today"] == before,
              "no job is spent under AUTOLOOP_HARD_STOP", box.state()["counters"])
        check(box.state()["idle_reason"] == "stopped", "idle_reason=stopped under the switch",
              box.state().get("idle_reason"))
    finally:
        box.cleanup()


def s_lock():
    head("one cloud box, several bots: the lock serialises them")
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import autoloop
    box = Box("lock")
    try:
        ctx = autoloop.Ctx(box.repo)
        lk = autoloop.Lock(ctx)
        held = lk.__enter__()
        check(held is not None, "test process holds the lock")
        rc, out = box.run("tick")
        check("loop.lock" in out, "second supervisor did nothing", out)
        check(box.state()["counters"]["turns_today"] == 0, "no state written by loser",
              box.state()["counters"])
        check(rc == 0, "loser exits 0, not an error for cron", rc)
        lk.__exit__(None, None, None)
        rc, out = box.run("tick")
        check("loop.lock" not in out, "lock released → tick works", out)
    finally:
        box.cleanup()


def s_reference():
    head("GitHub reference: one search per round, never invented")
    box = Box("ref", reference=True)
    try:
        box.add_task("Reference-fed work", ["src/*"])
        rc, out = box.tick(mode="good")
        ref = box.state().get("ref") or {}
        check(ref.get("repo") == "example/mature-project", "reference recorded", ref)
        check(ref.get("stars") == 4211, "stars come from the query, not memory", ref)
        tid = box.state()["slots"]["A"]["task"]
        box.wait_jobs()
        box.tick(mode="good")
        rc, body = box.run("prbody", "A")
        check("example/mature-project" in body and "4211" in body,
              "PR section 4 cites the queried repo", body)
        check("借鉴点" in body, "borrowing point left for the model to fill", body)

        # rate limit → cooldown, three hits → disabled for the day
        gh = box.gh()
        gh["search"] = "429"
        box.gh_state(gh)
        for i in range(3):
            st = box.state()
            st["gh_cooldown_until"] = 0
            box.write_state(st)
            box.tick(mode="good")
            hits = box.state()["counters"]["gh_hits_today"]
            check(hits == i + 1, "429 counted (%d)" % (i + 1), hits)
            check(box.state()["gh_cooldown_until"] > time.time(),
                  "cooldown set 15 minutes out", box.state()["gh_cooldown_until"])
        st = box.state()
        st["gh_cooldown_until"] = 0
        box.write_state(st)
        rc, out = box.tick(mode="good")
        check("disabled for the rest of today" in out,
              "three hits disable external reference for the day", out)
        rc, out = box.run("budget")
        check("gh rate hits today" in out, "budget table shows the counter", out)
    finally:
        box.cleanup()


def s_recover_worktree():
    head("worktree lost (box reset) → rebuilt from the branch, then judged")
    box = Box("recover")
    try:
        tid = box.add_task("Recoverable work", ["src/*"])
        box.tick(mode="good")
        box.wait_jobs()
        wt = box.state()["slots"]["A"]["worktree"]
        box.git("worktree", "remove", "--force", wt)
        shutil.rmtree(wt, ignore_errors=True)
        check(not os.path.isdir(wt), "worktree really gone")
        rc, out = box.run("recover")
        check("vanished" in out, "recovery noticed it", out)
        rec = box.state()["slots"]["A"]
        check(os.path.isdir(rec["worktree"]), "worktree rebuilt", rec["worktree"])
        rc, out = box.git("rev-parse", "-q", "--verify", "refs/heads/auto/" + tid)
        check(rc == 0, "local branch survived the reset", out)
        rc, out = box.tick(mode="good")
        check("VERIFY GREEN" in out, "verified the branch as it stands", out)
        check(rec["state"] in ("verifying", "pr_ready"), "state moved to verifying", rec)
    finally:
        box.cleanup()


def s_orphan_branch():
    head("stray auto/* on origin is reported, never silently adopted")
    box = Box("orphan")
    try:
        rc, out = box.git("branch", "auto/stray-branch", "origin/" + box.base())
        check(rc == 0, "stray branch created", out)
        rc, out = box.git("push", "-q", "origin", "auto/stray-branch")
        check(rc == 0, "stray branch pushed", out)
        rc, out = box.run("recover")
        check("not in a slot" in out, "engine reports it as unclaimed", out)
        check(box.state().get("orphan_branches"), "orphan list recorded",
              box.state().get("orphan_branches"))
        check(box.state()["slots"]["A"] is None and box.state()["slots"]["B"] is None,
              "no slot took it over", box.state()["slots"])
    finally:
        box.cleanup()


def s_day_rollover():
    head("day rollover: counters reset, yesterday archived, no lost history")
    box = Box("rollover")
    try:
        tid = box.add_task("Yesterday work", ["src/*"])
        box.tick(mode="good")
        box.wait_jobs()
        box.tick(mode="good")
        st = box.state()
        yday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
        st["day"] = yday
        st["counters"]["pr_today"] = 7
        st["counters"]["turns_today"] = 11
        st["pending_pr"] = [{"task": tid, "branch": "auto/" + tid}]
        box.write_state(st)
        ledger_before = _lines(os.path.join(box.alo, "ledger.jsonl"))
        rc, out = box.tick(mode="good")
        new = box.state()
        check(new["day"] == time.strftime("%Y-%m-%d"), "day advanced", new["day"])
        check(new["counters"]["pr_today"] == 0, "PR counter reset", new["counters"])
        check(new["counters"]["turns_today"] == 1, "turn counter reset", new["counters"])
        check(new["pending_pr"] == [], "pending PR queue is per-day", new["pending_pr"])
        check(os.path.exists(os.path.join(box.alo, "history", yday + ".json")),
              "yesterday snapshotted before reset", os.listdir(
                  os.path.join(box.alo, "history")))
        check(_lines(os.path.join(box.alo, "ledger.jsonl")) > ledger_before,
              "ledger appended, never rewritten")
        check(os.path.isdir(os.path.join(box.alo, "log")), "daily log dir exists")
        log_txt = open(os.path.join(box.alo, "log",
                                    time.strftime("%Y-%m-%d") + ".md"),
                       encoding="utf-8").read()
        check("day rollover" in log_txt and "## " in log_txt,
              "journal carries the rollover", log_txt[:300])
    finally:
        box.cleanup()


def s_footer_and_budget():
    head("fixed footer line + budget table")
    box = Box("footer")
    try:
        box.add_task("Nothing runnable here", ["src/*"], kind="new_feature",
                    pool="candidates.json")
        box.tick(mode="good")
        rc, out = box.run("report")
        last = out.strip().splitlines()[-1]
        pat = (r"^autoloop \d{4}-\d{2}-\d{2} \d{2}:\d{2} (day|night)｜分支 \S+ \S+｜"
               r"出PR \d+/\d+｜失败 \d+｜候选 \+\d+｜当日轮数 \d+/\d+｜下一步 \S.")
        check(re.match(pat, last), "footer matches the contract format", last)
        check("| slot | branch |" in out, "report carries the slot table", out)
        check("needs your decision" in out, "report lists what needs a human", out)
        rc, out = box.run("budget")
        for row in ("turns today", "worker jobs today", "PRs today",
                    "consecutive red", "free disk MB"):
            check(row in out, "budget row: %s" % row, out)
        check("AUTOLOOP_HARD_STOP" not in out, "no hard stop in play", out)
    finally:
        box.cleanup()


def s_suggest():
    head("self-discovery produces evidence-backed, deduplicated candidates")
    box = Box("suggest")
    try:
        rc, out = box.run("suggest", "--limit", "5")
        check(rc == 0, "suggest exits 0", out)
        cands = box.coll("candidates.json")
        check(len(cands) >= 2, "found the TODO/FIXME markers", out)
        for row in cands:
            for field in ("id", "title", "paths", "acceptance", "priority", "kind",
                          "policy", "deduplication", "evidence"):
                check(field in row, "candidate has %s" % field, row)
            check("TODO" in row["evidence"] or "FIXME" in row["evidence"]
                  or "roadmap" in row["evidence"],
                  "candidate evidence is a real line", row["evidence"])
            check(row["policy"] in ("auto_apply", "needs_approval"),
                  "policy declared on every candidate", row["policy"])
        n = len(cands)
        rc, out = box.run("suggest", "--limit", "5")
        check(len(box.coll("candidates.json")) == n,
              "second run adds no duplicates", out)
        check("nothing new" in out, "and says so plainly", out)
        check(box.state()["counters"]["cands_added"] == n, "counter matches additions")
    finally:
        box.cleanup()


def s_no_runner():
    head("no worker CLI → refuses to develop, cleans up after itself")
    box = Box("norunner")
    try:
        box.add_task("Unstaffed work", ["src/*"])
        rc, out = box.tick(mode="good", env={"AUTOLOOP_WORKER_CMD": ""})
        check("cannot dispatch" in out, "engine says it cannot dispatch", out)
        check(box.state()["idle_reason"] == "no-runner", "idle_reason=no-runner",
              box.state().get("idle_reason"))
        check(box.state()["slots"]["A"] is None, "no phantom slot left")
        rc, out = box.git("show-ref", "--verify", "--quiet", "refs/heads/auto/t01-x")
        left = [r for r in box.git("for-each-ref", "--format=%(refname:short)",
                                   "refs/heads/auto/")[1].splitlines() if r.strip()]
        check(left == [], "the half-made branch was removed", left)
        wt_left = [d for d in os.listdir(box.dir) if d.startswith("wt-")]
        check(wt_left == [], "no worktree left behind", os.listdir(box.dir))
    finally:
        box.cleanup()


def s_size_and_disk_caps():
    head("size cap and disk floor stop work mechanically")
    box = Box("size", caps={"max_business_lines": 1})
    try:
        box.add_task("Too much for one task", ["src/*"])
        box.tick(mode="good")
        box.wait_jobs()
        rc, out = box.tick(mode="good")
        rec = box.state()["slots"]["A"] or {}
        check("diff too large" in " ".join(rec.get("red_reasons", [])),
              "churn cap turns it red", rec.get("red_reasons"))
    finally:
        box.cleanup()

    box2 = Box("disk", caps={"min_free_disk_mb": 10 ** 9})
    try:
        box2.add_task("No room", ["src/*"])
        rc, out = box2.tick(mode="good")
        check("refusing to create worktrees" in out, "disk floor blocks dispatch", out)
        check(box2.state()["idle_reason"] == "no-disk", "idle_reason=no-disk")
        check(box2.state()["slots"]["A"] is None, "nothing started")
    finally:
        box2.cleanup()


def s_job_cap():
    head("nightly job cap: stop, do not lower quality to keep running")
    box = Box("jobcap")
    try:
        box.add_task("First", ["src/*"])
        env = {"AUTOLOOP_NIGHTLY_MAX_JOBS": "0"}
        rc, out = box.tick(mode="good", env=env)
        check("no new dispatch" in out, "cap stated", out)
        check(box.state()["idle_reason"] == "cap-jobs", "idle_reason=cap-jobs",
              box.state().get("idle_reason"))
        check(box.state()["slots"]["A"] is None, "no job launched at the cap")
        rc, out = box.run("budget", env=env)
        check("| 0 |" in out, "budget shows 0 used against cap 0", out)
    finally:
        box.cleanup()


def s_timeout():
    head("a worker that hangs is timed out, not waited on forever")
    box = Box("timeout", caps={"job_timeout_s": 1})
    try:
        box.add_task("Hanging worker", ["src/*"])
        env = {"FAKE_WORKER_SLEEP": "20"}
        started = time.time()
        rc, out = box.tick(mode="slow", env=env, wait=False)
        spent = time.time() - started
        check("started" in out, "job in flight", out)
        # the supervisor must never sit inside the bot's turn waiting for a worker
        check(spent < 5.0, "tick returned while the worker was still running (%.1fs)" % spent,
              out)
        time.sleep(2.0)
        rc, out = box.tick(mode="slow", env=env, wait=False)
        check("job timeout" in out or "ran past" in out or "timed out" in out,
              "timeout detected", out)
        rec = box.state()["slots"]["A"] or {}
        check(rec.get("state") in ("verifying", "repairing", "developing"),
              "slot still tracked, not abandoned", rec.get("state"))
        check("cannot be called green" in " ".join(rec.get("red_reasons", [])) or
              "was stopped" in out, "the stopped run was not judged green",
              rec.get("red_reasons"))
        check("no committed diff" in " ".join(rec.get("red_reasons", [])) or
              rec.get("state") == "repairing",
              "an empty branch cannot pass", rec)
    finally:
        box.cleanup()


def s_fail_gate():
    head("three consecutive reds → maintenance, and nothing invented")
    box = Box("failgate")
    try:
        for i in range(3):
            box.add_task("Doomed %d" % i, ["src/d%d/*" % i])
        box.tick(mode="forbidden")
        for _ in range(10):
            box.wait_jobs()
            rc, out = box.tick(mode="forbidden")
            if box.state().get("maintenance"):
                break
        check(box.state().get("maintenance") is True,
              "gate A put the loop in maintenance", box.state()["counters"])
        check("gate A" in out or "maintenance" in out, "the tick said so", out)
        before = box.state()["counters"]["jobs_today"]
        rc, out = box.tick(mode="good")
        check(box.state()["counters"]["jobs_today"] == before,
              "maintenance dispatches no business work",
              box.state()["counters"])
        check("nothing safe to do" in out or "Maintenance" in out or "maintenance" in out,
              "maintenance report present", out)
        rc, out = box.run("resume")
        check("maintenance cleared" in out or "HALTED removed" in out,
              "resume lifts the gate for a human", out)
        check(box.state().get("maintenance") is False, "flag cleared")
    finally:
        box.cleanup()


def s_ledger_and_journal():
    head("every round writes state + ledger + journal a fresh instance can read")
    box = Box("ledger")
    try:
        box.add_task("Auditable work", ["src/*"])
        box.tick(mode="good")
        box.wait_jobs()
        box.tick(mode="good")
        box.run("pr", "A", "--auto-body")
        lines = open(os.path.join(box.alo, "ledger.jsonl"),
                     encoding="utf-8").read().strip().splitlines()
        recs = []
        for line in lines:
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                check(False, "ledger line is valid JSON", line)
        check(len(recs) >= 3, "ledger has a line per event", len(recs))
        kinds = {r.get("result") for r in recs}
        check({"dispatched", "pr_open"} <= kinds, "dispatch and PR recorded", kinds)
        pr_line = [r for r in recs if r.get("result") == "pr_open"]
        check(pr_line and pr_line[0].get("pr") and pr_line[0].get("branch"),
              "the PR line carries the real number + branch", pr_line)
        check(all("ts" in r for r in recs), "every line stamped")
        journal = open(os.path.join(box.alo, "log",
                                    time.strftime("%Y-%m-%d") + ".md"),
                      encoding="utf-8").read()
        for needed in ("tick", "slot A", "VERIFY GREEN", "auto/"):
            check(needed in journal, "journal mentions %s" % needed, journal[:400])
        st = box.state()
        check(json.dumps(st, ensure_ascii=False).count("\n") < 200,
              "state stays readable", len(json.dumps(st).splitlines()))
        rc, out = box.run("status")
        check("PR today: 1/12" in out, "status counts the PR", out)
    finally:
        box.cleanup()


def s_event_timeline():
    head("事件日志 + timeline：§六 的七个问题只从 events.jsonl 回答，不调模型")
    box = Box("events")
    try:
        tid = box.add_task("Traceable work", ["src/*"])
        box.tick(mode="good")
        box.wait_jobs()
        box.tick(mode="good")
        box.run("pr", "A", "--auto-body")
        path = os.path.join(box.alo, "events.jsonl")
        check(os.path.exists(path), "events.jsonl 被写出来了", box.alo)
        raw = open(path, encoding="utf-8").read().strip().splitlines()
        evs = []
        for line in raw:
            try:
                evs.append(json.loads(line))
            except json.JSONDecodeError:
                check(False, "每行都是合法 JSON", line)
        kinds = [e.get("kind") for e in evs]
        check(len(evs) >= 3, "每个生命周期节点一行", kinds)
        for want in ("job_dispatched", "gate_green"):
            check(want in kinds, "记录了 %s" % want, kinds)
        check("pr_created" in kinds or "pr_adopted" in kinds, "记录了 PR", kinds)
        for e in evs:
            check(all(k in e for k in ("ts", "event_id", "phase", "kind")),
                  "事件含 时间戳/事件ID/阶段/类型", e)
        ids = [e["event_id"] for e in evs]
        check(len(set(ids)) == len(ids), "event_id 不重复", ids)
        disp = [e for e in evs if e["kind"] == "job_dispatched"][0]
        check(disp.get("base_ref") and disp.get("base_sha"),
              "派发前记下基线 SHA（§七）", disp)
        green = [e for e in evs if e["kind"] == "gate_green"][0]
        check(green.get("sha"), "绿灯结果绑定到具体 SHA", green)
        check(green.get("steps"), "闸门各步结果留痕", green)
        check(all(e["task_id"] == tid for e in evs if e.get("task_id")),
              "事件都挂在同一个任务 ID 上", tid)
        row = [r for r in box.coll("queue.json") if r.get("id") == tid]
        check(row and row[0].get("task_state") == "DONE",
              "任务行带八态机的规范状态", row)
        check(row and row[0].get("last_progress_at"), "任务行带最后有效进展时间", row)
        # the seven questions, answered from disk
        rc, out = box.run("timeline", tid)
        check(rc == 0, "timeline 退出 0", out[-400:])
        for label in ("何时启动", "当前停在哪一步", "最后成功操作", "最近失败原因",
                      "资源消耗", "代码留痕", "恢复应从哪继续"):
            check(label in out, "timeline 回答「%s」" % label, out[-700:])
        check(tid in out and "auto/" in out, "timeline 点名任务和分支", out[-500:])
        rc, js = box.run("timeline", tid, "--json")
        try:
            tl = json.loads(js)[0]
        except (ValueError, IndexError):
            tl = {}
            check(False, "timeline --json 可解析", js[-400:])
        check(tl.get("confidence") == "pr_open",
              "已开 PR 但未合并 → confidence=pr_open，不谎报 merged", tl.get("confidence"))
        check(tl.get("attempts") == 1 and tl.get("gate_runs") == 1,
              "派发次数/闸门次数可数", tl)
        check(tl.get("prs") and not tl.get("merged"), "PR 有、合并记录没有", tl)
        check("sync" in (tl.get("resume_from") or ""),
              "恢复建议指向先核实远端，不是直接重跑", tl.get("resume_from"))
        # §十一「持久化写入失败安全处理」: the log dying must not kill the loop.
        os.remove(path)
        os.makedirs(path)
        rc, out = box.run("recover")
        check(rc == 0, "events.jsonl 写不进去时 recover 仍然正常退出", out[-400:])
        notes = " ".join(box.state().get("notes", []))
        check("events.jsonl write failed" in notes,
              "写入失败被记成告警而不是静默吞掉", notes[-300:])
    finally:
        box.cleanup()


def s_redgate_timeline():
    head("红灯任务：timeline 报 unverified，不会把失败说成完成")
    box = Box("redevents")
    try:
        tid = box.add_task("Broken work", ["src/*"], accept=["must pass"])
        box.tick(mode="bad")
        box.wait_jobs()
        box.tick(mode="bad")
        evs = [json.loads(l) for l in
               open(os.path.join(box.alo, "events.jsonl"), encoding="utf-8")
               .read().strip().splitlines()]
        kinds = [e.get("kind") for e in evs]
        check("gate_red" in kinds, "红灯事件被记录", kinds)
        red = [e for e in evs if e["kind"] == "gate_red"][0]
        check(red.get("red"), "红灯事件带失败原因", red)
        check(red.get("tries") == 1, "红灯事件带尝试次数", red.get("tries"))
        rc, js = box.run("timeline", tid, "--json")
        tl = json.loads(js)[0] if js.strip().startswith("[") else {}
        check(tl.get("last_failure"), "timeline 给出最近失败原因", tl.get("last_failure"))
        check(tl.get("confidence") in ("unverified", "pr_open"),
              "未过闸门的任务不会被标成 verified", tl.get("confidence"))
        check("repair" in (tl.get("resume_from") or "")
              or "verify" in (tl.get("resume_from") or ""),
              "恢复建议是修复轮/复验，不是重新派发", tl.get("resume_from"))
    finally:
        box.cleanup()


def s_review_queue():
    head("PR 批量审查：脚本级分级 + 依赖排序 + 阈值/紧急触发，全程不派发、不调模型")
    box = Box("review")
    base = box.base()
    green_ci = GREEN_CI
    try:
        push_branch(box, "auto/one", {"src/one.py": "print('one')\n",
                                      "tests/test_one.py": "def test_one():\n    assert 1\n"})
        push_branch(box, "auto/two", {"src/one.py": "print('one and two')\n",
                                      "src/two.py": "print('two')\n",
                                      "tests/test_two.py": "def test_two():\n    assert 2\n"})
        push_branch(box, "auto/mig", {"migrations/0001.sql": "DROP TABLE users;\n"})
        box.git("fetch", "-q", "origin")
        # The local gate record for auto/one: green, measured on a known SHA.
        events = os.path.join(box.alo, "events.jsonl")
        seed_gate(box, "auto/one", "t01-one")
        box.gh_state({"prs": [
            pr_row(base, 1, "auto/one", 0.5, ci=green_ci),
            pr_row(base, 2, "auto/two", 3.0, ci=green_ci),
            pr_row(base, 3, "auto/mig", 0.2, ci=green_ci),
            pr_row(base, 4, "auto/gone", 9.0, state="MERGED"),
        ], "next": 5, "auth": True, "search": "ok", "calls": []})

        rc, out = box.run("review", "--json")
        check(rc == 0, "review 退出 0", out[-700:])
        res = json.loads(out)
        check(res["pending"] == 3, "只数 OPEN 的 PR（MERGED 不算待审）", res["pending"])
        check(res["reviewed"] == 3, "单批上限 5 内全部审查", res["reviewed"])
        check(res["triggered"] is True and res["trigger_reason"],
              "待审 3 ≥ 阈值 3 → 触发批量审查", res["trigger_reason"])
        by_num = {r["pr"]: r for r in res["rows"]}
        check(by_num[1]["verdict"] == "auto_merge_ok",
              "小改动 + CI 绿 + 本地闸门绿 → 可自动合并", by_num[1])
        check(by_num[1]["local_gate"] == "green" and by_num[1]["verified_sha"],
              "审查引用的是本机真实闸门记录与 SHA", by_num[1]["verified_sha"])
        check(by_num[3]["verdict"] == "hold"
              and any("protected paths" in b for b in by_num[3]["blockers"]),
              "碰 migrations/ 的 PR 被拦下，不进入自动序列", by_num[3]["blockers"])
        check(by_num[2]["verdict"] == "hold"
              and any("没有这条分支的闸门记录" in b for b in by_num[2]["blockers"]),
              "本机没验过的分支不能自动合并", by_num[2]["blockers"])
        check(2 in by_num[1]["blocked_by"] or 1 in by_num[2]["blocked_by"],
              "改同一文件的两个 PR 被识别为相互依赖",
              (by_num[1]["blocked_by"], by_num[2]["blocked_by"]))
        check(res["merge_order"] and 3 not in res["merge_order"],
              "合并顺序里没有高风险 PR", res["merge_order"])
        check(os.path.exists(os.path.join(box.alo, "review.json")),
              "结论落盘到 review.json")
        evs = [json.loads(l) for l in
               open(events, encoding="utf-8").read().strip().splitlines()]
        check(len([e for e in evs if e.get("kind") == "pr_reviewed"]) == 3,
              "每条 PR 一个独立审查事件", len(evs))
        check(box.state()["counters"]["jobs_today"] == 0,
              "review 不派发 worker —— 零模型调用",
              box.state()["counters"])
        check("merge 未启用" in box.run("review")[1],
              "默认 merge_enabled=false，结论只落盘不动主分支")

        # 紧急项提前触发：只有 1 条待审，但它已经等了 3 小时
        box.gh_state({"prs": [pr_row(base, 9, "auto/one", 3.0, ci=green_ci)],
                      "next": 10, "auth": True, "search": "ok", "calls": []})
        rc, out = box.run("review", "--json")
        res = json.loads(out)
        check(res["pending"] == 1 and res["triggered"] is True,
              "不足阈值但等待超时 → 仍然触发", res["trigger_reason"])
        check(any("3.0h" in w or "3." in w for w in res["trigger_reason"]),
              "触发原因写明是等待超时", res["trigger_reason"])

        # 不足阈值且不紧急：明确说「不为凑数延迟」，也不假装审查过了
        box.gh_state({"prs": [pr_row(base, 11, "auto/one", 0.1, ci=green_ci)],
                      "next": 12, "auth": True, "search": "ok", "calls": []})
        rc, out = box.run("review")
        check("未触发" in out and "不为凑数延迟" in out,
              "未达阈值时如实报告，不制造审查结论", out[-400:])

        # CI 红 / 冲突 / draft 一律 hold
        box.gh_state({"prs": [
            pr_row(base, 21, "auto/one", 0.1, ci=RED_CI),
            pr_row(base, 22, "auto/two", 0.1, mergeable="CONFLICTING", ci=green_ci),
        ], "next": 23, "auth": True, "search": "ok", "calls": []})
        rc, out = box.run("review", "--json")
        res = json.loads(out)
        by_num = {r["pr"]: r for r in res["rows"]}
        check(by_num[21]["ci"] == "red" and by_num[21]["verdict"] == "hold",
              "CI 红 → hold", by_num[21]["blockers"])
        check(by_num[22]["verdict"] == "hold"
              and any("conflict" in b for b in by_num[22]["blockers"]),
              "冲突 → hold", by_num[22]["blockers"])
        check(res["merge_order"] == [], "两条都被拦下时合并顺序为空", res["merge_order"])

        # gh 不可用：报缺口，不给结论
        rc, out = box.run("review", env={"FAKE_GH_STATE": "/nonexistent/ghstate.json"})
        check(rc != 0 and "cannot review" in out,
              "拿不到 PR 列表时如实报缺口，不假装通过", out[-300:])
    finally:
        box.cleanup()


def s_merge_refusal_ladder():
    head("合并的四道闸：配置开关 → dry-run → 命令行确认 → 阈值，每一道都能单独拦住")
    box = Box("mergegate", caps={"merge_threshold": 2})
    base = box.base()
    try:
        push_branch(box, "auto/alpha", {
            "src/alpha.py": "ALPHA = 1\n",
            "tests/test_alpha.py": "def test_alpha():\n    assert ALPHA == 1\n"})
        push_branch(box, "auto/beta", {
            "src/beta.py": "BETA = 2\n",
            "tests/test_beta.py": "def test_beta():\n    assert BETA == 2\n"})
        box.git("fetch", "-q", "origin")
        seed_verified_task(box, "t01-alpha", "alpha 的边界情况", "auto/alpha")
        seed_verified_task(box, "t02-beta", "beta 的边界情况", "auto/beta")
        # allow_merge is already true here on purpose: the point of this scenario is
        # that the *engine's* gates hold even when GitHub would happily say yes.
        box.gh_state({"prs": [pr_row(base, 1, "auto/alpha", 4.0, ci=GREEN_CI),
                              pr_row(base, 2, "auto/beta", 3.0, ci=GREEN_CI)],
                      "next": 3, "auth": True, "search": "ok", "calls": [],
                      "allow_merge": True})
        sha0 = origin_sha(box, base)

        rc, out = box.run("review", "--json")
        check(rc == 0, "review 先给出逐条结论", out[-400:])
        res = json.loads(out) if rc == 0 else {}
        check(res.get("merge_order") == [1, 2],
              "两条都够格自动合并，顺序按年龄不按编号", res.get("merge_order"))

        # 闸 1：caps.merge_enabled=false（默认值）——连 --yes-i-will-merge 都拦得住
        rc, out = box.run("merge", "--now", "--yes-i-will-merge")
        check(rc == 3 and "merge 未启用" in out,
              "第一道闸：默认不合并，命令行确认也不能绕过配置", out[-400:])
        check("PROTOCOL §八" in out, "拒绝理由指向用户自己写下的红线", out[-400:])
        check(not merge_calls(box), "一次 gh pr merge 都没发出去", merge_calls(box))
        check(origin_sha(box, base) == sha0, "主分支没动", sha0[:12])

        # dry-run 在关闭状态下允许：它只读，用途就是让人先看清要动哪些 PR
        rc, out = box.run("merge", "--dry-run", "--now")
        check(rc == 0 and "dry-run" in out,
              "第二道闸的例外：--dry-run 只读，关闭状态下也能先看", out[-400:])
        check("#1" in out and "#2" in out and "--admin" in out,
              "dry-run 说清楚顺序、方法，以及不会绕过分支保护", out[-600:])
        check(not merge_calls(box), "dry-run 不发 gh pr merge", merge_calls(box))
        check(origin_sha(box, base) == sha0, "dry-run 之后主分支仍然没动")
        mj = json.loads(read_text(os.path.join(box.alo, "merge.json")))
        check(mj["dry_run"] is True and len(mj["merged"]) == 2 and mj["stopped"] is None,
              "dry-run 的结果也落盘，并标明自己只是 dry-run", mj)

        # 闸 2：配置开了，但没带 --yes-i-will-merge
        set_caps(box, merge_enabled=True)
        rc, out = box.run("merge", "--now")
        check(rc == 3 and "--yes-i-will-merge" in out,
              "第三道闸：开了配置也不够，每次执行都要显式确认", out[-400:])
        check(not merge_calls(box), "缺确认时不发 gh pr merge", merge_calls(box))
        check(origin_sha(box, base) == sha0, "主分支没动")

        # 闸 3：两道开关都开了，但可合并数未到阈值，且没带 --now
        set_caps(box, merge_threshold=5)
        rc, out = box.run("merge", "--yes-i-will-merge")
        check(rc == 0 and "未到合并线阈值" in out,
              "第四道闸：阈值不够就等下一轮，不为「手上有东西可合」而动手", out[-400:])
        check(not merge_calls(box), "阈值不够时不合并", merge_calls(box))
        check(any(e.get("kind") == "merge_below_threshold" for e in events_of(box)),
              "「这轮没合」进了事件日志，不是静默跳过", events_of(box)[-2:])
        check(origin_sha(box, base) == sha0, "主分支没动")

        # 没有逐条审查结论时，两道开关都开也不合并
        os.remove(os.path.join(box.alo, "review.json"))
        rc, out = box.run("merge", "--now", "--yes-i-will-merge")
        check(rc == 1 and "先跑 `review`" in out,
              "没有审查结论 → 不合并（结论不能被跳过）", out[-400:])
        check(not merge_calls(box), "没有结论时也不发 gh pr merge", merge_calls(box))

        # --json 的拒绝也是 JSON：读它的定时任务不会把散文当成结果
        rc, out = box.run("merge", "--now", "--yes-i-will-merge", "--json")
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            payload = {}
        check(rc == 1 and payload.get("refused") is True
              and payload.get("reason") == "no_review",
              "--json 下拒绝也是可解析的 JSON，并写明原因", out[-400:])
        check(payload.get("merged") == [], "拒绝时 merged 是空的，不含任何 PR", payload)
    finally:
        box.cleanup()


def s_merge_serial():
    head("受控串行合并：真推主分支、逐条重验，审查之后变红的那条让整队停在它前面")
    box = Box("merge", caps={"merge_enabled": True, "merge_threshold": 1})
    set_cfg(box, merge_regression_cmd="bash tests/run.sh")
    base = box.base()
    try:
        push_branch(box, "auto/alpha", {
            "src/alpha.py": "def alpha():\n    return 1\n",
            "tests/test_alpha.py": "def test_alpha():\n    assert alpha() == 1\n"})
        push_branch(box, "auto/beta", {
            "src/beta.py": "def beta():\n    return 2\n",
            "tests/test_beta.py": "def test_beta():\n    assert beta() == 2\n"})
        push_branch(box, "auto/gamma", {
            "src/gamma.py": "def gamma():\n    return 3\n",
            "tests/test_gamma.py": "def test_gamma():\n    assert gamma() == 3\n"})
        box.git("fetch", "-q", "origin")
        seed_verified_task(box, "t01-alpha", "alpha 的边界情况", "auto/alpha")
        seed_verified_task(box, "t02-beta", "beta 的边界情况", "auto/beta")
        seed_verified_task(box, "t03-gamma", "gamma 的边界情况", "auto/gamma")
        # 编号与年龄反着排：#3 最旧、#1 最新，用来证明串行顺序是按依赖和风险算的
        box.gh_state({"prs": [
            pr_row(base, 1, "auto/alpha", 0.5, ci=GREEN_CI),
            pr_row(base, 2, "auto/beta", 2.0, ci=GREEN_CI),
            pr_row(base, 3, "auto/gamma", 4.0, ci=GREEN_CI),
        ], "next": 4, "auth": True, "search": "ok", "calls": [],
            "allow_merge": True})
        sha0 = origin_sha(box, base)

        rc, out = box.run("review", "--json")
        res = json.loads(out) if rc == 0 else {}
        check(res.get("merge_order") == [3, 2, 1],
              "串行顺序 = 最旧先走（#3 → #2 → #1），不按编号", res.get("merge_order"))

        # 审查跑完之后 gamma 的 CI 才出结果：红。合并线必须在动手前重读到这件事。
        st = box.gh()
        for row in st["prs"]:
            if row["number"] == 3:
                row["statusCheckRollup"] = RED_CI
        box.gh_state(st)

        rc, out = box.run("merge", "--yes-i-will-merge", "--json")
        check(rc == 1, "队里有一条不干净 → 退出码非 0，不报成功", out[-800:])
        try:
            mj = json.loads(out)
        except json.JSONDecodeError:
            mj = {}
        check([m["pr"] for m in mj.get("merged", [])] == [],
              "排在最前的 #3 就是变红那条 → 一条都没合，队列停在它前面", mj.get("merged"))
        check((mj.get("stopped") or {}).get("pr") == 3
              and (mj.get("stopped") or {}).get("stage") == "pre-merge",
              "停下的位置写明是 #3 的合并前重验", mj.get("stopped"))
        check(any("CI red" in b for b in (mj.get("stopped") or {}).get("blockers", [])),
              "拦下它的理由是 CI 红，不是「感觉不对」",
              (mj.get("stopped") or {}).get("blockers"))
        check(mj.get("remaining") == [3, 2, 1],
              "整队都还在等：后面两条不被跳过、也不被顺手合掉", mj.get("remaining"))
        check(not merge_calls(box), "一条 gh pr merge 都没发出去", merge_calls(box))
        check(origin_sha(box, base) == sha0, "主分支没动", sha0[:12])
        check(not any(e.get("kind") == "pr_merged" for e in events_of(box)),
              "没有合并就没有 pr_merged 事件", events_of(box, "pr_merged"))
        check(any(e.get("kind") == "merge_queue_stopped" for e in events_of(box)),
              "停队这件事进了事件日志", events_of(box, "merge_queue_stopped"))

        # CI 恢复绿：同一份审查结论重跑。先用 --limit 1 只做一条，剩下的必须
        # 如实报成「还在等」，而不是「没有了」。
        st = box.gh()
        for row in st["prs"]:
            if row["number"] == 3:
                row["statusCheckRollup"] = GREEN_CI
        box.gh_state(st)
        rc, out = box.run("merge", "--limit", "1", "--yes-i-will-merge", "--json")
        check(rc == 0, "全绿 → 合并成功退出 0", out[-900:])
        try:
            mj = json.loads(out)
        except json.JSONDecodeError:
            mj = {}
        check([m["pr"] for m in mj.get("merged", [])] == [3],
              "--limit 1 就只合一条，而且是队首那条", mj.get("merged"))
        check(mj.get("remaining") == [2, 1],
              "剩下两条如实报为「还在等」，没有被 --limit 藏起来", mj.get("remaining"))
        check(len(merge_calls(box)) == 1, "只发出一次 gh pr merge", merge_calls(box))
        sha_mid = origin_sha(box, base)
        check(is_ancestor(box, "origin/auto/gamma", "origin/" + base)
              and not is_ancestor(box, "origin/auto/beta", "origin/" + base),
              "只有 gamma 进了主分支，beta/alpha 还在等",
              box.git("log", "--oneline", "-4", "origin/" + base)[1])

        rc, out = box.run("merge", "--yes-i-will-merge", "--json")
        check(rc == 0, "第二轮把剩下的两条合完", out[-900:])
        try:
            mj = json.loads(out)
        except json.JSONDecodeError:
            mj = {}
        check([m["pr"] for m in mj.get("merged", [])] == [2, 1],
              "两轮合起来的顺序 == 审查给出的串行顺序 #3 → #2 → #1", mj.get("merged"))
        check(mj.get("remaining") == [], "合完之后没有剩的", mj.get("remaining"))
        sha1 = origin_sha(box, base)
        check(bool(sha1) and sha1 not in (sha0, sha_mid), "origin/%s 又前进了" % base,
              (sha_mid[:12], sha1[:12]))
        for branch in ("auto/alpha", "auto/beta", "auto/gamma"):
            check(is_ancestor(box, "origin/" + branch, "origin/" + base),
                  "%s 的提交进了主分支历史" % branch,
                  box.git("log", "--oneline", "-6", "origin/" + base)[1])
        first = (mj.get("merged") or [{}])[0]
        check(first.get("base_before") == sha_mid
              and first.get("base_after") not in (None, "", sha_mid),
              "每条记录都写了合并前后的 base SHA（可追责到具体提交）", first)
        check(all(m.get("result") == "merged" for m in mj.get("merged", [])),
              "配了 merge_regression_cmd 且回归绿 → 才敢写 merged",
              [m.get("result") for m in mj.get("merged", [])])
        check(first.get("regression_rc") == 0, "每条合并后真跑了仓库自己的回归命令", first)
        check(all("--admin" not in c and "--force" not in c for c in merge_calls(box)),
              "从不绕过分支保护：没有 --admin，没有 --force", merge_calls(box))
        check(len(merge_calls(box)) == 3, "三次合并 == 三次 gh pr merge，没有多余调用",
              merge_calls(box))
        check(all(c.endswith("--merge") for c in merge_calls(box)),
              "用的是配置里的 merge 方法（保留提交与任务 ID 的关联）", merge_calls(box))

        done = {d.get("branch"): d for d in box.coll("done.json")}
        check(all(done.get(b, {}).get("merged") is True
                  for b in ("auto/alpha", "auto/beta", "auto/gamma")),
              "三条都进 done.json", sorted(done))
        check(done.get("auto/gamma", {}).get("merge_sha") == sha_mid,
              "done.json 记的是 gamma 那一轮合并后的 base SHA（不是最后一条的）",
              done.get("auto/gamma"))
        check(done.get("auto/alpha", {}).get("merge_sha") == sha1,
              "最后合的那条记的才是 sha1", done.get("auto/alpha"))
        check(all(m.get("base_before") and m.get("base_after")
                  and m["base_before"] != m["base_after"] for m in mj.get("merged", [])),
              "每条都记了合并前后的 base SHA，且主分支真的动了",
              [(m.get("base_before"), m.get("base_after")) for m in mj.get("merged", [])])
        check(mj["merged"][0]["base_before"] == sha_mid
              and mj["merged"][-1]["base_after"] == sha1,
              "串行合并首尾相接：#2 从 gamma 之后开始，#1 结束在 sha1",
              (mj["merged"][0].get("base_before"), mj["merged"][-1].get("base_after")))
        rc, head_out = box.git("rev-parse", "HEAD")
        check((head_out or "").strip() == sha1,
              "本地检出被快进到合并后的 base —— 否则回归命令跑的是旧树，"
              "却报成新树的结论", (head_out or "").strip()[:12])
        q = {r.get("id"): r for r in box.coll("queue.json")}
        check(all(q.get(t, {}).get("status") == "merged"
                  and q.get(t, {}).get("task_state") == "DONE"
                  for t in ("t01-alpha", "t02-beta", "t03-gamma")),
              "任务池里这三条不再被当作可派发的活",
              {t: (q.get(t, {}).get("status"), q.get(t, {}).get("task_state"))
               for t in ("t01-alpha", "t02-beta", "t03-gamma")})
        evs = events_of(box, "pr_merged")
        check(len(evs) == 3 and all(e.get("phase") == "merge" for e in evs),
              "每条合并一个独立事件（timeline 可重建）", len(evs))
        check(all(e.get("sha") for e in evs), "每个合并事件都带 base SHA",
              [e.get("sha") for e in evs])
        check(box.state()["counters"]["jobs_today"] == 0,
              "merge 全程零模型调用", box.state()["counters"])
        states = sorted((p["number"], p["state"]) for p in box.gh()["prs"])
        check(states == [(1, "MERGED"), (2, "MERGED"), (3, "MERGED")],
              "GitHub 侧三条 PR 都是 MERGED", states)
        check(mj.get("remaining") == [], "合完之后没有剩的", mj.get("remaining"))

        # 拿着同一份结论重跑：重读 gh 发现三条都已 MERGED → 一条都不再合，
        # 而且这不是错误（下一轮 tick 拿到 rc=1 会以为出了事）。
        rc, out = box.run("merge", "--yes-i-will-merge", "--json")
        try:
            mj2 = json.loads(out)
        except json.JSONDecodeError:
            mj2 = {}
        check(rc == 0 and mj2.get("merged") == [],
              "重跑不再合并任何 PR：按 PR 当前状态重算，不照旧结论动手", out[-500:])
        check([s["pr"] for s in mj2.get("skipped", [])] == [3, 2, 1]
              and all(s.get("result") == "already_merged" for s in mj2.get("skipped", [])),
              "已合并的三条明确记为 already_merged，不是静默消失", mj2.get("skipped"))
        check(mj2.get("stopped") is None and mj2.get("remaining") == [],
              "没活干不报停队，也不虚报剩余", (mj2.get("stopped"), mj2.get("remaining")))
        check(len(merge_calls(box)) == 3, "重跑没有发出第四次 gh pr merge",
              merge_calls(box))
        check(origin_sha(box, base) == sha1, "重跑之后主分支停在同一个 SHA", sha1[:12])
        check(any(e.get("kind") == "pr_already_merged" for e in events_of(box)),
              "「跳过已合并」进了事件日志", events_of(box)[-2:])
        rc, out = box.run("merge", "--yes-i-will-merge")
        check(rc == 0 and "已经在主分支上" in out,
              "散文版说清这是重读 gh 确认的结论", out[-400:])

        # 全部合完之后再 review：待审为空，合并线不会凭空造出可合并的 PR
        rc, out = box.run("review", "--json")
        res = json.loads(out) if rc == 0 else {}
        check(res.get("pending") == 0 and res.get("merge_order") == [],
              "MERGED 的 PR 不再算待审，合并顺序为空", (res.get("pending"),
                                                        res.get("merge_order")))
        rc, out = box.run("merge", "--now", "--yes-i-will-merge", "--json")
        try:
            mj3 = json.loads(out)
        except json.JSONDecodeError:
            mj3 = {}
        check(rc == 1 and mj3.get("reason") == "no_review" and mj3.get("merged") == [],
              "没有审查结论就拒绝合并，不靠上一轮的旧结论动手", out[-400:])
        check(len(merge_calls(box)) == 3, "整个过程只有三次 gh pr merge", merge_calls(box))
    finally:
        box.cleanup()


def s_merge_regression_red():
    head("合并后回归红灯：如实说「已经在主分支上、我不自动回滚」，不四舍五入成成功")
    box = Box("mergered", caps={"merge_enabled": True, "merge_threshold": 1})
    set_cfg(box, merge_regression_cmd="bash tests/run.sh")
    base = box.base()
    try:
        push_branch(box, "auto/good", {
            "src/good.py": "def good():\n    return True\n",
            "tests/test_good.py": "def test_good():\n    assert good()\n"})
        # 这条分支自己的闸门是绿的（seed 的记录是真的），但合进主分支后仓库的
        # 测试跑不过 —— 正是「本地绿 ≠ 合并后绿」，回归命令存在的理由。
        push_branch(box, "auto/bad", {
            "src/bad.py": "def bad(:\n    return False\n",
            "tests/test_bad.py": "def test_bad():\n    assert bad()\n"})
        box.git("fetch", "-q", "origin")
        seed_verified_task(box, "t01-good", "good 的边界情况", "auto/good")
        seed_verified_task(box, "t02-bad", "bad 的边界情况", "auto/bad")
        box.gh_state({"prs": [
            pr_row(base, 1, "auto/good", 3.0, ci=GREEN_CI),
            pr_row(base, 2, "auto/bad", 1.0, ci=GREEN_CI),
        ], "next": 3, "auth": True, "search": "ok", "calls": [],
            "allow_merge": True})
        sha0 = origin_sha(box, base)

        rc, out = box.run("review", "--json")
        res = json.loads(out) if rc == 0 else {}
        check(res.get("merge_order") == [1, 2],
              "审查阶段两条都够格 —— 回归红灯只有合完才看得见", res.get("merge_order"))

        # 用人读的那一版跑合并：这次的输出既要如实交代「已经在主分支上」，
        # 也必须把回滚说成用户的决定。结构化的那一面从 merge.json 读。
        rc, out = box.run("merge", "--yes-i-will-merge")
        check(rc == 1, "回归红 → 退出码非 0，不报成功", out[-900:])
        check("regression_red" in out, "输出里写明这条的结果是回归红灯", out[-900:])
        check("我不自动做" in out and "已经在主分支上" in out,
              "散文版明确：回滚是用户的决定，引擎不自动做", out[-900:])
        check("已回滚" not in out and "已修复" not in out,
              "不谎称自己回滚了或修好了", out[-900:])
        check("重跑 `review`" in out, "给出下一步，而不是留一个烂摊子", out[-900:])
        mj = json.loads(read_text(os.path.join(box.alo, "merge.json")))
        results = [(m["pr"], m.get("result")) for m in mj.get("merged", [])]
        check(results == [(1, "merged"), (2, "regression_red")],
              "#1 回归绿记 merged，#2 回归红记 regression_red —— 不混为一谈", results)
        check((mj.get("stopped") or {}).get("stage") == "post-merge regression",
              "停下的位置写明是「合并后回归」", mj.get("stopped"))
        check((mj.get("stopped") or {}).get("merged_already"),
              "结构化结果里也交代了 #2 已经在主分支上", mj.get("stopped"))
        sha1 = origin_sha(box, base)
        check(sha1 != sha0, "主分支确实前进了（不假装没合）", (sha0[:12], sha1[:12]))
        check(is_ancestor(box, "origin/auto/bad", "origin/" + base),
              "那条坏改动真的在主分支上 —— 报告不能把它说成没发生",
              box.git("log", "--oneline", "-4", "origin/" + base)[1])
        check(is_ancestor(box, "origin/auto/good", "origin/" + base),
              "回归绿的那条也在主分支上，没有被一起否定",
              box.git("log", "--oneline", "-4", "origin/" + base)[1])
        second = (mj.get("merged") or [{}, {}])[-1]
        check(second.get("regression_rc") == 1 and second.get("regression_tail"),
              "回归命令的退出码和输出尾巴都留了证据", second)
        check(any(e.get("kind") == "regression_red" for e in events_of(box)),
              "regression_red 进了事件日志", events_of(box, "regression_red"))
        ev = events_of(box, "regression_red")
        check(ev and ev[0].get("pr") == 2 and ev[0].get("base_sha") == sha1,
              "事件里写明了是哪条 PR、停在哪个 SHA", ev[:1])
        check(len(events_of(box, "pr_merged")) == 2,
              "两条合并各有一个 pr_merged 事件", events_of(box, "pr_merged"))
        check(len(merge_calls(box)) == 2, "两次合并 == 两次 gh pr merge，红灯后不再往下试",
              merge_calls(box))
        done = {d.get("branch"): d for d in box.coll("done.json")}
        check(done.get("auto/bad", {}).get("result") == "regression_red",
              "done.json 里这条记的是 regression_red，不是成功", done.get("auto/bad"))
        check(done.get("auto/good", {}).get("result") == "merged",
              "回归绿的那条仍然是 merged", done.get("auto/good"))
        check(box.state()["counters"]["jobs_today"] == 0,
              "整条合并线零模型调用", box.state()["counters"])

        # 没配回归命令时：合并照做（这条线是用户显式打开的），但结论只能是「待验证」
        cfg = box.config()
        cfg["merge_regression_cmd"] = ""
        box.write_config(cfg)
        push_branch(box, "auto/later", {
            "src/later.py": "def later():\n    return 4\n",
            "tests/test_later.py": "def test_later():\n    assert later() == 4\n"})
        box.git("fetch", "-q", "origin")
        seed_verified_task(box, "t04-later", "later 的边界情况", "auto/later")
        st = box.gh()
        st["prs"] = [p for p in st["prs"] if p["state"] == "OPEN"]
        st["prs"].append(pr_row(base, 4, "auto/later", 0.4, ci=GREEN_CI))
        st["next"] = 5
        box.gh_state(st)
        rc, out = box.run("review", "--json")
        res = json.loads(out) if rc == 0 else {}
        check(res.get("merge_order") == [4], "已合并的两条不再进队列，只剩 #4",
              res.get("merge_order"))
        rc, out = box.run("merge", "--yes-i-will-merge")
        check(rc == 0, "没配回归命令也允许合并 —— 这条线是用户显式打开的", out[-700:])
        check("已合并但回归未跑" in out and "不算成功完成" in out,
              "但输出必须明说未验证、不算完成", out[-700:])
        mj2 = json.loads(read_text(os.path.join(box.alo, "merge.json")))
        check([m.get("result") for m in mj2.get("merged", [])] == ["pending_verification"],
              "结构化结果标成 pending_verification，不四舍五入成 merged",
              mj2.get("merged"))
        check((mj2.get("merged") or [{}])[0].get("note"),
              "并写明为什么是待验证", (mj2.get("merged") or [{}])[0].get("note"))
        evs = [e for e in events_of(box, "pr_merged") if e.get("pr") == 4]
        check(evs and evs[0].get("result") == "pending_verification",
              "事件里也是待验证，日志和结论一致", evs[:1])
    finally:
        box.cleanup()


def s_backup_queue():
    head("backup snapshots state on its own branch, never main")
    box = Box("backup")
    try:
        box.add_task("Backed up", ["src/*"])
        box.tick(mode="good")
        rc, out = box.run("backup")
        snap = re.search(r"snapshot at (\S+)", out)
        check(bool(snap), "backup reports its path", out)
        if snap:
            path = snap.group(1)
            branch = box.git("rev-parse", "--abbrev-ref", "HEAD", cwd=path)[1].strip()
            check(branch == "autoloop/state", "snapshot branch is autoloop/state",
                  branch)
            check(os.path.exists(os.path.join(path, "state.json")),
                  "snapshot holds the state file", os.listdir(path))
            check(box.base() not in branch, "never main")
    finally:
        box.cleanup()


def s_dispatch_by_hand():
    head("manual dispatch of a named task, with the same gates")
    box = Box("manual")
    try:
        rc, out = box.run("dispatch", "--title", "Hand-picked fix",
                          "--paths", "src/*", "--kind", "edge_case",
                          "--accept", "test -f src/autoloop_helper.py", mode="good")
        check(rc == 0 and "slot A" in out, "manual dispatch works", out)
        rec = box.state()["slots"]["A"]
        check(rec and rec["state"] == "developing", "slot occupied", rec)
        check(box.coll("queue.json"), "hand task registered in the queue",
              box.coll("queue.json"))
        rc, out = box.run("dispatch", "--slot", "A", "--title", "Second", mode="good")
        check(rc != 0 and "busy" in out, "slot A refuses a second occupant", out)
        box.wait_jobs()
        rc, out = box.tick(mode="good")
        check("VERIFY GREEN" in out, "hand task judged by the same gate", out)
    finally:
        box.cleanup()


def s_unit_rules():
    head("unit checks on the rule functions themselves")
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import autoloop
    tmp = tempfile.mkdtemp(prefix="autoloop-policy-")
    with open(os.path.join(tmp, "policy.json"), "w", encoding="utf-8") as fh:
        json.dump(autoloop._policy_template(), fh)
    pol = autoloop.read_policy(type("FakeCtx", (), {"alo": tmp})())
    check(pol["auto_apply"] and pol["needs_approval"],
          "the shipped policy template parses", pol)
    for path in ("package-lock.json", "pyproject.toml", "migrations/0001.sql",
                 ".github/workflows/ci.yml", "Dockerfile", "docker-compose.yml",
                 ".env", "id_rsa", ".autoloop/verify.sh", "requirements.txt"):
        check(bool(autoloop.is_forbidden(path, pol)), "forbidden: %s" % path,
              autoloop.is_forbidden(path, pol))
    for path in ("src/app.py", "tests/test_new.py", "docs/readme.md"):
        check(autoloop.is_forbidden(path, pol) is None, "allowed: %s" % path,
              autoloop.is_forbidden(path, pol))
    check(autoloop.is_test_path("tests/test_app.py")
          and autoloop.is_test_path("src/app.spec.ts")
          and autoloop.is_test_path("snapshots/__snapshots__/a.snap"),
          "test paths recognised")
    check(autoloop.paths_overlap(["src/*"], ["src/app.py"]), "glob vs file overlaps")
    check(autoloop.paths_overlap(["src/a/*"], ["src/a/b.py"]), "same dir overlaps")
    check(not autoloop.paths_overlap(["src/a/*"], ["src/b/*"]), "siblings do not overlap")
    check(autoloop.path_matches("src/*", "src/deep/inner.py"),
          "a `dir/*` pattern matches nested files")
    check(autoloop.SECRET_LINE_RE.search('api_key = "abc123def456ghi789"'),
          "long api-key assignment caught")
    check(autoloop.SECRET_LINE_RE.search("ghp_" + "x" * 30), "GitHub token caught")
    check(autoloop.SECRET_LINE_RE.search("xoxb-1234567890abc"), "Slack token caught")
    check(not autoloop.SECRET_LINE_RE.search("password_hint = None"),
          "obvious non-secret not flagged")
    check(autoloop.kind_policy("add_tests", pol) == "auto_apply"
          and autoloop.kind_policy("new_feature", pol) == "needs_approval"
          and autoloop.kind_policy("mystery", pol) == "needs_approval",
          "unknown kinds default to needs_approval")
    check(autoloop.is_run_noise("__pycache__/x.pyc")
          and autoloop.is_run_noise("src/__pycache__/app.cpython-314.pyc")
          and autoloop.is_run_noise(".tmp/keep/x")
          and autoloop.is_run_noise("venv/bin/python")
          and autoloop.is_run_noise("tests/.pytest_cache/v/cache"),
          "gate/test scratch is not counted as uncommitted work")
    check(not autoloop.is_run_noise("src/app.py")
          and not autoloop.is_run_noise("tests/test_new.py")
          and not autoloop.is_run_noise("myvenv/scripts.py"),
          "real source and look-alike dirs are still counted")
    check(autoloop.status_path("R  old.py -> new.py") == "new.py"
          and autoloop.status_path("?? src/app.py") == "src/app.py",
          "porcelain lines yield the path that matters")
    # §六: a credential that reaches an event field must not reach the log file.
    dirty = {"cmd": "gh auth login --with-token", "env": {"K": "ghp_" + "A" * 30},
             "list": ["xoxb-1234567890abc", "plain"], "n": 7, "ok": True}
    clean = autoloop.scrub_secrets(dirty)
    blob = json.dumps(clean)
    check("ghp_" not in blob and "xoxb-" not in blob,
          "scrub_secrets removes token shapes from nested structures", blob)
    check(clean["n"] == 7 and clean["ok"] is True and "plain" in clean["list"],
          "scrub_secrets keeps the non-secret parts intact", clean)
    check(dirty["env"]["K"].startswith("ghp_"),
          "scrub_secrets does not mutate the caller's dict", dirty["env"]["K"][:8])
    # §三: DONE must not be reachable from a running task without passing VERIFYING,
    # and UNKNOWN must never be a one-way street into success.
    check(set(autoloop.TASK_TRANSITIONS) == set(autoloop.TASK_STATES),
          "every declared state has a transition row", autoloop.TASK_STATES)
    check("DONE" not in autoloop.TASK_TRANSITIONS["RUNNING"],
          "RUNNING cannot jump straight to DONE", autoloop.TASK_TRANSITIONS["RUNNING"])
    check(autoloop.TASK_TRANSITIONS["DONE"] == (),
          "DONE is terminal", autoloop.TASK_TRANSITIONS["DONE"])
    check(autoloop.can_transition("VERIFYING", "DONE")
          and not autoloop.can_transition("QUEUED", "DONE"),
          "can_transition enforces the table")


def s_empty_gate():
    head("empty gate: a repo with nothing runnable can never be called green")
    box = Box("emptygate", test_cmd=None)
    try:
        # no --test-cmd at init and acceptance=[] on the task: every gate step is
        # [SKIP], the script exits 0, and a naive loop would open a PR on thin air
        box.add_task("Invented confidence", ["src/*"])
        box.tick(mode="good")            # dispatch only — a tick never waits
        rc, out = box.tick(mode="good")  # reap + verify
        rec = box.state()["slots"]["A"] or {}
        check("VERIFY RED" in out, "the all-[SKIP] gate is red, not green", out[-700:])
        check("no gate step executed" in " ".join(rec.get("red_reasons", [])),
              "the reason names the empty gate", rec.get("red_reasons"))
        check(box.gh()["prs"] == [], "no PR from an unchecked diff", box.gh()["prs"])
        for _ in range(4):
            rc, out = box.tick(mode="good")
            if box.coll("blocked.json"):
                break
        check(bool(box.coll("blocked.json")),
              "after the repair rounds the task is blocked, not shipped",
              (box.coll("blocked.json")[-1].get("reason") or "")[:140])
        check(box.gh()["prs"] == [], "still no PR", box.gh()["prs"])
        check(box.state()["counters"].get("fails_today", 0) > 0,
              "the failures are counted", box.state()["counters"])
    finally:
        box.cleanup()


# ------------------------------------------------------- v1.1: 空转 + 交付 + 协议


def s_protocol_text():
    head("PROTOCOL.md：协议原文出厂即完整，round 每轮把它交给监工")
    check(os.path.exists(PROTOCOL), "the protocol ships inside the plugin", PROTOCOL)
    text = read_text(PROTOCOL)
    for sec in ("一、角色与唯一职责", "二、必须先完成的初始化", "三、开发仓库与职责边界",
                "四、持续开发循环", "五、PR 交付数量要求", "六、PR 创建后的自动接续",
                "七、每小时监督与汇报", "八、异常处理与自动恢复", "九、低成本运行原则",
                "十、持久化状态与上下文恢复"):
        check(sec in text, "协议完整含「%s」" % sec, text[:160])
    for line in ("不亲自编写、修改、重构或修复项目业务代码",
                 "每轮监督周期至少争取交付 2 条有效 PR。",
                 "不得将 PR 自动合并。",
                 "暴露、打印或提交 API Key、Token、密码及其他凭据。",
                 "不能仅在提示词中声明每小时汇报，就认为后台定时执行已经建立。"):
        check(line in text, "原文逐字保留：「%s」" % line[:24], "")
    digest = hashlib.sha256(open(PROTOCOL, "rb").read()).hexdigest()
    box = Box("protocol-round", owner="worker")
    try:
        rc, out = box.run("round")
        check(rc == 0, "round runs on a fresh box", out)
        check("PROTOCOL.md" in out and digest[:12] in out,
              "round prints the path + a real sha256 prefix", out)
        check(box.state()["slots"] == {"A": None, "B": None}, "round dispatches nothing",
              box.state()["slots"])
        rc, outj = box.run("round", "--json")
        rec = json.loads(outj)
        check(rec["插件技能安装及验证状态"]["protocol_sha256"] == digest[:12],
              "the §十 record fingerprints the protocol it will inject",
              rec["插件技能安装及验证状态"])
        for key in ("目标仓库地址", "基分支", "运行中的 Agent 和任务", "本轮有效 PR 数量",
                    "PR 目标", "待我授权", "下一次巡检与调度状态", "成本闸"):
            check(key in rec, "状态记录含「%s」" % key, sorted(rec))
        check(rec["PR 目标"] == 2, "§五 的目标写进记录，不靠监工记性", rec.get("PR 目标"))
        check("cadence_s" in rec["下一次巡检与调度状态"],
              "记录写清下一次巡检的节拍，而不是「记得每小时汇报」",
              rec["下一次巡检与调度状态"])
    finally:
        box.cleanup()


def s_worker_delivers_pr():
    head("§四/§五：开发 Agent 自己建分支、开 PR；两个并行 agent 一轮交付 2 条")
    box = Box("workerpr", owner="worker")
    try:
        t1 = box.add_task("Guard the parser", ["src/a/*"],
                          accept=["test -f src/a/autoloop_helper.py"])
        t2 = box.add_task("Cover the writer", ["src/b/*"],
                          accept=["test -f src/b/autoloop_helper.py"])
        rc, out = box.tick(mode="good")
        check("slot A: started" in out and "slot B: started" in out,
              "both slots dispatched in parallel", out)
        rc, out = box.tick(mode="good")
        gh = box.gh()
        check(len(gh["prs"]) == 2, "each worker opened its own PR", gh["prs"])
        heads = sorted(p["headRefName"] for p in gh["prs"])
        check(heads == ["auto/" + t1, "auto/" + t2], "one PR per task branch", heads)
        check(all(p["baseRefName"] == box.base() for p in gh["prs"]),
              "every PR targets the real base branch", gh["prs"])
        check(all(p["body"].strip() for p in gh["prs"]),
              "the delivery report came with the PR", gh["prs"][0]["body"][:120])
        check(box.state()["slots"] == {"A": None, "B": None}, "both slots freed",
              box.state()["slots"])
        c = box.state()["counters"]
        check(c["pr_today"] == 2 and c["prs_valid_today"] == 2,
              "2 有效 PR counted for §五", c)
        lines = [json.loads(l) for l in open(os.path.join(box.alo, "ledger.jsonl"),
                                             encoding="utf-8").read().strip().splitlines()]
        prs = [l for l in lines if l.get("result") == "pr_open"]
        check(len(prs) == 2 and all(l.get("by") == "worker" for l in prs),
              "the ledger credits the worker, not the supervisor", prs)
        calls = " ".join(gh["calls"])
        for banned in ("pr merge", "pr close", "pr review"):
            check(banned not in calls, "the engine never ran `%s`" % banned, calls)
        for tid in (t1, t2):
            rc, _ = box.git("rev-parse", "-q", "--verify", "refs/remotes/origin/auto/" + tid)
            check(rc == 0, "worker pushed `%s` itself" % tid, rc)
        rc, out = box.run("report", "--patrol")
        check("- 本轮有效 PR：2 条" in out and "（已达）" in out,
              "patrol reads the same counter, not a chat memory", out)
    finally:
        box.cleanup()


def s_finish_guard():
    head("空转修复①：worker 一喊完成就结束会话，闸门与交付在同一轮跑完")
    box = Box("finish", owner="worker", caps={"finish_grace_s": 1, "job_timeout_s": 600})
    env = {"FAKE_WORKER_HANG": "120"}
    try:
        box.add_task("Declare done then sit", ["src/*"],
                     accept=["test -f src/autoloop_helper.py"])
        rc, out = box.tick(mode="hang", env=env, wait=False)
        check("slot A: started" in out, "worker launched", out)
        jid = box.state()["slots"]["A"]["job"]
        pid = int(json.loads(read_text(os.path.join(box.alo, "jobs", jid + ".json")))["pid"])
        time.sleep(2.5)
        started = time.time()
        rc, out = box.tick(mode="hang", env=env, wait=False)
        spent = time.time() - started
        check("session closed" in out, "the supervisor ended the session itself", out)
        check(spent < 20, "closed + gate + delivered in %.1fs (timeout was %ss)"
              % (spent, 600), out)
        check(not box.pid_alive(pid), "the finished worker process is really gone", pid)
        check("worker PR #" in out, "the PR the worker opened was adopted", out)
        check(box.state()["slots"]["A"] is None, "slot recycled at once",
              box.state()["slots"])
        check(box.state()["counters"]["prs_valid_today"] == 1, "counts toward the §五 quota")
        logtext = read_text(os.path.join(box.alo, "jobs", jid + ".log"))
        check("AUTOLOOP_DONE" in logtext, "the finish marker is what ended the run", logtext)
    finally:
        box.cleanup()


def s_nomarker_guard():
    head("空转修复②：提交完就沉默的 worker 照样收尾；它没开 PR 时监工兜底交付")
    box = Box("nomarker", owner="worker", caps={"finish_grace_s": 1, "job_timeout_s": 600})
    env = {"FAKE_WORKER_HANG": "120"}
    try:
        box.add_task("Commit then go mute", ["src/*"],
                     accept=["test -f src/autoloop_helper.py"])
        rc, out = box.tick(mode="nomarker", env=env, wait=False)
        check("started" in out, "worker launched", out)
        jid = box.state()["slots"]["A"]["job"]
        pid = int(json.loads(read_text(os.path.join(box.alo, "jobs", jid + ".json")))["pid"])
        time.sleep(2.5)
        started = time.time()
        rc, out = box.tick(mode="nomarker", env=env, wait=False)
        spent = time.time() - started
        check("closing anyway" in out, "committed + silent counts as finished", out)
        check(spent < 20, "no %ss wait for a protocol line nobody will print" % 600, out)
        check(not box.pid_alive(pid), "the silent session was closed", pid)
        check("VERIFY GREEN" in out, "the committed diff is judged on its merits", out)
        check("NO PR exists" in out, "engine refuses to invent a delivery", out)
        rec = box.state()["slots"]["A"] or {}
        check(rec.get("state") == "pr_ready" and rec.get("verify") == "green",
              "slot parked at pr_ready, waiting on delivery", rec)
        check("pr A" in str(rec.get("next")), "next action names the fallback", rec)
        check(box.state()["counters"]["prs_valid_today"] == 0,
              "an undelivered round does not count", box.state()["counters"])
        rc, out = box.run("pr", "A", "--auto-body", env=env)
        check(rc == 0, "the supervisor fallback delivers it", out)
        check(len(box.gh()["prs"]) == 1, "exactly one PR from the fallback", box.gh()["prs"])
        check(box.state()["counters"]["prs_valid_today"] == 1,
              "a green PR opened by the fallback still counts toward §五",
              box.state()["counters"])
    finally:
        box.cleanup()


def s_stall_guard():
    head("空转修复③：静默不产出的 worker 被停掉，不占着 slot 等一个没人答的审批")
    box = Box("stall", owner="worker",
              caps={"stall_s": 1, "finish_grace_s": 1, "job_timeout_s": 600})
    env = {"FAKE_WORKER_HANG": "120"}
    try:
        box.add_task("Spin in place", ["src/*"])
        rc, out = box.tick(mode="silent", env=env, wait=False)
        check("started" in out, "worker launched", out)
        jid = box.state()["slots"]["A"]["job"]
        pid = int(json.loads(read_text(os.path.join(box.alo, "jobs", jid + ".json")))["pid"])
        time.sleep(2.5)
        started = time.time()
        rc, out = box.tick(mode="silent", env=env, wait=False)
        spent = time.time() - started
        check("stalled → session closed" in out, "silence + no commits → stopped", out)
        check(spent < 20, "stopped in %.1fs instead of holding the slot %ss"
              % (spent, 600), out)
        check(not box.pid_alive(pid), "the spinning process was actually killed", pid)
        rec = box.state()["slots"]["A"] or {}
        reds = " ".join(str(r) for r in rec.get("red_reasons", []))
        check("went silent" in reds, "the red reason names the silence", reds)
        check("AUTOLOOP_DONE" in reds, "and names the marker it never printed", reds)
        check(rec.get("state") == "repairing", "a repair round took the slot over", rec)
        check(box.gh()["prs"] == [], "a silent run ships nothing", box.gh()["prs"])
        check(box.state()["counters"]["prs_valid_today"] == 0, "nothing counted")
        rc, out = box.run("report", "--patrol")
        check(out.startswith("【异常告警｜"), "§七 switches to the alert template", out[:60])
        check("静默" in out, "the alert reports the stall in words", out)
    finally:
        box.cleanup()


def s_badpr_unverified():
    head("§六：worker 已开 PR 但闸门是红的 —— 不合并、不关闭、只标注给人看")
    box = Box("badpr", owner="worker")
    try:
        tid = box.add_task("Broken but delivered", ["src/*"])
        rc, out = box.tick(mode="badpr")
        rc, out = box.tick(mode="badpr")
        check("未过闸门" in out and "不合并、不关闭、不覆盖" in out,
              "the red tick annotates the existing PR instead of touching it", out)
        rec = box.state()["slots"]["A"] or {}
        check(rec.get("pr") and rec.get("pr_unverified"),
              "the slot record carries PR# + why it is not verified", rec)
        check(rec.get("verify") == "red", "it is still red — annotation is not approval",
              rec.get("verify"))
        gh = box.gh()
        check(len(gh["prs"]) >= 1 and gh["prs"][0]["state"] == "OPEN",
              "the PR under review is still open", gh["prs"])
        check(all(p["headRefName"] == "auto/" + tid for p in gh["prs"]),
              "every PR on this slot is the same task branch", gh["prs"])
        calls = " ".join(gh["calls"])
        for banned in ("pr merge", "pr close"):
            check(banned not in calls, "the engine never ran `%s`" % banned, calls)
        rc, out = box.run("report", "--patrol")
        check("已建但未过监工闸门，勿合并" in out,
              "the human is told which PR must not be merged", out)
        check("异常类型" in out, "and it arrives through the alert template", out)
    finally:
        box.cleanup()


def s_dsh_adapter():
    head("DeepSeekHarness 适配：--json 事件、会话 id 落盘、修复轮 resume 同一个 agent")
    box = Box("dsh", owner="worker")
    env = {"PATH": os.path.dirname(DSH) + os.pathsep + os.environ.get("PATH", ""),
           "AUTOLOOP_WORKER_CMD": DSH_CMD,
           "AUTOLOOP_DSH_CALLS": os.path.join(box.dir, "dsh-calls.txt")}
    try:
        rc, out = box.run("preflight", "--json", env=env)
        data = json.loads(out)
        by_name = {c["name"]: c for c in data["checks"]}
        check(rc == 0, "preflight passes on a wired box", out[:300])
        check("dsh headless 接口" in by_name, "preflight probes the runner that is set",
              sorted(by_name))
        check(by_name.get("dsh headless 接口", {}).get("ok") is True,
              "the probe found --json and --session-id", by_name.get("dsh headless 接口"))
        box.add_task("Resume the same agent", ["src/*"],
                     accept=["test -f src/autoloop_helper.py"])
        rc, out = box.tick(mode="good", env=env)
        jid = box.state()["slots"]["A"]["job"]
        rc, out = box.tick(mode="good", env=env)
        meta = json.loads(read_text(os.path.join(box.alo, "jobs", jid + ".json")))
        check(meta.get("cli") == "dsh", "the engine resolved the runner as dsh", meta)
        check(meta.get("session_id") == "dsh-selftest-1",
              "a worker that finishes inside one tick still has its session captured",
              meta)
        box.add_task("Break the gate", ["src/b/*"])
        rc, out = box.tick(mode="failtest", env=env)
        rc, out = box.tick(mode="failtest", env=env)
        check("repair resumes session dsh-selftest-1" in out,
              "the repair round says it resumes the original agent", out)
        calls = read_text(os.path.join(box.dir, "dsh-calls.txt")).strip().splitlines()
        # calls[0] is the preflight probe (`--help`); the rest are real runs. Find the
        # resume by content, not by index — an extra tick would silently move it.
        dispatch = [c for c in calls if "--help" not in c]
        check(len(dispatch) >= 2, "dsh was invoked again for the repair", calls)
        resumed = [c for c in dispatch if "--session-id" in c]
        check(resumed and "dsh-selftest-1" in resumed[-1],
              "§六 由原开发 Agent 继续修复：the argv really carries the session id", dispatch)
        check(resumed and "--json" in resumed[-1] and "<" not in resumed[-1],
              "the prompt reaches dsh over stdin, never as a bare argument",
              resumed[-1] if resumed else dispatch)
        # The `<` is consumed by the shell the engine spawns, so it can only show up in
        # the wrapper script the job ran from — that is where "flag before the redirect"
        # has to be proven.
        wrappers = [os.path.join(box.alo, "jobs", n) for n in os.listdir(
            os.path.join(box.alo, "jobs"))] if os.path.isdir(
            os.path.join(box.alo, "jobs")) else []
        wrappers = sorted([p for p in wrappers if p.endswith(".sh")
                           and "--session-id" in read_text(p)], key=os.path.getmtime)
        check(bool(wrappers), "a repair wrapper carries the resume flag",
              os.listdir(os.path.join(box.alo, "jobs")))
        line = [l for l in read_text(wrappers[-1]).splitlines() if "--session-id" in l][-1]
        before, sep, after = line.partition(" < ")
        check(sep and "--session-id" in before and "<" not in after,
              "the resume flag is inserted before the stdin redirect", line[:240])

        # A probe that dies is not evidence about the CLI's flags. On the box this ships
        # from, `dsh --profile headless --help` cost 56 s cold and 8 s warm; the old 30 s
        # timeout made `doctor` print 「本机版本缺 --json」 for a flag the help text does
        # contain, and a supervisor that believes that turns --json off and loses its
        # 空转 detection for an environment accident.
        rc, out = box.run("preflight", env=dict(env, FAKE_DSH_HELP_FAIL="70"))
        check("探测失败 rc=70" in out, "a probe that exits non-zero is named by its rc",
              out[-500:])
        check("这一步不是 GitHub 缺口" in out,
              "an unproven probe tells you to run the probe by hand", out[-500:])
        check("找不到 --json" not in out and "缺 --json" not in out,
              "an unproven probe never claims the flags are absent", out[-500:])
        rc, out = box.run("preflight",
                          env=dict(env, FAKE_DSH_HELP="Usage: dsh --profile headless\n--json\n"))
        check("找不到 --session-id" in out,
              "a probe that really lacks one flag still names the right one", out[-500:])
    finally:
        box.cleanup()


def s_preflight_gaps():
    head("前置流程：GitHub 没接通时给出确切命令，而不是自己上手改环境")
    box = Box("preflight")
    try:
        box.add_task("Scheduled but untouched", ["src/*"])
        rc, out = box.run("preflight")
        check(rc == 0, "a fully wired box passes", out)
        for probe in ("git identity", "origin reachable", "gh CLI", "gh auth",
                      "worker CLI", "监督协议", "闸门有可跑的命令"):
            check(probe in out, "preflight checks %s" % probe, out)
        check(box.state()["slots"] == {"A": None, "B": None}, "preflight dispatches nothing")
        check(not os.path.isdir(os.path.join(box.alo, "jobs"))
              or os.listdir(os.path.join(box.alo, "jobs")) == [],
              "no job was launched", os.listdir(os.path.join(box.alo, "jobs")))
        calls = " ".join(box.gh()["calls"])
        check("pr create" not in calls and "pr merge" not in calls,
              "preflight only asks gh who it is — it opens nothing", calls)
        check(len(box.coll("queue.json")) == 1 and box.gh()["prs"] == [],
              "the pool and GitHub are untouched", (box.coll("queue.json"), box.gh()["prs"]))

        # A checkout nobody has `init`ed must come out of preflight exactly as it went
        # in: the command asks questions, it does not leave a state directory behind in
        # the user's repo (§八).
        fresh = os.path.join(box.dir, "fresh")
        os.makedirs(os.path.join(fresh, "src"))
        with open(os.path.join(fresh, "src", "a.py"), "w", encoding="utf-8") as fh:
            fh.write("print(1)\n")
        subprocess.run(["git", "init", "-q"], cwd=fresh, env=box.env, check=True)
        proc = subprocess.run([sys.executable, ENGINE, "--repo", fresh, "preflight"],
                              cwd=fresh, env=box.env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, universal_newlines=True,
                              timeout=120)
        check(not os.path.exists(os.path.join(fresh, ".autoloop")),
              "preflight on an un-initialised repo writes nothing into the checkout",
              sorted(os.listdir(fresh)))
        check(proc.returncode == 3 and "origin" in proc.stdout,
              "an un-connected repo is reported as a gap, not as a pass",
              proc.stdout[-260:])
        init = subprocess.run([sys.executable, ENGINE, "--repo", fresh, "init",
                               "--test-cmd", "python3 -m pytest -q"],
                              cwd=fresh, env=box.env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, universal_newlines=True,
                              timeout=120)
        check(init.returncode == 0 and os.path.exists(
                  os.path.join(fresh, ".autoloop", "state.json")),
              "so `init` is still the first thing to create the state dir",
              init.stdout[-260:])

        box.gh_state({"prs": [], "next": 1, "auth": False, "search": "ok", "calls": []})
        rc, out = box.run("preflight")
        check(rc == 3, "an unauthenticated gh exits 3", out)
        check("gh auth login" in out, "and prints the exact login command", out)
        check("先修" in out, "the verdict names the blockers", out)
        box.gh_state({"prs": [], "next": 1, "auth": True, "search": "ok", "calls": []})

        rc, out = box.run("preflight", env={"AUTOLOOP_GH": "/nonexistent/gh"})
        check(rc == 3 and "delivery.owner=supervisor" in out,
              "no gh at all → says how to run without it", out)
        rc, out = box.run("preflight", env={"AUTOLOOP_WORKER_CMD": ""})
        check(rc == 3 and "init --runner" in out,
              "no worker CLI → prints the runner line to paste", out)
        rc, out = box.run("preflight", "--json", env={"AUTOLOOP_WORKER_CMD": ""})
        data = json.loads(out)
        check(data["ok"] is False and "worker CLI" in data["blockers"],
              "--json gives the machine-readable blocker list", data["blockers"])
        gap = [c for c in data["checks"] if c["name"] == "worker CLI"][0]
        check(gap["fix"] and gap["level"] == "required", "every gap ships a fix", gap)
    finally:
        box.cleanup()


def s_watch_idle_exit():
    head("空转修复④：连续 6 轮无事可做就自己停下（软停），watch 不会空转到天亮")
    box = Box("idlespin")
    try:
        out = ""
        for _ in range(6):
            rc, out = box.tick(mode="good", env={"AUTOLOOP_WORKER_CMD": ""})
        check(box.state()["counters"]["idle_ticks"] == 6, "six idle ticks counted",
              box.state()["counters"])
        check("空转 6 轮" in out, "the tick says more ticking will not help", out)
        check("别再 tick" in str(box.state().get("next")), "next action is not `tick`",
              box.state().get("next"))
        # #12: 无进展熔断的循环档 —— 到 idle_gate 就不只是「说一句」，而是自己软停
        stop = os.path.join(box.alo, "STOP")
        check(os.path.exists(stop),
              "空转到阈值自己写 STOP：熔断要真的断开，不能只在日志里劝", out[-300:])
        check("idle" in (box.state().get("breakers") or {}),
              "state 记着是哪一档熔断、什么时候断的", box.state().get("breakers"))
        check(any(e.get("kind") == "idle_breaker" for e in events_of(box)),
              "断开是事件，不只是文件", events_of(box)[-3:])
        check(any(a.get("kind") == "idle_breaker" for a in alerts_of(box)),
              "并且发了结构化告警", alerts_of(box))
        check(read_text(stop).strip() != "", "STOP 里写了是谁写的、为什么", read_text(stop))
        rc, out = box.run("watch", "--interval", "5", env={"AUTOLOOP_WORKER_CMD": ""})
        check(rc == 4, "watch stops instead of burning the nightly budget", out)
        check("连续空转" in out or "熔断" in out, "watch says why it stopped", out)
        rc, out = box.run("report", "--patrol")
        check("空转" in out, "the patrol reports the spin", out)
        check("熔断" in out, "巡检说得出这是熔断停的，不是人工停的", out[:400])
        box.add_task("Something real", ["src/*"])
        rc, out = box.tick(mode="good")
        check("started" not in out,
              "补了任务也不会自动复工 —— 熔断要人来合闸，否则它就不叫熔断", out[-300:])
        rc, out = box.run("go", "--reason", "self-test")
        check(not os.path.exists(stop) and box.state().get("breakers") == {},
              "`go` 清 STOP 也清熔断记录", box.state().get("breakers"))
        rc, out = box.tick(mode="good")
        check("started" in out, "a real task dispatches again", out)
        check(box.state()["counters"]["idle_ticks"] == 0, "a productive tick resets the count",
              box.state()["counters"])
    finally:
        box.cleanup()


def s_patrol_templates():
    head("§七：两个固定模板由真实状态填好，且不含各 agent 的功能细节")
    box = Box("patrol", owner="worker")
    try:
        title = "导出 PDF 批量转换"
        box.add_task(title, ["src/*"], accept=["test -f src/autoloop_helper.py"])
        box.tick(mode="good")
        rc, out = box.tick(mode="good")
        check("worker PR #" in out, "one valid PR this round", out)
        rc, out = box.run("report", "--patrol")
        check(out.startswith("【定时巡检｜"), "a clean round uses 定时巡检", out[:60])
        for line in ("- 总体状态：正常", "- 开发循环：", "- 本轮有效 PR：1 条",
                     "- PR 目标：至少 2 条（还差 1 条）", "- 异常情况：无",
                     "- 采取的恢复措施：", "- 是否需要我介入："):
            check(line in out, "巡检模板含「%s」" % line, out)
        check(title not in out and "autoloop_helper" not in out,
              "§七 明确禁止汇报每个 agent 在开发什么功能", out)
        rc, out = box.run("halt", "--reason", "self-test")
        rc, out = box.run("report", "--patrol")
        check(out.startswith("【异常告警｜"), "a halted loop alerts instead of polling",
              out[:60])
        for line in ("- 异常类型：", "- 影响范围：", "- 已执行的恢复措施：", "- 当前状态：",
                     "- 是否需要我授权或处理：", "- 下一步监督动作："):
            check(line in out, "告警模板含「%s」" % line, out)
        check("已暂停" in out, "the halt is reflected in 当前状态", out)
        rc, out = box.run("round")
        check("report --patrol" in out, "round tells the supervisor to use the templates",
              out)
    finally:
        box.cleanup()


def s_watchdog_standalone():
    head("#12：独立看门狗 —— 循环自己死了也要有人发现，且它不碰 git/gh、不抢锁")
    box = Box("watchdog", caps={"watchdog_stale_s": 60, "stall_s": 1,
                                "finish_grace_s": 1, "job_timeout_s": 600})
    env = {"FAKE_WORKER_HANG": "120"}
    try:
        box.tick(mode="good")                      # a real heartbeat to compare against
        rc, out = box.run("watchdog", "--json")
        wj = json.loads(out)
        check(rc == 0 and wj.get("healthy") is True,
              "刚 tick 过的箱子：看门狗报健康，退出码 0（cron 拿它当探针）", out[-400:])
        check(wj.get("critical") == [] and wj.get("warn") == [],
              "健康就是没有 warn/critical，不是「大部分还行」", wj)
        hb = json.loads(read_text(os.path.join(box.alo, "watchdog.json")))
        check(hb.get("runs") == 1 and hb.get("last_run_epoch"),
              "看门狗留自己的心跳文件 —— 否则没人知道看门狗死了", hb)

        # --- the loop is dead: nobody has ticked for an hour -----------------
        box.patch_state(last_tick_at=int(time.time()) - 3600)
        rc, out = box.run("watchdog", "--json")
        wj = json.loads(out)
        check(rc == 4, "1 小时没 tick → 退出码 4（critical），监控不必解析文字", out[-300:])
        check("loop_stale" in [f.get("kind") for f in wj.get("critical", [])],
              "并且指名是循环心跳停了，不是笼统的「有问题」", wj.get("critical"))
        check(any(a.get("kind") == "loop_stale" and a.get("severity") == "critical"
                  for a in alerts_of(box)),
              "同一条结论落进结构化告警，不只打印在没人看的 stdout", alerts_of(box))
        rc, out = box.run("watchdog", "--json")
        check(rc == 4 and json.loads(read_text(os.path.join(box.alo, "watchdog.json")))["runs"] == 3,
              "重复跑不重复计数，心跳照常累加", out[-200:])

        # --- a deliberately stopped loop is stopped, not dead ----------------
        box.run("halt", "--reason", "self-test")
        rc, out = box.run("watchdog", "--json")
        kinds = [f.get("kind") for f in json.loads(out).get("critical", [])]
        check("loop_stale" not in kinds,
              "人工 HALTED 不算循环猝死 —— 否则每次你自己停机都会收到假告警", kinds)
        box.run("resume")

        # --- it must work while the loop holds the lock ----------------------
        box.patch_state(last_tick_at=int(time.time()))
        import fcntl
        lk = open(os.path.join(box.alo, "loop.lock.l"), "a+")
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            turns_before = box.state()["counters"]["turns_today"]
            rc_tick, out_tick = box.run("tick", "--quiet")
            # rc is 0 on purpose (see the `lock` scenario: a cron overlap is not an
            # error), so the precondition has to be proved by "it did nothing".
            check("loop.lock" in out_tick and rc_tick == 0
                  and box.state()["counters"]["turns_today"] == turns_before,
                  "前提成立：锁被占住时 tick 真的进不来", out_tick[-200:])
            rc, out = box.run("watchdog", "--json")
            check(rc != 2 and isinstance(json.loads(out), dict),
                  "看门狗不抢锁：循环卡死在锁里时它照样能报", out[-300:])
        finally:
            fcntl.flock(lk.fileno(), fcntl.LOCK_UN)
            lk.close()

        # --- a stuck worker: reported by default, stopped only with --act ----
        box.add_task("Spin in place", ["src/*"])
        rc, out = box.tick(mode="silent", env=env, wait=False)
        jid = box.state()["slots"]["A"]["job"]
        pid = int(json.loads(read_text(os.path.join(box.alo, "jobs", jid + ".json")))["pid"])
        time.sleep(2.5)
        gh_calls = list(box.gh()["calls"])
        base_before = origin_sha(box, "main")
        rc, out = box.run("watchdog", "--json")
        wj = json.loads(out)
        stuck = [f for f in wj.get("critical", []) if f.get("kind") == "worker_stuck"]
        check(bool(stuck), "静默卡死的 worker 被看见（哪怕这一轮根本没有 tick）",
              wj.get("critical"))
        check(stuck and stuck[0].get("slot") == "A" and stuck[0].get("pid") == pid,
              "告警里带着是哪个 slot、哪个 pid —— 不然人还得自己去翻 state", stuck)
        check(box.pid_alive(pid), "默认只报告，不动别人的进程", pid)
        rc, out = box.run("watchdog", "--act", "--json")
        check(not box.pid_alive(pid), "--act 才真的停掉卡死的进程组", pid)
        check(any(e.get("kind") == "watchdog_stopped_job" for e in events_of(box)),
              "动手就留事件，不静默改状态", events_of(box)[-3:])
        check(box.gh()["calls"] == gh_calls,
              "看门狗一次 gh 都没调：不烧配额，也不替我做任何仓库决定",
              box.gh()["calls"][len(gh_calls):])
        check(origin_sha(box, "main") == base_before and box.gh()["prs"] == [],
              "git/GitHub 侧一个字都没动", (origin_sha(box, "main"), base_before))
    finally:
        box.cleanup()


def s_no_progress_breaker():
    head("#12：无进展熔断 —— 连续几个 job 什么都没交付就硬停，不靠人盯日志")
    box = Box("noprog", owner="worker",
              caps={"no_progress_gate": 2, "max_repair_rounds": 0})
    try:
        box.add_task("Always red", ["src/*"], accept=["test -f src/never_here.py"])
        box.tick(mode="good")
        box.tick(mode="good")
        st = box.state()
        check(len(box.coll("blocked.json")) == 1, "前提成立：第一个任务被判红并阻塞",
              box.coll("blocked.json"))
        check(st["counters"].get("no_progress_streak") == 1,
              "一个什么都没交付的 job = 一次无进展（数的是交付物，不是进程死没死）",
              st["counters"])
        check(not os.path.exists(os.path.join(box.alo, "HALTED")),
              "gate=2，一次还不熔断", st["counters"])

        box.add_task("Delivers for real", ["src/*"])
        box.tick(mode="good")
        box.tick(mode="good")
        st = box.state()
        check(st["counters"].get("prs_valid_today") == 1,
              "前提成立：这一轮真的交付了一个 PR", st["counters"])
        check(st["counters"].get("no_progress_streak") == 0,
              "有交付就把连续无进展清零 —— 熔断数的是「连续」", st["counters"])

        for i in range(2):
            box.add_task("Also always red %d" % i, ["src/*"],
                         accept=["test -f src/still_never_%d.py" % i])
            box.tick(mode="good")
            box.tick(mode="good")
        st = box.state()
        halted = os.path.join(box.alo, "HALTED")
        check(os.path.exists(halted),
              "连续 2 个 job 零交付 → 熔断写硬停文件：只有 `resume` 能清", st["counters"])
        check("no_progress" in (st.get("breakers") or {}),
              "state 里记着是哪一档熔断打开的、什么时候开的", st.get("breakers"))
        trip = [e for e in events_of(box) if e.get("kind") == "no_progress_breaker"]
        check(len(trip) == 1, "熔断只发一次事件，不是每轮重复喊", len(trip))
        check(trip and trip[0].get("streak") == 2 and trip[0].get("gate") == 2,
              "事件带着连续次数与阈值", trip)
        check(any(a.get("kind") == "no_progress_breaker" and a.get("severity") == "critical"
                  for a in alerts_of(box)),
              "并且发结构化 critical 告警", alerts_of(box))
        rc, out = box.tick(mode="good")
        check(rc == 0 and "no business work" in out, "熔断后 tick 不派活", out[-300:])
        rc, out = box.run("report", "--patrol")
        check("熔断" in out, "§七 巡检模板说得出是熔断停的，不是笼统「已暂停」", out[:400])
        check(read_text(halted).strip() != "", "HALTED 文件里写了原因和时间", read_text(halted))

        box.run("resume")
        st = box.state()
        check(not os.path.exists(halted) and st.get("breakers") == {}
              and st["counters"].get("no_progress_streak") == 0,
              "`resume` 同时清文件、清熔断记录、清零连续数", st.get("breakers"))
        box.add_task("Works after resume", ["src/*"])
        rc, out = box.tick(mode="good")
        check("started" in out, "人决定继续之后，循环真的能继续", out[-300:])
    finally:
        box.cleanup()

    # 阶梯：维护模式先接住，硬停不能在同一条证据上抢跑
    box = Box("noprog2", owner="worker",
              caps={"no_progress_gate": 1, "fail_gate": 1, "max_repair_rounds": 0})
    try:
        box.add_task("Red from the start", ["src/*"], accept=["test -f src/nope.py"])
        box.tick(mode="good")
        box.tick(mode="good")
        st = box.state()
        check(st.get("maintenance") is True, "前提成立：fail_gate 先进了维护模式", st["counters"])
        check(int(st["counters"].get("no_progress_streak") or 0) >= 1,
              "前提成立：零交付也照实计数", st["counters"])
        check(not os.path.exists(os.path.join(box.alo, "HALTED")),
              "维护模式已经在处理同一个坏闸门，硬停不能同一轮抢跑 —— "
              "否则维护模式在它唯一被设计出来的场景里永远到不了",
              st.get("breakers"))
        check(st.get("no_progress_deferred") is True,
              "推迟要写在 state 里，不是默默不计", st.get("no_progress_deferred"))
        rc, out = box.run("watchdog", "--json")
        wj = json.loads(out)
        check(any(f.get("kind") == "no_progress_streak" and "推迟" in str(f.get("detail", ""))
                  for f in wj.get("info", [])),
              "看门狗说得出「已过阈值但被推迟」，而不是「还没到阈值」", wj.get("info"))
        box.run("go", "--reason", "self-test ladder")
        st = box.state()
        check(st.get("no_progress_deferred") is False
              and st["counters"].get("no_progress_streak") == 0,
              "`go` 把推迟标记和连续数一起清掉，下一档从零开始数", st["counters"])
    finally:
        box.cleanup()


def s_budget_backoff():
    head("#12：预算退避 —— 额度快用完先降速，而不是一头撞到硬上限才停")
    box = Box("backoff", caps={"nightly_max_jobs": 10, "watch_interval_s": 60,
                               "backoff_at": 0.8, "backoff_max_s": 900,
                               "max_turns_per_day": 36})
    try:
        rc, out = box.run("budget", "--json")
        bj = json.loads(out)
        bo = bj.get("backoff") or {}
        check(rc == 0 and bo.get("throttled") is False and bo.get("parallel") == 2
              and bo.get("interval_s") == 60,
              "额度充足时按配置的并发和节奏跑，退避不无谓降速", bo)
        rc, out = box.run("budget")
        check("退避" in out, "文字版也说了当前节奏，不只是 JSON 里有", out[-300:])

        st = box.state()
        st["counters"]["jobs_today"] = 8            # 8/10 = 80% = backoff_at
        box.write_state(st)
        rc, out = box.run("budget", "--json")
        bo = json.loads(out).get("backoff") or {}
        check(bo.get("throttled") is True and bo.get("parallel") == 1,
              "到 80% 先降到单并发：把剩余额度留给收尾，而不是并行撞墙", bo)
        check(bo.get("interval_s") == 120, "节奏同时放慢一倍", bo)
        check(any("jobs" in str(r) for r in bo.get("reasons") or []),
              "退避说得出是哪项额度触发的", bo.get("reasons"))

        box.add_task("Backoff one", ["src/one.py"])
        box.add_task("Backoff two", ["src/two.py"])
        rc, out = box.tick(mode="good", wait=False)
        slots = box.state()["slots"]
        check(bool(slots.get("A")) and not slots.get("B"),
              "退避是真的生效在派发上：这一轮只占一个 slot",
              {k: bool(v) for k, v in slots.items()})
        box.kill_jobs()

        # idle backoff is exponential, and it has a ceiling
        box2 = Box("backoffidle", caps={"watch_interval_s": 60, "backoff_max_s": 900})
        try:
            seen = []
            for _ in range(5):
                box2.tick(mode="good", env={"AUTOLOOP_WORKER_CMD": ""})
                rc, out = box2.run("budget", "--json")
                seen.append(json.loads(out)["backoff"]["interval_s"])
            check(seen == [120, 240, 480, 900, 900],
                  "空转越多退得越远，但封顶在 backoff_max_s（%s）：%s"
                  % (900, seen), seen)
            check(all(v <= 900 for v in seen), "任何情况下都不超过上限", seen)
        finally:
            box2.cleanup()

        # backoff slows down; it never raises a cap
        st = box.state()
        st["counters"]["jobs_today"] = 10
        st["slots"] = {"A": None, "B": None}
        box.write_state(st)
        rc, out = box.tick(mode="good")
        check("cap" in out and box.state()["slots"].get("A") is None,
              "退避不等于放宽：到了硬上限照样一条都不派", out[-300:])
    finally:
        box.cleanup()


def s_structured_alerts():
    head("#12：结构化告警 —— 机器可读、冷却窗口内去重、可确认，且确认不改历史")
    box = Box("alerts", caps={"watchdog_stale_s": 60, "alert_cooldown_min": 30})
    try:
        rc, out = box.run("alerts", "--json")
        aj = json.loads(out)
        check(rc == 0 and aj.get("open") == [], "干净的箱子没有告警，退出码 0", out[-200:])

        box.patch_state(last_tick_at=int(time.time()) - 3600,
                        next="resume with ghp_%s" % ("A" * 40))
        box.run("watchdog")
        box.run("watchdog")
        raw = read_text(os.path.join(box.alo, "alerts.jsonl"))
        aj = open_alerts(box)
        check(len(aj["open"]) == 1,
              "同一个问题在冷却窗口内只算一条开放告警（每 60s 重复一遍的告警没人看）",
              aj["open"])
        a = aj["open"][0]
        check(a.get("count") == 2 and a.get("kind") == "loop_stale"
              and a.get("severity") == "critical",
              "去重不是丢弃：次数记在 count 上", a)
        for field in ("ts", "alert_id", "title", "detail", "action",
                      "dedupe_key", "first_seen_epoch", "epoch"):
            check(bool(a.get(field) is not None), "告警字段 %s 在（人能读、脚本能判）" % field, a)
        check("ghp_" not in json.dumps(a, ensure_ascii=False)
              and "<redacted:credential-pattern>" in json.dumps(a, ensure_ascii=False),
              "告警内容过脱敏：这文件正是会被贴进 bug 报告的那一个", a)
        check(len(alerts_of(box)) == 2, "落盘仍是追加式两行，读的时候才折叠", alerts_of(box))

        rc, out = box.run("alerts", "--severity", "warn", "--json")
        check(json.loads(out).get("open") == [], "--severity 能筛（只要 critical 的监控用得上）",
              out[-200:])
        rc, out = box.run("alerts")
        check("loop_stale" in out and "critical" in out,
              "默认输出是给人看的文字，含 kind 与级别", out[-400:])

        rc, out = box.run("alerts", "--ack", a["alert_id"], "--note", "已在处理")
        check(rc == 0, "确认一条告警", out[-200:])
        check(open_alerts(box)["open"] == [], "确认之后它不再算开放", open_alerts(box))
        acks = json.loads(read_text(os.path.join(box.alo, "alert_acks.json")))
        check(acks.get(a["alert_id"], {}).get("note") == "已在处理",
              "确认记着谁在什么时候说了什么", acks)
        check(read_text(os.path.join(box.alo, "alerts.jsonl")) == raw,
              "确认只写 alert_acks.json，一个字节都没改告警历史", raw[-200:])
        box.run("watchdog")
        check(open_alerts(box)["open"] == [],
              "问题还在、又报了一次：同一段告警不会因为重复触发就重新吵人",
              open_alerts(box))

        set_caps(box, alert_cooldown_min=0)
        box.run("watchdog")
        aj = open_alerts(box)
        check(len(aj["open"]) == 1 and aj["open"][0]["count"] == 1,
              "冷却窗口过了就是新的一段：确认过的事再犯，会重新开放", aj["open"])
        rc, out = box.run("alerts", "--ack-all", "--note", "全部知道了")
        check(rc == 0 and open_alerts(box)["open"] == [], "--ack-all 一次清完", out[-200:])
        rc, out = box.run("alerts", "--ack", "no-such-id")
        check(rc != 0, "确认一个不存在的 id 是错误，不是静默成功", out[-200:])
    finally:
        box.cleanup()


# ------------------------------------------------------------------------ main

def _lines(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return len([l for l in fh if l.strip()])
    except OSError:
        return 0


SCENARIOS = [
    ("init", s_init_doctor),
    ("unit", s_unit_rules),
    ("green", s_green_pr),
    ("red", s_redlines),
    ("rounds", s_blocked_after_rounds),
    ("parallel", s_parallel_refill),
    ("collision", s_path_collision),
    ("cap", s_pr_cap),
    ("policy", s_policy_and_approval),
    ("stop", s_stop_halt),
    ("lock", s_lock),
    ("ref", s_reference),
    ("recover", s_recover_worktree),
    ("orphan", s_orphan_branch),
    ("rollover", s_day_rollover),
    ("footer", s_footer_and_budget),
    ("suggest", s_suggest),
    ("norunner", s_no_runner),
    ("emptygate", s_empty_gate),
    ("size", s_size_and_disk_caps),
    ("jobcap", s_job_cap),
    ("timeout", s_timeout),
    ("failgate", s_fail_gate),
    ("ledger", s_ledger_and_journal),
    ("events", s_event_timeline),
    ("redevents", s_redgate_timeline),
    ("reviewqueue", s_review_queue),
    ("mergegate", s_merge_refusal_ladder),
    ("mergeserial", s_merge_serial),
    ("mergered", s_merge_regression_red),
    ("backup", s_backup_queue),
    ("manual", s_dispatch_by_hand),
    ("protocol", s_protocol_text),
    ("workerpr", s_worker_delivers_pr),
    ("finish", s_finish_guard),
    ("nomarker", s_nomarker_guard),
    ("stall", s_stall_guard),
    ("badpr", s_badpr_unverified),
    ("dsh", s_dsh_adapter),
    ("preflight", s_preflight_gaps),
    ("idlespin", s_watch_idle_exit),
    ("patrol", s_patrol_templates),
    ("watchdog", s_watchdog_standalone),
    ("noprog", s_no_progress_breaker),
    ("backoff", s_budget_backoff),
    ("alerts", s_structured_alerts),
]


def main(argv):
    only = None
    if "--only" in argv:
        only = argv[argv.index("--only") + 1]
    print("AutoLoop self-test — engine %s" % ENGINE)
    print("git: %s" % subprocess.run(["git", "--version"], stdout=subprocess.PIPE,
                                     universal_newlines=True).stdout.strip())
    started = time.time()
    for name, fn in SCENARIOS:
        if only and only not in name:
            continue
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — a broken scenario must not hide others
            FAIL.append("%s raised %s: %s\n%s"
                        % (name, type(exc).__name__, exc,
                           traceback.format_exc(limit=3)))
            print("  ERROR in %s: %s: %s" % (name, type(exc).__name__, exc))
    print("\n%s passed, %s failed  (%.1fs)"
          % (len(PASS), len(FAIL), time.time() - started))
    for line in FAIL:
        print("  · %s" % line)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
