"""History-divergence guard: make accidental force-pushes to ``main`` impossible.

History: the remote ``main`` history was rewritten exactly once, deliberately
(2026-09-28 — the local checkout was declared authoritative, the old remote tip
was backed up to a backup branch, and the replacement was pushed with
``--force-with-lease``). A rewrite is legitimate but rare and high-blast-radius;
a typo'd ``--force`` in an ordinary sync silently destroys everyone else's work.
This guard keeps the next rewrite deliberate instead of accidental:

1. **Pre-push hook** (this script in hook mode, wired as a pre-commit
   ``pre-push`` hook): pre-commit's pre-push contract exposes the pushed refs
   as environment variables — ``PRE_COMMIT_FROM_REF`` (the remote tip before
   the push), ``PRE_COMMIT_TO_REF`` (the local tip being pushed) and
   ``PRE_COMMIT_REMOTE_BRANCH``. For ``main`` the remote tip must already be
   an ancestor of the local tip — i.e. the push must be a fast-forward. A
   diverged remote (the signature of a pending history rewrite) fails the
   push with the exact recovery commands. ``ALLOW_HISTORY_REWRITE=1``
   acknowledges a deliberate rewrite. Raw-git hook installs (no pre-commit)
   are also supported: without the env vars the script falls back to grading
   git-pre-push(1) ref lines from stdin.
2. **CI backstop** (``--check`` mode, run by the ``history-guard`` job in
   ``ci.yml``): the workflow passes ``github.event.before`` (the branch tip
   before the push) and ``github.sha`` (the new tip); a non-fast-forward push
   event fails the run. Hooks are skippable (``--no-verify``, machines without
   ``pre-commit install``) and pre-commit exposes only the *first* pushed ref
   — a multi-ref push that lists another branch before ``main`` slips past
   the local hook. CI is not skippable and sees the real event. An
   acknowledged rewrite carries ``[history-rewrite]`` in the head commit
   message and passes loudly as the audit trail.
3. **Branch protection** (the final, repo-owner step): Settings > Branches >
   main — require the status checks named in the CI header and disable
   "Allow force pushes" / "Allow deletions". Settings cannot be set from a
   file; the CI header documents the exact toggles.

Stdlib-only on purpose — same rationale as ``check_broken_imports.py``: the
plain path runs on a bare checkout (only ``git`` is required) and cannot be
defeated by a broken install step.

Usage:
    python scripts/check_history_divergence.py              # hook mode (pre-commit env or stdin)
    python scripts/check_history_divergence.py --check BEFORE AFTER
                                                            # CI: verify one push event
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

# Branch the guard protects. Every other ref (feature branches, tags) passes
# untouched — the point is protecting shared history, not personal workflows.
PROTECTED_REF = "refs/heads/main"
PROTECTED_BRANCH = "main"

# Commit-message marker that makes a CI non-fast-forward pass loudly. Mirrors
# ALLOW_HISTORY_REWRITE for the environment where an env var cannot be set.
ALLOW_MARKER = "[history-rewrite]"

_ZERO_SHA = re.compile(r"^0+$")


def _is_zero(sha: str) -> bool:
    """True for the all-zero object name git uses for creations/deletions."""
    return bool(_ZERO_SHA.match(sha))


def _git(args: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(["git", *args], capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def _is_ancestor(old: str, new: str) -> tuple[bool | None, str]:
    """``(True, '')`` if ``old`` is an ancestor of (or equal to) ``new``.

    ``(False, '')`` means a verified non-fast-forward; ``(None, stderr)``
    means git could not answer — typically a missing object because the
    remote moved since the last fetch (or a shallow clone that skipped it),
    which must fail closed.
    """
    code, _, stderr = _git(["merge-base", "--is-ancestor", old, new])
    if code == 0:
        return True, ""
    if code == 1:
        return False, ""
    return None, stderr.strip() or f"git merge-base exited {code}"


def _object_exists(sha: str) -> bool:
    code, _, _ = _git(["cat-file", "-e", f"{sha}^{{commit}}"])
    return code == 0


def _advice(lines: list[str]) -> None:
    lines.append(
        "This push would rewrite history already on origin/main. Rewrites are legitimate but rare —"
    )
    lines.append("make it deliberate:")
    lines.append("  git fetch origin && git log --oneline origin/main..HEAD   # see what diverged")
    lines.append(
        "  git merge origin/main                                     "
        "# rebase/replay your work onto the remote tip"
    )
    lines.append(
        "  ALLOW_HISTORY_REWRITE=1 git push --force-with-lease origin main   "
        "# acknowledge a deliberate rewrite"
    )


def grade_pair(old: str, new: str, *, allow_rewrite: bool = False) -> tuple[int, list[str]]:
    """Grade one ``old remote tip`` -> ``new local tip`` push; ``(exit, lines)``.

    Shared core of all three modes: hook mode (pre-commit env vars), stdin
    mode (raw git pre-push hook) and ``--check`` mode (CI backstop).
    """
    out: list[str] = []
    if _is_zero(old):
        out.append(f"history-divergence guard OK — {PROTECTED_REF} is being created — OK")
        return 0, out
    if _is_zero(new):
        out.append(f"history-divergence guard FAILED — deleting {PROTECTED_REF} is never allowed")
        out.append("(a rewrite is the only legitimate reason the remote tip would move,")
        out.append(" and this push removes the branch instead of replacing it)")
        return 1, out
    ancestor, stderr = _is_ancestor(old, new)
    if ancestor is True:
        out.append(f"history-divergence guard OK — {PROTECTED_REF} push is a fast-forward")
        return 0, out
    out.append(f"history-divergence guard FAILED — non-fast-forward push to {PROTECTED_REF}")
    out.append(f"  remote tip: {old}")
    out.append(f"  local tip:  {new}")
    if ancestor is None:
        out.append(f"  git error: {stderr}")
        out.append("The remote probably moved since your last fetch (or the object was")
        out.append("pruned). Run `git fetch origin` and re-push; the guard will then")
        out.append("either pass (fast-forward) or fail with the rewrite commands below.")
    if allow_rewrite:
        out.append("ALLOW_HISTORY_REWRITE=1 — rewrite acknowledged; continuing.")
        return 0, out
    _advice(out)
    return 1, out


def evaluate_push_lines(lines_in: list[str], *, allow_rewrite: bool) -> tuple[int, list[str]]:
    """Grade git-pre-push(1) stdin lines; return ``(exit_code, output_lines)``.

    Each non-empty line is ``<local_ref> <local_sha> <remote_ref> <remote_sha>``.
    Only pushes touching ``PROTECTED_REF`` are graded. This is the raw-git
    fallback when the pre-commit env vars are absent (stdin is drained in that
    case, so a hook-mode invocation simply finds no lines here).
    """
    out: list[str] = []
    graded = False
    for line in lines_in:
        parts = line.split()
        if len(parts) != 4:
            continue  # blank/malformed line — nothing to grade
        _local_ref, local_sha, remote_ref, remote_sha = parts
        if remote_ref != PROTECTED_REF:
            continue
        graded = True
        code, lines = grade_pair(remote_sha, local_sha, allow_rewrite=allow_rewrite)
        out.extend(lines)
        if code != 0:
            return code, out
    if not graded:
        out.append(f"history-divergence guard OK — no push to {PROTECTED_REF}")
    return 0, out


def evaluate_hook_env(allow_rewrite: bool) -> tuple[int, list[str]] | None:
    """Grade the pre-commit pre-push contract; None when the vars are absent.

    ``PRE_COMMIT_FROM_REF`` is the remote tip before the push and
    ``PRE_COMMIT_TO_REF`` the local tip (pre-commit maps the first pushed ref
    onto these). ``PRE_COMMIT_REMOTE_BRANCH`` says which branch that ref is:
    only ``main`` is graded here — pre-commit exposes just the first pushed
    ref, so a multi-ref push that lists another branch first is not visible
    to this hook at all (documented limitation; the CI backstop and branch
    protection still catch main rewrites).
    """
    from_ref = os.environ.get("PRE_COMMIT_FROM_REF", "")
    to_ref = os.environ.get("PRE_COMMIT_TO_REF", "")
    if not from_ref or not to_ref:
        return None
    remote_branch = os.environ.get("PRE_COMMIT_REMOTE_BRANCH", "").removeprefix("refs/heads/")
    if remote_branch and remote_branch != PROTECTED_BRANCH:
        return [
            0,
            [
                "history-divergence guard OK — pushed ref is "
                f"'{remote_branch}', not '{PROTECTED_BRANCH}'",
                "(note: pre-commit exposes only the first pushed ref; a multi-ref",
                f" push including {PROTECTED_BRANCH} is backstopped by the CI history-guard job)",
            ],
        ]
    code, lines = grade_pair(from_ref, to_ref, allow_rewrite=allow_rewrite)
    return code, lines


def check_event(before: str, after: str, *, commit_message: str) -> tuple[int, list[str]]:
    """Grade one push event for CI; return ``(exit_code, output_lines)``.

    ``before`` is ``github.event.before`` (empty on non-push events, all-zero
    on branch creation); ``after`` is ``github.sha``.
    """
    out: list[str] = []
    if not before:
        out.append("history-divergence guard OK — not a push event (no github.event.before)")
        return 0, out
    if _is_zero(before):
        out.append(f"history-divergence guard OK — {PROTECTED_REF} was created by this push")
        return 0, out
    if ALLOW_MARKER in commit_message:
        out.append(
            f"history-divergence guard ACKNOWLEDGED — head commit message carries {ALLOW_MARKER}"
        )
        out.append("This run records a deliberate history rewrite as the audit trail.")
        return 0, out
    if not _object_exists(after):
        out.append(f"history-divergence guard FAILED — new tip {after} is not in the checkout")
        out.append("(the history-guard job needs `fetch-depth: 0`; did the checkout change?)")
        return 1, out
    ancestor, stderr = _is_ancestor(before, after)
    if ancestor is None:
        # For a fast-forward push the old tip is part of the fetched history,
        # so a missing object is itself the rewrite signature; on a shallow
        # checkout it would be a false alarm — hence the fetch-depth note.
        out.append("history-divergence guard FAILED — pre-push tip is not in the fetched history")
        out.append(f"  git error: {stderr}")
        out.append("For a fast-forward push this object would have been fetched; missing it is")
        out.append("the force-push signature. (Shallow checkout? The job must set fetch-depth: 0.)")
        return 1, out
    if ancestor is False:
        out.append(f"history-divergence guard FAILED — push rewrote {PROTECTED_REF}")
        out.append(f"  before: {before}")
        out.append(f"  after:  {after}")
        out.append(
            f"To record a deliberate rewrite, include {ALLOW_MARKER} in the head commit message."
        )
        _advice(out)
        return 1, out
    out.append(f"history-divergence guard OK — push to {PROTECTED_REF} was a fast-forward")
    return 0, out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Block non-fast-forward pushes to refs/heads/main (history-divergence guard)"
    )
    parser.add_argument(
        "--check",
        nargs=2,
        metavar=("BEFORE", "AFTER"),
        help="CI mode: verify one push event (github.event.before, github.sha) instead of stdin",
    )
    parser.add_argument(
        "--commit-message",
        default="",
        help="head commit message (CI mode); containing the marker acknowledges a rewrite",
    )
    args = parser.parse_known_args(argv)[0]  # pre-push stage appends remote name/url — ignore them

    allow_rewrite = os.environ.get("ALLOW_HISTORY_REWRITE") == "1"
    if args.check is not None:
        before, after = args.check
        code, out = check_event(before, after, commit_message=args.commit_message)
    else:
        hook = evaluate_hook_env(allow_rewrite)
        if hook is not None:
            code, out = hook
        else:
            code, out = evaluate_push_lines(
                sys.stdin.read().splitlines(), allow_rewrite=allow_rewrite
            )
    for line in out:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
