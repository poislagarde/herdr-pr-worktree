#!/usr/bin/env python3
"""Exercise PR checkout behavior with real Git repositories and isolated fakes."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location(
    "pr_worktree", Path(__file__).resolve().parents[1] / "pr-worktree.py")
HELPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPER)


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="herdr-pr-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / "source repo"
        self.remote = self.directory / "remote repo.git"
        self.checkout = self.directory / "PR checkout with spaces"
        self.branch = "feature/pr-4105"
        self.calls = []
        self.workspaces = []
        self.panes = {}
        self.remote_urls = {"origin": "git@github.com:example/project.git"}
        self.local_remotes = {str(self.remote)}
        self.git_environment = {key: value for key, value in os.environ.items()
                                if not key.startswith("GIT_")}
        self.git_environment.update({"GIT_CONFIG_NOSYSTEM": "1",
                                     "GIT_CONFIG_GLOBAL": os.devnull,
                                     "GIT_TERMINAL_PROMPT": "0"})
        self.git("init", "--bare", str(self.remote), cwd=self.directory)
        self.git("init", "--initial-branch=main", str(self.root), cwd=self.directory)
        for key, value in (("user.name", "PR Worktree Test"),
                           ("user.email", "pr-worktree@example.invalid"),
                           ("commit.gpgsign", "false"),
                           ("core.hooksPath", str(self.directory / "no-hooks"))):
            self.git("config", key, value)
        (self.root / "tracked.txt").write_text("base\n")
        self.git("add", "tracked.txt")
        self.git("commit", "-m", "base")
        self.base_sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "origin", "main")
        self.git("checkout", "-b", self.branch)
        (self.root / "tracked.txt").write_text("PR head\n")
        self.git("commit", "-am", "PR head")
        self.pr_sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("push", "origin", "HEAD:refs/pull/4105/head",
                 "HEAD:refs/heads/" + self.branch)
        self.git("checkout", "main")
        self.git("branch", "-D", self.branch)
        self.metadata = {
            "number": 4105,
            "base": {"repo": {"full_name": "example/project"}},
            "head": {"ref": self.branch, "sha": self.pr_sha,
                     "repo": {"full_name": "example/project"}},
        }
        self.real_run = HELPER.run
        self.environment = mock.patch.dict(os.environ, {"HERDR_BIN_PATH": "test-herdr"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.runner = mock.patch.object(HELPER, "run", side_effect=self.dispatch)
        self.runner.start()
        self.addCleanup(self.runner.stop)

    def git(self, *args, cwd=None, check=True):
        return subprocess.run(["git", *args], cwd=cwd or self.root,
                              env=self.git_environment, text=True,
                              capture_output=True, check=check)

    @staticmethod
    def result(args, value):
        return subprocess.CompletedProcess(args, 0, json.dumps(value), "")

    def registered_worktrees(self):
        trees = []
        tree = {}
        raw = self.git("worktree", "list", "--porcelain", "-z").stdout
        for field in raw.split("\0"):
            if not field:
                if tree:
                    trees.append(tree)
                    tree = {}
                continue
            key, _, value = field.partition(" ")
            tree[key] = value
        if tree:
            trees.append(tree)
        return trees

    def dispatch(self, *args, cwd=None, check=True, **kwargs):
        self.calls.append(args)
        if args[0] == "gh":
            self.assertIn("repos/example/project/pulls/4105", args)
            return self.result(args, self.metadata)
        if args[0] == "test-herdr":
            if args[1:3] == ("workspace", "list"):
                return self.result(args, {"result": {"workspaces": self.workspaces}})
            if args[1:3] == ("pane", "list"):
                workspace = args[args.index("--workspace") + 1]
                return self.result(args, {"result": {"panes": self.panes.get(workspace, [])}})
            self.assertEqual(args[1], "worktree")
            operation = args[2]
            source = Path(args[args.index("--cwd") + 1]).resolve()
            if operation == "list":
                self.assertIn(source, [Path(tree["worktree"]).resolve()
                                       for tree in self.registered_worktrees()])
                trees = [{"path": tree["worktree"],
                          "branch": tree.get("branch", "").removeprefix("refs/heads/"),
                          "is_prunable": "prunable" in tree,
                          "is_locked": "locked" in tree}
                         for tree in self.registered_worktrees()]
                return self.result(args, {"result": {"worktrees": trees,
                    "source": {"source_checkout_path": str(self.root)}}})
            self.assertEqual(source, self.root.resolve())
            if operation == "create":
                branch = args[args.index("--branch") + 1]
                sha = args[args.index("--base") + 1]
                existing = self.git("show-ref", "--verify", "refs/heads/" + branch,
                                    check=False).returncode == 0
                if existing:
                    self.git("worktree", "add", str(self.checkout), branch)
                else:
                    self.git("worktree", "add", "-b", branch, str(self.checkout), sha)
            elif operation == "open":
                self.assertEqual(Path(args[args.index("--path") + 1]).resolve(),
                                 self.checkout.resolve())
                self.assertTrue(self.checkout.is_dir())
            else:
                self.fail("Unexpected Herdr operation: " + operation)
            return self.result(args, {"result": {"workspace": {
                "workspace_id": "test:w2", "cwd": str(self.checkout)},
                "worktree": {"path": str(self.checkout)}}})
        if args[:3] == ("git", "remote", "get-url"):
            actual = self.real_run(*args, cwd=cwd, check=check, **kwargs)
            if actual.stdout.strip() in self.local_remotes:
                return subprocess.CompletedProcess(args, 0, self.remote_urls[args[-1]] + "\n", "")
            return actual
        return self.real_run(*args, cwd=cwd, check=check, **kwargs)

    def open_pr(self, url="https://github.com/example/project/pull/4105", cwd=None):
        with contextlib.redirect_stdout(io.StringIO()):
            HELPER.open_pr(url, str(cwd or self.root), focus=False)

    def unrelated_repository(self):
        unrelated = self.directory / "unrelated repo"
        self.git("init", "--initial-branch=main", str(unrelated), cwd=self.directory)
        self.git("remote", "add", "origin", "https://github.com/unrelated/project.git",
                 cwd=unrelated)
        return unrelated

    def matching_workspace(self, workspace_id="test:matching"):
        return {"workspace_id": workspace_id, "label": "Project", "worktree": {
            "repo_root": str(self.root), "checkout_path": str(self.root)}}

    def assert_private_refs_removed(self):
        refs = self.git("for-each-ref", "--format=%(refname)",
                        "refs/herdr/pr-worktree/").stdout
        self.assertEqual(refs, "")

    def assert_tracking(self, remote, merge=None):
        self.assertEqual(self.git("config", "--get", "branch." + self.branch + ".remote",
                                  cwd=self.checkout).stdout.strip(), remote)
        self.assertEqual(self.git("config", "--get", "branch." + self.branch + ".merge",
                                  cwd=self.checkout).stdout.strip(),
                         merge or "refs/heads/" + self.branch)

    def advance_remote(self, remote, target=None):
        upstream = self.directory / "upstream contributor"
        self.git("worktree", "add", "--detach", str(upstream), self.pr_sha)
        (upstream / "tracked.txt").write_text("updated PR head\n")
        self.git("commit", "-am", "update PR head", cwd=upstream)
        expected = self.git("rev-parse", "HEAD", cwd=upstream).stdout.strip()
        self.git("push", str(remote), "HEAD:" + (target or "refs/heads/" + self.branch),
                 cwd=upstream)
        return expected

    def assert_pull_reaches(self, expected):
        pulled = self.git("pull", "--rebase", cwd=self.checkout, check=False)
        self.assertEqual(pulled.returncode, 0, pulled.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.checkout).stdout.strip(),
                         expected)

    def prepare_fork(self):
        fork = self.directory / "fork repo.git"
        self.git("init", "--bare", str(fork), cwd=self.directory)
        self.git("push", str(fork), self.pr_sha + ":refs/heads/" + self.branch)
        self.local_remotes.add(str(fork))
        self.metadata["head"]["repo"]["full_name"] = "contributor/project"
        # A matching branch name in the base repository must not select that remote.
        self.git("update-ref", "refs/heads/" + self.branch, self.base_sha, cwd=self.remote)
        return fork

    def recovery_checkout(self, path=None, branch=None):
        path = path or self.directory / "expired temporary checkout"
        self.git("worktree", "add", "-b", branch or self.branch, str(path), self.pr_sha)
        admin = Path(self.git("rev-parse", "--absolute-git-dir", cwd=path).stdout.strip())
        return path, admin

    def assert_recovery_refused(self, path, admin, sha=None):
        with self.assertRaises(HELPER.WorktreeError):
            self.open_pr()
        self.assertTrue(admin.is_dir())
        self.assertEqual(self.git("rev-parse", "refs/heads/" + self.branch).stdout.strip(),
                         sha or self.pr_sha)
        self.assertIn(str(path.parent.resolve() / path.name),
                      [tree["worktree"] for tree in self.registered_worktrees()])
        self.assertFalse(self.checkout.exists())
        self.assertFalse(any(call[:3] in (("git", "worktree", "remove"),
                                        ("git", "worktree", "prune"),
                                        ("test-herdr", "worktree", "create"),
                                        ("test-herdr", "worktree", "open"))
                             for call in self.calls))
        self.assert_private_refs_removed()

    def assert_no_checkout_created(self):
        self.assertFalse(self.checkout.exists())
        self.assertEqual(len(self.registered_worktrees()), 1)
        self.assertFalse(any(call[:3] in (("test-herdr", "worktree", "create"),
                                        ("test-herdr", "worktree", "open"))
                             for call in self.calls))
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD").stdout.strip(), "main")
        self.assertEqual(self.git("status", "--porcelain").stdout, "")
        self.assert_private_refs_removed()

    def test_slash_branch_exact_head_and_spaced_path(self):
        fetch_head = self.root / ".git" / "FETCH_HEAD"
        fetch_head.write_text("previous fetch state\n")
        self.open_pr()
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD",
                                  cwd=self.checkout).stdout.strip(), self.branch)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.checkout).stdout.strip(),
                         self.pr_sha)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.base_sha)
        self.assertEqual(fetch_head.read_text(), "previous fetch state\n")
        self.assertEqual(len(self.registered_worktrees()), 2)
        creation = next(call for call in self.calls
                        if call[:3] == ("test-herdr", "worktree", "create"))
        self.assertIn("--no-focus", creation)
        self.assert_private_refs_removed()

    def test_new_branch_can_pull_without_explicit_remote_or_branch(self):
        self.open_pr()
        self.assert_tracking("origin")
        self.assert_pull_reaches(self.advance_remote(self.remote))

    def test_fork_pr_tracks_existing_head_remote_and_pulls_its_branch(self):
        fork = self.prepare_fork()
        self.git("remote", "add", "contributor", str(fork))
        self.remote_urls["contributor"] = "https://github.com/contributor/project.git"
        self.open_pr()
        self.assert_tracking("contributor")
        self.assert_pull_reaches(self.advance_remote(fork))

    def assert_unconfigured_fork_can_pull(self, base_url, fork_url):
        fork = self.prepare_fork()
        self.remote_urls["origin"] = base_url
        self.git("config", "url." + str(fork) + ".insteadOf", fork_url)
        remotes = self.git("config", "--get-regexp", r"^remote\.").stdout
        self.open_pr()
        self.assert_tracking(fork_url)
        self.assertEqual(self.git("config", "--get-regexp", r"^remote\.").stdout, remotes)
        self.assert_pull_reaches(self.advance_remote(fork))

    def test_unconfigured_fork_tracks_ssh_url_and_pulls(self):
        self.assert_unconfigured_fork_can_pull(
            "git@github.com:example/project.git", "git@github.com:contributor/project.git")

    def test_unconfigured_fork_tracks_https_url_and_pulls(self):
        self.assert_unconfigured_fork_can_pull(
            "https://github.com/example/project.git", "https://github.com/contributor/project.git")

    def test_deleted_head_repository_tracks_base_pull_ref(self):
        self.metadata["head"]["repo"] = None
        self.git("update-ref", "-d", "refs/heads/" + self.branch, cwd=self.remote)
        self.open_pr()
        self.assert_tracking("origin", "refs/pull/4105/head")
        self.assert_pull_reaches(self.advance_remote(self.remote, "refs/pull/4105/head"))

    def test_narrow_fetch_configuration_is_preserved_and_pull_works(self):
        refspec = "+refs/heads/main:refs/remotes/origin/main"
        self.git("config", "remote.origin.fetch", refspec)
        self.open_pr()
        self.assert_tracking("origin")
        self.assertEqual(self.git("config", "--get-all", "remote.origin.fetch").stdout.strip(),
                         refspec)
        self.assert_pull_reaches(self.advance_remote(self.remote))

    def test_origin_preferred_when_multiple_remotes_match_head_repository(self):
        self.git("remote", "add", "alternate", str(self.remote))
        self.remote_urls["alternate"] = "https://github.com/example/project.git"
        self.open_pr()
        self.assert_tracking("origin")

    def test_existing_tracking_configuration_is_preserved_when_reopened(self):
        self.git("worktree", "add", "-b", self.branch, str(self.checkout), self.pr_sha)
        configurations = (
            {"remote": "origin", "merge": "refs/heads/main"},
            {"remote": "removed-remote", "merge": "refs/heads/deleted-branch"},
            {"remote": "removed-remote"},
            {"merge": "refs/heads/deleted-branch"},
        )
        for configuration in configurations:
            with self.subTest(configuration=configuration):
                self.git("config", "--remove-section", "branch." + self.branch, check=False)
                for key, value in configuration.items():
                    self.git("config", "branch." + self.branch + "." + key, value)
                before = self.git("config", "--local", "--list").stdout
                self.calls.clear()
                self.open_pr()
                self.assertEqual(self.git("config", "--local", "--list").stdout, before)
                self.assertFalse(any(call[:2] == ("git", "fetch") for call in self.calls))

    def test_fork_pr_without_source_branch_in_base_remote(self):
        self.git("update-ref", "-d", "refs/heads/" + self.branch, cwd=self.remote)
        self.metadata["head"]["repo"]["full_name"] = "contributor/project"
        self.open_pr()
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.checkout).stdout.strip(),
                         self.pr_sha)
        self.assertNotEqual(self.git("show-ref", "--verify", "refs/heads/" + self.branch,
                                     cwd=self.remote, check=False).returncode, 0)
        self.assert_private_refs_removed()

    def test_linked_invoking_checkout_creates_sibling_from_parent(self):
        source = self.directory / "invoking worktree"
        self.git("worktree", "add", "-b", "feature/other-work", str(source), self.base_sha)
        (source / "tracked.txt").write_text("keep invoking work\n")
        self.open_pr(cwd=source)
        listing = next(call for call in self.calls
                       if call[:3] == ("test-herdr", "worktree", "list"))
        self.assertEqual(Path(listing[listing.index("--cwd") + 1]).resolve(), source.resolve())
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.checkout).stdout.strip(),
                         self.pr_sha)
        self.assertEqual((source / "tracked.txt").read_text(), "keep invoking work\n")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=source).stdout.strip(), self.base_sha)
        self.assertEqual(len(self.registered_worktrees()), 3)
        self.assert_private_refs_removed()

    def test_linked_invoking_checkout_reopens_itself_with_local_work_intact(self):
        self.git("worktree", "add", "-b", self.branch, str(self.checkout), self.pr_sha)
        (self.checkout / "tracked.txt").write_text("committed local progress\n")
        self.git("commit", "-am", "local progress", cwd=self.checkout)
        local_sha = self.git("rev-parse", "HEAD", cwd=self.checkout).stdout.strip()
        (self.checkout / "tracked.txt").write_text("uncommitted local progress\n")
        (self.checkout / "untracked.txt").write_text("preserve me\n")
        before = self.git("status", "--porcelain", cwd=self.checkout).stdout
        self.open_pr(cwd=self.checkout)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.checkout).stdout.strip(), local_sha)
        self.assertEqual(self.git("status", "--porcelain", cwd=self.checkout).stdout, before)
        self.assertEqual((self.checkout / "tracked.txt").read_text(), "uncommitted local progress\n")
        self.assertEqual((self.checkout / "untracked.txt").read_text(), "preserve me\n")
        self.assertFalse(any(call[:2] == ("git", "fetch") for call in self.calls))
        self.assertEqual(len(self.registered_worktrees()), 2)
        self.assert_tracking("origin")
        self.assert_private_refs_removed()

    def test_matching_upstream_remote_is_used(self):
        self.remote_urls["origin"] = "git@github.com:contributor/project.git"
        self.remote_urls["upstream"] = "https://github.com/example/project.git"
        self.git("remote", "add", "upstream", str(self.remote))
        self.open_pr()
        fetch = next(call for call in self.calls if call[:2] == ("git", "fetch"))
        self.assertIn("upstream", fetch)
        self.assertNotIn("origin", fetch)
        self.assert_tracking("upstream")

    def test_unrelated_repository_rejected_before_github_or_fetch(self):
        self.remote_urls["origin"] = "git@github.com:unrelated/project.git"
        with self.assertRaises(HELPER.WorktreeError):
            self.open_pr()
        self.assertFalse(any(call[0] == "gh" or call[:2] == ("git", "fetch")
                             for call in self.calls))
        self.assert_no_checkout_created()

    def test_unrelated_current_repository_finds_matching_open_space(self):
        unrelated = self.unrelated_repository()
        self.workspaces = [self.matching_workspace()]
        self.open_pr(cwd=unrelated)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.checkout).stdout.strip(),
                         self.pr_sha)
        self.assertEqual(len(self.registered_worktrees()), 2)
        self.assertEqual(self.git("status", "--porcelain", cwd=unrelated).stdout, "")
        self.assertEqual(self.git("for-each-ref", cwd=unrelated).stdout, "")
        self.assertTrue(any(call[:3] == ("test-herdr", "workspace", "list")
                            for call in self.calls))
        self.assert_private_refs_removed()

    def test_non_git_current_directory_finds_matching_open_space(self):
        self.workspaces = [self.matching_workspace()]
        self.open_pr(cwd=self.directory)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.checkout).stdout.strip(),
                         self.pr_sha)
        self.assertFalse((self.directory / ".git").exists())
        self.assert_private_refs_removed()

    def test_matching_current_repository_does_not_search_other_spaces(self):
        self.workspaces = [{"workspace_id": "test:unused", "worktree": None}]
        self.open_pr()
        self.assertFalse(any(call[:3] in (("test-herdr", "workspace", "list"),
                                        ("test-herdr", "pane", "list"))
                             for call in self.calls))

    def test_plain_space_repository_is_found_from_foreground_pane_directory(self):
        nested = self.root / "nested project"
        nested.mkdir()
        self.workspaces = [{"workspace_id": "test:plain", "worktree": None}]
        self.panes["test:plain"] = [{"pane_id": "test:plain:p1",
                                      "foreground_cwd": str(nested),
                                      "cwd": str(self.directory)}]
        self.open_pr(cwd=self.directory)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.checkout).stdout.strip(),
                         self.pr_sha)
        self.assertTrue(any(call[:3] == ("test-herdr", "pane", "list")
                            and "test:plain" in call for call in self.calls))

    def test_plain_space_falls_back_to_pane_cwd(self):
        self.workspaces = [{"workspace_id": "test:plain"}]
        self.panes["test:plain"] = [{"pane_id": "test:plain:p1",
                                      "foreground_cwd": str(self.directory / "gone foreground"),
                                      "cwd": str(self.root)}]
        self.open_pr(cwd=self.directory)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.checkout).stdout.strip(),
                         self.pr_sha)

    def test_stale_space_candidates_are_skipped_before_matching_checkout_path(self):
        self.workspaces = [
            {"workspace_id": "test:invalid", "worktree": {"repo_root": 42}},
            {"workspace_id": "test:non-git", "worktree": {
                "repo_root": str(self.directory), "checkout_path": str(self.directory)}},
            {"workspace_id": "test:missing", "worktree": {
                "repo_root": str(self.directory / "missing repo"),
                "checkout_path": str(self.directory / "missing checkout")}},
            {"workspace_id": "test:matching", "worktree": {
                "repo_root": str(self.directory / "moved primary repo"),
                "checkout_path": str(self.root)}},
        ]
        self.open_pr(cwd=self.directory)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.checkout).stdout.strip(),
                         self.pr_sha)
        self.assert_private_refs_removed()

    def test_no_matching_open_repository_fails_before_github_or_fetch(self):
        unrelated = self.unrelated_repository()
        self.workspaces = [{"workspace_id": "test:unrelated", "worktree": {
            "repo_root": str(unrelated), "checkout_path": str(unrelated)}}]
        self.panes["test:unrelated"] = [{"cwd": str(unrelated), "foreground_cwd": None}]
        with self.assertRaises(HELPER.WorktreeError) as error:
            self.open_pr(cwd=self.directory)
        self.assertIn("example/project", str(error.exception))
        self.assertFalse(any(call[0] == "gh" or call[:2] == ("git", "fetch")
                             for call in self.calls))
        self.assert_no_checkout_created()

    def test_metadata_base_repository_mismatch_rejected(self):
        self.metadata["base"]["repo"]["full_name"] = "other/repository"
        with self.assertRaises(HELPER.WorktreeError):
            self.open_pr()
        self.assertFalse(any(call[:2] == ("git", "fetch") for call in self.calls))
        self.assert_no_checkout_created()

    def test_different_local_branch_commits_are_preserved(self):
        self.git("branch", self.branch, self.base_sha)
        with self.assertRaises(HELPER.WorktreeError):
            self.open_pr()
        self.assertEqual(self.git("rev-parse", "refs/heads/" + self.branch).stdout.strip(),
                         self.base_sha)
        self.assert_no_checkout_created()

    def test_matching_checkout_reopened_with_dirty_edits_intact(self):
        self.git("worktree", "add", "-b", self.branch, str(self.checkout), self.pr_sha)
        (self.checkout / "tracked.txt").write_text("local work in progress\n")
        (self.checkout / "untracked.txt").write_text("keep this too\n")
        before = self.git("status", "--porcelain", cwd=self.checkout).stdout
        self.open_pr()
        self.assertEqual(self.git("status", "--porcelain", cwd=self.checkout).stdout, before)
        self.assertEqual((self.checkout / "tracked.txt").read_text(), "local work in progress\n")
        self.assertEqual((self.checkout / "untracked.txt").read_text(), "keep this too\n")
        self.assertEqual(len(self.registered_worktrees()), 2)
        self.assertTrue(any(call[:3] == ("test-herdr", "worktree", "open")
                            for call in self.calls))
        self.assertFalse(any(call[:3] == ("test-herdr", "worktree", "create")
                             for call in self.calls))
        self.assertFalse(any(call[:2] == ("git", "fetch") for call in self.calls))
        self.assert_private_refs_removed()

    def test_existing_checkout_found_from_other_space_keeps_local_commits_and_dirty_edits(self):
        self.git("worktree", "add", "-b", self.branch, str(self.checkout), self.pr_sha)
        (self.checkout / "tracked.txt").write_text("committed local progress\n")
        self.git("commit", "-am", "local progress", cwd=self.checkout)
        local_sha = self.git("rev-parse", "HEAD", cwd=self.checkout).stdout.strip()
        self.assertNotEqual(local_sha, self.pr_sha)
        (self.checkout / "tracked.txt").write_text("more uncommitted progress\n")
        (self.checkout / "untracked.txt").write_text("preserve me\n")
        before = self.git("status", "--porcelain", cwd=self.checkout).stdout
        self.workspaces = [self.matching_workspace()]
        self.open_pr(cwd=self.unrelated_repository())
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.checkout).stdout.strip(),
                         local_sha)
        self.assertEqual(self.git("status", "--porcelain", cwd=self.checkout).stdout, before)
        self.assertEqual((self.checkout / "tracked.txt").read_text(), "more uncommitted progress\n")
        self.assertEqual((self.checkout / "untracked.txt").read_text(), "preserve me\n")
        self.assertTrue(any(call[:3] == ("test-herdr", "worktree", "open")
                            for call in self.calls))
        self.assertFalse(any(call[:2] == ("git", "fetch") for call in self.calls))
        self.assertEqual(len(self.registered_worktrees()), 2)

    def test_missing_registered_checkout_is_recovered_without_pruning_other_worktrees(self):
        missing, admin = self.recovery_checkout()
        before = {str(path.relative_to(admin)): path.read_bytes()
                  for path in admin.rglob("*") if path.is_file()}
        unrelated, unrelated_admin = self.recovery_checkout(
            self.directory / "another expired checkout", "feature/unrelated")
        unrelated_before = {str(path.relative_to(unrelated_admin)): path.read_bytes()
                            for path in unrelated_admin.rglob("*") if path.is_file()}
        shutil.rmtree(missing)
        shutil.rmtree(unrelated)
        (self.root / "tracked.txt").write_text("preserve the source checkout\n")

        self.open_pr()

        self.assertFalse(missing.exists())
        self.assertFalse(admin.exists())
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.checkout).stdout.strip(),
                         self.pr_sha)
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD",
                                  cwd=self.checkout).stdout.strip(), self.branch)
        self.assertEqual((self.root / "tracked.txt").read_text(),
                         "preserve the source checkout\n")
        self.assertEqual(len(self.registered_worktrees()), 3)
        self.assertEqual({str(path.relative_to(unrelated_admin)): path.read_bytes()
                          for path in unrelated_admin.rglob("*") if path.is_file()},
                         unrelated_before)
        backups = list((self.root / ".git" / "herdr-pr-worktree-recovery").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual({str(path.relative_to(backups[0])): path.read_bytes()
                          for path in backups[0].rglob("*") if path.is_file()}, before)
        removals = [call for call in self.calls if call[:3] == ("git", "worktree", "remove")]
        self.assertEqual(removals, [("git", "worktree", "remove", "--", str(missing.resolve()))])
        self.assertFalse(any(call[:3] == ("git", "worktree", "prune") for call in self.calls))
        self.assert_private_refs_removed()

    def test_missing_checkout_with_staged_changes_preserves_its_index(self):
        missing, admin = self.recovery_checkout()
        (missing / "tracked.txt").write_text("staged work that is only in the index\n")
        self.git("add", "tracked.txt", cwd=missing)
        index = (admin / "index").read_bytes()
        shutil.rmtree(missing)

        self.assert_recovery_refused(missing, admin)

        self.assertEqual((admin / "index").read_bytes(), index)
        self.assertEqual(self.git("--git-dir", str(admin), "show", ":tracked.txt").stdout,
                         "staged work that is only in the index\n")

    def test_missing_checkout_with_ignored_staged_submodule_change_is_preserved(self):
        missing, admin = self.recovery_checkout()
        staged_commit = self.pr_sha
        self.git("update-index", "--add", "--cacheinfo",
                 "160000," + self.base_sha + ",submodule", cwd=missing)
        self.git("commit", "-m", "add submodule pointer", cwd=missing)
        self.pr_sha = self.git("rev-parse", "HEAD", cwd=missing).stdout.strip()
        self.metadata["head"]["sha"] = self.pr_sha
        self.git("push", "origin", "HEAD:refs/pull/4105/head", cwd=missing)
        self.git("update-index", "--cacheinfo", "160000," + staged_commit + ",submodule",
                 cwd=missing)
        self.git("config", "diff.ignoreSubmodules", "all")
        index = (admin / "index").read_bytes()
        self.assertEqual(self.git("diff", "--cached", "--quiet", "HEAD", "--",
                                  cwd=missing, check=False).returncode, 0)
        shutil.rmtree(missing)

        self.assert_recovery_refused(missing, admin)

        self.assertEqual((admin / "index").read_bytes(), index)
        self.assertIn(staged_commit, self.git("--git-dir", str(admin), "ls-files",
                                              "--stage", "--", "submodule").stdout)

    def test_missing_checkout_with_unmerged_index_preserves_conflict_stages(self):
        missing, admin = self.recovery_checkout()
        # Insert actual conflict stages without depending on merge conflict heuristics.
        base_blob = self.git("rev-parse", self.base_sha + ":tracked.txt").stdout.strip()
        pr_blob = self.git("rev-parse", self.pr_sha + ":tracked.txt").stdout.strip()
        records = ("0 " + "0" * 40 + "\ttracked.txt\n"
                   + f"100644 {base_blob} 1\ttracked.txt\n"
                   + f"100644 {pr_blob} 2\ttracked.txt\n"
                   + f"100644 {base_blob} 3\ttracked.txt\n")
        subprocess.run(["git", "update-index", "--index-info"], cwd=missing,
                       env=self.git_environment, input=records, text=True,
                       capture_output=True, check=True)
        stages = self.git("ls-files", "--unmerged", cwd=missing).stdout
        self.assertTrue(stages)
        shutil.rmtree(missing)

        self.assert_recovery_refused(missing, admin)

        self.assertEqual(self.git("--git-dir", str(admin), "ls-files", "--unmerged").stdout,
                         stages)

    def test_missing_locked_checkout_is_preserved_without_lock_reason(self):
        missing, admin = self.recovery_checkout()
        self.git("worktree", "lock", str(missing))
        shutil.rmtree(missing)
        self.assertEqual((admin / "locked").read_text(), "")
        self.assert_recovery_refused(missing, admin)
        self.assertTrue((admin / "locked").exists())

    def test_missing_checkout_with_operation_or_lock_metadata_is_preserved(self):
        missing, admin = self.recovery_checkout()
        shutil.rmtree(missing)
        for name in ("index.lock", "HEAD.lock", "MERGE_HEAD", "CHERRY_PICK_HEAD",
                     "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer", "BISECT_START"):
            with self.subTest(name=name):
                marker = admin / name
                marker.write_text(self.pr_sha + "\n")
                self.calls.clear()
                self.assert_recovery_refused(missing, admin)
                self.assertEqual(marker.read_text(), self.pr_sha + "\n")
                marker.unlink()

    def test_missing_checkout_with_absent_index_is_preserved(self):
        missing, admin = self.recovery_checkout()
        (admin / "index").unlink()
        shutil.rmtree(missing)
        self.assert_recovery_refused(missing, admin)

    def test_registered_path_occupied_by_file_or_symlink_is_preserved(self):
        missing, admin = self.recovery_checkout()
        shutil.rmtree(missing)
        for kind in ("file", "dangling symlink"):
            with self.subTest(kind=kind):
                if kind == "file":
                    missing.write_text("unrelated replacement file\n")
                else:
                    missing.symlink_to(self.directory / "absent symlink target")
                self.calls.clear()
                self.assert_recovery_refused(missing, admin)
                if kind == "file":
                    self.assertEqual(missing.read_text(), "unrelated replacement file\n")
                else:
                    self.assertTrue(missing.is_symlink())
                missing.unlink()

    def test_registered_directory_without_git_metadata_is_preserved(self):
        missing, admin = self.recovery_checkout()
        (missing / ".git").unlink()
        (missing / "untracked.txt").write_text("preserve this directory\n")
        self.assert_recovery_refused(missing, admin)
        self.assertEqual((missing / "untracked.txt").read_text(), "preserve this directory\n")

    def test_missing_checkout_below_absent_parent_is_preserved(self):
        parent = self.directory / "offline parent"
        parent.mkdir()
        missing, admin = self.recovery_checkout(parent / "checkout")
        shutil.rmtree(parent)
        self.assert_recovery_refused(missing, admin)

    def test_missing_checkout_with_different_local_commits_is_preserved(self):
        missing, admin = self.recovery_checkout()
        (missing / "tracked.txt").write_text("local committed progress\n")
        self.git("commit", "-am", "local progress", cwd=missing)
        local_sha = self.git("rev-parse", "HEAD", cwd=missing).stdout.strip()
        shutil.rmtree(missing)
        self.assert_recovery_refused(missing, admin, sha=local_sha)

    def test_missing_checkout_is_preserved_when_fetch_fails(self):
        missing, admin = self.recovery_checkout()
        shutil.rmtree(missing)
        self.git("update-ref", "-d", "refs/pull/4105/head", cwd=self.remote)
        self.assert_recovery_refused(missing, admin)

    def test_recovery_creation_failure_retains_branch_and_metadata_backup(self):
        missing, admin = self.recovery_checkout()
        index = (admin / "index").read_bytes()
        shutil.rmtree(missing)
        dispatch = self.dispatch

        def fail_creation(*args, **kwargs):
            if args[:3] == ("test-herdr", "worktree", "create"):
                self.calls.append(args)
                raise HELPER.WorktreeError("creation failed")
            return dispatch(*args, **kwargs)

        with mock.patch.object(HELPER, "run", side_effect=fail_creation):
            with self.assertRaisesRegex(HELPER.WorktreeError, "creation failed"):
                self.open_pr()
        self.assertFalse(missing.exists())
        self.assertFalse(self.checkout.exists())
        self.assertFalse(admin.exists())
        self.assertEqual(len(self.registered_worktrees()), 1)
        self.assertEqual(self.git("rev-parse", "refs/heads/" + self.branch).stdout.strip(),
                         self.pr_sha)
        backups = list((self.root / ".git" / "herdr-pr-worktree-recovery").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "index").read_bytes(), index)
        self.assertEqual((backups[0] / "HEAD").read_text(), "ref: refs/heads/" + self.branch + "\n")
        self.assert_private_refs_removed()

    def test_shell_metacharacters_in_branch_name_remain_literal(self):
        self.branch = "feature/$(touch${IFS}PWNED)"
        self.metadata["head"]["ref"] = self.branch
        self.open_pr()
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD",
                                  cwd=self.checkout).stdout.strip(), self.branch)
        self.assertEqual(self.git("status", "--porcelain").stdout, "")
        self.assertEqual(self.git("status", "--porcelain", cwd=self.checkout).stdout, "")
        self.assertFalse((self.root / "PWNED").exists())
        self.assertFalse((self.checkout / "PWNED").exists())
        self.assert_private_refs_removed()

    def test_matching_unchecked_local_branch_is_reused(self):
        self.git("branch", self.branch, self.pr_sha)
        self.open_pr()
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.checkout).stdout.strip(),
                         self.pr_sha)
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD",
                                  cwd=self.checkout).stdout.strip(), self.branch)
        self.assert_tracking("origin")
        self.assert_private_refs_removed()

    def test_missing_pull_ref_does_not_create_worktree(self):
        self.git("update-ref", "-d", "refs/pull/4105/head", cwd=self.remote)
        with self.assertRaises(HELPER.WorktreeError):
            self.open_pr()
        self.assert_no_checkout_created()

    def test_fetched_head_mismatch_does_not_create_branch_or_worktree(self):
        self.metadata["head"]["sha"] = self.base_sha
        with self.assertRaises(HELPER.WorktreeError):
            self.open_pr()
        self.assertNotEqual(self.git("show-ref", "--verify", "refs/heads/" + self.branch,
                                     check=False).returncode, 0)
        self.assert_no_checkout_created()

    def test_url_files_suffix_query_and_fragment_create_expected_pr(self):
        self.open_pr("https://github.com/example/project/pull/4105/files?diff=split#diff-test")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.checkout).stdout.strip(),
                         self.pr_sha)


class UrlTests(unittest.TestCase):
    def test_supported_suffixes_and_queries(self):
        for suffix in ("", "/", "/files", "/files/", "/commits", "/checks",
                       "?tab=files#section", "/files?diff=split#file"):
            with self.subTest(suffix=suffix):
                self.assertEqual(HELPER.pr_url("https://github.com/Owner/Repo/pull/4105" + suffix),
                                 ("Owner/Repo", "4105"))

    def test_non_pr_urls_are_rejected(self):
        for value in ("https://github.com/owner/repo/issues/4105",
                      "https://github.com/owner/repo/pull/0",
                      "https://github.com/owner/repo/pull/4105/not-a-pr-view",
                      "https://github.com.evil.invalid/owner/repo/pull/4105",
                      "https://github.com@evil.invalid/owner/repo/pull/4105",
                      "http://github.com/owner/repo/pull/4105",
                      "git@github.com:owner/repo.git"):
            with self.subTest(value=value), self.assertRaises(HELPER.WorktreeError):
                HELPER.pr_url(value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
