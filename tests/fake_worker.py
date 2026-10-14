#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fake coding worker for the AutoLoop self-test.

Stands in for whatever headless agent CLI lives on the box (dsh / codex / claude
/ qoder). It reads the task card the supervisor wrote at $AUTOLOOP_PROMPT_FILE,
behaves according to $FAKE_WORKER_MODE, and exits. What it is allowed to push is
decided by the card: `$AUTOLOOP_DELIVERY_OWNER` says whether the worker or the
supervisor owns the PR, and the assertions cover both.
"""

import json
import os
import re
import subprocess
import sys
import time

MODE = os.environ.get("FAKE_WORKER_MODE", "good")
PROMPT = os.environ.get("AUTOLOOP_PROMPT_FILE", "")
WT = os.environ.get("AUTOLOOP_WORKTREE", os.getcwd())
OWNER = os.environ.get("AUTOLOOP_DELIVERY_OWNER", "worker")
BRANCH = os.environ.get("AUTOLOOP_BRANCH", "")
GH = os.environ.get("AUTOLOOP_GH", "gh")
HANG_S = float(os.environ.get("FAKE_WORKER_HANG", "25"))


def task_paths(text):
    try:
        section = text.split("## Files you may change", 1)[1].split("##", 1)[0]
    except IndexError:
        return []
    return re.findall(r"^- `(.+?)`\s*$", section, re.M)


def target_dir(paths):
    """Where a well-behaved worker puts its file: inside the first declared path."""
    for pattern in paths:
        head = pattern.split("*")[0].rstrip("/")
        if head and "." not in os.path.basename(head):
            return head
    return "src"


def git(*args):
    return subprocess.run(["git"] + list(args), cwd=WT, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True)


def shell(cmd):
    return subprocess.run(cmd, cwd=WT, shell=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, universal_newlines=True)


def write(rel, body):
    path = os.path.join(WT, rel)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


def commit(msg, force=False):
    git("add", "-fA" if force else "-A")
    rc = git("-c", "user.email=bot@example.com", "-c", "user.name=autoloop-worker",
             "commit", "-qm", msg)
    return rc.returncode


def deliver():
    """`delivery.owner=worker`: push my own branch and open the PR myself, through
    whatever gh (or gh stand-in) the supervisor put on the environment. The body
    file goes to the scratch dir, never the worktree — a stray uncommitted file is
    the supervisor's red line, not an artefact of the delivery step."""
    rc = git("push", "-u", "origin", BRANCH).returncode
    if rc != 0:
        print("push FAILED rc=%s" % rc)
        return "none"
    body = os.path.join(os.environ.get("TMPDIR") or "/tmp",
                        "pr-body-%s.md" % (BRANCH.replace("/", "_") or "x"))
    with open(body, "w", encoding="utf-8") as fh:
        fh.write("## 改动意图\nfake worker deliver\n\n## 验证情况\n`bash tests/run.sh` → 0\n"
                 "\n## 已知问题\n无\n")
    cmd = ("%s pr create --base %s --head %s --title %s --body-file %s"
           % (GH, os.environ.get("AUTOLOOP_BASE_BRANCH", "main"), BRANCH,
              json.dumps("autoloop: fake deliver"), json.dumps(body)))
    res = shell(cmd)
    print("gh pr create rc=%s out=%s" % (res.returncode, (res.stdout or "").strip()[:120]))
    m = re.search(r"(\d+)$", (res.stdout or "").strip())
    return m.group(1) if m else "none"


def main():
    if not PROMPT or not os.path.exists(PROMPT):
        sys.stderr.write("fake worker: no prompt file\n")
        return 9
    with open(PROMPT, encoding="utf-8") as fh:
        text = fh.read()
    paths = task_paths(text)
    print("fake worker mode=%s paths=%s worktree=%s owner=%s" % (MODE, paths, WT, OWNER))

    if MODE == "slow":
        time.sleep(float(os.environ.get("FAKE_WORKER_SLEEP", "4")))

    if MODE == "forbidden":
        write("package.json", '{"name": "sabotage", "version": "9.9.9"}\n')
    if MODE == "outside":
        write("docs/random-note.md", "unrelated rewrite\n")
    if MODE == "testbreak":
        write("tests/test_app.py", "assert 1 == 2  # weakened existing assertion\n")
    if MODE == "failtest":
        # a real code change that makes the repo's own gate go red
        write("src/BROKEN", "boom\n")
    if MODE == "badpr":
        # red gate, but the worker still delivers its own PR — the supervisor has to
        # report that PR as "exists, not verified" instead of pretending it is absent
        write("src/BROKEN", "boom\n")
    if MODE == "autoloop":
        write(".autoloop/queue.json", "[]\n")
    if MODE == "secret":
        write("src/token.py", 'API_KEY = "ghp_%s"\n' % ("A" * 40))

    # every mode except a deliberately broken build leaves a clean, in-scope change
    # behind; `dirty` writes it but never commits it, which is its own red line
    if MODE != "failbuild":
        where = target_dir(paths)
        write(os.path.join(where, "autoloop_helper.py"),
              "def helper():\n    return 42\n")
        write("tests/test_autoloop_helper.py", "def test_helper():\n    assert True\n")

    if MODE == "dirty":
        print("FINISH: left the work uncommitted on purpose")
        return 0

    if MODE == "silent":
        # 空转 case 1: alive, writing nothing, committing nothing — the approval
        # prompt nobody will answer. The stall guard has to stop it.
        time.sleep(HANG_S)
        return 0

    rc = commit("autoloop worker %s" % MODE, force=(MODE == "autoloop"))
    print("committed rc=%s" % rc)

    pr = "none"
    if OWNER == "worker" and MODE in ("good", "deliver", "hang", "badpr"):
        pr = deliver()

    if MODE == "hang":
        # 空转 case 2: it really did the work and said so, then never got out of
        # the chair — the finish guard closes the session and delivers anyway.
        print("AUTOLOOP_DONE pr=%s files=2 tests=pass" % pr)
        sys.stdout.flush()
        time.sleep(HANG_S)
        return 0

    if MODE == "nomarker":
        # a worker that ignores the protocol's last line must not hold the slot:
        # committed + silent is enough for the supervisor to act on
        print("FINISH: files changed, no marker printed")
        sys.stdout.flush()
        time.sleep(HANG_S)
        return 0

    print("FINISH: files changed %s/autoloop_helper.py tests/test_autoloop_helper.py "
          "| commands run: git commit | exit %s" % (target_dir(paths), rc))
    print("AUTOLOOP_DONE pr=%s files=2 tests=pass" % pr)
    if MODE == "crash":
        return 7
    return 0


if __name__ == "__main__":
    sys.exit(main())
