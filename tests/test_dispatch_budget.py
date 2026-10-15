import importlib.util
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import autoloop as engine


class Context:
    def __init__(self, root):
        self.alo = root
        self.state = {'counters': {'jobs_today': 0, 'turns_today': 0, 'prs_today': 0},
                      'slots': {'A': None, 'B': None}}

    def cap(self, key):
        return {'nightly_max_jobs': 2, 'max_turns_per_day': 4,
                'pr_max_per_day': 2, 'max_parallel': 2}[key]


class DispatchBudgetTests(unittest.TestCase):
    def test_budget_and_stop_flags_prevent_every_worker_launch(self):
        with tempfile.TemporaryDirectory() as root:
            ctx = Context(root)
            self.assertEqual(engine.worker_launch_guard(ctx), '')
            ctx.state['counters']['jobs_today'] = 2
            self.assertIn('nightly_max_jobs', engine.worker_launch_guard(ctx))
            ctx.state['counters']['jobs_today'] = 0
            with open(os.path.join(root, 'STOP'), 'w') as stream:
                stream.write('operator pause')
            self.assertIn('STOP', engine.worker_launch_guard(ctx))

    def test_manual_launch_refuses_exhausted_turn_budget(self):
        with tempfile.TemporaryDirectory() as root:
            ctx = Context(root)
            ctx.state['counters']['turns_today'] = 4
            self.assertIn('max_turns_per_day', engine.worker_launch_guard(ctx))
            self.assertEqual(engine.worker_launch_guard(ctx, counted_turn=True), '')
            ctx.state['counters']['turns_today'] = 5
            self.assertIn('max_turns_per_day', engine.worker_launch_guard(ctx, counted_turn=True))

    def test_maintenance_and_parallel_limit_stop_new_launches(self):
        with tempfile.TemporaryDirectory() as root:
            ctx = Context(root)
            ctx.state['maintenance'] = True
            self.assertIn('maintenance', engine.worker_launch_guard(ctx))
            ctx.state['maintenance'] = False
            ctx.state['slots'] = {'A': {'task': 'one'}, 'B': {'task': 'two'}}
            self.assertIn('parallel', engine.worker_launch_guard(ctx))
            self.assertEqual(engine.worker_launch_guard(ctx, replacing_slot='A'), '')


if __name__ == '__main__':
    unittest.main()
