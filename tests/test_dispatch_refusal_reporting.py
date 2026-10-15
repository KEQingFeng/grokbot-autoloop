import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from selftest import Box


class DispatchRefusalReportingTests(unittest.TestCase):
    """A refused dispatch has to name why it was refused.

    `start_task` classified a failed launch by grepping the message for the word
    "worker" (`"no-runner" if "worker" in lerr else "dispatch-failed"`), and
    `worker_launch_guard`'s own refusal text contains that word too
    ("worker launch refused: max_turns_per_day"). So a spent turn budget was
    recorded as `no-runner` — the word `init`, SKILL §3/§5, `report --patrol` and
    `watchdog` all escalate as an environment gap ("configure runner.cmd", "run
    `doctor`"). The morning page sent the operator to look for a CLI that was
    installed and working, while the real cause (caps.max_turns_per_day) appeared
    nowhere: `idle_reason` was wrong and the footer's 下一步 promised dispatch the
    next tick was mechanically going to refuse.
    """

    def _spend_the_turn_budget(self, box):
        """Hand the tick a box whose budget is already gone. The tick bumps the
        counter itself, so the run reports `cap + 1` as the current value."""
        state = box.state()
        state["counters"]["turns_today"] = int(box.config()["caps"]["max_turns_per_day"])
        state["slots"] = {"A": None, "B": None}
        box.write_state(state)

    def test_spent_turn_budget_is_a_cap_not_a_missing_worker(self):
        box = Box("refusalturns", caps={"max_turns_per_day": 36})
        try:
            box.add_task("Budget spent but work is queued", ["src/*"])
            self._spend_the_turn_budget(box)
            cap = int(box.config()["caps"]["max_turns_per_day"])
            spend = cap + 1                      # the tick counts itself first
            rc, out = box.tick(mode="good")

            self.assertEqual(box.state()["idle_reason"], "cap-turns", out)
            self.assertEqual(box.state()["counters"]["turns_today"], spend)
            self.assertEqual(box.state()["slots"], {"A": None, "B": None},
                             "nothing may be dispatched on a spent budget")
            # the guard's own message stays visible — it is the raw reason
            self.assertIn("max_turns_per_day", out)
            self.assertIn("- cap: turn budget %s/%s reached" % (spend, cap), out)
            self.assertNotIn("no-runner", out)

            footer = out.strip().splitlines()[-1]
            self.assertIn("cap: turns %s/%s spent" % (spend, cap), footer)
            self.assertNotIn("继续派发", footer,
                             "the footer must not promise a dispatch the budget refuses")

            # the two surfaces that turned this into a false environment alarm
            watchdog = json.loads(box.run("watchdog", "--json")[1])
            self.assertEqual([f["kind"] for f in watchdog["warn"] + watchdog["critical"]
                              if f["kind"] == "loop_broken"], [],
                             "a spent budget is not a broken environment")
            patrol = box.run("report", "--patrol")[1]
            self.assertIn("定时巡检", patrol)
            self.assertNotIn("no-runner", patrol)
            self.assertIn("当日轮数额度已用完", patrol)
            self.assertIn("是否需要我介入：否", patrol)
        finally:
            box.cleanup()

    def test_spent_job_cap_reaches_the_footer_too(self):
        box = Box("refusaljobcap", caps={"nightly_max_jobs": 0})
        try:
            box.add_task("No job budget left", ["src/*"])
            rc, out = box.tick(mode="good")
            self.assertEqual(box.state()["idle_reason"], "cap-jobs", out)
            footer = out.strip().splitlines()[-1]
            self.assertIn("cap: jobs 0/0 spent", footer)
            self.assertNotIn("继续派发", footer)
            patrol = box.run("report", "--patrol")[1]
            self.assertIn("当晚 job 额度已用完", patrol)
        finally:
            box.cleanup()

    def test_a_box_with_no_worker_cli_is_still_no_runner(self):
        """The other side of the classification: `no-runner` keeps its meaning."""
        box = Box("refusalnocli")
        try:
            box.add_task("Nobody to run it", ["src/*"])
            rc, out = box.tick(mode="good", env={"AUTOLOOP_WORKER_CMD": ""})
            self.assertEqual(box.state()["idle_reason"], "no-runner", out)
            self.assertIn("no worker configured", out)
            watchdog = json.loads(box.run("watchdog", "--json")[1])
            self.assertIn("loop_broken", [f["kind"] for f in watchdog["warn"]],
                          "a box with no worker CLI is still an environment gap")
        finally:
            box.cleanup()


if __name__ == "__main__":
    unittest.main()
