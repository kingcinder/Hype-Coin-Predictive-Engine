from __future__ import annotations

import runpy
import sys
from types import SimpleNamespace

import pytest

from validation.cli import main


def test_missing_cli_operation_prints_help_and_returns_one(capsys) -> None:
    assert main([]) == 1
    assert "usage:" in capsys.readouterr().out.lower()


def test_self_test_success_and_failure_choose_distinct_exit_codes(monkeypatch, capsys) -> None:
    passed = SimpleNamespace(
        passed=True,
        case="noise",
        verdict="inconclusive",
        leakage_flagged=False,
        metrics={"accuracy": 0.5},
        leakage_reason=None,
    )
    monkeypatch.setattr("validation.cli.run_self_tests", lambda: [passed])

    assert main(["--self-test"]) == 0
    assert "ALL SELF-TESTS PASSED" in capsys.readouterr().out

    failed = SimpleNamespace(
        **{
            **passed.__dict__,
            "passed": False,
            "leakage_reason": "future row leaked",
        }
    )
    monkeypatch.setattr("validation.cli.run_self_tests", lambda: [failed])

    assert main(["--self-test"]) == 2
    output = capsys.readouterr()
    assert "SELF-TESTS FAILED" in output.err
    assert "future row leaked" in output.out


def test_database_cli_forwards_arguments_and_writes_report(monkeypatch) -> None:
    import sqlalchemy.orm

    import storage.database
    import validation.harness

    class SessionContext:
        def __enter__(self):
            return object()

        def __exit__(self, *_exc):
            return None

    class Report:
        generated_at_utc = "2026-10-23T01:00:00+00:00"
        cells = []
        suspicious_results = []
        saved_path: str | None = None

        def save(self, path: str) -> str:
            self.saved_path = path
            return path

    report = Report()
    captured: dict[str, object] = {}
    monkeypatch.setattr(storage.database, "make_engine", lambda **kwargs: kwargs)
    monkeypatch.setattr(sqlalchemy.orm, "sessionmaker", lambda **_kwargs: SessionContext)

    def fake_harness(session, *, git_sha, model_version, min_samples):
        captured.update(
            session=session,
            git_sha=git_sha,
            model_version=model_version,
            min_samples=min_samples,
        )
        return report

    monkeypatch.setattr(validation.harness, "run_harness", fake_harness)

    result = main(["--db", "prices.db", "--out", "reports/test.json", "--min-samples", "7"])

    assert result == 0
    assert report.saved_path == "reports/test.json"
    assert captured["model_version"] == "engine-current"
    assert captured["min_samples"] == 7


def test_module_entrypoint_propagates_cli_exit_code(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["validation"])
    monkeypatch.setattr("validation.cli.main", lambda: 7)

    with pytest.raises(SystemExit) as exc_info:
        runpy.run_module("validation.__main__", run_name="__main__")

    assert exc_info.value.code == 7
