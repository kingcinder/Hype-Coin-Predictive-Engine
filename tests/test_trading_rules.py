from __future__ import annotations

import json

import pytest

from trading.errors import RulesIncomplete
from trading.rules import load_rules, validate_for_open


def _write(tmp_path, payload: dict):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(payload))
    return str(path)


def test_missing_file_lists_three_missing(tmp_path) -> None:
    with pytest.raises(RulesIncomplete) as exc:
        validate_for_open(load_rules(str(tmp_path / "x.json")))
    assert exc.value.missing == [
        "buy_trigger",
        "stop_pct",
        "target_pct_or_time_stop_minutes",
    ]


def test_only_stop_pct(tmp_path) -> None:
    rules = load_rules(_write(tmp_path, {"stop_pct": 20}))
    with pytest.raises(RulesIncomplete) as exc:
        validate_for_open(rules)
    assert exc.value.missing == ["buy_trigger", "target_pct_or_time_stop_minutes"]


def test_full_rules_pass(tmp_path) -> None:
    rules = load_rules(
        _write(
            tmp_path,
            {"buy_trigger": "ignition_detected", "stop_pct": 20, "target_pct": 50},
        )
    )
    validate_for_open(rules)


def test_target_null_but_time_stop_set(tmp_path) -> None:
    rules = load_rules(
        _write(
            tmp_path,
            {"buy_trigger": "ignition_detected", "stop_pct": 20, "time_stop_minutes": 120},
        )
    )
    validate_for_open(rules)
