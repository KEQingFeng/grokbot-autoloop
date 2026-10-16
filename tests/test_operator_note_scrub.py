"""Operator-written notes must not persist a credential into a state file.

Two commands take a free-text note and write it into `.autoloop/*.json`:
`alerts --ack --note` (→ `alert_acks.json`) and `approve`/`reject --note`
(→ `candidates.json` / `queue.json`). Both stored it raw. The note is exactly the field
that invites a secret — the documented use is 「附一句处理说明」, so the natural thing to
paste there is the log line or the command being investigated — and README states the
invariant plainly: no credentials in state files. `alert()` and `ctx.event` already
scrub what they write; these two paths into persistent state did not.

Offline: throwaway Boxes from the self-test harness; `watchdog` and `alerts` touch no
network and no real `gh`.
"""

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from selftest import Box

TOKEN = "ghp_" + "A" * 36


class AlertAckScrubTests(unittest.TestCase):
    def setUp(self):
        self.box = Box("ackscrub", caps={"watchdog_stale_s": 60})
        self.box.patch_state(last_tick_at=int(time.time()) - 3600)
        self.box.run("watchdog")
        self.alert_id = json.loads(self.box.run("alerts", "--json")[1])["open"][0]["alert_id"]

    def tearDown(self):
        self.box.cleanup()

    def _acks(self):
        with open(os.path.join(self.box.alo, "alert_acks.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_a_credential_in_the_note_is_redacted_before_it_lands(self):
        rc, out = self.box.run("alerts", "--ack", self.alert_id,
                               "--note", "已在处理：%s" % TOKEN)
        self.assertEqual(rc, 0, out)
        ack = self._acks()[self.alert_id]
        self.assertNotIn("ghp_", json.dumps(ack, ensure_ascii=False),
                         "token persisted in alert_acks.json: %s" % ack)
        self.assertIn("<redacted:credential-pattern>", ack["note"])
        # the acknowledgement itself still happened, and still says what it said
        self.assertIn("已在处理", ack["note"])
        self.assertEqual(ack["dedupe_key"], "loop_stale")
        self.assertEqual(json.loads(self.box.run("alerts", "--json")[1])["open"], [])

    def test_an_ordinary_note_is_stored_verbatim(self):
        rc, out = self.box.run("alerts", "--ack", self.alert_id, "--note", "已在处理")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self._acks()[self.alert_id]["note"], "已在处理")


class TriageNoteScrubTests(unittest.TestCase):
    def setUp(self):
        self.box = Box("trinotescrub")
        self.tid = self.box.add_task("Rework the retry path", ["src/*"],
                                     kind="new_feature", pool="candidates.json")

    def tearDown(self):
        self.box.cleanup()

    def _row(self):
        for row in self.box.coll("candidates.json"):
            if row["id"] == self.tid:
                return row
        self.fail("task %s vanished from candidates.json" % self.tid)

    def test_a_credential_in_the_triage_note_is_redacted_before_it_lands(self):
        rc, out = self.box.run("reject", self.tid, "--note", "见日志 %s" % TOKEN)
        self.assertEqual(rc, 0, out)
        row = self._row()
        self.assertNotIn("ghp_", json.dumps(row, ensure_ascii=False),
                         "token persisted in the pool: %s" % row.get("triage_note"))
        self.assertIn("<redacted:credential-pattern>", row["triage_note"])
        # the decision is still recorded, and the note still reads as a note
        self.assertEqual(row["status"], "rejected")
        self.assertIn("见日志", row["triage_note"])

    def test_an_ordinary_triage_note_is_stored_verbatim(self):
        rc, out = self.box.run("approve", self.tid, "--note", "morning review")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self._row()["triage_note"], "morning review")


if __name__ == "__main__":
    unittest.main()
