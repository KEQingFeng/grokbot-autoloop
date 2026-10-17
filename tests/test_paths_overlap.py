"""Regression tests for paths_overlap: a glob must overlap the literal path it owns.

Before the fix, `paths_overlap` compared the two pattern strings, so a declared
glob whose wildcard was not the last character never matched the other task's
literal path. `eligible()` therefore let two slots run on the same file — the
parallel-work rule §四 explicitly forbids. These tests run offline: no git, no
gh, no worker.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import autoloop as engine


class PathsOverlapTests(unittest.TestCase):
    def test_glob_covers_the_other_tasks_literal_path(self):
        # The reported bug: `scripts/*.py` and `scripts/autoloop.py` are the same file.
        self.assertTrue(engine.paths_overlap(["scripts/*.py"], ["scripts/autoloop.py"]))
        self.assertTrue(engine.paths_overlap(["scripts/autoloop.py"], ["scripts/*.py"]))

    def test_middle_and_suffix_wildcards_are_detected(self):
        self.assertTrue(engine.paths_overlap(["src/*/handler.py"], ["src/api/handler.py"]))
        self.assertTrue(engine.paths_overlap(["pkg/mod/*.js"], ["pkg/mod/index.js"]))
        self.assertTrue(engine.paths_overlap(["src/foo/*.py"], ["src/foo/bar.py"]))

    def test_two_globs_with_nested_anchors_overlap(self):
        self.assertTrue(engine.paths_overlap(["src/*/handler.py"], ["src/api/*.py"]))

    def test_existing_cases_still_hold(self):
        self.assertTrue(engine.paths_overlap(["src/*"], ["src/app.py"]))
        self.assertTrue(engine.paths_overlap(["src/a/*"], ["src/a/b.py"]))
        self.assertTrue(engine.paths_overlap(["src/*"], ["src/*"]))

    def test_unrelated_scopes_still_do_not_overlap(self):
        self.assertFalse(engine.paths_overlap(["src/a/*"], ["src/b/*"]))
        self.assertFalse(engine.paths_overlap(["src/foo/*.py"], ["src/other/bar.py"]))
        self.assertFalse(engine.paths_overlap(["src/a/**"], ["src/b/**"]))
        # A narrow glob and a different file extension share an anchor directory
        # but no file; the anchor check must not turn that into an overlap.
        self.assertFalse(engine.paths_overlap(["src/*.py"], ["src/foo.js"]))
        # Same anchor, different filename: no file matches both.
        self.assertFalse(engine.paths_overlap(["src/*/handler.py"], ["src/api/other.py"]))

    def test_empty_scopes_never_overlap(self):
        self.assertFalse(engine.paths_overlap([], ["src/*.py"]))
        self.assertFalse(engine.paths_overlap(["src/*.py"], []))


_POLICY = {"auto_apply": [], "needs_approval": [], "extra_forbidden": [],
           "kinds_default": "auto_apply"}


class _FakeCtx:
    def __init__(self, slots):
        self.state = {"slots": slots, "pending_pr": [], "maintenance": False}


class EligibleScopeTests(unittest.TestCase):
    def test_running_glob_blocks_a_literal_scope_it_owns(self):
        ctx = _FakeCtx({"A": {"task": "t1", "state": "developing",
                              "paths": ["scripts/*.py"]},
                        "B": None})
        task = {"id": "t2", "status": "open", "kind": "add_tests",
                "paths": ["scripts/autoloop.py"]}
        ok, why = engine.eligible(ctx, task, _POLICY, "day")
        self.assertFalse(ok)
        self.assertIn("paths overlap with slot A", why)

    def test_unrelated_scope_is_still_eligible(self):
        ctx = _FakeCtx({"A": {"task": "t1", "state": "developing",
                              "paths": ["scripts/*.py"]},
                        "B": None})
        task = {"id": "t3", "status": "open", "kind": "add_tests",
                "paths": ["docs/*.md"]}
        ok, why = engine.eligible(ctx, task, _POLICY, "day")
        self.assertTrue(ok, why)


if __name__ == '__main__':
    unittest.main()
