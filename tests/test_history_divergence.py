"""Unit tests for the history-divergence guard
(scripts/check_history_divergence.py).

Pins the three grading modes of the force-push guard:

- ``grade_pair`` — the shared core: branch creation passes, deletion never
  passes, fast-forward passes, non-fast-forward fails with recovery commands
  (unless ``ALLOW_HISTORY_REWRITE`` acknowledges the rewrite), and an
  unverifiable comparison fails CLOSED (git couldn't answer → block).
- ``evaluate_push_lines`` — the raw git-pre-push(1) stdin fallback: only
  ``refs/heads/main`` lines are graded, malformed lines are skipped, a
  failing main line fails the whole push, and a push with no main line is
  explicitly OK.
- ``evaluate_hook_env`` — the pre-commit pre-push contract
  (``PRE_COMMIT_FROM_REF`` / ``PRE_COMMIT_TO_REF`` /
  ``PRE_COMMIT_REMOTE_BRANCH``): absent vars mean "fall through to stdin"
  (``None``), a non-main remote branch passes ungraded with the multi-ref
  caveat printed, and a main ref grades the pair.
- ``check_event`` — the CI ``--check`` backstop: non-push and branch-creation
  events pass, the ``[history-rewrite]`` head-commit marker acknowledges a
  deliberate rewrite BEFORE any git call, a new tip missing from the checkout
  fails with the fetch-depth hint, and a verified non-fast-forward fails with
  the marker hint.

The git-backed cases run against a tiny scratch repo (base → child, plus an
amended sibling of child) built once per module; ``monkeypatch.chdir`` points
the script's ``git`` subprocess calls at it, since the guard deliberately
inherits the hook's cwd. A CLI section runs the REAL script as a subprocess
with a scrubbed environment (no ``PRE_COMMIT_*`` / ``ALLOW_HISTORY_REWRITE``
leakage), pinning exit codes and message content for ``--check``, stdin, and
the ignored trailing ``remote``/``url`` args the pre-push stage appends.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.check_history_divergence import (
    ALLOW_MARKER,
    check_event,
    evaluate_hook_env,
    evaluate_push_lines,
    grade_pair,
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_history_divergence.py"
ZERO = "0" * 40
BOGUS = "f" * 40  # well-formed but nonexistent — git cannot answer


@pytest.fixture(scope="module")
def scratch(tmp_path_factory) -> SimpleNamespace:
    """base → child, plus sibling (an amended child, i.e. diverged)."""
    repo = tmp_path_factory.mktemp("hist-guard") / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
        return proc.stdout.strip()

    git("init", "-b", "main", "-q")
    git("config", "user.email", "guard@example.com")
    git("config", "user.name", "guard-test")
    (repo / "a.txt").write_text("a\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (repo / "b.txt").write_text("b\n")
    git("add", "-A")
    git("commit", "-qm", "child")
    child = git("rev-parse", "HEAD")
    git("commit", "--amend", "-qm", "sibling")
    sibling = git("rev-parse", "HEAD")
    return SimpleNamespace(repo=repo, base=base, child=child, sibling=sibling)


@pytest.fixture(autouse=True)
def _clean_guard_env(monkeypatch: pytest.MonkeyPatch, scratch: SimpleNamespace) -> None:
    """Scrub the guard's inputs and point git calls at the scratch repo.

    Module-scoped ``scratch`` + function-scoped chdir is safe: the fixture
    only ever reads the scratch repo, never writes after setup.
    """
    for var in (
        "PRE_COMMIT_FROM_REF",
        "PRE_COMMIT_TO_REF",
        "PRE_COMMIT_REMOTE_BRANCH",
        "PRE_COMMIT_REMOTE_NAME",
        "PRE_COMMIT_REMOTE_URL",
        "ALLOW_HISTORY_REWRITE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(scratch.repo)


# ---------------------------------------------------------------- grade_pair


class TestGradePair:
    def test_branch_creation_passes(self) -> None:
        code, out = grade_pair(ZERO, "abc123", allow_rewrite=False)
        assert code == 0
        assert any("being created" in line for line in out)

    def test_deletion_never_passes(self) -> None:
        code, out = grade_pair("abc123", ZERO, allow_rewrite=False)
        assert code == 1
        assert any("deleting" in line and "never allowed" in line for line in out)

    def test_fast_forward_passes(self, scratch: SimpleNamespace) -> None:
        code, out = grade_pair(scratch.base, scratch.child, allow_rewrite=False)
        assert code == 0
        assert any("fast-forward" in line for line in out)

    def test_noop_push_passes(self, scratch: SimpleNamespace) -> None:
        # old == new: --is-ancestor treats equality as ancestry (already up to date)
        code, _ = grade_pair(scratch.child, scratch.child, allow_rewrite=False)
        assert code == 0

    def test_non_fast_forward_fails_with_recovery_commands(self, scratch: SimpleNamespace) -> None:
        code, out = grade_pair(scratch.sibling, scratch.child, allow_rewrite=False)
        assert code == 1
        assert any("non-fast-forward" in line for line in out)
        assert any("ALLOW_HISTORY_REWRITE=1 git push --force-with-lease" in line for line in out)
        assert any("git fetch origin" in line for line in out)

    def test_non_fast_forward_with_override_passes(self, scratch: SimpleNamespace) -> None:
        code, out = grade_pair(scratch.sibling, scratch.child, allow_rewrite=True)
        assert code == 0
        assert any("acknowledged" in line for line in out)

    def test_unverifiable_fails_closed(self, scratch: SimpleNamespace) -> None:
        code, out = grade_pair(scratch.base, BOGUS, allow_rewrite=False)
        assert code == 1
        assert any("git error" in line for line in out)

    def test_unverifiable_with_override_passes(self, scratch: SimpleNamespace) -> None:
        # The operator override also blesses "cannot verify" — it is explicit consent.
        code, _ = grade_pair(scratch.base, BOGUS, allow_rewrite=True)
        assert code == 0


# ------------------------------------------------------- evaluate_push_lines


class TestEvaluatePushLines:
    def test_main_fast_forward_line_passes(self, scratch: SimpleNamespace) -> None:
        lines = [f"refs/heads/main {scratch.child} refs/heads/main {scratch.base}"]
        code, out = evaluate_push_lines(lines, allow_rewrite=False)
        assert code == 0
        assert any("fast-forward" in line for line in out)

    def test_main_non_fast_forward_line_fails(self, scratch: SimpleNamespace) -> None:
        # remote tip = sibling (amended), local tip = child -> diverged
        lines = [f"refs/heads/main {scratch.child} refs/heads/main {scratch.sibling}"]
        code, out = evaluate_push_lines(lines, allow_rewrite=False)
        assert code == 1
        assert any("non-fast-forward" in line for line in out)

    def test_other_branch_ignored(self) -> None:
        lines = ["refs/heads/feature abc refs/heads/feature def"]
        code, out = evaluate_push_lines(lines, allow_rewrite=False)
        assert code == 0
        assert any("no push" in line for line in out)

    def test_malformed_lines_skipped(self) -> None:
        code, _ = evaluate_push_lines(["", "one two three", "a b c d e"], allow_rewrite=False)
        assert code == 0

    def test_failing_main_line_short_circuits(self, scratch: SimpleNamespace) -> None:
        # The good feature line must not mask the failing main line.
        lines = [
            f"refs/heads/feature {scratch.base} refs/heads/feature {scratch.base}",
            f"refs/heads/main {scratch.child} refs/heads/main {scratch.sibling}",
        ]
        code, out = evaluate_push_lines(lines, allow_rewrite=False)
        assert code == 1
        assert any("non-fast-forward" in line for line in out)

    def test_empty_stdin_is_ok(self) -> None:
        code, out = evaluate_push_lines([], allow_rewrite=False)
        assert code == 0
        assert any("no push" in line for line in out)


# --------------------------------------------------------- evaluate_hook_env


class TestEvaluateHookEnv:
    def test_missing_vars_mean_fall_through_to_stdin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert evaluate_hook_env(allow_rewrite=False) is None

    def test_non_main_branch_passes_ungraded(
        self, scratch: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Even a nonsensical pair must not be graded for a non-main ref.
        monkeypatch.setenv("PRE_COMMIT_FROM_REF", "abc123")
        monkeypatch.setenv("PRE_COMMIT_TO_REF", "def456")
        monkeypatch.setenv("PRE_COMMIT_REMOTE_BRANCH", "refs/heads/feature")
        code, out = evaluate_hook_env(allow_rewrite=False)
        assert code == 0
        assert any("not 'main'" in line for line in out)
        assert any("backstopped by the CI history-guard job" in line for line in out)

    def test_main_ref_grades_the_pair(
        self, scratch: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRE_COMMIT_FROM_REF", scratch.sibling)
        monkeypatch.setenv("PRE_COMMIT_TO_REF", scratch.child)
        monkeypatch.setenv("PRE_COMMIT_REMOTE_BRANCH", "refs/heads/main")
        code, out = evaluate_hook_env(allow_rewrite=False)
        assert code == 1
        assert any("non-fast-forward" in line for line in out)

    def test_main_ref_fast_forward(
        self, scratch: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRE_COMMIT_FROM_REF", scratch.base)
        monkeypatch.setenv("PRE_COMMIT_TO_REF", scratch.child)
        monkeypatch.setenv("PRE_COMMIT_REMOTE_BRANCH", "refs/heads/main")
        code, _ = evaluate_hook_env(allow_rewrite=False)
        assert code == 0

    def test_missing_remote_branch_grades_as_main(
        self, scratch: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Conservative direction: absent branch info is graded, not skipped.
        monkeypatch.setenv("PRE_COMMIT_FROM_REF", scratch.sibling)
        monkeypatch.setenv("PRE_COMMIT_TO_REF", scratch.child)
        monkeypatch.delenv("PRE_COMMIT_REMOTE_BRANCH", raising=False)
        code, _ = evaluate_hook_env(allow_rewrite=False)
        assert code == 1


# ---------------------------------------------------------------- check_event


class TestCheckEvent:
    def test_non_push_event_passes(self) -> None:
        code, out = check_event("", "abc123", commit_message="")
        assert code == 0
        assert any("not a push event" in line for line in out)

    def test_branch_creation_passes(self) -> None:
        code, out = check_event(ZERO, "abc123", commit_message="")
        assert code == 0
        assert any("was created" in line for line in out)

    def test_marker_acknowledges_before_any_git_call(self, scratch: SimpleNamespace) -> None:
        # before/after are intentionally absurd — the marker short-circuits first.
        code, out = check_event(BOGUS, BOGUS, commit_message=f"reset main {ALLOW_MARKER}")
        assert code == 0
        assert any("ACKNOWLEDGED" in line for line in out)

    def test_fast_forward_event_passes(self, scratch: SimpleNamespace) -> None:
        code, out = check_event(scratch.base, scratch.child, commit_message="")
        assert code == 0
        assert any("fast-forward" in line for line in out)

    def test_non_fast_forward_event_fails_with_marker_hint(self, scratch: SimpleNamespace) -> None:
        code, out = check_event(scratch.sibling, scratch.child, commit_message="")
        assert code == 1
        assert any("rewrote" in line for line in out)
        assert any("include [history-rewrite] in the head commit message" in line for line in out)

    def test_missing_new_tip_fails_with_fetch_depth_hint(self, scratch: SimpleNamespace) -> None:
        code, out = check_event(scratch.base, BOGUS, commit_message="")
        assert code == 1
        assert any("not in the checkout" in line for line in out)
        assert any("fetch-depth: 0" in line for line in out)

    def test_missing_before_tip_fails_closed(self, scratch: SimpleNamespace) -> None:
        # A fast-forward push would have fetched the old tip; missing it is
        # itself the rewrite signature (or a shallow checkout misconfig).
        code, out = check_event(BOGUS, scratch.child, commit_message="")
        assert code == 1
        assert any("not in the fetched history" in line for line in out)


# --------------------------------------------------------- CLI (real script)


def _scrubbed_env(**extra: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("PRE_COMMIT") and k != "ALLOW_HISTORY_REWRITE"
    }
    env.update(extra)
    return env


def _run_cli(
    *args: str, cwd: Path, input_text: str = "", **env_extra: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        input=input_text,
        capture_output=True,
        text=True,
        env=_scrubbed_env(**env_extra),
    )


class TestCli:
    def test_check_fast_forward_exits_zero(self, scratch: SimpleNamespace) -> None:
        proc = _run_cli("--check", scratch.base, scratch.child, cwd=scratch.repo)
        assert proc.returncode == 0
        assert "fast-forward" in proc.stdout

    def test_check_non_fast_forward_exits_one(self, scratch: SimpleNamespace) -> None:
        proc = _run_cli("--check", scratch.sibling, scratch.child, cwd=scratch.repo)
        assert proc.returncode == 1
        assert "push rewrote" in proc.stdout
        assert "--force-with-lease" in proc.stdout  # recovery commands present

    def test_check_marker_exits_zero(self, scratch: SimpleNamespace) -> None:
        proc = _run_cli(
            "--check",
            scratch.sibling,
            scratch.child,
            "--commit-message",
            f"replace history {ALLOW_MARKER}",
            cwd=scratch.repo,
        )
        assert proc.returncode == 0
        assert "ACKNOWLEDGED" in proc.stdout

    def test_stdin_fast_forward_exits_zero(self, scratch: SimpleNamespace) -> None:
        line = f"refs/heads/main {scratch.child} refs/heads/main {scratch.base}\n"
        proc = _run_cli(cwd=scratch.repo, input_text=line)
        assert proc.returncode == 0

    def test_stdin_non_fast_forward_exits_one(self, scratch: SimpleNamespace) -> None:
        line = f"refs/heads/main {scratch.child} refs/heads/main {scratch.sibling}\n"
        proc = _run_cli(cwd=scratch.repo, input_text=line)
        assert proc.returncode == 1
        assert "non-fast-forward" in proc.stdout

    def test_stdin_non_fast_forward_override_exits_zero(self, scratch: SimpleNamespace) -> None:
        line = f"refs/heads/main {scratch.child} refs/heads/main {scratch.sibling}\n"
        proc = _run_cli(cwd=scratch.repo, input_text=line, ALLOW_HISTORY_REWRITE="1")
        assert proc.returncode == 0
        assert "acknowledged" in proc.stdout

    def test_pre_push_stage_trailing_args_are_ignored(self, scratch: SimpleNamespace) -> None:
        # pre-commit's pre-push stage appends `remote` and `url` positionally;
        # parse_known_args must swallow them, not error.
        line = f"refs/heads/main {scratch.child} refs/heads/main {scratch.base}\n"
        proc = _run_cli("origin", str(scratch.repo), cwd=scratch.repo, input_text=line)
        assert proc.returncode == 0

    def test_env_contract_pre_commit_vars_drive_hook_mode(self, scratch: SimpleNamespace) -> None:
        # Simulate exactly what pre-commit hands the hook on a forced push.
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=scratch.repo,
            capture_output=True,
            text=True,
            env=_scrubbed_env(
                PRE_COMMIT_FROM_REF=scratch.sibling,
                PRE_COMMIT_TO_REF=scratch.child,
                PRE_COMMIT_REMOTE_BRANCH="refs/heads/main",
            ),
        )
        assert proc.returncode == 1
        assert "non-fast-forward" in proc.stdout

    def test_env_contract_non_main_branch_not_graded(self, scratch: SimpleNamespace) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=scratch.repo,
            capture_output=True,
            text=True,
            env=_scrubbed_env(
                PRE_COMMIT_FROM_REF="abc123",
                PRE_COMMIT_TO_REF="def456",
                PRE_COMMIT_REMOTE_BRANCH="refs/heads/feature",
            ),
        )
        assert proc.returncode == 0
        assert "not 'main'" in proc.stdout
