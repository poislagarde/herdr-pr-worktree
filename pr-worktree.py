#!/usr/bin/env python3
"""Open a GitHub pull request's head branch in a herdr worktree space."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import urlsplit
import uuid


class WorktreeError(Exception):
    pass


def run(*args, cwd=None, check=True):
    env = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_PREFIX"):
        env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True)
    if check and result.returncode:
        raise WorktreeError(result.stderr.strip() or result.stdout.strip()
                            or f"{args[0]} exited with status {result.returncode}")
    return result


def pr_url(value):
    parsed = urlsplit(value.strip())
    match = re.fullmatch(
        r"/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)"
        r"(?:/(?:files|commits|checks))?/?", parsed.path)
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com" or not match:
        raise WorktreeError("Enter a GitHub PR URL: https://github.com/owner/repo/pull/123")
    owner, repo, number = match.groups()
    if owner in (".", "..") or repo in (".", ".."):
        raise WorktreeError("Enter a GitHub URL with a repository owner and name.")
    return f"{owner}/{repo}", number


def github_repo(remote_url):
    match = re.fullmatch(r"git@github\.com:([^/]+/[^/]+?)(?:\.git)?/?", remote_url)
    if match:
        return match[1].lower()
    parsed = urlsplit(remote_url)
    if parsed.scheme in ("https", "ssh") and parsed.hostname == "github.com":
        path = parsed.path.strip("/")
        if path.endswith(".git"):
            path = path[:-4]
        if re.fullmatch(r"[^/]+/[^/]+", path):
            return path.lower()
    return None


def find_repository(repository, cwd, herdr):
    """Prefer the current repo, then search repo groups and ordinary space panes."""
    seen = set()

    def matching_checkout(path):
        if not isinstance(path, str) or not path:
            return None
        try:
            result = run("git", "rev-parse", "--show-toplevel", cwd=path, check=False)
            if result.returncode:
                return None
            root = result.stdout.rstrip("\n")
            if root in seen:
                return None
            seen.add(root)
            remotes = run("git", "remote", cwd=root).stdout.splitlines()
            matches = [remote for remote in remotes if github_repo(
                run("git", "remote", "get-url", remote, cwd=root).stdout.strip())
                == repository.lower()]
            if matches:
                return root, "origin" if "origin" in matches else matches[0]
        except (WorktreeError, OSError):
            # A closed or moved checkout must not hide other open repositories.
            return None
        return None

    match = matching_checkout(cwd)
    if match:
        return match
    workspaces = json.loads(run(herdr, "workspace", "list").stdout)["result"]["workspaces"]
    for workspace in workspaces:
        tree = workspace.get("worktree") or {}
        for path in (tree.get("repo_root"), tree.get("checkout_path")):
            match = matching_checkout(path)
            if match:
                return match
    # Ordinary spaces may have no worktree provenance until a worktree is opened.
    for workspace in workspaces:
        response = run(herdr, "pane", "list", "--workspace", workspace["workspace_id"],
                       check=False)
        if response.returncode:
            continue
        for pane in json.loads(response.stdout)["result"]["panes"]:
            for path in (pane.get("foreground_cwd"), pane.get("cwd")):
                match = matching_checkout(path)
                if match:
                    return match
    raise WorktreeError(f"No open space or current directory has a GitHub remote for {repository}. "
                        "Open that repository in a herdr space, then retry.")


def ensure_upstream(root, branch, head_repo, base_remote, number):
    """Supply pull defaults without replacing any existing tracking settings."""
    for key in ("remote", "merge"):
        configured = run("git", "config", "--get", f"branch.{branch}.{key}",
                         cwd=root, check=False)
        if configured.returncode == 0:
            return
        if configured.returncode != 1:
            raise WorktreeError(configured.stderr.strip() or "Unable to read branch tracking settings.")

    remote = base_remote
    merge = f"refs/pull/{number}/head"
    if head_repo is not None:
        repository = head_repo["full_name"]
        if (not isinstance(repository, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
                or any(part in (".", "..") for part in repository.split("/"))):
            raise WorktreeError("GitHub returned an invalid PR head repository.")
        matches = [name for name in run("git", "remote", cwd=root).stdout.splitlines()
                   if github_repo(run("git", "remote", "get-url", name,
                                      cwd=root).stdout.strip()) == repository.lower()]
        if matches:
            remote = "origin" if "origin" in matches else matches[0]
        else:
            # A URL remote supports fork pulls without adding a named remote.
            base_url = run("git", "remote", "get-url", base_remote, cwd=root).stdout.strip()
            remote = (f"https://github.com/{repository}.git" if base_url.startswith("https://")
                      else f"git@github.com:{repository}.git")
        merge = "refs/heads/" + branch

    run("git", "config", f"branch.{branch}.remote", remote, cwd=root)
    run("git", "config", f"branch.{branch}.merge", merge, cwd=root)


def finish_open(response, branch, head_repo, base_remote, number, sha=None):
    result = json.loads(response.stdout)["result"]
    path = result["worktree"]["path"]
    actual_branch = run("git", "symbolic-ref", "--quiet", "HEAD", cwd=path).stdout.strip()
    actual_sha = run("git", "rev-parse", "HEAD", cwd=path).stdout.strip()
    if actual_branch != "refs/heads/" + branch or (sha is not None and actual_sha != sha):
        raise WorktreeError(f"The branch changed while opening {path}. "
                            "Inspect that checkout before working; it has been left unchanged.")
    ensure_upstream(path, branch, head_repo, base_remote, number)
    print(f"Opened {branch}\n{path}")


def invocation_cwd():
    """Plugin commands start in the plugin checkout; prefer the invoking pane."""
    if os.environ.get("PR_WORKTREE_CWD"):
        return os.environ["PR_WORKTREE_CWD"]
    context = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON", "{}"))
    for path in (context.get("focused_pane_cwd"), context.get("workspace_cwd"),
                 (context.get("worktree") or {}).get("checkout_path")):
        if isinstance(path, str) and path:
            return path
    return os.environ.get("HERDR_ACTIVE_PANE_CWD") or os.getcwd()


def recover_missing_worktree(root, path, branch, sha):
    """Remove only a confirmed stale registration, retaining its metadata."""
    path = Path(path)
    hint = (f"The checkout for '{branch}' needs repair: {path}. "
            "If it moved, run git worktree repair at its new location; "
            "if its disk is offline, reconnect it before retrying.")

    def require_missing_path():
        try:
            path.lstat()
        except FileNotFoundError:
            if path.parent.is_dir():
                return
        raise WorktreeError(hint)

    require_missing_path()

    def require_stale_registration():
        trees = []
        tree = {}
        for field in run("git", "worktree", "list", "--porcelain", "-z", cwd=root).stdout.split("\0"):
            if field:
                key, _, value = field.partition(" ")
                tree[key] = value
            elif tree:
                trees.append(tree)
                tree = {}
        matching = [entry for entry in trees if entry.get("worktree") == str(path)]
        if (len(matching) != 1 or matching[0] is trees[0]
                or matching[0].get("branch") != "refs/heads/" + branch
                or matching[0].get("HEAD") != sha
                or "locked" in matching[0] or "prunable" not in matching[0]):
            raise WorktreeError(hint)

    require_stale_registration()

    common = Path(run("git", "rev-parse", "--path-format=absolute", "--git-common-dir",
                      cwd=root).stdout.rstrip("\n"))
    candidates = [entry for entry in (common / "worktrees").iterdir()
                  if entry.is_dir() and (entry / "gitdir").is_file()
                  and (entry / "gitdir").read_text().rstrip("\n") == str(path / ".git")]
    if len(candidates) != 1:
        raise WorktreeError(hint)
    admin = candidates[0]

    def require_clean_metadata():
        # A deleted checkout may still have staged work or an interrupted operation.
        if ((admin / "gitdir").read_text().rstrip("\n") != str(path / ".git")
                or not (admin / "index").is_file() or any(admin.rglob("*.lock"))
                or any((admin / name).exists() for name in (
                    "locked", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD",
                    "rebase-merge", "rebase-apply", "sequencer", "BISECT_START"))):
            raise WorktreeError(hint + " Its Git metadata needs manual inspection.")
        clean = run("git", "--git-dir", str(admin), "diff", "--cached", "--quiet",
                    "--no-ext-diff", "--ignore-submodules=none", "HEAD", "--", cwd=root,
                    check=False)
        if clean.returncode:
            raise WorktreeError(hint + " Its index may contain staged changes; it was preserved.")

    require_clean_metadata()
    backup = common / "herdr-pr-worktree-recovery" / uuid.uuid4().hex
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(admin, backup)
    print(f"Saved missing checkout metadata to {backup}", flush=True)
    require_missing_path()
    require_stale_registration()
    require_clean_metadata()
    # No force: Git must still enforce locks and reject a checkout that reappears dirty.
    run("git", "worktree", "remove", "--", str(path), cwd=root)
    print(f"Removed stale registration for {branch}; recreating its checkout …", flush=True)


def open_pr(url, cwd, focus=True):
    repository, number = pr_url(url)
    herdr = os.environ.get("HERDR_BIN_PATH", "herdr")
    root, remote = find_repository(repository, cwd, herdr)
    # Validate the server and repository before fetching anything.
    listing = json.loads(run(herdr, "worktree", "list", "--cwd", root).stdout)["result"]
    # Listing accepts linked checkouts; create/open require their parent checkout.
    source_root = listing["source"]["source_checkout_path"]
    canonical = f"https://github.com/{repository}/pull/{number}"
    print(f"Looking up {canonical} …", flush=True)
    pr = json.loads(run("gh", "api", "--hostname", "github.com",
                        f"repos/{repository}/pulls/{number}").stdout)
    branch = pr["head"]["ref"]
    sha = pr["head"]["sha"]
    if pr["base"]["repo"]["full_name"].lower() != repository.lower():
        raise WorktreeError("GitHub returned a different repository; use the PR's canonical URL.")
    if not isinstance(branch, str) or branch.startswith("-") or not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        raise WorktreeError("GitHub returned an invalid PR head.")
    run("git", "check-ref-format", "refs/heads/" + branch, cwd=root)
    focus_flag = "--focus" if focus else "--no-focus"
    existing = next((tree for tree in listing["worktrees"]
                     if tree.get("branch") == branch), None)
    missing = existing and (existing.get("is_prunable") or not Path(existing["path"]).is_dir())
    if existing and not missing:
        response = run(herdr, "worktree", "open", "--cwd", source_root,
                       "--path", existing["path"], focus_flag)
        finish_open(response, branch, pr["head"].get("repo"), remote, number)
        return
    print(f"Fetching PR #{number}: {branch} …", flush=True)
    # A unique ref avoids FETCH_HEAD races and works for fork PRs too.
    fetch_ref = f"refs/herdr/pr-worktree/{uuid.uuid4().hex}"
    fetched = None
    try:
        run("git", "fetch", "--no-tags", "--no-auto-maintenance", "--no-write-fetch-head",
            "--refmap=", "--", remote,
            f"refs/pull/{number}/head:{fetch_ref}", cwd=root)
        fetched = run("git", "rev-parse", "--verify", fetch_ref, cwd=root).stdout.strip()
        if fetched != sha:
            raise WorktreeError("The PR changed while fetching. Try again to use its latest head.")
        refs = run("git", "for-each-ref", "--format=%(refname) %(objectname)",
                   "refs/heads/" + branch, cwd=root).stdout.splitlines()
        local = next((line.split(" ", 1)[1] for line in refs
                      if line.startswith("refs/heads/" + branch + " ")), None)
        if local is not None and local != sha:
            raise WorktreeError(f"Local branch '{branch}' has different commits from PR #{number}. "
                                "Update or rename it, then retry. It has been left unchanged.")
        if missing:
            recover_missing_worktree(root, existing["path"], branch, sha)
        response = run(herdr, "worktree", "create", "--cwd", source_root,
                       "--branch", branch, "--base", sha, focus_flag)
        finish_open(response, branch, pr["head"].get("repo"), remote, number, sha)
    finally:
        if fetched:
            run("git", "update-ref", "-d", fetch_ref, fetched, cwd=root, check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", help="GitHub pull request URL (prompts when omitted)")
    parser.add_argument("--cwd",
                        help="Preferred directory; also searches open herdr spaces")
    parser.add_argument("--no-focus", action="store_true", help="Keep the current space focused")
    args = parser.parse_args()
    interactive = args.url is None
    try:
        url = args.url
        if interactive:
            print("Paste a PR URL. Leave empty or press Ctrl+C to cancel.\n")
            url = input("PR URL: ").strip()
        if not url:
            return 0
        open_pr(url, args.cwd or invocation_cwd(), focus=not args.no_focus)
        return 0
    except (EOFError, KeyboardInterrupt):
        return 130
    except (WorktreeError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"\nUnable to open PR worktree: {error}", file=sys.stderr)
        if interactive and sys.stdin.isatty():
            try:
                input("\nPress Enter to close.")
            except (EOFError, KeyboardInterrupt):
                pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
