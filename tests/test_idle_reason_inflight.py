"""`idle_reason` must describe the *loop*, not one empty slot.

Step 6 of a tick fills the free slots one by one, and until now it wrote
`idle_reason = "no-work"` as soon as any single slot failed to find an eligible
task. With two slots and one task in the pool the second slot always fails — the
only task is already being developed in the first — so every tick that dispatched
anything recorded the loop as idle. `report` then printed `idle_reason: no-work`
and `report --patrol` turned it into 「总体状态：正常（待办池空，需要 `suggest`
补任务）」 … in a report that printed 「队列 1」 two lines later and a slot in
`developing`. §七 forbids reporting work that is in flight as work that is not
there, and `_finish()` already uses exactly the opposite definition: a tick is only
空转 when no slot is in `ACTIVE_STATES`.

The fix keeps the per-slot line ("slot B: no eligible task …") — that is a true
statement about slot B — and writes the loop-level `idle_reason` only once the fill
loop is over and nothing is in flight. A genuinely empty pool still reports
`no-work`, which the second test pins so the first one cannot be "fixed" by simply
deleting the reason.

Offline: the worker is the local stand-in in `slow` mode, so the slot is still
`developing` when the tick that dispatched it has returned.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from selftest import Box


class IdleReasonTests(unittest.TestCase):
    def test_a_running_worker_is_not_reported_as_an_empty_pool(self):
        box = Box("idleinflight", owner="worker")
        try:
            box.add_task("The only task in the pool", ["src/*"])
            rc, out = box.run("tick", mode="slow")     # still developing on return
            self.assertEqual(rc, 0, out)

            st = box.state()
            self.assertEqual((st["slots"].get("A") or {}).get("state"), "developing",
                             "the fixture did not leave a worker in flight: %s" % st["slots"])
            self.assertIn("no eligible task", out,
                          "slot B really had nothing to start, and that is still said")

            self.assertIsNone(st.get("idle_reason"),
                              "a worker is developing: the loop is not out of work, %s"
                              % st.get("idle_reason"))

            rc, report = box.run("report")
            self.assertNotIn("idle_reason: no-work", report,
                             "the per-round report must not call a busy loop idle: %s"
                             % report[-600:])

            rc, patrol = box.run("report", "--patrol")
            self.assertIn("队列 1", patrol, patrol)
            self.assertNotIn("待办池空", patrol,
                             "§七: the patrol cannot report an empty pool while the pool "
                             "holds a running task: %s" % patrol)
        finally:
            box.cleanup()

    def test_an_empty_pool_still_says_no_work(self):
        box = Box("idleempty")
        try:
            rc, out = box.run("tick")
            self.assertEqual(rc, 0, out)
            st = box.state()
            self.assertEqual(st.get("idle_reason"), "no-work",
                             "nothing in the pool and nothing in flight is 空转: %s" % st)
            self.assertEqual(st["counters"]["idle_ticks"], 1,
                             "and the idle streak keeps counting: %s" % st["counters"])

            rc, patrol = box.run("report", "--patrol")
            self.assertIn("待办池空", patrol, patrol)
        finally:
            box.cleanup()


if __name__ == "__main__":
    unittest.main()
