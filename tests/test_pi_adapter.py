import os
import sys
import tempfile
import unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import autoloop as engine


class PiAdapterTests(unittest.TestCase):
    def test_pi_is_recognized_with_print_mode_and_explicit_resume(self):
        spec = engine.WORKER_CLIS.get('pi', {})
        self.assertTrue(spec, 'PI must be selectable as a worker')
        self.assertIn('-p', spec['must_support'])
        self.assertIn('--mode', spec['must_support'])
        self.assertIn('--session', spec['resume_flag'])
        self.assertNotIn('--continue', spec['resume_flag'])

    def test_pi_preflight_never_uses_dsh_credentials_as_evidence(self):
        report = engine.worker_account_hint('pi')
        self.assertIn('pi', report)
        self.assertNotIn('dsh', report)
        self.assertIn('未验证', report)

    def test_pi_json_session_header_is_captured_for_repair(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, 'worker.log')
            with open(path, 'w') as stream:
                stream.write('{"type":"session","version":3,"id":"pi-episode-123"}\n')
            ctx = type('Context', (), {'job_dir': root})()
            meta = {'jid': 'pi-job', 'log': path, 'cli': 'pi'}
            self.assertEqual(engine.capture_session(ctx, meta), 'pi-episode-123')


if __name__ == '__main__':
    unittest.main()
