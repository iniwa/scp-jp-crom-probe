from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / "scripts" / "publish_monitor_feed.py"
SPEC = importlib.util.spec_from_file_location("publish_monitor_feed", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="monitor-feed-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.remote = self.root / "remote.git"
        self.repo = self.root / "source"
        self.public = self.repo / "monitor-output" / "public"
        self.run_git(self.root, "init", "--bare", str(self.remote))
        self.run_git(self.root, "init", "-b", "main", str(self.repo))
        self.run_git(self.repo, "config", "user.name", "Test")
        self.run_git(self.repo, "config", "user.email", "test@example.invalid")
        # Even on Windows the committed blobs must preserve generated LF bytes.
        self.run_git(self.repo, "config", "core.autocrlf", "true")
        (self.repo / "source.txt").write_text("source\n", encoding="utf-8")
        self.run_git(self.repo, "add", "source.txt")
        self.run_git(self.repo, "commit", "-m", "source")
        self.run_git(self.repo, "remote", "add", "origin", str(self.remote))
        self.run_git(self.repo, "push", "origin", "main")
        self.run_git(self.repo, "push", "origin", "HEAD:monitor-state")
        self.initial = self.run_git(self.repo, "rev-parse", "HEAD").strip()
        self.public.mkdir(parents=True)
        self.write_generation("2026-10-07T06:17:00+09:00")

    @staticmethod
    def run_git(repo, *args):
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True,
            capture_output=True, text=True, encoding="utf-8",
        ).stdout

    def write_generation(self, generation):
        for name in MODULE.FILES:
            document = {"generated_at_jst": generation, "articles": [{"title": "SCP-JP"}]}
            # Preserve whitespace and CRLF too; the publisher must not serialize.
            raw = (json.dumps(document, ensure_ascii=False, indent=2) + "\r\n").encode()
            (self.public / name).write_bytes(raw)

    def tip(self):
        return self.run_git(self.remote, "rev-parse", "refs/heads/monitor-feed").strip()

    def publish(self):
        return MODULE.publish_feed(self.repo, self.public)

    def assert_feed(self):
        names = self.run_git(self.remote, "ls-tree", "--name-only", "monitor-feed").splitlines()
        self.assertEqual(set(names), set(MODULE.FILES))
        for name in MODULE.FILES:
            raw = subprocess.run(
                ["git", "-C", str(self.remote), "show", f"monitor-feed:{name}"],
                check=True, capture_output=True,
            ).stdout
            self.assertEqual(raw, (self.public / name).read_bytes())
        self.assertEqual(self.run_git(self.repo, "branch", "--show-current").strip(), "main")
        self.assertEqual(self.run_git(self.repo, "rev-parse", "HEAD").strip(), self.initial)
        self.assertEqual(self.run_git(self.remote, "rev-parse", "monitor-state").strip(), self.initial)
        self.assertTrue((self.repo / "source.txt").is_file())
        self.assertEqual(self.run_git(self.repo, "worktree", "list").count("\n"), 1)

    def test_first_publication_is_orphan_and_byte_identical(self):
        self.assertTrue(self.publish())
        self.assert_feed()
        self.assertEqual(self.run_git(self.remote, "rev-list", "--count", "monitor-feed").strip(), "1")
        self.assertEqual(self.run_git(self.remote, "show", "-s", "--format=%an", "monitor-feed").strip(),
                         "github-actions[bot]")

    def test_subsequent_publication_adds_one_coherent_commit(self):
        self.publish()
        previous = self.tip()
        self.write_generation("2026-10-08T06:17:00+09:00")
        self.assertTrue(self.publish())
        self.assertEqual(self.run_git(self.remote, "rev-parse", "monitor-feed^").strip(), previous)
        self.assert_feed()

    def test_identical_bytes_create_no_commit(self):
        self.publish()
        previous = self.tip()
        self.assertFalse(self.publish())
        self.assertEqual(self.tip(), previous)
        self.assert_feed()

    def test_invalid_json_preserves_previous_feed(self):
        self.publish()
        previous = self.tip()
        (self.public / "delta.json").write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.tip(), previous)

    def test_mismatched_generation_preserves_previous_feed(self):
        self.publish()
        previous = self.tip()
        (self.public / "delta.json").write_text('{"generated_at_jst":"other"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.tip(), previous)

    def test_missing_generation_preserves_previous_feed(self):
        self.publish()
        previous = self.tip()
        (self.public / "health.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.tip(), previous)

    def test_latest_generation_when_present_must_match(self):
        self.publish()
        previous = self.tip()
        (self.public / "latest.json").write_text('{"generated_at_jst":null}', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.tip(), previous)

    def test_latest_without_generation_is_supported(self):
        (self.public / "latest.json").write_bytes(b'{"articles":[]}\n')
        self.assertTrue(self.publish())
        self.assert_feed()

    def test_stale_tracked_files_are_removed(self):
        self.publish()
        other = self.root / "stale"
        self.run_git(self.root, "clone", "--branch", "monitor-feed", str(self.remote), str(other))
        self.run_git(other, "config", "user.name", "Test")
        self.run_git(other, "config", "user.email", "test@example.invalid")
        (other / "state.json").write_text("{}", encoding="utf-8")
        (other / ".gitignore").write_text("*.json\n", encoding="utf-8")
        self.run_git(other, "add", "--force", "state.json", ".gitignore")
        self.run_git(other, "commit", "-m", "stale content")
        self.run_git(other, "push", "origin", "monitor-feed")
        self.assertTrue(self.publish())
        self.assert_feed()

    def test_remote_error_is_not_treated_as_absent_branch(self):
        self.publish()
        previous = self.tip()
        self.run_git(self.repo, "remote", "set-url", "origin", str(self.root / "missing.git"))
        with self.assertRaises(subprocess.CalledProcessError):
            self.publish()
        self.assertEqual(self.tip(), previous)

    def test_push_failure_preserves_previous_feed_and_cleans_worktree(self):
        self.publish()
        previous = self.tip()
        self.write_generation("2026-10-08T06:17:00+09:00")
        original_git = MODULE.git

        def reject_push(repository, *args, **kwargs):
            if args[0] == "push":
                raise subprocess.CalledProcessError(1, ["git", *args], stderr="push rejected")
            return original_git(repository, *args, **kwargs)

        with patch.object(MODULE, "git", side_effect=reject_push):
            with self.assertRaises(subprocess.CalledProcessError):
                self.publish()
        self.assertEqual(self.tip(), previous)
        self.assertEqual(self.run_git(self.repo, "branch", "--show-current").strip(), "main")
        self.assertEqual(self.run_git(self.repo, "worktree", "list").count("\n"), 1)

    def test_failed_first_push_can_be_retried(self):
        original_git = MODULE.git

        def reject_push(repository, *args, **kwargs):
            if args[0] == "push":
                raise subprocess.CalledProcessError(1, ["git", *args], stderr="push rejected")
            return original_git(repository, *args, **kwargs)

        with patch.object(MODULE, "git", side_effect=reject_push):
            with self.assertRaises(subprocess.CalledProcessError):
                self.publish()
        self.assertTrue(self.publish())
        self.assert_feed()


if __name__ == "__main__":
    unittest.main()
