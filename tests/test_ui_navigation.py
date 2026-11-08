from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

import ui.api_client
from ui.app import NAV_SECTIONS
from ui.health_diagnostics_view import _format_timestamp, _weight_evolution_figure


def test_health_timestamp_accepts_numeric_api_value() -> None:
    assert _format_timestamp(1_792_934_400.0) == "1792934400.0"


def test_weight_evolution_chart_accepts_mixed_null_history() -> None:
    fig = _weight_evolution_figure(
        [
            {
                "ts": "2026-10-25T12:00:00",
                "weights": {"rule": 0.4, "ml": 0.4, "heuristic": 0.2},
            }
        ],
        [{"ts": "2026-10-25T13:00:00", "new_weight": 0.1}],
    )

    assert fig is not None
    assert {trace.name for trace in fig.data} == {"Rule", "ML", "Heuristic", "LLM"}


def test_sidebar_selection_switches_the_active_view(monkeypatch) -> None:
    monkeypatch.setattr(ui.api_client, "API_BASE_URL", "http://127.0.0.1:9")
    app = AppTest.from_file(Path(__file__).parents[1] / "ui" / "app.py", default_timeout=30).run()

    for section_index, (_, views) in enumerate(NAV_SECTIONS):
        for view in views:
            app.radio[section_index].set_value(view).run()

            assert not app.exception, f"{view}: {[error.message for error in app.exception]}"
            assert app.session_state["nav_view"] == view
            assert [radio.value for radio in app.radio] == [
                view if index == section_index else None for index in range(len(NAV_SECTIONS))
            ]
