import json
import os
import sys
import unittest
sys.path.insert(0, os.path.dirname(__file__))
from selftest import Box
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
from autoloop import read_jsonl


class SupervisionCliTests(unittest.TestCase):
    def test_metrics_reports_load_quality_budget_without_model_call(self):
        box = Box('supervisionmetrics')
        try:
            box.add_task('Metrics task', ['src/*'])
            rc, output = box.run('metrics', '--json')
            self.assertEqual(rc, 0, output)
            data = json.loads(output)
            self.assertEqual(data['budget']['grok_daily_max'], 0)
            self.assertEqual(data['load']['pool'], 1)
            self.assertIn('gate_red', data['quality'])
            self.assertEqual(data['load']['active_slots'], 0)
        finally:
            box.cleanup()

    def test_report_outbox_cli_saves_pending_report_and_suppresses_duplicate(self):
        box = Box('supervisionreport', caps={'report_interval_s': 7200})
        try:
            rc, output = box.run('report', '--outbox', '--force')
            self.assertEqual(rc, 0, output)
            self.assertTrue(json.loads(output).get('emitted'))
            rc2, output2 = box.run('report', '--outbox', '--force')
            self.assertEqual(rc2, 1, output2)
            self.assertEqual(json.loads(output2).get('reason'), 'unchanged')
            path = os.path.join(box.alo, 'report_outbox.jsonl')
            self.assertTrue(os.path.exists(path))
            with open(path) as stream:
                rows = [json.loads(line) for line in stream if line.strip()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['status'], 'pending')
        finally:
            box.cleanup()

    def test_mark_sent_updates_outbox_and_rejects_unknown_or_duplicate(self):
        box = Box('supervisionsent', caps={'report_interval_s': 7200})
        try:
            rc, output = box.run('report', '--outbox', '--force')
            report_id = json.loads(output)['record']['report_id']
            self.assertEqual(box.run('report', '--mark-sent', report_id)[0], 0)
            rows = read_jsonl(os.path.join(box.alo, 'report_outbox.jsonl'))
            self.assertEqual(rows[0]['status'], 'sent')
            self.assertIn('sent_at', rows[0])
            self.assertEqual(box.run('report', '--mark-sent', report_id)[0], 2)
            self.assertEqual(box.run('report', '--mark-sent', 'nope')[0], 2)
        finally:
            box.cleanup()

    def test_lifecycle_command_surfaces_gh_failure_without_writing(self):
        box = Box('supervisionlifecycle')
        try:
            box.env['AUTOLOOP_GH'] = '/nonexistent/gh'
            rc, output = box.run('lifecycle', '--json')
            self.assertEqual(rc, 3)
            self.assertIn('error', json.loads(output))
            self.assertFalse(os.path.exists(os.path.join(box.alo, 'issue_state.json')))
        finally:
            box.cleanup()


if __name__ == '__main__':
    unittest.main()
