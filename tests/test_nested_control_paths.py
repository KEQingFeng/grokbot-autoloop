import os
import sys
import unittest
sys.path.insert(0, os.path.dirname(__file__))
from selftest import Box
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import autoloop as engine


class _RedlineCtx:
    """Just enough of Ctx for `redline_check` to read the diff and the cap."""

    def __init__(self, repo, base, cap=100000):
        self.root = repo
        self.origin_base = base
        self._cap = cap

    def cap(self, key):
        if key == "max_business_lines":
            return self._cap
        return 0


class NestedControlDirTests(unittest.TestCase):
    """The loop's own control directory and CI config are forbidden *at any depth*.

    §四 says the gate runs from the control copy of `.autoloop/verify.sh`, "so a worker
    cannot edit the rules it is judged by". That promise only held at the repo root:
    `.autoloop/*` matches `.autoloop/verify.sh` but nothing under `vendor/`, and
    `.autoloop/**/*` matches a file *inside* the directory while never matching the
    directory's own entry point. So a worker committing `vendor/.autoloop/verify.sh`
    (or `web/.github/workflows/ci.yml`) was reported clean by `redline_check` — a
    second copy of the gate rules plus a CI change, both inside a path-scoped task.
    Each test below commits a real diff and asserts the red-line check refuses it.
    """

    def _findings(self, box, base_sha, task_paths):
        pol = {"auto_apply": [], "needs_approval": [], "extra_forbidden": [],
               "kinds_default": "needs_approval"}
        task = {"id": "t1", "paths": task_paths, "branch": "auto/t1"}
        findings, rows, churn = engine.redline_check(
            _RedlineCtx(box.repo, base_sha), task, box.repo, pol)
        return findings, rows

    def test_nested_autoloop_control_dir_is_forbidden(self):
        box = Box('nestedctrlalo')
        try:
            rc, out = box.git('rev-parse', 'HEAD')
            base_sha = out.strip()
            for path, body in (("vendor/.autoloop/verify.sh", "#!/bin/sh\nexit 0\n"),
                               ("vendor/.autoloop/jobs/j.json", "{}\n")):
                full = os.path.join(box.repo, path)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, 'w') as stream:
                    stream.write(body)
            rc, out = box.git('add', '-A')
            self.assertEqual(rc, 0, out)
            rc, out = box.git('commit', '-qm', 'worker smuggles a second gate')
            self.assertEqual(rc, 0, out)
            findings, rows = self._findings(box, base_sha, ["vendor/*"])
            forbidden = [f for f in findings if "forbidden path touched" in f]
            self.assertTrue(forbidden,
                            "a nested .autoloop/ must be refused; got %r" % (findings,))
            joined = "\n".join(forbidden)
            self.assertIn("verify.sh", joined)
        finally:
            box.cleanup()

    def test_nested_github_ci_config_is_forbidden(self):
        box = Box('nestedctrlgh')
        try:
            rc, out = box.git('rev-parse', 'HEAD')
            base_sha = out.strip()
            full = os.path.join(box.repo, "web", ".github", "workflows", "ci.yml")
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, 'w') as stream:
                stream.write("name: ci\non: push\njobs: {}\n")
            rc, out = box.git('add', '-A')
            self.assertEqual(rc, 0, out)
            rc, out = box.git('commit', '-qm', 'worker edits CI inside web/')
            self.assertEqual(rc, 0, out)
            findings, rows = self._findings(box, base_sha, ["web/*"])
            self.assertTrue([f for f in findings if "forbidden path touched" in f],
                            "a nested .github/ must be refused; got %r" % (findings,))
        finally:
            box.cleanup()

    def test_root_level_control_paths_stay_forbidden(self):
        """The original root-level promise must not regress while fixing the depth."""
        box = Box('nestedctrlroot')
        try:
            rc, out = box.git('rev-parse', 'HEAD')
            base_sha = out.strip()
            for path, body in ((".autoloop/verify.sh", "exit 0\n"),
                               (".autoloop/jobs/j.json", "{}\n"),
                               (".github/workflows/ci.yml", "name: ci\n"),
                               ("migrations/0001.sql", "SELECT 1;\n"),
                               ("package.json", "{}\n")):
                full = os.path.join(box.repo, path)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, 'w') as stream:
                    stream.write(body)
            rc, out = box.git('add', '-A')
            self.assertEqual(rc, 0, out)
            # .autoloop/ is gitignored by design (the loop's own state is never
            # committed), so a worker smuggling it in has to force-add it — which is
            # precisely the case `git add -fA` of the control directory exists to
            # catch. Stage it explicitly or the diff is empty and proves nothing.
            rc, out = box.git('add', '-f', '.autoloop/verify.sh', '.autoloop/jobs/j.json')
            self.assertEqual(rc, 0, out)
            rc, out = box.git('commit', '-qm', 'root control files')
            self.assertEqual(rc, 0, out)
            findings, rows = self._findings(box, base_sha, ["*"])
            joined = "\n".join(findings)
            for name in ("verify.sh", "j.json", "ci.yml", "0001.sql", "package.json"):
                self.assertIn("forbidden path touched", joined)
                self.assertIn(name, joined)
        finally:
            box.cleanup()

    def test_normal_source_change_is_not_called_forbidden(self):
        """No false positives: the fix must not redden an ordinary path-scoped task."""
        box = Box('nestedctrlok')
        try:
            rc, out = box.git('rev-parse', 'HEAD')
            base_sha = out.strip()
            for path, body in (("src/app.py", "def add(a, b):\n    return a + b\n"),
                               ("src/autoloop_helper.py", "HELP = 1\n"),
                               ("lib/github_api.py", "URL = 'x'\n"),
                               ("config/env.py", "MODE = 'dev'\n")):
                full = os.path.join(box.repo, path)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, 'w') as stream:
                    stream.write(body)
            rc, out = box.git('add', '-A')
            self.assertEqual(rc, 0, out)
            rc, out = box.git('commit', '-qm', 'ordinary source work')
            self.assertEqual(rc, 0, out)
            findings, rows = self._findings(box, base_sha, ["src/*", "lib/*", "config/*"])
            self.assertEqual([f for f in findings if "forbidden path touched" in f], [],
                             "ordinary source work must not be red: %r" % (findings,))
        finally:
            box.cleanup()


if __name__ == '__main__':
    unittest.main()
