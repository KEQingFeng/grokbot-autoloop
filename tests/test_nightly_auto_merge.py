"""The nightly auto-merge driver: what `install-cron.sh` prints, installs, removes.

The engine side lives in selftest.py (`s_nightly_auto_merge`). This file covers the one
thing a user actually runs to get a nightly merge — the wake-up driver — against a fake
`crontab(1)` in a temp dir. Nothing here touches the machine's real crontab.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from selftest import Box  # noqa: E402  (the throwaway repo + initialised .autoloop/)

INSTALLER = Path(__file__).resolve().parents[1] / "install-cron.sh"

CRONTAB_SHIM = """#!/bin/sh
# Minimal crontab(1) stand-in: the table lives in $FAKE_CRONTAB_STORE.
STORE="${FAKE_CRONTAB_STORE:?}"
case "${1:-}" in
  -l) if [ -f "$STORE" ]; then cat "$STORE"; else exit 1; fi ;;
  -r) rm -f "$STORE" ;;
  -)  cat > "$STORE" ;;
  "") cat > "$STORE" ;;
  *)  if [ -f "$1" ]; then cp "$1" "$STORE"; else echo "fake crontab: $1 is not a table" >&2; exit 1; fi ;;
esac
"""

TOOLS = ("sed", "grep", "wc", "tr", "pgrep", "dirname", "basename", "cat", "cp",
         "rm", "mktemp", "date", "sort", "head")


class NightlyAutoMergeDriverTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="autoloop-driver-"))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.bin = self.work / "bin"
        self.bin.mkdir()
        shim = self.bin / "crontab"
        shim.write_text(CRONTAB_SHIM)
        shim.chmod(0o755)
        self.store = self.work / "crontab.table"

    def bare_bin(self):
        """A PATH with everything the installer needs *except* crontab(1)."""
        bare = self.work / "bare"
        bare.mkdir(exist_ok=True)
        for name in TOOLS:
            found = shutil.which(name)
            if found:
                os.symlink(found, str(bare / name))
        # python3 comes from its own real directory: the installer resolves `$PY` into
        # the crontab line it prints, so a symlinked interpreter living under a path
        # with a space would break the very line this test is checking.
        return "%s:%s" % (bare, os.path.dirname(sys.executable))

    def run_installer(self, repo, *args, crontab=True, timeout=60):
        home = str(Path(repo).parent)
        if crontab:
            path = "%s:%s:/usr/bin:/bin" % (self.bin, os.path.dirname(sys.executable))
        else:
            path = self.bare_bin()
        env = {"HOME": home, "PATH": path, "FAKE_CRONTAB_STORE": str(self.store)}
        return subprocess.run(["/bin/bash", str(INSTALLER), "--repo", repo, *args],
                              cwd=home, env=env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout)

    def table_lines(self):
        if not self.store.exists():
            return []
        return [l for l in self.store.read_text().splitlines() if l.strip()]

    def cron_lines(self, text, mark="autoloop-tick"):
        """The driver line(s) carrying `mark` in the installer's output.

        v1.6 split the driver into three schedules on purpose — an expensive tick, a
        cheap 15-minute `light`, and an hourly silent report — so the tick line is no
        longer the only crontab line. Filtering by mark keeps this assertion about the
        line it claims to be about.
        """
        lines = [l for l in text.splitlines()
                 if l.strip().startswith("*/") and mark in l]
        self.assertEqual(len(lines), 1, text)
        return lines[0].strip()

    def enable(self, box, regression="bash tests/run.sh"):
        args = ["init", "--auto-merge"]
        if regression:
            args += ["--merge-regression-cmd", regression]
        rc, out = box.run(*args)
        self.assertEqual(rc, 0, out)

    # -- the switch itself -------------------------------------------------------

    def test_init_auto_merge_writes_the_cap_and_merges_nothing(self):
        box = Box("driverinit")
        try:
            self.assertFalse((box.config().get("caps") or {}).get("merge_enabled"),
                             "默认 caps.merge_enabled=false")
            rc, out = box.run("init", "--auto-merge")
            self.assertEqual(rc, 0, out)
            self.assertTrue(box.config()["caps"]["merge_enabled"], box.config()["caps"])
            self.assertIn("循环本身永不合并", out)
            self.assertIn("install-cron.sh", out)
            # turning the cap on must not merge, review or dispatch anything
            self.assertFalse(os.path.exists(os.path.join(box.alo, "merge.json")))
            self.assertFalse(os.path.exists(os.path.join(box.alo, "review.json")))
            self.assertEqual(box.state()["counters"]["jobs_today"], 0)
        finally:
            box.cleanup()

    def test_merge_regression_cmd_alone_does_not_open_the_line(self):
        box = Box("driverreg")
        try:
            rc, out = box.run("init", "--merge-regression-cmd", "bash tests/run.sh")
            self.assertEqual(rc, 0, out)
            cfg = box.config()
            self.assertEqual(cfg.get("merge_regression_cmd"), "bash tests/run.sh")
            self.assertFalse((cfg.get("caps") or {}).get("merge_enabled"),
                             "配置回归命令不等于授权合并：%s" % cfg.get("caps"))
        finally:
            box.cleanup()

    # -- what the driver prints --------------------------------------------------

    def test_print_with_the_line_off_shows_tick_only(self):
        box = Box("driveroff")
        try:
            proc = self.run_installer(box.repo)
            self.assertEqual(proc.returncode, 0, proc.stdout)
            self.assertIn("合并线：关闭", proc.stdout)
            self.assertIn("init --auto-merge --merge-regression-cmd", proc.stdout)
            line = self.cron_lines(proc.stdout)
            self.assertIn("tick --quiet", line)
            self.assertNotIn("review", line)
            self.assertNotIn("--yes-i-will-merge", line)
            self.assertIn("NOTHING WAS WRITTEN", proc.stdout)
            self.assertEqual(self.table_lines(), [], "打印模式不装 crontab")
        finally:
            box.cleanup()

    def test_print_with_the_line_on_shows_review_then_merge(self):
        box = Box("driveron")
        try:
            self.enable(box)
            proc = self.run_installer(box.repo)
            self.assertEqual(proc.returncode, 0, proc.stdout)
            self.assertIn("合并线：已启用", proc.stdout)
            self.assertIn("合并后回归：bash tests/run.sh", proc.stdout)
            self.assertIn("watch 只跑 tick", proc.stdout)
            self.assertIn("merge.out", proc.stdout)
            line = self.cron_lines(proc.stdout)
            self.assertIn("tick --quiet", line)
            self.assertIn("review --json", line)
            self.assertIn("merge --yes-i-will-merge --now --json", line)
            self.assertLess(line.index("review --json"), line.index("merge --yes-i-will-merge"),
                            "审查排在合并之前，顺序写死在驱动里：%s" % line)
            # `;` not `&&`: a tick that found the lock held must not cancel the merge
            self.assertIn("cron.out 2>&1; ", line, line)
            self.assertNotIn("cron.out 2>&1 && ", line, line)
        finally:
            box.cleanup()

    def test_print_warns_when_the_line_has_no_regression_command(self):
        box = Box("drivernoreg")
        try:
            self.enable(box, regression=None)
            proc = self.run_installer(box.repo)
            self.assertIn("未配 merge_regression_cmd", proc.stdout)
            self.assertIn("pending_verification", proc.stdout)
        finally:
            box.cleanup()

    def test_hand_edited_config_produces_the_same_driver(self):
        box = Box("driveredit")
        try:
            cfg = box.config()
            cfg.setdefault("caps", {})["merge_enabled"] = True
            cfg["merge_regression_cmd"] = "make ci"
            box.write_config(cfg)
            proc = self.run_installer(box.repo)
            self.assertIn("合并线：已启用", proc.stdout)
            self.assertIn("合并后回归：make ci", proc.stdout)
            self.assertIn("make ci", json.dumps(cfg))
        finally:
            box.cleanup()

    def test_no_crontab_still_prints_the_other_drivers(self):
        box = Box("drivernocrontab")
        try:
            self.enable(box)
            proc = self.run_installer(box.repo, crontab=False)
            self.assertEqual(proc.returncode, 0, proc.stdout)
            self.assertIn("no crontab(1)", proc.stdout)
            self.assertIn("Option 1", proc.stdout)
            self.assertIn("review --json", proc.stdout)
        finally:
            box.cleanup()

    # -- argument validation ------------------------------------------------------

    def test_missing_flag_value_is_refused_not_hung(self):
        """`--repo`/`--every` with no value used to loop forever (no output, no exit)."""
        box = Box("driverdangling")
        try:
            for dangling in ("--repo", "--every"):
                with self.subTest(flag=dangling):
                    proc = self.run_installer(box.repo, dangling, timeout=10)
                    self.assertEqual(proc.returncode, 2, proc.stdout)
                    self.assertIn(dangling, proc.stdout)
                    self.assertFalse(proc.stdout.startswith("== AutoLoop"),
                                     "must fail before printing or writing anything")
                    self.assertEqual(self.table_lines(), [], self.store.read_text()
                                     if self.store.exists() else "")
        finally:
            box.cleanup()

    def test_invalid_every_is_refused_before_any_cron_line(self):
        box = Box("driverbadevery")
        try:
            for value in ("0", "abc", "1.5", "-1", ""):
                with self.subTest(every=value):
                    proc = self.run_installer(box.repo, "--write", "--every", value, timeout=10)
                    self.assertEqual(proc.returncode, 2, proc.stdout)
                    self.assertNotIn("PASS", proc.stdout)
                    self.assertEqual(self.table_lines(), [], self.store.read_text()
                                     if self.store.exists() else "")
        finally:
            box.cleanup()

    # -- installing and removing --------------------------------------------------

    def test_write_installs_the_full_round_and_reads_it_back(self):
        box = Box("driverwrite")
        try:
            self.enable(box)
            proc = self.run_installer(box.repo, "--write", "--every", "15")
            self.assertEqual(proc.returncode, 0, proc.stdout)
            self.assertIn("PASS", proc.stdout)
            lines = self.table_lines()
            # v1.6: three schedules — tick (+merge), the 15-minute light check, and the
            # hourly silent report.
            self.assertEqual(len(lines), 3, lines)
            tick = next(l for l in lines if "autoloop-tick" in l)
            self.assertTrue(tick.startswith("*/15 * * * * "), tick)
            for stage in ("tick --quiet", "review --json", "merge --yes-i-will-merge --now"):
                self.assertIn(stage, tick, tick)
        finally:
            box.cleanup()

    def test_write_installs_light_and_hourly_report_too(self):
        box = Box("driverlight")
        try:
            proc = self.run_installer(box.repo, "--write", "--every", "10")
            self.assertEqual(proc.returncode, 0, proc.stdout)
            lines = self.table_lines()
            self.assertEqual(len(lines), 3, lines)
            light = next(l for l in lines if l.rstrip().endswith("# autoloop-light"))
            report = next(l for l in lines if l.rstrip().endswith("# autoloop-report"))
            # `light` keeps its own fixed 15-minute cadence: it is cheap, and its whole
            # value is being there soon enough to catch a wedged worker before the
            # budget is gone. `--every` belongs to the expensive tick only.
            self.assertTrue(light.startswith("*/15 * * * * "), light)
            self.assertNotIn("--quiet", light, "轻检测成功静默，不该有 --quiet")
            # 成功静默：report 用 --if-needed，而不是每小时无条件刷一屏
            self.assertTrue(report.startswith("0 * * * * "), report)
            self.assertIn("report --if-needed", report)
        finally:
            box.cleanup()

    def test_write_twice_leaves_one_of_each(self):
        box = Box("driveridem")
        try:
            self.enable(box)
            self.assertEqual(self.run_installer(box.repo, "--write").returncode, 0)
            proc = self.run_installer(box.repo, "--write")
            self.assertEqual(proc.returncode, 0, proc.stdout)
            lines = self.table_lines()
            self.assertEqual(len(lines), 3, "重装必须幂等，否则每跑一次就多三条：%s" % lines)
            # The trailing mark comment is what identifies a line as ours; the repo path
            # appears in all three, so only the mark is a safe discriminator.
            for mark in ("# autoloop-tick", "# autoloop-light", "# autoloop-report"):
                self.assertEqual(len([l for l in lines if l.rstrip().endswith(mark)]), 1, lines)
        finally:
            box.cleanup()

    def test_remove_leaves_someone_elses_cron_alone(self):
        box = Box("driverremove")
        try:
            self.store.write_text("0 6 * * * /usr/local/bin/other-job\n")
            self.enable(box)
            self.assertEqual(self.run_installer(box.repo, "--write").returncode, 0)
            self.assertIn("autoloop-tick", self.store.read_text())
            proc = self.run_installer(box.repo, "--remove")
            self.assertEqual(proc.returncode, 0, proc.stdout)
            self.assertIn("autoloop entries now: 0", proc.stdout)
            self.assertEqual(self.table_lines(), ["0 6 * * * /usr/local/bin/other-job"],
                             self.store.read_text())
        finally:
            box.cleanup()

    def test_a_line_off_round_never_installs_a_merge_stage(self):
        box = Box("driveroffwrite")
        try:
            proc = self.run_installer(box.repo, "--write")
            self.assertEqual(proc.returncode, 0, proc.stdout)
            self.assertIn("PASS", proc.stdout)
            lines = self.table_lines()
            for l in lines:
                self.assertNotIn("--yes-i-will-merge", l, l)
            tick = next(l for l in lines if "autoloop-tick" in l)
            self.assertIn("tick --quiet", tick)
        finally:
            box.cleanup()


if __name__ == "__main__":
    unittest.main()
