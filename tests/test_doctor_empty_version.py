import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from selftest import Box

# A worker CLI whose `--version` succeeds but prints nothing to stdout. This is the
# real shape of `pi --version` on this box: it exits 0 and writes the version to
# stderr, so `(out or "").splitlines()[0]` raised IndexError and took `doctor` down
# with it. `--help` still answers so the CLI probe path is exercised too.
FAKE_DSH = """#!/bin/sh
case "$1" in
  --version) exit 0 ;;
esac
printf '%s\\n' 'Usage: dsh [--profile NAME] [--json] [--session-id]'
exit 0
"""


class DoctorEmptyVersionTests(unittest.TestCase):
    def test_doctor_survives_worker_version_with_empty_stdout(self):
        box = Box("doctoremptyversion")
        fakebin = tempfile.mkdtemp(prefix="autoloop-fakebin-")
        try:
            path = os.path.join(fakebin, "dsh")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(FAKE_DSH)
            os.chmod(path, 0o755)

            env = {"PATH": fakebin + os.pathsep + box.env["PATH"]}
            rc, out = box.run("doctor", env=env)

            self.assertEqual(rc, 0, out)
            self.assertNotIn("Traceback", out)
            # empty stdout must degrade to the honest "present", not crash or invent a version
            self.assertIn("dsh(present)", out)
        finally:
            box.cleanup()
            shutil.rmtree(fakebin, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
