import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import autoloop as engine


def _git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t"] + list(args),
        cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        universal_newlines=True)


def _init(repo):
    _git(repo, "init", "-q")
    with open(os.path.join(repo, "seed.txt"), "w") as fh:
        fh.write("seed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")


class _Ctx:
    """Just enough Ctx for diff_stat / redline_check, which read a cap and a base."""

    def __init__(self, base="HEAD~1", cap=400):
        self.origin_base = base
        self._cap = cap

    def cap(self, key):
        return self._cap


class DiffStatChurnTests(unittest.TestCase):
    """`diff_stat` feeds `max_business_lines` and `risk_score`, both safety inputs.
    A pure-addition diff used to be reported as the *file count*: the summary line
    "1 file changed, 500 insertions(+)" was parsed by the fallback and the leading
    "1" became the churn, so a 500-line single-file change slipped under the cap."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="autoloop-churn-")
        _init(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pure_addition_counts_added_lines_not_files(self):
        with open(os.path.join(self.tmp, "big.txt"), "w") as fh:
            fh.write("x\n" * 500)
        _git(self.tmp, "add", "-A")
        _git(self.tmp, "commit", "-qm", "add 500 lines")
        _, churn = engine.diff_stat(_Ctx(), self.tmp, "HEAD~1")
        self.assertEqual(churn, 500)

    def test_pure_deletion_counts_removed_lines(self):
        with open(os.path.join(self.tmp, "big.txt"), "w") as fh:
            fh.write("x\n" * 40)
        _git(self.tmp, "add", "-A")
        _git(self.tmp, "commit", "-qm", "seed big")
        os.remove(os.path.join(self.tmp, "big.txt"))
        _git(self.tmp, "add", "-A")
        _git(self.tmp, "commit", "-qm", "delete big")
        _, churn = engine.diff_stat(_Ctx(), self.tmp, "HEAD~1")
        self.assertEqual(churn, 40)

    def test_mixed_diff_sums_both_directions(self):
        with open(os.path.join(self.tmp, "seed.txt"), "w") as fh:
            fh.write("seed\n" + "new\n" * 9)
        with open(os.path.join(self.tmp, "gone.txt"), "w") as fh:
            fh.write("bye\n" * 3)
        _git(self.tmp, "add", "-A")
        _git(self.tmp, "commit", "-qm", "seed gone")
        os.remove(os.path.join(self.tmp, "gone.txt"))
        with open(os.path.join(self.tmp, "seed.txt"), "w") as fh:
            fh.write("seed\n")
        _git(self.tmp, "add", "-A")
        _git(self.tmp, "commit", "-qm", "mixed")
        _, churn = engine.diff_stat(_Ctx(), self.tmp, "HEAD~1")
        self.assertEqual(churn, 12)

    def test_churn_cap_catches_a_single_large_file(self):
        """The end-to-end consequence: one 500-line file with a 400-line cap is red."""
        with open(os.path.join(self.tmp, "big.txt"), "w") as fh:
            fh.write("x\n" * 500)
        _git(self.tmp, "add", "-A")
        _git(self.tmp, "commit", "-qm", "add 500 lines")
        findings, _rows, churn = engine.redline_check(
            _Ctx(), {"paths": ["big.txt"]}, self.tmp, {"extra_forbidden": []})
        self.assertEqual(churn, 500)
        self.assertTrue(any("diff too large" in f for f in findings), findings)


if __name__ == "__main__":
    unittest.main()
