import os
import sys
import tempfile
import unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import autoloop as engine


class ProgressSupervisionTests(unittest.TestCase):
    def test_repeated_log_activity_does_not_reset_real_progress_clock(self):
        with tempfile.TemporaryDirectory() as root:
            log = os.path.join(root, 'job.log')
            with open(log, 'w') as stream:
                stream.write('heartbeat 1\nheartbeat 2\n')
            now = [2000]
            original_now = engine.now
            engine.now = lambda: now[0]
            try:
                ctx = type('Context', (), {'cap': lambda self, key: 900,
                                           'event': lambda *args, **kwargs: None})()
                rec = {'started': 1000, 'last_real_progress_at': 1000,
                       'last_real_progress_token': 'commit:3|test:PASS'}
                meta = {'log': log, 'started': 800}
                result = engine.progress_snapshot(ctx, rec, meta, commits=3, test_state='PASS')
                self.assertEqual(result['state'], 'stalled')
                self.assertEqual(rec['last_real_progress_at'], 1000)
            finally:
                engine.now = original_now

    def test_new_commit_or_test_result_advances_progress_clock(self):
        with tempfile.TemporaryDirectory() as root:
            log = os.path.join(root, 'job.log')
            with open(log, 'w') as stream:
                stream.write('more output\n')
            now = [2500]
            original_now = engine.now
            engine.now = lambda: now[0]
            try:
                ctx = type('Context', (), {'cap': lambda self, key: 900,
                                           'event': lambda *args, **kwargs: None})()
                rec = {'last_real_progress_at': 1000, 'last_real_progress_token': 'commit:3|test:unknown'}
                meta = {'log': log, 'started': 800}
                result = engine.progress_snapshot(ctx, rec, meta, commits=4, test_state='PASS')
                self.assertEqual(result['state'], 'progressing')
                self.assertEqual(rec['last_real_progress_at'], 2500)
            finally:
                engine.now = original_now

    def test_repeated_failure_signature_trips_bounded_breaker(self):
        with tempfile.TemporaryDirectory() as root:
            ctx = type('Context', (), {'state': {'failure_signatures': {}},
                                       'cap': lambda self, key: 3,
                                       'event': lambda *args, **kwargs: None})()
            signature = engine.failure_signature(['tests failed: expected 3 got 2'])
            self.assertFalse(engine.record_failure_signature(ctx, signature))
            self.assertFalse(engine.record_failure_signature(ctx, signature))
            self.assertTrue(engine.record_failure_signature(ctx, signature))
            self.assertEqual(ctx.state['failure_signatures'][signature], 3)


if __name__ == '__main__':
    unittest.main()
