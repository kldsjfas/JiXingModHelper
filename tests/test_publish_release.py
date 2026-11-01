"""Release publication never moves an existing tag or replaces an existing asset."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.publish_release import PublicationError, REPOSITORY, checked_version, project_version, publish

COMMIT = "a" * 40
OLD_COMMIT = "b" * 40
TAG = "v1.3.11"
PREFIX = "/repos/" + REPOSITORY


class FakeGh:
    def __init__(self):
        self.tags = {}
        self.annotations = {}
        self.releases = {}
        self.errors = {}
        self.calls = []
        self.release_exit = 0

    @staticmethod
    def response(status, payload):
        return subprocess.CompletedProcess([], 0 if status < 400 else 1,
            f"HTTP/2.0 {status} Status\r\nContent-Type: application/json\r\n\r\n" + json.dumps(payload), "")

    def __call__(self, arguments, *, input_text=None):
        self.calls.append((list(arguments), input_text))
        if arguments[:2] == ["release", "create"]:
            if not self.release_exit:
                self.releases[arguments[2]] = {"tag_name": arguments[2], "draft": False}
            return subprocess.CompletedProcess(arguments, self.release_exit, "", "private transport details")
        method, path = arguments[3], arguments[4].removeprefix(PREFIX)
        if (method, path) in self.errors:
            status = self.errors[(method, path)]
            if status is None:
                return subprocess.CompletedProcess(arguments, 1, "", "secret-token @ private-proxy.example")
            return self.response(status, {"message": "remote details must not be printed"})
        if method == "GET" and path.startswith("/git/ref/tags/"):
            tag = path.removeprefix("/git/ref/tags/")
            target = self.tags.get(tag)
            return self.response(404, {}) if target is None else self.response(200, {"ref": "refs/tags/" + tag, "object": target})
        if method == "GET" and path.startswith("/git/tags/"):
            return self.response(200, {"object": self.annotations[path.removeprefix("/git/tags/")]})
        if method == "GET" and path.startswith("/releases/tags/"):
            release = self.releases.get(path.removeprefix("/releases/tags/"))
            return self.response(404, {}) if release is None else self.response(200, release)
        if method == "POST" and path == "/git/refs":
            payload = json.loads(input_text)
            tag = payload["ref"].removeprefix("refs/tags/")
            if tag in self.tags:
                return self.response(422, {})
            self.tags[tag] = {"type": "commit", "sha": payload["sha"]}
            return self.response(201, {"ref": payload["ref"], "object": self.tags[tag]})
        raise AssertionError(arguments)

    def mutations(self):
        return [args for args, _ in self.calls if args[:2] == ["release", "create"]
                or args[0] == "api" and args[3] != "GET"]


class PublishReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="jixing-publish-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.artifacts = self.root / "release"
        self.artifacts.mkdir()
        self.package = self.artifacts / "JiXingModHelper.zip"
        self.package.write_bytes(b"the same checked build bytes")
        checksum = hashlib.sha256(self.package.read_bytes()).hexdigest()
        (self.artifacts / "SHA256SUMS.txt").write_text(checksum + "  JiXingModHelper.zip\r\n", encoding="ascii")
        (self.artifacts / "RELEASE_NOTES.md").write_text("# 吉星派对 Mod 助手 v1.3.11\n\n更新内容。\n", encoding="utf-8")
        self.environment = {"GITHUB_REPOSITORY": REPOSITORY, "GITHUB_EVENT_NAME": "push",
                            "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": COMMIT, "BUILD_VERSION": "1.3.11"}
        self.gh = FakeGh()

    def publish(self, version="1.3.11"):
        return publish(version, self.environment, directory=self.artifacts, runner=self.gh)

    def test_new_main_version_creates_exact_tag_then_publishes_same_artifacts(self):
        result = self.publish()
        self.assertEqual(result, {"status": "published", "tag": TAG, "commit": COMMIT})
        self.assertEqual(self.gh.tags[TAG], {"type": "commit", "sha": COMMIT})
        mutation_calls = self.gh.mutations()
        self.assertEqual(mutation_calls[0][:4], ["api", "--include", "--method", "POST"])
        command = mutation_calls[1]
        self.assertEqual(command[:5], ["release", "create", TAG, str(self.package), str(self.artifacts / "SHA256SUMS.txt")])
        self.assertIn("--verify-tag", command)
        self.assertEqual(command[command.index("--target") + 1], COMMIT)
        self.assertEqual(command[command.index("--notes-file") + 1], str(self.artifacts / "RELEASE_NOTES.md"))
        self.assertNotIn("--clobber", command)

    def test_main_existing_older_tag_is_left_unchanged(self):
        self.gh.tags[TAG] = {"type": "commit", "sha": OLD_COMMIT}
        result = self.publish()
        self.assertEqual(result["reason"], "existing_tag_unchanged")
        self.assertEqual(self.gh.tags[TAG]["sha"], OLD_COMMIT)
        self.assertEqual(self.gh.mutations(), [])

    def test_main_retry_resumes_only_matching_tag_with_missing_release(self):
        self.gh.tags[TAG] = {"type": "commit", "sha": COMMIT}
        result = self.publish()
        self.assertEqual(result["status"], "published")
        self.assertEqual(len(self.gh.mutations()), 1)
        self.assertEqual(self.gh.mutations()[0][:2], ["release", "create"])
        self.assertIn("--verify-tag", self.gh.mutations()[0])

    def test_existing_formal_release_and_assets_are_never_overwritten(self):
        self.gh.tags[TAG] = {"type": "commit", "sha": COMMIT}
        original = {"tag_name": TAG, "draft": False, "assets": [{"id": 19}]}
        self.gh.releases[TAG] = original.copy()
        self.assertEqual(self.publish()["reason"], "existing_release_unchanged")
        self.assertEqual(self.gh.releases[TAG], original)
        self.assertEqual(self.gh.mutations(), [])

    def test_formal_release_without_a_tag_is_not_repaired_or_replaced(self):
        self.gh.releases[TAG] = {"tag_name": TAG, "draft": False}
        self.assertEqual(self.publish()["status"], "skipped")
        self.assertNotIn(TAG, self.gh.tags)
        self.assertEqual(self.gh.mutations(), [])

    def test_existing_draft_stops_instead_of_touching_unknown_assets(self):
        self.gh.tags[TAG] = {"type": "commit", "sha": COMMIT}
        self.gh.releases[TAG] = {"tag_name": TAG, "draft": True, "assets": [{"id": 20}]}
        with self.assertRaisesRegex(PublicationError, "草稿"):
            self.publish()
        self.assertEqual(self.gh.mutations(), [])

    def test_auth_server_and_transport_errors_never_mean_tag_missing(self):
        for status in (401, 403, 429, 500, None):
            with self.subTest(status=status):
                self.gh = FakeGh()
                self.gh.errors[("GET", "/git/ref/tags/" + TAG)] = status
                with self.assertRaises(PublicationError) as caught:
                    self.publish()
                self.assertNotIn("secret-token", str(caught.exception))
                self.assertNotIn("private-proxy", str(caught.exception))
                self.assertEqual(self.gh.mutations(), [])

    def test_release_lookup_failure_stops_before_creating_tag(self):
        self.gh.errors[("GET", "/releases/tags/" + TAG)] = 503
        with self.assertRaises(PublicationError):
            self.publish()
        self.assertEqual(self.gh.mutations(), [])

    def test_tag_push_requires_its_exact_built_commit(self):
        self.environment["GITHUB_REF"] = "refs/tags/" + TAG
        for target in (None, OLD_COMMIT):
            with self.subTest(target=target):
                self.gh = FakeGh()
                if target:
                    self.gh.tags[TAG] = {"type": "commit", "sha": target}
                with self.assertRaisesRegex(PublicationError, "构建提交"):
                    self.publish()
                self.assertEqual(self.gh.mutations(), [])

    def test_matching_annotated_tag_remains_supported_without_moving_it(self):
        annotation = "c" * 40
        self.environment["GITHUB_REF"] = "refs/tags/" + TAG
        self.gh.tags[TAG] = {"type": "tag", "sha": annotation}
        self.gh.annotations[annotation] = {"type": "commit", "sha": COMMIT}
        self.assertEqual(self.publish()["status"], "published")
        self.assertEqual(self.gh.tags[TAG], {"type": "tag", "sha": annotation})
        self.assertEqual(len(self.gh.mutations()), 1)

    def test_untrusted_event_branch_repo_sha_or_build_version_never_reaches_github(self):
        for key, value in (("GITHUB_REPOSITORY", "someone/fork"), ("GITHUB_EVENT_NAME", "workflow_dispatch"),
                           ("GITHUB_REF", "refs/heads/feature"), ("GITHUB_REF", "refs/tags/v9.9.9"),
                           ("GITHUB_SHA", "main"), ("BUILD_VERSION", "1.3.10")):
            with self.subTest(key=key, value=value):
                previous = self.environment[key]
                self.environment[key] = value
                with self.assertRaises(PublicationError):
                    self.publish()
                self.environment[key] = previous
                self.assertEqual(self.gh.calls, [])

    def test_corrupt_artifact_or_wrong_release_notes_never_creates_tag(self):
        self.package.write_bytes(b"changed bytes")
        with self.assertRaisesRegex(PublicationError, "校验文件不一致"):
            self.publish()
        self.assertEqual(self.gh.mutations(), [])
        self.package.write_bytes(b"the same checked build bytes")
        (self.artifacts / "RELEASE_NOTES.md").write_text("# 吉星派对 Mod 助手 v1.3.10\n", encoding="utf-8")
        with self.assertRaisesRegex(PublicationError, "发布说明与版本不一致"):
            self.publish()
        self.assertEqual(self.gh.mutations(), [])

    def test_tag_creation_race_or_failure_does_not_publish_against_another_tag(self):
        self.gh.errors[("POST", "/git/refs")] = 422
        with self.assertRaises(PublicationError):
            self.publish()
        self.assertFalse(any(args[:2] == ["release", "create"] for args, _ in self.gh.calls))

    def test_failed_release_command_keeps_its_checked_tag_for_safe_resume(self):
        self.gh.release_exit = 1
        with self.assertRaisesRegex(PublicationError, "未确认"):
            self.publish()
        self.assertEqual(self.gh.tags[TAG], {"type": "commit", "sha": COMMIT})
        self.gh.release_exit = 0
        self.assertEqual(self.publish()["status"], "published")
        self.assertEqual(sum(args[0] == "api" and args[3] == "POST" for args, _ in self.gh.calls), 1)

    def test_prerelease_is_not_published_as_a_stable_release(self):
        version = "1.3.11-preview.1"
        self.environment["BUILD_VERSION"] = version
        (self.artifacts / "RELEASE_NOTES.md").write_text("# 吉星派对 Mod 助手 v" + version + "\n", encoding="utf-8")
        self.assertEqual(self.publish(version)["status"], "published")
        self.assertIn("--prerelease", self.gh.mutations()[-1])

    def test_version_read_does_not_execute_source_and_rejects_ambiguous_assignments(self):
        package = self.root / "astral_party_auto"
        package.mkdir()
        source = package / "__init__.py"
        source.write_text('__version__ = "1.3.11"\nraise RuntimeError("not executed")\n', encoding="utf-8")
        self.assertEqual(project_version(self.root), "1.3.11")
        for second in ('__version__ = dynamic_value()', '__version__: str = "9.9.9"', '__version__ += "-other"'):
            with self.subTest(second=second):
                source.write_text('__version__ = "1.3.11"\n' + second + '\n', encoding="utf-8")
                with self.assertRaises(PublicationError):
                    project_version(self.root)

    def test_version_rejects_shell_fragments_leading_zeros_and_multiline_values(self):
        for version in ("01.3.11", "1.3", "1.3.11\nother=1", "1.3.11-preview.01", "1.3.11;echo secret", "1.3.11+meta"):
            with self.subTest(version=version), self.assertRaises(PublicationError):
                checked_version(version)


if __name__ == "__main__":
    unittest.main()
