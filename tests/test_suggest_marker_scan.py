import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from selftest import Box


PROSE_MD = """# Notes

`autoloop.py suggest` reads TODO/FIXME markers and skipped tests.
A loop that manufactures TODOs to look busy is worse than one that stopped.
"""

# A real regex in real source: `TODO` here is one arm of an alternation, not a note.
CLAIM_RE_PY = """import re

CLAIM = re.compile(r"(?:支持|实现|TODO[:：]|待办|roadmap)[^\\n]{4,60}")
"""

REAL_MARKER_PY = """def add(a, b):
    return a + b  # TODO: handle overflow
"""

TEST_MARKER_PY = """def test_add():
    # FIXME: this only covers the happy path
    assert 1 == 1
"""


class SuggestMarkerScanTests(unittest.TestCase):
    """`suggest` must not invent work out of the word TODO.

    The marker scan was `git grep -nE "TODO|FIXME|XXX|HACK"`, and the word alone
    became a `docs_align` candidate. On this very repository — which mentions
    markers constantly, because it ships a scanner that looks for them — that
    produced four candidates and zero real markers in non-test files, one of them
    pointing at the scanner's own pattern line. "Clean up" that line and the
    scanner stops scanning. A test file's marker became one too, and a `docs_align`
    candidate that edits an existing test can never go green: the red-line check
    refuses a modified test, so the task is born blocked.

    The candidate's `acceptance[]` was vacuous on top of that:
    `git grep -c -E 'TODO|FIXME|XXX|HACK' -- <path> || true` exits 0 whether or not
    the marker survived, and `verify.sh` reads only the exit code. A diff that left
    the marker exactly where it was came back [PASS] — the one outcome §四 forbids.
    """

    def setUp(self):
        self.box = Box("suggestmarkers")

    def tearDown(self):
        self.box.cleanup()

    def _add(self, path, body):
        full = os.path.join(self.box.repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(body)
        self.box.git("add", "-A")
        self.box.git("commit", "-qm", "add %s" % path)

    def _suggest(self):
        rc, out = self.box.run("suggest", "--limit", "8")
        self.assertEqual(rc, 0, out)
        return self.box.coll("candidates.json"), out

    def _marker_candidates(self, cands):
        return [c for c in cands if c["title"].startswith("清理遗留标记")]

    def _acceptance_rc(self, command):
        """Run one acceptance command the way `.autoloop/verify.sh` does."""
        return subprocess.run(["bash", "-c", command], cwd=self.box.repo,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode

    def test_prose_and_regex_arms_are_not_markers(self):
        self._add("docs/NOTES.md", PROSE_MD)
        self._add("src/claims.py", CLAIM_RE_PY)
        cands, out = self._suggest()
        paths = {c["paths"][0] for c in self._marker_candidates(cands)}
        # `src/app.py` ships a genuine `# TODO: handle overflow` and must stay.
        self.assertIn("src/app.py", paths, out)
        self.assertNotIn("docs/NOTES.md", paths,
                         "prose that mentions markers is not a marker: %s" % out)
        self.assertNotIn("src/claims.py", paths,
                         "an alternation arm in a regex is not a marker: %s" % out)

    def test_a_marker_in_a_test_file_is_not_a_docs_align_candidate(self):
        self._add("tests/test_extra.py", TEST_MARKER_PY)
        cands, out = self._suggest()
        self.assertNotIn("tests/test_extra.py",
                         {c["paths"][0] for c in self._marker_candidates(cands)},
                         "a docs_align task on an existing test can never go green: %s"
                         % out)

    def test_marker_acceptance_fails_while_the_marker_is_present(self):
        cands, out = self._suggest()
        rows = [c for c in self._marker_candidates(cands) if c["paths"] == ["src/app.py"]]
        self.assertTrue(rows, "expected a marker candidate for src/app.py: %s" % out)
        command = rows[0]["acceptance"][0]
        self.assertNotIn("|| true", command,
                         "an acceptance that cannot fail is not an acceptance: %s"
                         % command)
        self.assertNotEqual(self._acceptance_rc(command), 0,
                            "the gate must be red while the marker is still there: %s"
                            % command)
        # The fix the task asks for: the line is gone.
        self._add("src/app.py", "def add(a, b):\n    return a + b\n")
        self.assertEqual(self._acceptance_rc(command), 0,
                         "the gate must go green once the marker line is gone: %s"
                         % command)

if __name__ == "__main__":
    unittest.main()
