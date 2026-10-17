"""A repair round is judged on its *own* run, not on the run it replaced.

`stall_s` exists so a silent worker stops burning a slot (SKILL §5, README `stall`
scenario): the process group is killed, the reason is recorded, and the task goes to
the repair round. That repair round is a fresh dispatch, so the reasons that are
about *a run* — "this run never printed AUTOLOOP_DONE", "this run was stopped" —
must not survive into it. `repair()` already resets `worker_exit` and `timed_out`
for exactly that reason; `stalled` was missed, and `verify_slot()` reads it as a red
reason for as long as the record lives. The result was a task that could never be
delivered after one silent run, however clean the repair round was.

The fix keeps `stalled` run-scoped (cleared when the repair round starts) and keeps
the task-level fact the patrol reports under `stall_seen`, so §七 can still say a run
of this task was stopped.

Offline: the worker is a local stub that stalls on its first invocation and delivers
a committed, in-scope diff on the second.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from selftest import Box

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
from autoloop import read_jsonl


# Invocation 1: alive, silent, nothing committed — the stall case.
# Invocation 2 (the repair round): a real commit on the task branch + the marker.
STUB_WORKER = '''
import os, subprocess, sys, time

run_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs.txt")
run = int(open(run_file).read()) if os.path.exists(run_file) else 0
with open(run_file, "w") as fh:
    fh.write(str(run + 1))
wt = os.environ["AUTOLOOP_WORKTREE"]
sys.stdout.write("stub worker run=%d working in %s\\n" % (run, wt))
sys.stdout.flush()
if run == 0:
    time.sleep(120)
    sys.exit(0)
with open(os.path.join(wt, "src", "autoloop_helper.py"), "w") as fh:
    fh.write("def helper():\\n    return 42\\n")
subprocess.run(["git", "add", "-A"], cwd=wt)
subprocess.run(["git", "-c", "user.email=w@example.com", "-c", "user.name=w",
                "commit", "-qm", "deliver after the stall"], cwd=wt)
sys.stdout.write("AUTOLOOP_DONE pr=none files=1 tests=pass\\n")
sys.stdout.flush()
sys.exit(0)
'''


class StallRepairRecoveryTests(unittest.TestCase):
    def test_repair_round_after_a_stall_is_judged_on_its_own_run(self):
        box = Box('stallrepair',
                  caps={'stall_s': 1, 'finish_grace_s': 1, 'job_timeout_s': 600})
        try:
            stub = os.path.join(box.dir, 'stub_worker.py')
            with open(stub, 'w', encoding='utf-8') as fh:
                fh.write(STUB_WORKER)
            box.env['AUTOLOOP_WORKER_CMD'] = '%s %s' % (sys.executable, stub)
            box.add_task('Deliver after a silent first run', ['src/*'])

            rc, out = box.run('tick')
            self.assertIn('started', out, out)
            time.sleep(2.5)

            # the stall is detected, the run is stopped, and the repair round takes it
            rc, out = box.run('tick')
            self.assertIn('stalled → session closed', out, out)
            rec = box.state()['slots']['A']
            self.assertEqual(rec['state'], 'repairing', rec)
            stopped = [e for e in read_jsonl(os.path.join(box.alo, 'events.jsonl'))
                       if e.get('kind') == 'job_stopped']
            self.assertEqual([e.get('verdict') for e in stopped], ['stalled'], stopped)
            self.assertIn('went silent', ' '.join(rec.get('red_reasons') or []), rec)

            # …and this run really delivers: committed diff, marker, gate green
            self.assertTrue(box.wait_jobs(), 'the repair worker never finished')
            rc, out = box.run('tick')
            rec = box.state()['slots']['A']
            self.assertEqual(box.coll('blocked.json'), [],
                             'a clean repair round must not be blocked: %s' % out)
            self.assertIsNotNone(rec, 'the slot was freed instead of going green: %s' % out)
            self.assertEqual(rec.get('verify'), 'green', out)
            self.assertEqual(rec.get('state'), 'pr_ready', rec)
            self.assertFalse(rec.get('stalled'), 'the silence belongs to the old run: %s' % rec)
            self.assertTrue(rec.get('stall_seen'), 'the patrol still owns the fact: %s' % rec)
            self.assertEqual(box.state()['counters']['fails_consecutive'], 0, rec)

            # §七 keeps saying a run of this task was stopped for silence: that fact is
            # the task's, and only the gate verdict is run-scoped.
            rc, out = box.run('report', '--patrol')
            self.assertIn('静默', out, out)
        finally:
            box.cleanup()


if __name__ == '__main__':
    unittest.main()
