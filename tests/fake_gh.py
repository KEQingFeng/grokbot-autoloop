#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`gh` stand-in for the AutoLoop self-test.

Point the engine at it with AUTOLOOP_GH=<abs path to this file> (it is executable
and reads its own argv, so the engine's `gh ...` shell-outs land here). Everything
it "knows" lives in the JSON file named by FAKE_GH_STATE, which lets a test seed a
merged PR, force a 429, or read back what the supervisor actually opened.

`pr merge` is refused unless the fixture sets `allow_merge: true` — the same shape
as the engine's own `caps.merge_enabled`, so a scenario cannot merge by accident.
`--admin` and `--force` are refused even then, because bypassing branch protection
is forbidden from both sides of the contract.
"""

import fcntl
import json
import os
import sys

STATE = os.environ.get("FAKE_GH_STATE", "")


class locked(object):
    """One state file, several callers: in `delivery.owner=worker` two agents push and
    open their PRs at the same moment, so a read-modify-write without a lock loses a
    PR and the test then blames the engine for a bug in the stand-in."""

    def __enter__(self):
        self.handle = None
        if STATE:
            self.handle = open(STATE + ".lock", "w")
            fcntl.flock(self.handle, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self.handle:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()
        return False


def load():
    if not STATE or not os.path.exists(STATE):
        return {"prs": [], "next": 1, "auth": True, "search": "ok", "calls": []}
    try:
        with open(STATE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("prs", [])
    data.setdefault("next", 1)
    data.setdefault("auth", True)
    data.setdefault("search", "ok")
    data.setdefault("calls", [])
    return data


def save(data):
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)


def opt(argv, name, default=None):
    for i, token in enumerate(argv):
        if token == name and i + 1 < len(argv):
            return argv[i + 1]
        if token.startswith(name + "="):
            return token.split("=", 1)[1]
    return default


def _git_merge(head_ref, base_ref, method, num):
    """Really merge origin/<head> into the checked-out base and push it.

    Runs in the caller's cwd, which is the repo the engine pointed gh at."""
    import subprocess

    def git(*args):
        proc = subprocess.run(["git"] + list(args), stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, universal_newlines=True)
        return proc.returncode, (proc.stdout or "").strip()

    rc, out = git("fetch", "-q", "origin")
    if rc != 0:
        return rc
    rc, cur = git("rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0 or cur != base_ref:
        rc, _ = git("checkout", "-q", base_ref)
        if rc != 0:
            return rc
    flag = "--squash" if method == "squash" else "--no-ff"
    rc, out = git("merge", flag, "-m", "Merge PR #%s (%s)" % (num, head_ref),
                  "origin/" + head_ref)
    if rc != 0:
        return rc
    if method == "squash":
        rc, out = git("commit", "-qm", "Squash PR #%s (%s)" % (num, head_ref))
        if rc != 0:
            return rc
    rc, out = git("push", "-q", "origin", base_ref)
    return rc


def handle(argv):
    data = load()
    if not argv:
        sys.stderr.write("fake-gh: no arguments\n")
        return 2
    data["calls"].append(" ".join(argv))
    head, rest = argv[0], argv[1:]

    if head == "auth":
        save(data)
        if not data.get("auth", True):
            sys.stderr.write("Please run `gh auth login`\n")
            return 1
        sys.stdout.write("github.com: Logged in\n")
        return 0

    if head == "pr":
        action = rest[0] if rest else ""
        if action == "list":
            save(data)
            sys.stdout.write(json.dumps(data["prs"]))
            return 0
        if action == "merge":
            save(data)
            # The refusal is the default and it stays the default: a scenario has to
            # seed `allow_merge` explicitly, exactly like the engine requires
            # caps.merge_enabled. `--admin` / `--force` are refused even then, because
            # bypassing protection is forbidden in both directions.
            joined = " ".join(rest)
            if "--admin" in joined or "--force" in joined:
                sys.stderr.write("fake-gh: REFUSED — --admin/--force bypasses protection\n")
                return 1
            if not data.get("allow_merge"):
                sys.stderr.write("fake-gh: MERGE IS REFUSED — the supervisor must never merge\n")
                return 1
            num = None
            for token in rest[1:]:
                if str(token).isdigit():
                    num = int(token)
                    break
            row = next((r for r in data["prs"] if r.get("number") == num), None)
            if row is None:
                sys.stderr.write("fake-gh: no such PR: %s\n" % num)
                return 1
            if str(row.get("mergeable") or "").upper() == "CONFLICTING":
                sys.stderr.write("fake-gh: merge conflict\n")
                return 1
            # Do the merge for real so the engine's post-merge `git rev-parse` sees
            # the base actually move — a fake that only flips a JSON field would let
            # a broken SHA check pass.
            head_ref = row.get("headRefName")
            base_ref = row.get("baseRefName") or "main"
            method = "squash" if "--squash" in joined else (
                "rebase" if "--rebase" in joined else "merge")
            rc = _git_merge(head_ref, base_ref, method, num)
            if rc != 0:
                sys.stderr.write("fake-gh: git merge failed rc=%s\n" % rc)
                return rc
            row["state"] = "MERGED"
            save(data)
            sys.stdout.write("https://github.com/fake/repo/pull/%s" % num)
            return 0
        if action == "view":
            save(data)
            num = None
            for token in rest[1:]:
                if str(token).isdigit():
                    num = int(token)
                    break
            row = next((r for r in data["prs"] if r.get("number") == num), None)
            if row is None:
                sys.stderr.write("fake-gh: no such PR: %s\n" % num)
                return 1
            sys.stdout.write(json.dumps(row))
            return 0
        if action == "create":
            num = int(data.get("next", 1))
            data["next"] = num + 1
            body = ""
            bf = opt(rest, "--body-file")
            if bf and os.path.exists(bf):
                with open(bf, encoding="utf-8") as fh:
                    body = fh.read()
            data["prs"].append({"number": num,
                                "headRefName": opt(rest, "--head", ""),
                                "baseRefName": opt(rest, "--base", "main"),
                                "state": "OPEN",
                                "title": opt(rest, "--title", ""),
                                "url": "https://github.com/fake/repo/pull/%s" % num,
                                "body": body, "labels": []})
            save(data)
            sys.stdout.write("https://github.com/fake/repo/pull/%s" % num)
            return 0
        if action == "edit":
            num = None
            for token in rest[1:]:
                if token.isdigit():
                    num = int(token)
            for row in data["prs"]:
                if row.get("number") == num:
                    row.setdefault("labels", []).append(opt(rest, "--add-label", "?"))
            save(data)
            return 0
        save(data)
        sys.stderr.write("fake-gh: unsupported pr subcommand: %s\n" % action)
        return 2

    if head == "search" and rest[:1] == ["repos"]:
        mode = data.get("search", "ok")
        save(data)
        if mode == "429":
            sys.stderr.write("HTTP 429: You have exceeded a secondary rate limit\n")
            return 1
        if mode == "empty":
            sys.stdout.write("[]")
            return 0
        if mode == "error":
            sys.stderr.write("HTTP 500: something else\n")
            return 1
        sys.stdout.write(json.dumps([
            {"fullName": "example/mature-project", "stargazersCount": 4211,
             "updatedAt": "2026-10-01T09:00:00Z",
             "description": "reference implementation for the self-test"},
            {"fullName": "example/older-one", "stargazersCount": 12,
             "updatedAt": "2024-01-01T09:00:00Z", "description": "ignored"},
        ]))
        return 0

    save(data)
    sys.stderr.write("fake-gh: unsupported call: %s\n" % " ".join(argv))
    return 2


def main(argv):
    with locked():
        return handle(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
