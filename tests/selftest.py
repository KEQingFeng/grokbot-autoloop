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
    head("空转修复④：连续 6 轮无事可做就停下汇报，watch 不会空转到天亮")
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
        rc, out = box.run("watch", "--interval", "5", env={"AUTOLOOP_WORKER_CMD": ""})
        check(rc == 4, "watch stops instead of burning the nightly budget", out)
        check("连续空转" in out, "watch says why it stopped", out)
        rc, out = box.run("report", "--patrol")
        check("连续空转 7 轮" in out or "空转" in out, "the patrol reports the spin", out)
        box.add_task("Something real", ["src/*"])
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
