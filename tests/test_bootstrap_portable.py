import os
from pathlib import Path
import subprocess
import tempfile
import unittest


BOOTSTRAP = Path(__file__).resolve().parents[1] / "bootstrap.sh"


class BootstrapPortableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autoloop portable ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "python-calls"
        for name, body in {
            "sh": "exit 0\n",
            "git": "exit 0\n",
            "python3": 'printf "%s\\n" "$@" >> "$FAKE_PYTHON_CALLS"\nexit 3\n',
        }.items():
            command = self.bin / name
            command.write_text("#!/bin/bash\n" + body)
            command.chmod(0o755)
        self.env = {
            "HOME": str(self.home),
            "PATH": str(self.bin) + ":/usr/bin:/bin",
            "PYTHON": "python3",
            "FAKE_PYTHON_CALLS": str(self.calls),
        }

    def run_bootstrap(self, *args, **env):
        return subprocess.run(
            ["/bin/bash", str(BOOTSTRAP), *map(str, args)],
            cwd=self.root,
            env=dict(self.env, **env),
            text=True,
            capture_output=True,
            timeout=15,
        )

    def assert_installed(self, result, skills):
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        installed = skills / "grokbot-autoloop"
        self.assertTrue((installed / "bootstrap.sh").is_file(), result.stdout + result.stderr)
        self.assertEqual((installed / "bootstrap.sh").read_bytes(), BOOTSTRAP.read_bytes())
        self.assertEqual(
            self.calls.read_text().splitlines(),
            [str(installed / "scripts/autoloop.py"), "--repo", str(self.root.resolve()), "preflight"],
        )
        self.assertFalse((self.root / ".autoloop").exists())

    def snapshot(self):
        return sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))

    def test_help_writes_nothing(self):
        before = self.snapshot()
        result = self.run_bootstrap("--help", AUTOLOOP_SKILLS_DIR=str(self.root / "cloud skills"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_missing_or_empty_flag_stops_before_install(self):
        for args in [("--skills-dir",), ("--skills-dir", ""), ("--skills-dir", "--force")]:
            with self.subTest(args=args):
                before = self.snapshot()
                result = self.run_bootstrap(*args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("--skills-dir", result.stderr)
                self.assertEqual(self.snapshot(), before)

    def test_empty_new_env_stops_before_install(self):
        before = self.snapshot()
        result = self.run_bootstrap(AUTOLOOP_SKILLS_DIR="")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("AUTOLOOP_SKILLS_DIR", result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_default_remains_dsh_skills(self):
        self.assert_installed(self.run_bootstrap(), self.home / ".dsh/skills")

    def test_legacy_env_still_selects_directory(self):
        skills = self.root / "legacy skills"
        self.assert_installed(self.run_bootstrap(DSH_SKILLS=str(skills)), skills)
        self.assertFalse((self.home / ".dsh").exists())

    def test_new_env_overrides_legacy_env(self):
        skills = self.root / "cloud skills"
        legacy = self.root / "legacy"
        self.assert_installed(self.run_bootstrap(AUTOLOOP_SKILLS_DIR=str(skills), DSH_SKILLS=str(legacy)), skills)
        self.assertFalse(legacy.exists())
        self.assertFalse((self.home / ".dsh").exists())

    def test_flag_with_spaces_overrides_both_envs(self):
        skills = self.root / "cloud skills"
        new_env = self.root / "new env"
        legacy = self.root / "legacy"
        self.assert_installed(self.run_bootstrap("--skills-dir", skills,
                              AUTOLOOP_SKILLS_DIR=str(new_env), DSH_SKILLS=str(legacy)), skills)
        self.assertFalse(new_env.exists())
        self.assertFalse(legacy.exists())
        self.assertFalse((self.home / ".dsh").exists())

    def test_valid_flag_overrides_empty_new_env(self):
        skills = self.root / "cloud skills"
        self.assert_installed(self.run_bootstrap("--skills-dir", skills, AUTOLOOP_SKILLS_DIR=""), skills)

    def check_backup(self, skills, backup, *args, **env):
        installed = skills / "grokbot-autoloop"
        installed.mkdir(parents=True)
        (installed / "old-marker").write_text("previous version")
        self.assert_installed(self.run_bootstrap(*args, **env), skills)
        backups = list(backup.glob("grokbot-autoloop.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "old-marker").read_text(), "previous version")

    def test_flag_backup_defaults_to_sibling_with_trailing_slash(self):
        skills = self.root / "cloud" / "custom skills"
        self.check_backup(skills, self.root / "cloud/skills-backup", "--skills-dir", str(skills) + "/")
        self.assertFalse((self.home / ".dsh").exists())

    def test_new_env_backup_defaults_to_sibling(self):
        skills = self.root / "cloud/custom"
        self.check_backup(skills, self.root / "cloud/skills-backup", AUTOLOOP_SKILLS_DIR=str(skills))

    def test_legacy_env_backup_defaults_to_sibling(self):
        skills = self.root / "legacy/custom"
        self.check_backup(skills, self.root / "legacy/skills-backup", DSH_SKILLS=str(skills))

    def test_default_backup_remains_dsh_skills_backup(self):
        self.check_backup(self.home / ".dsh/skills", self.home / ".dsh/skills-backup")

    def test_backup_env_overrides_explicit_directory_default(self):
        skills = self.root / "cloud skills"
        backup = self.root / "custom backups"
        self.check_backup(skills, backup, "--skills-dir", skills, DSH_SKILLS_BACKUP=str(backup))
        self.assertFalse((self.root / "skills-backup").exists())


if __name__ == "__main__":
    unittest.main()
