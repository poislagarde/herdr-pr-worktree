# PR Worktree for Herdr

Paste a GitHub pull request URL to open its branch in a worktree beneath the
repository's existing Herdr space. Run the action from any space: the plugin
finds the matching open repository. Existing worktrees are reused with their
local commits and uncommitted changes intact.

Requires Herdr 0.9.0+, Python 3.9+, Git, and authenticated GitHub CLI (`gh`) on
macOS or Linux. No Python packages or build step are needed.

## Install

```sh
herdr plugin install poislagarde/herdr-pr-worktree --ref v0.1.1
gh auth status
```

Add a shortcut to `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+alt+g"
type = "plugin_action"
description = "new worktree from GitHub PR"
command = "poislagarde.pr-worktree.open"
```

Apply it with `herdr server reload-config`. Press `Ctrl+B`, then `Alt+G`,
paste a PR URL, and press Enter. Leave the prompt empty or press Ctrl+C to cancel.

The same action is available as **New worktree from GitHub PR** in Herdr's
plugin actions, or through:

```sh
herdr plugin action invoke poislagarde.pr-worktree.open
```

The prompt uses a terminal popup and leaves the existing tab layout intact.
Errors remain visible until you press Enter.

## Repository and branch selection

The plugin first checks the invoking pane's directory. If its Git remotes do
not match the PR's GitHub repository, it searches open repository groups and
pane directories in the same Herdr session. The first matching open repository
is used. A checkout must have an HTTPS or SSH remote for `github.com` matching
the URL's owner and repository. Open the repository in a space before invoking
the action if it is not already available.
When invoked from a linked worktree, new and existing PR worktrees open beneath
the repository's parent space.

- An existing local worktree on the PR branch is opened as-is. The plugin does
  not fetch, reset, or change its files or commits.
- A new worktree uses the PR's actual head branch name and commit, including
  PRs from forks. Herdr manages its location and sidebar grouping.
- If the branch exists locally without a worktree and has different commits,
  the plugin reports the conflict. Update or rename that branch before retrying.
- Existing upstream settings are preserved. New branches have no upstream.
- A missing or stale checkout registration is reported for repair.

The plugin does not clone repositories, change remotes, switch the original
checkout's branch, or automatically clean up worktrees. It runs only when invoked.

URLs may include `/files`, `/commits`, `/checks`, a query, or a fragment.
GitHub Enterprise hosts and SSH host aliases are not supported.

## Local development

```sh
git clone https://github.com/poislagarde/herdr-pr-worktree.git
cd herdr-pr-worktree
herdr plugin link .
python3 -m unittest discover -s tests -p 'test_*.py'
python3 tests/herdr_smoke.py
```

The smoke test requires an installed Herdr binary and creates a private
headless session, repositories, and GitHub fixture data under a temporary
directory. It does not use your live spaces or GitHub account.

The core helper also accepts a URL directly:

```sh
python3 pr-worktree.py https://github.com/example/project/pull/123
```

Use `--cwd /path/to/repo` to prefer a repository directory, or `--no-focus` to
leave the selected space unchanged. Without a URL, it prompts in the terminal.

Unlink a development checkout before switching to a GitHub installation:

```sh
herdr plugin unlink poislagarde.pr-worktree
herdr plugin install poislagarde/herdr-pr-worktree --ref v0.1.1
```

## Remove

Remove the shortcut from `config.toml`, reload Herdr's configuration, then run:

```sh
herdr plugin uninstall poislagarde.pr-worktree
```

Existing worktrees and spaces remain available.

## License

MIT.
