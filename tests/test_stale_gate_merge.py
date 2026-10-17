"""A green gate is bound to the SHA it was measured on (§七) — the merge line must
honour that binding, not just the verdict written minutes earlier.

`review` grades a PR from the diff plus this box's record that the local gate went
green; `merge` re-reads the PR from gh immediately before acting. Neither compared the
PR's *head commit* with the SHA the gate actually verified, and a branch is not frozen:
the worker (or a human) can push another commit, or rebase, after the gate ran. The
pre-fix engine therefore said `auto_merge_ok` for a head it never tested and merged it —
the merge record even paired `verified_sha` with a base branch that had a different
commit in it. Reproduced against the pre-fix engine: `merge --yes-i-will-merge` returned
0, issued `gh pr merge`, moved `origin/<base>` and wrote `pr_merged` for a commit no
gate had judged.

The check is deliberately "known head ≠ known verified SHA". A gh stand-in (or a record
written before `headRefOid` was kept) that reports no head at all is not evidence of a
mismatch, so that third test pins the boundary the other two rely on.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from selftest import (Box, GREEN_CI, events_of, merge_calls, origin_sha, pr_row,
                      push_branch, seed_verified_task, set_cfg)

ALPHA = {"src/alpha.py": "def alpha():\n    return 1\n",
         "tests/test_alpha.py": "def test_alpha():\n    assert alpha() == 1\n"}


def push_after_gate(box, branch):
    """One more commit on a branch that is already on origin.

    This is the whole failure mode: the commit the gate measured is no longer the
    commit the PR carries.
    """
    base = box.base()
    box.git("checkout", "-q", "-B", branch, "origin/" + branch)
    with open(os.path.join(box.repo, "src/alpha.py"), "w", encoding="utf-8") as fh:
        fh.write("def alpha():\n    return 1\n\n\ndef later():\n    return 42\n")
    box.git("add", "-A")
    box.git("commit", "-qm", "commit pushed after the gate went green")
    rc, out = box.git("push", "-q", "origin", branch)
    box.git("checkout", "-q", base)
    box.git("fetch", "-q", "origin")
    assert rc == 0, out
    _, sha = box.git("rev-parse", "origin/" + branch)
    return sha.strip()



def verified_branch(box, branch="auto/alpha", tid="t01-alpha"):
    """A pushed branch with a local gate_green record naming the SHA origin holds."""
    push_branch(box, branch, ALPHA)
    box.git("fetch", "-q", "origin")
    return seed_verified_task(box, tid, "alpha 的边界情况", branch)


def gh_prs(box, rows, **extra):
    state = {"prs": rows, "next": len(rows) + 1, "auth": True, "search": "ok",
             "calls": []}
    state.update(extra)
    box.gh_state(state)


class StaleGateMergeTests(unittest.TestCase):
    def test_review_holds_a_pr_whose_head_moved_after_the_gate(self):
        box = Box("stalehead")
        try:
            base = box.base()
            verified = verified_branch(box)
            moved = push_after_gate(box, "auto/alpha")
            self.assertNotEqual(verified, moved, "the fixture must really move the head")
            row = pr_row(base, 1, "auto/alpha", 0.5, ci=GREEN_CI)
            row["headRefOid"] = moved
            gh_prs(box, [row])

            rc, out = box.run("review", "--json")
            self.assertEqual(rc, 0, out)
            graded = json.loads(out)["rows"][0]
            self.assertEqual(graded["verified_sha"], verified, graded)
            self.assertEqual(graded["verdict"], "hold",
                             "an unmeasured head is not 可自动合并: %s" % graded)
            self.assertTrue(any(moved[:12] in b and verified[:12] in b
                                for b in graded["blockers"]),
                            "the blocker has to name both commits: %s" % graded["blockers"])
        finally:
            box.cleanup()

    def test_merge_refuses_a_head_that_moved_after_the_review(self):
        box = Box("staleheadmerge", caps={"merge_enabled": True, "merge_threshold": 1})
        set_cfg(box, merge_regression_cmd="bash tests/run.sh")
        try:
            base = box.base()
            verified = verified_branch(box)
            row = pr_row(base, 1, "auto/alpha", 0.5, ci=GREEN_CI)
            row["headRefOid"] = verified          # the review sees the verified head
            gh_prs(box, [row], allow_merge=True)

            # the review is written while the head still matches what the gate measured
            rc, out = box.run("review", "--json")
            self.assertEqual(rc, 0, out)
            self.assertEqual(json.loads(out)["rows"][0]["verdict"], "auto_merge_ok", out)

            # the branch moves after that conclusion was written: it is now about a
            # commit the PR does not carry any more
            moved = push_after_gate(box, "auto/alpha")
            self.assertNotEqual(verified, moved, "the fixture must really move the head")
            row["headRefOid"] = moved
            gh_prs(box, [row], allow_merge=True)
            sha_before = origin_sha(box, base)
            rc, out = box.run("merge", "--yes-i-will-merge", "--json")
            self.assertNotEqual(rc, 0, out)
            result = json.loads(out)
            self.assertEqual(result["merged"], [], result)
            self.assertEqual((result.get("stopped") or {}).get("stage"), "pre-merge", result)
            self.assertTrue(any(moved[:12] in b
                                for b in (result.get("stopped") or {}).get("blockers", [])),
                            result.get("stopped"))
            self.assertEqual(merge_calls(box), [], "no gh pr merge may be issued")
            self.assertEqual(origin_sha(box, base), sha_before,
                             "the base branch must not move")
            self.assertEqual(events_of(box, "pr_merged"), [],
                             "nothing may be recorded as merged")
        finally:
            box.cleanup()

    def test_an_unreported_head_is_not_a_mismatch(self):
        box = Box("nohead", caps={"merge_enabled": True, "merge_threshold": 1})
        set_cfg(box, merge_regression_cmd="bash tests/run.sh")
        try:
            base = box.base()
            verified_branch(box)
            # a stand-in (or a record from before this field was kept) reports no head:
            # "unknown" must not be read as "different"
            gh_prs(box, [pr_row(base, 1, "auto/alpha", 0.5, ci=GREEN_CI)], allow_merge=True)
            rc, out = box.run("review", "--json")
            self.assertEqual(rc, 0, out)
            self.assertEqual(json.loads(out)["rows"][0]["verdict"], "auto_merge_ok", out)

            rc, out = box.run("merge", "--yes-i-will-merge", "--json")
            self.assertEqual(rc, 0, out)
            self.assertEqual([m["pr"] for m in json.loads(out)["merged"]], [1], out)
            self.assertEqual(len(merge_calls(box)), 1, merge_calls(box))
        finally:
            box.cleanup()


if __name__ == "__main__":
    unittest.main()
