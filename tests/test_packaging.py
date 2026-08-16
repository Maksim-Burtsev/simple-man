import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"
SYNC = ROOT / "scripts" / "sync_surfaces.py"


class PackagingTests(unittest.TestCase):
    def tree_snapshot(self, root: Path) -> dict[str, tuple[str, int, bytes | str]]:
        snapshot: dict[str, tuple[str, int, bytes | str]] = {}
        for path in sorted((root, *root.rglob("*"))):
            rel = "." if path == root else path.relative_to(root).as_posix()
            mode = stat.S_IMODE(path.lstat().st_mode)
            if path.is_symlink():
                snapshot[rel] = ("symlink", mode, os.readlink(path))
            elif path.is_dir():
                snapshot[rel] = ("dir", mode, b"")
            else:
                snapshot[rel] = ("file", mode, path.read_bytes())
        return snapshot

    def run_installer(
        self,
        home: Path,
        *,
        codex_home: Path | None = None,
        skill_root: Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["HOME"] = str(home)
        env.pop("CODEX_HOME", None)
        env.pop("SIMPLE_MAN_SKILL_ROOT", None)
        if codex_home is not None:
            env["CODEX_HOME"] = str(codex_home)
        if skill_root is not None:
            env["SIMPLE_MAN_SKILL_ROOT"] = str(skill_root)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", str(INSTALLER)],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
        )

    def assert_installed_skill(self, skill_dir: Path) -> None:
        self.assertEqual(
            (skill_dir / "SKILL.md").read_bytes(),
            (ROOT / "skills" / "simple-man" / "SKILL.md").read_bytes(),
        )
        self.assertEqual(
            (skill_dir / "agents" / "openai.yaml").read_bytes(),
            (ROOT / "skills" / "simple-man" / "agents" / "openai.yaml").read_bytes(),
        )

    def assert_one_discoverable_skill(self, skills_root: Path) -> None:
        self.assertEqual(
            [path.relative_to(skills_root).as_posix() for path in skills_root.rglob("SKILL.md")],
            ["simple-man/SKILL.md"],
        )

    def test_new_install_uses_portable_skill_root_and_codex_agents_surface(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()

            result = self.run_installer(home)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_installed_skill(home / ".agents" / "skills" / "simple-man")
            self.assert_one_discoverable_skill(home / ".agents" / "skills")
            self.assertTrue((home / ".codex" / "AGENTS.md").is_file())
            self.assertFalse((home / ".codex" / "skills" / "simple-man").exists())

    def test_explicit_codex_home_owns_skill_and_agents_surface(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            codex_home = Path(tmp) / "custom-codex"
            legacy = home / ".codex" / "skills" / "simple-man"
            legacy.mkdir(parents=True)
            (legacy / "SKILL.md").write_text("legacy sentinel\n")

            result = self.run_installer(home, codex_home=codex_home)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_installed_skill(codex_home / "skills" / "simple-man")
            self.assert_one_discoverable_skill(codex_home / "skills")
            self.assertTrue((codex_home / "AGENTS.md").is_file())
            self.assertFalse((home / ".agents" / "skills" / "simple-man").exists())
            self.assertEqual((legacy / "SKILL.md").read_text(), "legacy sentinel\n")

    def test_existing_legacy_install_is_upgraded_without_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            legacy = home / ".codex" / "skills" / "simple-man"
            legacy.mkdir(parents=True)
            (legacy / "SKILL.md").write_text("legacy skill\n")
            old_backup = legacy.with_name("simple-man.backup")
            old_backup.mkdir()
            (old_backup / "SKILL.md").write_text("old discoverable backup\n")

            result = self.run_installer(home)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_installed_skill(legacy)
            self.assert_one_discoverable_skill(home / ".codex" / "skills")
            self.assertFalse(old_backup.exists())
            self.assertFalse((home / ".agents" / "skills" / "simple-man").exists())

    def test_explicit_skill_root_wins_over_codex_home_and_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            codex_home = root / "custom-codex"
            skill_root = root / "portable skills"
            legacy = home / ".codex" / "skills" / "simple-man"
            legacy.mkdir(parents=True)
            (legacy / "SKILL.md").write_text("keep legacy\n")

            result = self.run_installer(
                home,
                codex_home=codex_home,
                skill_root=skill_root,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_installed_skill(skill_root / "simple-man")
            self.assert_one_discoverable_skill(skill_root)
            self.assertEqual((legacy / "SKILL.md").read_text(), "keep legacy\n")
            self.assertFalse((codex_home / "skills" / "simple-man").exists())
            self.assertTrue((codex_home / "AGENTS.md").is_file())
            self.assertIn(
                str(skill_root / "simple-man"),
                (codex_home / "AGENTS.md").read_text(),
            )

    def test_empty_explicit_codex_home_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()

            result = self.run_installer(home, extra_env={"CODEX_HOME": ""})

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("CODEX_HOME", result.stderr)
            self.assertEqual(list(home.iterdir()), [])

    def test_empty_or_relative_explicit_skill_root_fails_closed(self):
        for value in ("", "relative/skills"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp) / "home"
                home.mkdir()

                result = self.run_installer(
                    home,
                    extra_env={"SIMPLE_MAN_SKILL_ROOT": value},
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("SIMPLE_MAN_SKILL_ROOT", result.stderr)
                self.assertEqual(list(home.iterdir()), [])

    def test_installer_rerun_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()

            first = self.run_installer(home)
            agents_file = home / ".codex" / "AGENTS.md"
            first_agents = agents_file.read_bytes()
            second = self.run_installer(home)

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(agents_file.read_bytes(), first_agents)
            agents = first_agents.decode()
            self.assertEqual(agents.count("simple-man-always-on-begin"), 1)
            self.assertEqual(agents.count("simple-man-always-on-end"), 1)
            self.assert_one_discoverable_skill(home / ".agents" / "skills")
            self.assertFalse(
                (home / ".agents" / "skills" / "simple-man.backup").exists()
            )

    def test_malformed_managed_block_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            agents_file = home / ".codex" / "AGENTS.md"
            agents_file.parent.mkdir(parents=True)
            malformed = (
                "<!-- simple-man-always-on-end -->\n"
                "<!-- simple-man-always-on-begin -->\n"
            )
            agents_file.write_text(malformed)
            before = self.tree_snapshot(home)

            result = self.run_installer(home)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("malformed managed block", result.stderr)
            self.assertEqual(self.tree_snapshot(home), before)

    def test_symlinked_agents_file_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            codex_dir = home / ".codex"
            codex_dir.mkdir(parents=True)
            target = root / "shared-AGENTS.md"
            target.write_text("# Shared instructions\n")
            target.chmod(0o640)
            agents_file = codex_dir / "AGENTS.md"
            relative_target = os.path.relpath(target, agents_file.parent)
            agents_file.symlink_to(relative_target)

            first = self.run_installer(home)
            second = self.run_installer(home)

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertTrue(agents_file.is_symlink())
            self.assertEqual(os.readlink(agents_file), relative_target)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
            self.assertIn("# Shared instructions", target.read_text())
            self.assertIn("simple-man-always-on-begin", target.read_text())
            self.assertEqual(target.read_text().count("simple-man-always-on-begin"), 1)

    def test_broken_agents_symlink_fails_before_installing_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            codex_dir = home / ".codex"
            codex_dir.mkdir(parents=True)
            agents_file = codex_dir / "AGENTS.md"
            agents_file.symlink_to("missing/AGENTS.md")
            before = self.tree_snapshot(home)

            result = self.run_installer(home)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("symlink", result.stderr)
            self.assertEqual(self.tree_snapshot(home), before)

    def test_downloaded_snippet_with_managed_marker_fails_before_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            source = root / "source"
            source_skill = source / "skills" / "simple-man"
            (source_skill / "agents").mkdir(parents=True)
            shutil.copy2(ROOT / "skills" / "simple-man" / "SKILL.md", source_skill / "SKILL.md")
            shutil.copy2(
                ROOT / "skills" / "simple-man" / "agents" / "openai.yaml",
                source_skill / "agents" / "openai.yaml",
            )
            (source / "AGENTS.md.snippet").write_text(
                "<!-- simple-man-always-on-begin -->\nunsafe nested block\n"
            )
            home.mkdir()
            before = self.tree_snapshot(home)
            env = os.environ.copy()
            env["HOME"] = str(home)
            env.pop("CODEX_HOME", None)
            env.pop("SIMPLE_MAN_SKILL_ROOT", None)
            env["SIMPLE_MAN_RAW_BASE"] = source.as_uri()

            result = subprocess.run(
                ["bash", "-s"],
                cwd=root,
                env=env,
                input=INSTALLER.read_text(),
                text=True,
                capture_output=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("managed marker", result.stderr)
            self.assertEqual(self.tree_snapshot(home), before)

    def test_agents_write_failure_restores_skill_backup_and_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            codex_home = root / "codex"
            skill = codex_home / "skills" / "simple-man"
            backup = codex_home / "skills" / "simple-man.backup"
            skill.mkdir(parents=True)
            backup.mkdir()
            (skill / "SKILL.md").write_text("installed before run\n")
            (backup / "SKILL.md").write_text("backup before run\n")
            agents_file = codex_home / "AGENTS.md"
            agents_file.write_text("# Agents before run\n")
            agents_file.chmod(0o640)
            before = self.tree_snapshot(codex_home)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            real_mv = shutil.which("mv")
            self.assertIsNotNone(real_mv)
            fake_mv = fake_bin / "mv"
            fail_marker = root / "failed-after-agents-mv"
            fake_mv.write_text(
                "#!/bin/sh\n"
                'if [ "$#" -eq 2 ] && [ "$2" = "$FAIL_AGENTS_TARGET" ] '
                '&& [ ! -e "$FAIL_MARKER" ]; then\n'
                f'  "{real_mv}" "$@"\n'
                '  : > "$FAIL_MARKER"\n'
                "  exit 73\n"
                "fi\n"
                f'exec "{real_mv}" "$@"\n'
            )
            fake_mv.chmod(0o755)

            result = self.run_installer(
                home,
                codex_home=codex_home,
                extra_env={
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                    "FAIL_AGENTS_TARGET": str(agents_file),
                    "FAIL_MARKER": str(fail_marker),
                },
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(fail_marker.exists())
            self.assertEqual(self.tree_snapshot(codex_home), before)

    def test_symlinked_agents_write_failure_restores_target_and_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            codex_home = home / ".codex"
            codex_home.mkdir(parents=True)
            target = root / "shared-AGENTS.md"
            target.write_text("# Shared before failure\n")
            target.chmod(0o640)
            agents_file = codex_home / "AGENTS.md"
            link_text = os.path.relpath(target, agents_file.parent)
            agents_file.symlink_to(link_text)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            real_mv = shutil.which("mv")
            self.assertIsNotNone(real_mv)
            fail_marker = root / "failed-after-symlink-target-mv"
            fake_mv = fake_bin / "mv"
            fake_mv.write_text(
                "#!/bin/sh\n"
                'if [ "$#" -eq 2 ] && [ "$2" = "$FAIL_AGENTS_TARGET" ] '
                '&& [ ! -e "$FAIL_MARKER" ]; then\n'
                f'  "{real_mv}" "$@"\n'
                '  : > "$FAIL_MARKER"\n'
                "  exit 73\n"
                "fi\n"
                f'exec "{real_mv}" "$@"\n'
            )
            fake_mv.chmod(0o755)
            before_target = (target.read_bytes(), stat.S_IMODE(target.stat().st_mode))

            result = self.run_installer(
                home,
                extra_env={
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                    "FAIL_AGENTS_TARGET": str(target.resolve()),
                    "FAIL_MARKER": str(fail_marker),
                },
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(fail_marker.exists())
            self.assertTrue(agents_file.is_symlink())
            self.assertEqual(os.readlink(agents_file), link_text)
            self.assertEqual(
                (target.read_bytes(), stat.S_IMODE(target.stat().st_mode)),
                before_target,
            )
            self.assertFalse((home / ".agents").exists())

    def test_sync_surfaces_write_then_check_repairs_only_generated_surfaces(self):
        self.assertTrue(SYNC.is_file(), "sync_surfaces.py must exist")
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            canonical = repo / "skills" / "simple-man"
            plugin = repo / "plugins" / "simple-man" / "skills" / "simple-man"
            scripts = repo / "scripts"
            candidate = repo / "evals" / "policies" / "simple_man_candidate_runtime.md"
            (canonical / "agents").mkdir(parents=True)
            plugin.mkdir(parents=True)
            scripts.mkdir()
            candidate.parent.mkdir(parents=True)
            snippet = b"## Canonical policy\n\nKeep material facts.\n"
            (repo / "AGENTS.md.snippet").write_bytes(snippet)
            (canonical / "SKILL.md").write_text("canonical skill\n")
            (canonical / "SKILL.md").chmod(0o750)
            (canonical / "agents" / "openai.yaml").write_text("display: canonical\n")
            (plugin / "SKILL.md").write_text("stale skill\n")
            (plugin / "stale.txt").write_text("remove me\n")
            candidate.write_text("candidate sentinel\n")
            candidate_stat = candidate.stat()
            candidate_before = (
                candidate.read_bytes(),
                stat.S_IMODE(candidate_stat.st_mode),
                candidate_stat.st_mtime_ns,
            )
            shutil.copy2(SYNC, scripts / SYNC.name)

            written = subprocess.run(
                ["python3", str(scripts / SYNC.name), "--write"],
                cwd=repo,
                text=True,
                capture_output=True,
            )

            self.assertEqual(written.returncode, 0, written.stderr)
            for name in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"):
                self.assertEqual((repo / name).read_bytes(), snippet)
            self.assertEqual((plugin / "SKILL.md").read_text(), "canonical skill\n")
            self.assertEqual(stat.S_IMODE((plugin / "SKILL.md").stat().st_mode), 0o750)
            self.assertEqual(
                (plugin / "agents" / "openai.yaml").read_text(),
                "display: canonical\n",
            )
            self.assertFalse((plugin / "stale.txt").exists())
            self.assertEqual(
                (
                    candidate.read_bytes(),
                    stat.S_IMODE(candidate.stat().st_mode),
                    candidate.stat().st_mtime_ns,
                ),
                candidate_before,
            )

            checked = subprocess.run(
                ["python3", str(scripts / SYNC.name), "--check"],
                cwd=repo,
                text=True,
                capture_output=True,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)

            (plugin / "stale-empty-directory").mkdir()
            extra_directory = subprocess.run(
                ["python3", str(scripts / SYNC.name), "--check"],
                cwd=repo,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(extra_directory.returncode, 0)
            subprocess.run(
                ["python3", str(scripts / SYNC.name), "--write"],
                cwd=repo,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertFalse((plugin / "stale-empty-directory").exists())

            (repo / "CLAUDE.md").write_text("drift\n")
            drifted = subprocess.run(
                ["python3", str(scripts / SYNC.name), "--check"],
                cwd=repo,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(drifted.returncode, 0)
            self.assertIn("CLAUDE.md", drifted.stderr)
            self.assertEqual(
                (
                    candidate.read_bytes(),
                    stat.S_IMODE(candidate.stat().st_mode),
                    candidate.stat().st_mtime_ns,
                ),
                candidate_before,
            )

    def test_plugin_metadata_does_not_claim_installation_is_always_on(self):
        manifest = json.loads(
            (ROOT / "plugins" / "simple-man" / ".codex-plugin" / "plugin.json").read_text()
        )

        interface_text = " ".join(
            str(manifest["interface"].get(field, ""))
            for field in ("shortDescription", "longDescription")
        ).lower()
        self.assertNotIn("always-on", interface_text)

    def test_docs_separate_portable_plugin_and_always_on_installation(self):
        for name in ("README.md", "INSTALL.md"):
            with self.subTest(name=name):
                text = (ROOT / name).read_text()
                self.assertIn("## Portable Agent Skill", text)
                self.assertIn("## Codex Plugin", text)
                self.assertIn("## Always-on Codex policy", text)
                self.assertIn("does not enable the always-on policy", text)


if __name__ == "__main__":
    unittest.main()
