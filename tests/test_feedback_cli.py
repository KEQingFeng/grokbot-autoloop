import json
import os
import sys
import tempfile
import unittest
sys.path.insert(0, os.path.dirname(__file__))
from selftest import Box
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import autoloop as engine


class FeedbackCliTests(unittest.TestCase):
    def test_import_promote_and_maintenance_via_real_cli(self):
        box = Box('feedbackcli')
        try:
            file = os.path.join(box.dir, 'feedback.json')
            with open(file, 'w') as stream:
                json.dump([{'source': 'github', 'source_id': 'owner/repo#7',
                            'title': 'Crash when adding values', 'body': 'crash',
                            'paths': ['src/app.py']}], stream)
            rc, output = box.run('feedback', 'import', '--file', file)
            self.assertEqual(rc, 0, output)
            item_id = json.loads(output)['items'][0]['id']
            rc, output = box.run('feedback', 'promote', '--id', item_id)
            self.assertEqual(rc, 0, output)
            with open(os.path.join(box.alo, 'queue.json')) as stream:
                task = json.load(stream)[0]
            self.assertFalse(task['approved'])
            self.assertEqual(task['policy'], 'needs_approval')
            ctx = engine.Ctx(box.repo)
            prompt = engine.task_prompt(ctx, dict(task, branch='auto/test'), box.repo, 'A', 'day')
            self.assertIn('crash', prompt)
            self.assertIn('untrusted data', prompt)
            rc, output = box.run('feedback', 'cycle', '--lane', 'maintenance')
            self.assertEqual(rc, 0, output)
            self.assertEqual(json.loads(output)['result'], 'maintenance_completed')
            self.assertTrue(os.path.exists(os.path.join(box.alo, 'lessons.json')))
        finally:
            box.cleanup()


if __name__ == '__main__':
    unittest.main()
