import json
import os
import sys
import tempfile
import unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import autoloop as engine


class LifecycleReportingTests(unittest.TestCase):
    def test_issue_report_flags_reopen_and_unverified_closure_without_writing(self):
        with tempfile.TemporaryDirectory() as root:
            ctx = type('Context', (), {})()
            ctx.alo = root
            ctx.state = {'slots': {'A': None, 'B': None}, 'prs_seen': {'auto/x': 'MERGED'}}
            ctx.coll = lambda name: [{'id': 'task-1', 'issue': 42, 'branch': 'auto/x',
                                      'status': 'pr_open', 'pr': 8, 'verify': 'green'},
                                     {'id': 'task-2', 'issue': 44, 'branch': 'auto/y',
                                      'status': 'open', 'verify': 'pending'}]
            report = engine.issue_lifecycle_report(ctx, [
                {'number': 42, 'state': 'OPEN', 'title': 'bug'},
                {'number': 43, 'state': 'CLOSED', 'title': 'untracked'},
                {'number': 44, 'state': 'CLOSED', 'title': 'linked unverified'},
            ])
            self.assertEqual(report['reopened'], [42])
            self.assertEqual(report['unverified_closed'], [44])
            self.assertEqual(report['untracked_closed'], [43])
            self.assertFalse(os.path.exists(os.path.join(root, 'issue_state.json')))

    def test_pr_lifecycle_is_read_only_and_tracks_checks_review_and_conflicts(self):
        row = {'number': 7, 'headRefName': 'auto/one', 'state': 'OPEN',
               'mergeStateStatus': 'DIRTY', 'statusCheckRollup': [{'name': 'ci', 'state': 'PENDING'}],
               'reviewDecision': 'CHANGES_REQUESTED', 'createdAt': '2026-01-01T00:00:00Z'}
        data = engine.pr_lifecycle_snapshot(row)
        self.assertEqual(data['ci'], 'pending')
        self.assertEqual(data['mergeability'], 'conflict')
        self.assertEqual(data['review'], 'changes_requested')
        self.assertEqual(data['state'], 'open')

    def test_report_outbox_emits_delta_once_and_saves_for_retry(self):
        with tempfile.TemporaryDirectory() as root:
            ctx = type('Context', (), {'alo': root, 'state': {'slots': {'A': None, 'B': None},
                'counters': {'turns_today': 1}, 'last_report_at': 0},
                'cap': lambda self, key: 7200})()
            first = engine.report_outbox(ctx, {'changed': ['task-1']}, force=True)
            second = engine.report_outbox(ctx, {'changed': ['task-1']}, force=True)
            self.assertTrue(first['emitted'])
            self.assertFalse(second['emitted'])
            rows = engine.read_jsonl(os.path.join(root, 'report_outbox.jsonl'))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
