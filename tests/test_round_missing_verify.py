import hashlib
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from selftest import Box  # noqa: E402


class RoundMissingVerifyTests(unittest.TestCase):
    """`round` is the recovery command a new supervisor instance runs before every
    development round (§二.6 / §十), and its whole job is to report a suspect state
    instead of trusting chat memory. Its own state record crashed on exactly such a
    state: `verify.sh` lives under the git-excluded `.autoloop/`, so a partially
    restored control directory (a state.json without the gate script, a `git clean`,
    an operator moving `.autoloop/`) leaves it missing. `sha256_file()` returns None
    for a missing file and `state_record()` subscripted it, so `round` died with

        TypeError: 'NoneType' object is not subscriptable

    before it printed a single line. `run_verify` and `doctor` already degrade
    gracefully to "verify.sh missing"/"missing"; the recovery record must too.
    """

    def test_round_survives_missing_verify_sh(self):
        box = Box("roundmissingverify")
        try:
            vsh = os.path.join(box.alo, "verify.sh")
            self.assertTrue(os.path.exists(vsh), os.listdir(box.alo))
            os.remove(vsh)

            rc, out = box.run("round")
            self.assertEqual(rc, 0, out)
            self.assertNotIn("Traceback", out)

            rc, outj = box.run("round", "--json")
            self.assertEqual(rc, 0, outj)
            self.assertNotIn("Traceback", outj)
            rec = json.loads(outj)
            self.assertEqual(
                rec["插件技能安装及验证状态"]["verify.sh_sha256"], "missing")
        finally:
            box.cleanup()


    def test_present_verify_sh_still_fingerprinted(self):
        """The None-guard must not turn a real gate fingerprint into "missing"."""
        box = Box("roundverifyhash")
        try:
            with open(os.path.join(box.alo, "verify.sh"), "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            rc, outj = box.run("round", "--json")
            self.assertEqual(rc, 0, outj)
            rec = json.loads(outj)
            self.assertEqual(
                rec["插件技能安装及验证状态"]["verify.sh_sha256"], digest[:12])
        finally:
            box.cleanup()


if __name__ == "__main__":
    unittest.main()
