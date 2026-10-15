import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from selftest import Box


class OperatorStopIsNotIdleSpinTests(unittest.TestCase):
    """A human `stop` must never be re-labelled as the engine's own 空转 breaker.

    `_finish()` counted *every* idle tick toward `idle_ticks`, including the ticks a
    tick refuses because `.autoloop/STOP`/`HALTED` is present. Six of the operator's
    own stopped ticks therefore tripped the `idle` breaker: the flag got a breaker
    record behind it, `watch` exited 4, and `report --patrol` attributed the shutdown
    to 熔断已断开（idle） — the exact 「who stopped it」 confusion the state record
    exists to prevent. An operator stop is not a spin: it resets the streak.
    """

    def test_human_stop_does_not_trip_the_idle_breaker(self):
        box = Box("stopnotidle")
        try:
            rc, out = box.run("stop", "--reason", "operator asleep")
            self.assertEqual(rc, 0, out)
            for _ in range(7):
                box.tick(mode="good")

            st = box.state()
            self.assertEqual(st["counters"]["idle_ticks"], 0,
                             "an operator stop is not a 空转 round")
            self.assertEqual(st.get("breakers"), {},
                             "no breaker may be invented over the human's STOP")
            self.assertTrue(os.path.exists(os.path.join(box.alo, "STOP")))

            rc, out = box.run("report", "--patrol")
            self.assertNotIn("熔断", out,
                             "the patrol must not blame a breaker for the operator's stop")
            self.assertIn("STOP 在场", out, out)

            # watch exits cleanly (0) on an operator stop; it must not report a fault
            # (4) for a command the operator typed themselves.
            rc, out = box.run("watch", "--interval", "5")
            self.assertEqual(rc, 0, out)
            self.assertIn("人工停机", out, out)
        finally:
            box.cleanup()


if __name__ == "__main__":
    unittest.main()
