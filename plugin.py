#!/usr/bin/env python3
"""Open the plugin's PR URL prompt with the invoking space's context."""

import json
import os
import subprocess
import sys


def open_prompt():
    context = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON", "{}"))
    command = [os.environ.get("HERDR_BIN_PATH", "herdr"), "plugin", "pane", "open",
               "--plugin", "poislagarde.pr-worktree", "--entrypoint", "prompt", "--focus"]
    # Popups always target the active pane. Carry the originating directory
    # separately so repository selection remains tied to the action's context.
    for path in (context.get("focused_pane_cwd"), context.get("workspace_cwd"),
                 (context.get("worktree") or {}).get("checkout_path")):
        if isinstance(path, str) and path:
            command.extend(["--env", "PR_WORKTREE_CWD=" + path])
            break
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip()
                           or "Herdr could not open the PR URL prompt.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(open_prompt())
    except (OSError, ValueError, RuntimeError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
