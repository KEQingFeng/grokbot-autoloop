#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`gh` stand-in for the AutoLoop self-test.

Point the engine at it with AUTOLOOP_GH=<abs path to this file> (it is executable
and reads its own argv, so the engine's `gh ...` shell-outs land here). Everything
it "knows" lives in the JSON file named by FAKE_GH_STATE, which lets a test seed a
merged PR, force a 429, or read back what the supervisor actually opened.

`pr merge` always fails on purpose: the contract says the supervisor never merges,
and one of the assertions is that it never even tries.
"""

import json
import os
import sys

STATE = os.environ.get("FAKE_GH_STATE", "")


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


def main(argv):
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
            sys.stderr.write("fake-gh: MERGE IS REFUSED — the supervisor must never merge\n")
            return 1
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


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
