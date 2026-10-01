"""Thin wrapper around the ``git`` command-line client.

We shell out to git rather than using a library so the tool never handles
credentials itself: authentication is whatever the user's own git
configuration already does (credential helper, SSH agent, GCM). Nothing
credential-shaped is read, logged, or written by this package.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional, Sequence

from discovery_agent.errors import GitCommandError

DEFAULT_TIMEOUT_SECONDS = 600
_STDERR_EXCERPT_CHARS = 2000


def _git_env() -> dict:
    """Environment for git subprocesses.

    ``GIT_TERMINAL_PROMPT=0`` makes an unauthenticated clone fail immediately
    instead of blocking forever on an interactive credential prompt.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def run_git(
    args: Sequence[str],
    cwd: Optional[Path] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Run one git command and return its stdout, stripped.

    Raises GitCommandError if git is missing, times out, or exits non-zero.
    The command is passed as an argument vector, never through a shell.
    """
    command = ["git"] + [str(a) for a in args]
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            timeout=timeout,
            env=_git_env(),
        )
    except FileNotFoundError:
        raise GitCommandError(
            args, None, "git executable not found on PATH; install Git and retry"
        )
    except subprocess.TimeoutExpired:
        raise GitCommandError(args, None, f"git timed out after {timeout}s")

    stdout = completed.stdout.decode("utf-8", errors="replace")
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise GitCommandError(args, completed.returncode, stderr[:_STDERR_EXCERPT_CHARS])
    return stdout.strip()
