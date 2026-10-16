import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from selftest import Box


class TriageApproveTests(unittest.TestCase):
    """`approve` must either make the task startable or say that it did not.

    `reject` writes `status="rejected"` into the pool row, and `pick_task` treats that
    status as terminal. Because `approve` never cleared it, changing your mind —
    `reject <id>` and later `approve <id>` — printed "they may now start on the next
    tick" while every following tick refused the row as "already rejected": a false
    success report about a task that could never start again.
    """

    def setUp(self):
        self.box = Box("triagereopen")

    def tearDown(self):
        self.box.cleanup()

    def _pool_row(self, tid):
        for row in self.box.coll("candidates.json"):
            if row.get("id") == tid:
                return row
        self.fail("task %s vanished from candidates.json" % tid)

    def test_approve_after_reject_makes_the_task_startable_again(self):
        tid = self.box.add_task("Add an export command", ["src/*"], kind="new_feature",
                                pool="candidates.json")
        # a human triage decision is not tied to the hour of the day
        self.box.patch_state(mode_forced="day")

        rc, out = self.box.run("reject", tid, "--note", "not this week")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self._pool_row(tid)["status"], "rejected")
        self.assertEqual(self.box.run("plan")[0], 1, "rejected work must not be planned")

        rc, out = self.box.run("approve", tid, "--note", "changed my mind")
        self.assertEqual(rc, 0, out)
        self.assertIn("they may now start on the next tick", out)
        row = self._pool_row(tid)
        self.assertEqual(row["status"], "open")
        self.assertTrue(row["approved"])
        rc, planned = self.box.run("plan")
        self.assertEqual(rc, 0, "approved task is not eligible: %s" % planned)
        body = "\n".join(line for line in planned.splitlines()
                         if not line.startswith("#"))
        self.assertEqual(json.loads(body)["id"], tid)

    def test_approve_does_not_promise_a_start_for_a_terminal_row(self):
        tid = self.box.add_task("Already delivered", ["src/*"], kind="new_feature",
                                pool="candidates.json", status="pr_open")
        rc, out = self.box.run("approve", tid, "--note", "fine")
        self.assertEqual(rc, 0, out)
        self.assertIn("not restarted", out)
        self.assertNotIn("they may now start", out)
        self.assertEqual(self._pool_row(tid)["status"], "pr_open")


if __name__ == "__main__":
    unittest.main()
