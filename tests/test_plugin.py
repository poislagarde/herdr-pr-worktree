#!/usr/bin/env python3
"""Verify plugin invocation context and the prompt launcher without a live server."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import unittest
from unittest import mock


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CORE = load_module("pr_worktree_context_test", "pr-worktree.py")
PLUGIN = load_module("pr_worktree_plugin_test", "plugin.py")


class ContextTests(unittest.TestCase):
    def setUp(self):
        environment = mock.patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def set_context(self, value):
        os.environ["HERDR_PLUGIN_CONTEXT_JSON"] = json.dumps(value)

    def test_directory_carried_from_action_wins_over_popup_and_legacy_context(self):
        self.set_context({"focused_pane_cwd": "/projects/newly focused pane",
                          "workspace_cwd": "/projects/newly focused workspace"})
        os.environ["PR_WORKTREE_CWD"] = "/projects/original action directory"
        os.environ["HERDR_ACTIVE_PANE_CWD"] = "/projects/legacy pane"
        self.assertEqual(CORE.invocation_cwd(), "/projects/original action directory")

    def test_invoking_pane_wins_over_workspace_worktree_and_legacy_context(self):
        self.set_context({"focused_pane_cwd": "/projects/invoking pane",
                          "workspace_cwd": "/projects/workspace",
                          "worktree": {"checkout_path": "/projects/worktree"}})
        os.environ["HERDR_ACTIVE_PANE_CWD"] = "/projects/legacy pane"
        self.assertEqual(CORE.invocation_cwd(), "/projects/invoking pane")

    def test_workspace_directory_is_used_when_pane_directory_is_unavailable(self):
        self.set_context({"focused_pane_cwd": None,
                          "workspace_cwd": "/projects/workspace",
                          "worktree": {"checkout_path": "/projects/worktree"}})
        self.assertEqual(CORE.invocation_cwd(), "/projects/workspace")

    def test_worktree_directory_is_used_when_other_context_is_empty(self):
        self.set_context({"focused_pane_cwd": "", "workspace_cwd": None,
                          "worktree": {"checkout_path": "/projects/worktree"}})
        self.assertEqual(CORE.invocation_cwd(), "/projects/worktree")

    def test_legacy_active_pane_directory_remains_a_fallback(self):
        self.set_context({"focused_pane_cwd": None, "workspace_cwd": "",
                          "worktree": None})
        os.environ["HERDR_ACTIVE_PANE_CWD"] = "/projects/legacy pane"
        self.assertEqual(CORE.invocation_cwd(), "/projects/legacy pane")

    def test_no_context_uses_current_working_directory(self):
        with mock.patch.object(CORE.os, "getcwd", return_value="/projects/current directory"):
            self.assertEqual(CORE.invocation_cwd(), "/projects/current directory")


class LauncherTests(unittest.TestCase):
    def setUp(self):
        environment = mock.patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    @staticmethod
    def success():
        return subprocess.CompletedProcess([], 0, '{"result": {}}', "")

    def test_prompt_uses_explicit_bin_and_carries_context_cwd_without_target_overrides(self):
        os.environ["HERDR_BIN_PATH"] = "/apps/Herdr Test/bin/herdr"
        os.environ["HERDR_PLUGIN_CONTEXT_JSON"] = json.dumps({
            "workspace_id": "w7", "focused_pane_id": "w7:p12",
            "focused_pane_cwd": "/projects/current $(literal) directory",
            "workspace_cwd": "/projects/workspace directory",
            "worktree": {"checkout_path": "/projects/worktree directory"}})
        with mock.patch.object(PLUGIN.subprocess, "run", return_value=self.success()) as run:
            self.assertEqual(PLUGIN.open_prompt(), 0)
        run.assert_called_once_with([
            "/apps/Herdr Test/bin/herdr", "plugin", "pane", "open",
            "--plugin", "poislagarde.pr-worktree", "--entrypoint", "prompt", "--focus",
            "--env", "PR_WORKTREE_CWD=/projects/current $(literal) directory",
        ], text=True, capture_output=True)
        for option in ("--placement", "--workspace", "--target-pane"):
            self.assertNotIn(option, run.call_args.args[0])

    def test_context_free_action_uses_default_binary_without_invented_targets(self):
        with mock.patch.object(PLUGIN.subprocess, "run", return_value=self.success()) as run:
            self.assertEqual(PLUGIN.open_prompt(), 0)
        run.assert_called_once_with([
            "herdr", "plugin", "pane", "open", "--plugin", "poislagarde.pr-worktree",
            "--entrypoint", "prompt", "--focus",
        ], text=True, capture_output=True)

    def test_workspace_id_without_directory_does_not_add_target_or_env_overrides(self):
        os.environ["HERDR_PLUGIN_CONTEXT_JSON"] = json.dumps({"workspace_id": "w3"})
        with mock.patch.object(PLUGIN.subprocess, "run", return_value=self.success()) as run:
            PLUGIN.open_prompt()
        for option in ("--workspace", "--target-pane", "--env"):
            self.assertNotIn(option, run.call_args.args[0])

    def test_prompt_directory_falls_back_through_workspace_and_worktree_context(self):
        for context, expected in (
                ({"focused_pane_cwd": None, "workspace_cwd": "/projects/workspace",
                  "worktree": {"checkout_path": "/projects/worktree"}}, "/projects/workspace"),
                ({"focused_pane_cwd": "", "workspace_cwd": None,
                  "worktree": {"checkout_path": "/projects/worktree"}}, "/projects/worktree")):
            with self.subTest(expected=expected):
                os.environ["HERDR_PLUGIN_CONTEXT_JSON"] = json.dumps(context)
                with mock.patch.object(PLUGIN.subprocess, "run", return_value=self.success()) as run:
                    PLUGIN.open_prompt()
                self.assertEqual(run.call_args.args[0][-2:], ["--env", "PR_WORKTREE_CWD=" + expected])

    def test_malformed_context_fails_before_any_subprocess(self):
        os.environ["HERDR_PLUGIN_CONTEXT_JSON"] = "{invalid json"
        with mock.patch.object(PLUGIN.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                PLUGIN.open_prompt()
        run.assert_not_called()

    def test_command_error_is_reported_without_retry_or_cleanup_commands(self):
        for stdout, stderr, message in (
                ("", "popup unavailable\n", "popup unavailable"),
                ("plugin disabled\n", "", "plugin disabled"),
                ("", "", "Herdr could not open the PR URL prompt.")):
            with self.subTest(message=message):
                failure = subprocess.CompletedProcess([], 1, stdout, stderr)
                with mock.patch.object(PLUGIN.subprocess, "run", return_value=failure) as run:
                    with self.assertRaises(RuntimeError) as error:
                        PLUGIN.open_prompt()
                self.assertEqual(str(error.exception), message)
                run.assert_called_once()
                self.assertEqual(run.call_args.args[0][1:4], ["plugin", "pane", "open"])

    def test_cli_reports_missing_binary_with_nonzero_exit(self):
        stderr = io.StringIO()
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("missing Herdr binary")) as run:
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as exit_error:
                runpy.run_path(str(ROOT / "plugin.py"), run_name="__main__")
        self.assertEqual(exit_error.exception.code, 1)
        self.assertIn("missing Herdr binary", stderr.getvalue())
        run.assert_called_once()

    def test_cli_reports_rejected_prompt_without_followup_mutations(self):
        failure = subprocess.CompletedProcess([], 1, "", "workspace no longer exists")
        stderr = io.StringIO()
        with mock.patch("subprocess.run", return_value=failure) as run:
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as exit_error:
                runpy.run_path(str(ROOT / "plugin.py"), run_name="__main__")
        self.assertEqual(exit_error.exception.code, 1)
        self.assertIn("workspace no longer exists", stderr.getvalue())
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][1:4], ["plugin", "pane", "open"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
