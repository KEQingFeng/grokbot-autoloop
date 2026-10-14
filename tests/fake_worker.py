#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fake coding worker for the AutoLoop self-test.

Stands in for whatever headless agent CLI lives on the box (dsh / codex / claude
/ qoder). It reads the task card the supervisor wrote at $AUTOLOOP_PROMPT_FILE,
behaves according to $FAKE_WORKER_MODE, and exits. It never pushes: that is the
supervisor's job, and one of the assertions is that a worker could not.
"""

import os
import re
import subprocess
import sys
import time

MODE = os.environ.get("FAKE_WORKER_MODE", "good")
PROMPT = os.environ.get("AUTOLOOP_PROMPT_FILE", "")
WT = os.environ.get("AUTOLOOP_WORKTREE", os.getcwd())


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


def main():
    if not PROMPT or not os.path.exists(PROMPT):
        sys.stderr.write("fake worker: no prompt file\n")
        return 9
    with open(PROMPT, encoding="utf-8") as fh:
        text = fh.read()
    paths = task_paths(text)
    print("fake worker mode=%s paths=%s worktree=%s" % (MODE, paths, WT))

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

    rc = commit("autoloop worker %s" % MODE, force=(MODE == "autoloop"))
    print("committed rc=%s" % rc)
    print("FINISH: files changed %s/autoloop_helper.py tests/test_autoloop_helper.py "
          "| commands run: git commit | exit %s" % (target_dir(paths), rc))
    if MODE == "crash":
        return 7
    return 0


if __name__ == "__main__":
    sys.exit(main())
