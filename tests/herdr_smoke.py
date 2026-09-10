#!/usr/bin/env python3
"""Check the real plugin popup and worktree contract in a private headless server.

Requires Python 3.9+, Git, and Herdr 0.9.0+. No live session, browser, GitHub
account, or network Git remote is used. All runtime state lives under /tmp.
"""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch


PLUGIN_ID = "poislagarde.pr-worktree"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = 20


def wait_for(description, predicate):
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for " + description)


class Fixture:
    def __init__(self, root, binary):
        self.root = root
        self.binary = str(Path(binary).resolve())
        self.git = shutil.which("git")
        self.session = "pr-smoke"
        config = root / "config" / "herdr" / "config.toml"
        self.socket = config.parent / "sessions" / self.session / "herdr.sock"
        self.socket.parent.mkdir(parents=True)
        (root / "runtime").mkdir(mode=0o700)
        config.write_text(
            'onboarding = false\n'
            '[terminal]\ndefault_shell = "/bin/sh"\nshell_mode = "non_login"\n'
            '[update]\nversion_check = false\nmanifest_check = false\n'
            '[ui.toast]\ndelivery = "off"\n[ui.sound]\nenabled = false\n'
            '[worktrees]\ndirectory = ' + json.dumps(str(root / "worktrees")) + '\n'
            '[[keys.command]]\nkey = "prefix+alt+g"\ntype = "plugin_action"\n'
            'command = "poislagarde.pr-worktree.open"\n')
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("HERDR_", "GIT_", "PR_WORKTREE_"))
                    and key not in {"TMUX", "TMUX_PANE", "ENV", "BASH_ENV", "ZDOTDIR",
                                    "GH_TOKEN", "GITHUB_TOKEN"}}
        self.env.update({
            "XDG_CONFIG_HOME": str(root / "config"), "XDG_STATE_HOME": str(root / "state"),
            "XDG_DATA_HOME": str(root / "data"), "XDG_RUNTIME_DIR": str(root / "runtime"),
            "HERDR_CONFIG_PATH": str(config), "HERDR_SOCKET_PATH": str(self.socket),
            "SHELL": "/bin/sh", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GH_CONFIG_DIR": str(root / "gh"),
        })
        # Record the real popup process's launch, then execute it unchanged.
        bin_dir = root / "bin"
        bin_dir.mkdir()
        self.marker = root / "popup-launch.json"
        python_shim = bin_dir / "python3"
        python_shim.write_text(
            "#!" + sys.executable + "\n"
            "import json, os, sys\nfrom pathlib import Path\n"
            "if len(sys.argv) > 1 and Path(sys.argv[1]).name == 'pr-worktree.py':\n"
            "    Path(os.environ['PR_WORKTREE_SMOKE_MARKER']).write_text(json.dumps({\n"
            "        'pid': os.getpid(), 'cwd': os.getcwd(),\n"
            "        'origin': os.environ.get('PR_WORKTREE_CWD'),\n"
            "        'context': json.loads(os.environ.get('HERDR_PLUGIN_CONTEXT_JSON', '{}'))}))\n"
            "os.execv(" + repr(sys.executable) + ", [" + repr(sys.executable) + "] + sys.argv[1:])\n")
        python_shim.chmod(0o755)
        self.env["PATH"] = str(bin_dir) + os.pathsep + self.env.get("PATH", "/usr/bin:/bin")
        self.env["PR_WORKTREE_SMOKE_MARKER"] = str(self.marker)
        self.log_path = root / "server.log"
        self.log_file = self.log_path.open("w")
        self.process = subprocess.Popen(
            [self.binary, "--session", self.session, "server"], cwd=root, env=self.env,
            stdin=subprocess.DEVNULL, stdout=self.log_file, stderr=subprocess.STDOUT,
            start_new_session=True)

    def run(self, *args, cwd=None):
        result = subprocess.run([str(arg) for arg in args], cwd=cwd or self.root,
                                env=self.env, text=True, capture_output=True, timeout=TIMEOUT)
        if result.returncode:
            raise AssertionError("{} failed ({})\n{}\n{}".format(
                args, result.returncode, result.stdout, result.stderr))
        return result.stdout.strip()

    def api(self, *args):
        assert self.socket.is_relative_to(self.root)
        value = json.loads(self.run(self.binary, "--session", self.session, *args))
        assert not value.get("error"), value
        return value["result"]

    def ready(self):
        def check():
            if self.process.poll() is not None:
                raise AssertionError("Private server exited: " + self.log_path.read_text())
            return self.socket.exists() and self.api("workspace", "list")
        wait_for("private server", check)

    def git_run(self, *args, cwd=None):
        return self.run(self.git, *args, cwd=cwd or self.repo)

    def repositories(self):
        self.repo = self.root / "repo"
        self.remote = self.root / "remote.git"
        self.unrelated = self.root / "unrelated"
        self.non_git = self.root / "non-git"
        self.repo.mkdir()
        self.non_git.mkdir()
        self.git_run("init", "-b", "main")
        self.git_run("config", "user.name", "PR smoke fixture")
        self.git_run("config", "user.email", "fixture@example.invalid")
        self.git_run("config", "core.hooksPath", "/dev/null")
        (self.repo / "file.txt").write_text("base\n")
        self.git_run("add", "file.txt")
        base_tree = self.git_run("write-tree")
        self.base = self.git_run("commit-tree", base_tree, "-m", "base")
        self.git_run("update-ref", "refs/heads/main", self.base)
        self.git_run("reset", "--hard", "main")
        (self.repo / "file.txt").write_text("pull request\n")
        self.git_run("add", "file.txt")
        self.tree = self.git_run("write-tree")
        self.sha = self.git_run("commit-tree", self.tree, "-p", self.base, "-m", "PR fixture")
        self.git_run("update-ref", "refs/pull/4105/head", self.sha)
        self.git_run("reset", "--hard", "main")
        self.git_run("clone", "--bare", self.repo, self.remote)
        self.git_run("push", self.remote, "refs/pull/4105/head:refs/pull/4105/head")
        self.git_run("update-ref", "-d", "refs/pull/4105/head")
        self.git_run("remote", "add", "origin", self.remote)
        self.git_run("clone", self.repo, self.unrelated)
        self.git_run("config", "core.hooksPath", "/dev/null", cwd=self.unrelated)
        self.git_run("remote", "set-url", "origin", "https://github.com/example/unrelated.git",
                     cwd=self.unrelated)
        self.parent = self.api("workspace", "create", "--cwd", str(self.repo), "--no-focus")
        self.caller = self.api("workspace", "create", "--cwd", str(self.unrelated), "--focus")
        self.non_git_space = self.api("workspace", "create", "--cwd", str(self.non_git), "--no-focus")

    def popup(self):
        plugin = self.api("plugin", "link", str(PLUGIN_ROOT))["plugin"]
        assert not plugin.get("warnings"), plugin
        prompt = next(p for p in plugin["panes"] if p["id"] == "prompt")
        assert (prompt["placement"], prompt["width"], prompt["height"]) == ("popup", "85%", "60%")
        action = next(a for a in self.api("plugin", "action", "list", "--plugin", PLUGIN_ID)["actions"]
                      if a["action_id"] == "open")
        assert "workspace" in action["contexts"]
        before = self.api("api", "snapshot")["snapshot"]
        invoked = self.api("plugin", "action", "invoke", PLUGIN_ID + ".open")
        assert invoked["context"]["workspace_id"] == self.caller["workspace"]["workspace_id"]
        wait_for("popup process launch", self.marker.exists)
        launch = json.loads(self.marker.read_text())
        assert launch["cwd"] == str(PLUGIN_ROOT), launch
        assert launch["origin"] == str(self.unrelated), launch
        def action_finished():
            logs = self.api("plugin", "log", "list", "--plugin", PLUGIN_ID)["logs"]
            current = next(log for log in logs if log["log_id"] == invoked["log"]["log_id"])
            assert current["status"] != "failed", current
            return current["status"] == "succeeded"
        wait_for("popup action completion", action_finished)
        os.kill(launch["pid"], 0)  # Probe only the recorded private popup PID.
        after = self.api("api", "snapshot")["snapshot"]
        for collection, key in (("workspaces", "workspace_id"), ("tabs", "tab_id"), ("panes", "pane_id")):
            assert [item[key] for item in before[collection]] == [item[key] for item in after[collection]]
        assert before["layouts"] == after["layouts"]
        assert before["focused_workspace_id"] == after["focused_workspace_id"]
        print("PASS: native plugin action opens its real prompt as a popup, preserves origin and layout")

    def worktrees(self):
        spec = importlib.util.spec_from_file_location("pr_worktree", PLUGIN_ROOT / "pr-worktree.py")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        branch = "feature/pr-url-fixture"
        metadata = {"head": {"ref": branch, "sha": self.sha},
                    "base": {"repo": {"full_name": "example/repo"}}}
        original_run = helper.run
        def fixture_run(*args, **kwargs):
            if args[:3] == ("git", "remote", "get-url"):
                actual = original_run(*args, **kwargs)
                if actual.stdout.strip() == str(self.remote):
                    return subprocess.CompletedProcess(args, 0, "https://github.com/example/repo.git\n", "")
                return actual
            if args[:2] == ("gh", "api"):
                return subprocess.CompletedProcess(args, 0, json.dumps(metadata), "")
            return original_run(*args, **kwargs)
        helper.run = fixture_run
        with patch.dict(os.environ, self.env, clear=True):
            os.environ["HERDR_BIN_PATH"] = self.binary
            os.environ["HERDR_SESSION"] = self.session
            os.environ["PR_WORKTREE_CWD"] = str(self.unrelated)
            assert helper.invocation_cwd() == str(self.unrelated)
            # Fresh repository spaces have no worktree metadata: pane-CWD fallback must work.
            helper.open_pr("https://github.com/example/repo/pull/4105", str(self.unrelated), focus=False)
            listed = self.api("worktree", "list", "--cwd", str(self.repo))["worktrees"]
            matching = [tree for tree in listed if tree["branch"] == branch]
            assert len(matching) == 1, matching
            path = Path(matching[0]["path"])
            assert path.is_relative_to(self.root)
            assert self.git_run("symbolic-ref", "--short", "HEAD", cwd=path) == branch
            assert self.git_run("rev-parse", "HEAD", cwd=path) == self.sha
            spaces = self.api("workspace", "list")["workspaces"]
            parent = next(w for w in spaces if w["workspace_id"] == self.parent["workspace"]["workspace_id"])
            child = next(w for w in spaces if (w.get("worktree") or {}).get("checkout_path") == str(path))
            assert child["worktree"]["repo_key"] == parent["worktree"]["repo_key"]
            assert child["worktree"]["is_linked_worktree"] and not child["focused"]
            advanced = self.git_run("commit-tree", self.tree, "-p", self.sha, "-m", "local change", cwd=path)
            self.git_run("update-ref", "refs/heads/" + branch, advanced, cwd=path)
            (path / "file.txt").write_text("local dirty work\n")
            dirty = self.git_run("status", "--porcelain", cwd=path)
            helper.open_pr("https://github.com/example/repo/pull/4105", str(self.non_git), focus=False)
            assert self.git_run("rev-parse", "HEAD", cwd=path) == advanced
            assert (path / "file.txt").read_text() == "local dirty work\n"
            assert self.git_run("status", "--porcelain", cwd=path) == dirty
            assert [w["workspace_id"] for w in self.api("workspace", "list")["workspaces"]] == [w["workspace_id"] for w in spaces]
            assert len(self.api("worktree", "list", "--cwd", str(self.repo))["worktrees"]) == len(listed)
            assert self.git_run("rev-parse", "HEAD") == self.base
            assert not self.git_run("status", "--porcelain")
            assert not self.git_run("for-each-ref", "refs/herdr/pr-worktree")
        print("PASS: cross-space discovery, exact branch/SHA, correct group, dirty existing checkout reuse")

    def stop(self):
        if self.process.poll() is None:
            try:
                self.run(self.binary, "--session", self.session, "server", "stop")
                self.process.wait(timeout=TIMEOUT)
            except (OSError, AssertionError, subprocess.TimeoutExpired):
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
        self.log_file.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--herdr", default=shutil.which("herdr"), help="Herdr binary to test")
    args = parser.parse_args()
    if not args.herdr or not shutil.which("git"):
        parser.error("herdr and git must be installed")
    with tempfile.TemporaryDirectory(prefix="pr-smoke-", dir="/tmp") as temporary:
        fixture = Fixture(Path(temporary).resolve(), args.herdr)
        try:
            fixture.ready()
            fixture.repositories()
            fixture.popup()
            fixture.worktrees()
        except Exception:
            print(fixture.log_path.read_text()[-10000:], file=sys.stderr)
            raise
        finally:
            fixture.stop()
    print("PASS: private server stopped and temporary state removed")


if __name__ == "__main__":
    main()
