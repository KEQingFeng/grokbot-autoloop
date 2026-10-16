"""「needs your decision」 must name only rows an approval could still start.

`reject <id>` writes `status="rejected"` into the pool row, and `pick_task` refuses
that status outright (`eligible()` calls it terminal). Three separate reporting sites
asked the human for a triage decision without looking at `status` at all:

  * `report_text()`'s `- needs your decision (approve/reject)` line,
  * `next_action()` — which is what the fixed footer's 下一步 and every tick carry,
  * `state_record()`'s 「待我授权」, the §十 record a fresh supervisor restores from.

So `reject t01 --note no` printed `rejected 1 task(s)` and then every later `report`
and `round` named the same id again, while `plan` refused it as `pool is empty` and
the footer kept saying 「approve or reject 1 needs_approval candidates, then tick」.
The operator is sent back to a decision they already made, and the one row that
really does wait (a `needs_approval` row the human never saw) is invisible in the
same list. The three sites also disagreed about *which pools* they read: `report`
looked at queue + candidates, `state_record` only at candidates.json, so a task
added with `queue --add` showed up in the report and vanished from `round`.

The fix gives all three one definition, matching `eligible()`'s own rule (an explicit
`policy` wins, otherwise the kind decides) and excluding the statuses `pick_task`
refuses. `approve` still reverses a rejection, and then the row is listed again —
triage state is the human's to change.

Offline: throwaway repo + fake gh/worker from the self-test harness, no network.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from selftest import Box


def triage_line(text):
    """The one `- needs your decision …` line of a report, or ''."""
    for line in text.splitlines():
        if "needs your decision" in line:
            return line
    return ""


def record_approvals(text):
    """The 「待我授权」 value out of a `round` text dump."""
    for line in text.splitlines():
        if "待我授权" in line:
            return line.split("：", 1)[-1].strip()
    return ""


class AwaitingDecisionTests(unittest.TestCase):
    def test_rejected_row_leaves_the_triage_list_everywhere(self):
        box = Box("awaitingreject")
        try:
            tid = box.add_task("Add an export command", ["src/*"], kind="new_feature",
                               pool="candidates.json", policy="needs_approval")
            box.patch_state(mode_forced="day")

            self.assertIn(tid, triage_line(box.run("report")[1]))
            self.assertIn(tid, record_approvals(box.run("round")[1]))
            footer = box.run("tick")[1].strip().splitlines()[-1]
            self.assertIn("approve or reject 1 needs_approval candidates", footer)

            rc, out = box.run("reject", tid, "--note", "not this week")
            self.assertEqual(rc, 0, out)
            self.assertEqual(
                [r for r in box.coll("candidates.json") if r["id"] == tid][0]["status"],
                "rejected")

            # The decision is made: stop asking for it.
            self.assertEqual(triage_line(box.run("report")[1]), "",
                             "a rejected row is not waiting for a decision")
            self.assertEqual(record_approvals(box.run("round")[1]), "-",
                             "the §十 record must not list it either")
            footer = box.run("tick")[1].strip().splitlines()[-1]
            self.assertNotIn("approve or reject", footer,
                             "the footer must not send the operator back to it: %s" % footer)
            # The row itself is still visible as rejected, not silently deleted.
            self.assertIn("rejected", box.run("queue")[1])

            # ...and an explicit change of mind puts the row back to work. `approve`
            # flips its policy to auto_apply, so it leaves the triage list by becoming
            # startable rather than by being re-listed as a pending decision; the
            # invariant under test is that the list and `plan` agree about who waits.
            rc, out = box.run("approve", tid, "--note", "changed my mind")
            self.assertEqual(rc, 0, out)
            self.assertEqual(triage_line(box.run("report")[1]), "",
                             "an approved row needs no further decision")
            rc, planned = box.run("plan")
            self.assertEqual(rc, 0, "approve must make the row startable: %s" % planned)
        finally:
            box.cleanup()

    def test_queue_pool_and_terminal_rows_are_counted_the_same_way(self):
        """One definition for report / footer / §十 record, over both pools."""
        box = Box("awaitingpools")
        try:
            queued = box.add_task("Ship a new feature", ["src/queued/*"],
                                  kind="new_feature", pool="queue.json",
                                  policy="needs_approval")
            done = box.add_task("Already delivered", ["src/done/*"], kind="new_feature",
                                pool="candidates.json", policy="needs_approval",
                                status="pr_open")
            box.patch_state(mode_forced="day")

            report = box.run("report")[1]
            self.assertIn(queued, triage_line(report),
                          "a needs_approval row in queue.json waits for the human too")
            self.assertNotIn(done, triage_line(report),
                             "an already-delivered row is not a decision")
            self.assertIn(queued, record_approvals(box.run("round")[1]),
                          "round must read the same pools as report")
            footer = box.run("tick")[1].strip().splitlines()[-1]
            self.assertIn("approve or reject 1 needs_approval candidates", footer,
                          "one waiting row, not two: %s" % footer)
        finally:
            box.cleanup()

    def test_kind_policy_decides_when_the_row_has_no_explicit_policy(self):
        """`eligible()` judges by kind when `policy` is absent; so must the report."""
        box = Box("awaitingkind")
        try:
            tid = box.add_task("Kind decides", ["src/*"], kind="new_feature",
                               pool="candidates.json")
            row = [r for r in box.coll("candidates.json") if r["id"] == tid][0]
            self.assertNotIn("policy", row)
            box.patch_state(mode_forced="day")
            self.assertIn(tid, triage_line(box.run("report")[1]))
            self.assertEqual(json.loads(
                box.run("round", "--json")[1])["待我授权"], [tid])
        finally:
            box.cleanup()


if __name__ == "__main__":
    unittest.main()
