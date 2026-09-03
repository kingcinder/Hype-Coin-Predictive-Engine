"""Tests for the persisted-vs-live score-distribution drift alarm.

``ops/score_drift.run_score_drift`` samples the most recent decision window
(what the GUI serves), re-runs the CURRENT formula over the same feature
vectors, and grades the divergence with a pure-numpy two-sample KS test plus a
distinct-value quantization ratio and mean per-token delta. These tests pin:

- the no-drift path (persisted == live formula output → ``ok`` health),
- the stale/quantized path (persisted collapsed to a handful of values while
  live is rich → ``red`` + deduped Alert + ntfy push, then push cooldown),
- the disabled / insufficient-sample skips,
- the ``latest_score_drift`` health-row parser the API reads,
- the pure-statistics building blocks (KS exactness on identical/disjoint
  samples, distinct-ratio semantics).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from common.config import Settings
from common.enums import AlertState
from common.time import utc_now
from ops.score_drift import (
    SCORE_DRIFT_COMPONENT,
    _distinct_ratio,
    _ks_2samp,
    latest_score_drift,
    recent_score_drift_runs,
    rescue_drift,
    run_score_drift,
)
from scoring.formulas import compute_scores
from storage import models
from storage.database import Base

DECISION_TS = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _settings(**overrides) -> Settings:
    base = dict(
        _env_file=None,
        score_drift_enabled=True,
        score_drift_sample_size=50,
        score_drift_min_samples=5,
        score_drift_ks_d_warn=0.20,
        score_drift_ks_d_red=0.35,
        score_drift_ks_p_red=0.01,
        score_drift_distinct_ratio_warn=0.35,
        score_drift_distinct_ratio_red=0.15,
        score_drift_mean_delta_warn=5.0,
        score_drift_mean_delta_red=15.0,
        score_drift_alert_cooldown_hours=24.0,
    )
    base.update(overrides)
    return Settings(**base)


def _oracle(session: Session, features: dict[str, float]) -> float:
    """What the live formula yields for a feature set (returns .risk)."""
    return compute_scores(features, [], session=session).risk


def _seed_fleet(
    session: Session,
    *,
    feature_sets: list[dict[str, float]],
    persisted_risk: list[float],
) -> None:
    """Seed N assets with the given feature sets + persisted risk values.

    Features are identical across decision windows by construction (one
    window), so ``run_score_drift`` compares every sampled row against the
    live oracle over the same vectors.
    """
    chain = models.Chain(slug="solana", name="Solana", vm_type="solana", native_symbol="SOL")
    session.add(chain)
    session.flush()
    for i, (features, risk) in enumerate(zip(feature_sets, persisted_risk, strict=True)):
        asset = models.Asset(
            chain_id=chain.id,
            symbol=f"T{i:02d}",
            address=f"addr_{i:02d}",
            first_seen_at=DECISION_TS,
        )
        session.add(asset)
        session.flush()
        session.add(
            models.Score(
                asset_id=asset.id,
                decision_ts=DECISION_TS,
                observed_at=DECISION_TS,
                model_version="test",
                risk=risk,
                exit_risk=0.0,
                hype=50.0,
                ethos=50.0,
                liquidity_access=50.0,
                manipulation=0.0,
                confidence=50.0,
                uncertainty=50.0,
                catalyst=0.0,
                research_priority=0.0,
                risk_band="YELLOW",
            )
        )
        for name, value in features.items():
            session.add(
                models.Feature(
                    asset_id=asset.id,
                    decision_ts=DECISION_TS,
                    observed_at=DECISION_TS,
                    feature_name=name,
                    feature_value=value,
                    missing_flag=False,
                )
            )
    session.commit()


def _varying_feature_sets(n: int) -> list[dict[str, float]]:
    """n low-liquidity feature sets that produce distinct live risks."""
    out = []
    for i in range(n):
        out.append(
            {
                "liquidity_depth": 5_000.0 + i * 500.0,
                "pair_age_minutes": 2.0 + i,
                "spread_estimate": 15.0 + i * 2,
                "buy_sell_ratio": 0.2 + i * 0.05,
                "volatility": 40.0 + i * 3,
                "top_holder_concentration": 0.7 - i * 0.02,
            }
        )
    return out


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    with SessionLocal() as db:
        yield db


# ---------------------------------------------------------------------------
# Pure statistics
# ---------------------------------------------------------------------------


class TestKsTwoSample:
    def test_identical_samples_exact(self) -> None:
        x = [50.0, 55.0, 60.0, 65.0, 70.0]
        d, p = _ks_2samp(x, list(x))
        assert d == 0.0
        assert p == 1.0

    def test_disjoint_samples_d_equals_one(self) -> None:
        # n=m=8: at this size the asymptotic two-sample p for D=1 converges
        # (~1.6e-4); small n would overstate it (n=m=3 gives ~0.033), so the
        # asymptotic approximation is only pinned here at a legitimate size.
        d, p = _ks_2samp(
            [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0],
            [90.0, 91.0, 92.0, 93.0, 94.0, 95.0, 96.0, 97.0],
        )
        assert d == 1.0
        assert p < 1e-3

    def test_offset_mild_shift(self) -> None:
        # Persisted quantized to 2 bands (5+5) vs live spread over 10 with a
        # clear gap: D must land between 0 and 1 and the p-value must be far
        # below 1. (A narrower overlap — e.g. 3 bands vs 8 interleaved — gives
        # D=0.375 with asymptotic p~0.52: honestly not significant at n=m=8,
        # so this shape is chosen to be detectably shifted.)
        persisted = [20.0, 20.0, 20.0, 20.0, 20.0, 80.0, 80.0, 80.0, 80.0, 80.0]
        live = [25.0, 25.0, 35.0, 45.0, 55.0, 65.0, 75.0, 85.0, 90.0, 95.0]
        d, p = _ks_2samp(persisted, live)
        assert 0.0 < d < 1.0
        assert p < 0.2

    def test_empty_side_is_trivial(self) -> None:
        assert _ks_2samp([], [1.0, 2.0]) == (0.0, 1.0)


class TestDistinctRatio:
    def test_equal_richness_is_one(self) -> None:
        assert _distinct_ratio([10.0, 20.0, 30.0], [10.0, 20.0, 30.5]) == 1.0

    def test_quantized_collapse_is_small(self) -> None:
        # Persisted 2 distinct values, live 8 -> ratio 0.25 (stored scores
        # collapsed into a handful of bands — the pre-rescore signature).
        persisted = [50.0, 50.0, 50.0, 60.0, 60.0, 60.0]
        live = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0]
        assert _distinct_ratio(persisted, live) == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# Probe states
# ---------------------------------------------------------------------------


class TestRunScoreDrift:
    def test_no_drift_when_persisted_matches_live(self, session: Session) -> None:
        """Persisted == live formula output -> ok health, no alert, no push."""
        feature_sets = _varying_feature_sets(6)
        persisted = [_oracle(session, features) for features in feature_sets]
        _seed_fleet(session, feature_sets=feature_sets, persisted_risk=persisted)

        result = run_score_drift(session, settings=_settings())

        assert result["status"] == "ok"
        assert result["compared"] == 6
        assert result["pushed"] is False
        row = session.scalar(
            select(models.SystemHealth).where(
                models.SystemHealth.component == SCORE_DRIFT_COMPONENT
            )
        )
        assert row is not None
        assert row.state == "ok"
        assert "no drift" in (row.message or "")
        assert session.scalars(select(models.Alert)).first() is None

    def test_stale_quantized_scores_trip_red(self, session: Session) -> None:
        """Persisted collapsed to 2 values while live is rich -> red + alert +
        ntfy push."""
        feature_sets = _varying_feature_sets(6)
        live_risks = [_oracle(session, features) for features in feature_sets]
        # Stale: everything persisted at two band values, wildly off the live
        # formula (the pre-rescore quantization symptom).
        persisted = [25.0, 25.0, 25.0, 80.0, 80.0, 80.0]
        assert all(abs(p - live) > 10 for p, live in zip(persisted, live_risks, strict=True))
        _seed_fleet(session, feature_sets=feature_sets, persisted_risk=persisted)

        result = run_score_drift(session, settings=_settings())

        assert result["status"] == "red"
        assert result["distinct_ratio"] < 0.5  # quantized persistence detected
        row = session.scalar(
            select(models.SystemHealth).where(
                models.SystemHealth.component == SCORE_DRIFT_COMPONENT
            )
        )
        assert row is not None
        assert row.state == "red"
        # Deduped alert opened exactly once.
        alerts = session.scalars(
            select(models.Alert).where(models.Alert.alert_type == SCORE_DRIFT_COMPONENT)
        ).all()
        assert len(alerts) == 1

    def test_red_push_cooldown(self, session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
        """After a red run, a second red run inside the cooldown must not
        push again — but the health row still refreshes."""
        feature_sets = _varying_feature_sets(6)
        persisted = [25.0, 25.0, 25.0, 80.0, 80.0, 80.0]
        _seed_fleet(session, feature_sets=feature_sets, persisted_risk=persisted)

        pushes = {"n": 0}
        monkeypatch.setattr(
            "ops.score_drift._notify",
            lambda *a, **k: pushes.__setitem__("n", pushes["n"] + 1) or True,
        )

        first = run_score_drift(session, settings=_settings())
        assert first["status"] == "red"
        assert first["pushed"] is True
        assert pushes["n"] == 1

        time.sleep(0.01)  # distinct health-row timestamps (unique component+ts)
        second = run_score_drift(session, settings=_settings())
        assert second["status"] == "red"
        assert second["pushed"] is False  # cooldown suppresses the page
        assert pushes["n"] == 1
        # Health row still refreshed on the second run.
        rows = session.scalars(
            select(models.SystemHealth).where(
                models.SystemHealth.component == SCORE_DRIFT_COMPONENT
            )
        ).all()
        assert len(rows) == 2
        assert {row.state for row in rows} == {"red"}

    def test_disabled_skips(self, session: Session) -> None:
        result = run_score_drift(session, settings=_settings(score_drift_enabled=False))
        assert result == {"skipped": True}

    def test_insufficient_samples_skips(self, session: Session) -> None:
        feature_sets = _varying_feature_sets(2)  # below min_samples=5
        _seed_fleet(session, feature_sets=feature_sets, persisted_risk=[10.0, 10.0])
        result = run_score_drift(session, settings=_settings())
        assert result.get("skipped") is True

    def test_error_records_red_health(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed probe records red health and returns {'error': ...} — it
        must never raise into the caller."""
        feature_sets = _varying_feature_sets(6)
        persisted = [_oracle(session, features) for features in feature_sets]
        _seed_fleet(session, feature_sets=feature_sets, persisted_risk=persisted)

        import ops.score_drift as sd

        def _boom(feat, missing, session=None):
            raise RuntimeError("formula exploded")

        monkeypatch.setattr(sd, "compute_scores", _boom)
        result = run_score_drift(session, settings=_settings())
        assert "error" in result
        row = session.scalar(
            select(models.SystemHealth).where(
                models.SystemHealth.component == SCORE_DRIFT_COMPONENT
            )
        )
        assert row is not None
        assert row.state == "red"


class TestLatestScoreDrift:
    def test_latest_parses_health_row(self, session: Session) -> None:
        assert latest_score_drift(session) is None  # nothing run yet
        feature_sets = _varying_feature_sets(6)
        persisted = [_oracle(session, features) for features in feature_sets]
        _seed_fleet(session, feature_sets=feature_sets, persisted_risk=persisted)
        run_score_drift(session, settings=_settings())
        session.commit()

        latest = latest_score_drift(session)
        assert latest is not None
        assert latest["state"] == "ok"
        assert latest["compared"] == 6
        assert latest["sampled"] == 6
        assert latest["ks_d"] == pytest.approx(0.0)
        assert latest["mean_abs_delta"] is not None


class TestNotifyScoreDrift:
    def test_notify_disabled_and_post(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ops.notifier import notify_score_drift

        disabled = Settings(_env_file=None, ntfy_enabled=False, ntfy_topic="t")
        assert (
            notify_score_drift(["KS D=0.60"], 0.6, 1e-6, 0.10, 40.0, 50, settings=disabled) is False
        )

        sent: dict = {}

        def fake_post(self, message, headers):
            sent.update({"message": message, "headers": headers})

        monkeypatch.setattr("ops.notifier.NtfyNotifier._post", fake_post)
        enabled = Settings(_env_file=None, ntfy_enabled=True, ntfy_topic="serpent-test")
        assert (
            notify_score_drift(["KS D=0.60"], 0.6, 1e-6, 0.10, 40.0, 50, settings=enabled) is True
        )
        assert "Score-distribution drift" in sent["message"]
        assert "rescore.py" in sent["message"]
        assert sent["headers"]["Title"] == "Serpent Circle - Score Drift Alarm"


# ---------------------------------------------------------------------------
# Trend series (score_drift_runs)
# ---------------------------------------------------------------------------


def _red_fleet(session: Session, *, n: int = 6) -> list[float]:
    """Seed the stale-quantized fleet used by the rescue tests.

    ``n`` assets default to the in-session settings (``min_samples=5``); CLI
    tests pass ``n=12`` because the real ``__main__`` runs under the production
    defaults where ``score_drift_min_samples=10``."""
    feature_sets = _varying_feature_sets(n)
    persisted = [25.0] * (n // 2) + [80.0] * (n - n // 2)
    _seed_fleet(session, feature_sets=feature_sets, persisted_risk=persisted)
    return [_oracle(session, features) for features in feature_sets]


class TestScoreDriftRuns:
    def test_probe_appends_trend_row(self, session: Session) -> None:
        """A comparable probe appends a score_drift_runs point with the full
        signal vector — the trend chart's building block."""
        feature_sets = _varying_feature_sets(6)
        persisted = [_oracle(session, features) for features in feature_sets]
        _seed_fleet(session, feature_sets=feature_sets, persisted_risk=persisted)

        result = run_score_drift(session, settings=_settings())
        assert result["status"] == "ok"

        runs = session.scalars(select(models.ScoreDriftRun)).all()
        assert len(runs) == 1
        row = runs[0]
        assert row.state == "ok"
        assert row.sampled == 6 and row.compared == 6
        assert row.ks_d == pytest.approx(0.0)
        assert row.ks_p == pytest.approx(1.0)
        assert row.distinct_ratio == pytest.approx(1.0)
        assert row.mean_abs_delta == pytest.approx(0.0)
        assert row.no_features == 0 and row.errors == 0

    def test_red_run_records_quantization(self, session: Session) -> None:
        """The quantization signature lands in the trend row for red probes."""
        _red_fleet(session)

        result = run_score_drift(session, settings=_settings())
        assert result["status"] == "red"

        row = session.scalar(select(models.ScoreDriftRun))
        assert row is not None
        assert row.state == "red"
        assert row.distinct_persisted == 2  # the stale band collapse
        assert row.distinct_live > 2
        assert row.distinct_ratio < 0.5

    def test_skips_do_not_append(self, session: Session) -> None:
        """Disabled / insufficient-sample probes leave the series untouched."""
        assert run_score_drift(session, settings=_settings(score_drift_enabled=False)) == {
            "skipped": True
        }
        feature_sets = _varying_feature_sets(2)
        _seed_fleet(session, feature_sets=feature_sets, persisted_risk=[10.0, 10.0])
        assert run_score_drift(session, settings=_settings()) == {"skipped": True}
        assert session.scalars(select(models.ScoreDriftRun)).first() is None

    def test_pruned_to_keep_window(self, session: Session) -> None:
        """The series is bounded: beyond ``score_drift_runs_keep`` the oldest
        rows are pruned on the probe itself."""
        feature_sets = _varying_feature_sets(6)
        persisted = [_oracle(session, features) for features in feature_sets]
        _seed_fleet(session, feature_sets=feature_sets, persisted_risk=persisted)
        settings = _settings(score_drift_runs_keep=3)
        for _ in range(5):
            run_score_drift(session, settings=settings)
            time.sleep(0.01)  # distinct health-row timestamps (unique component+ts)

        runs = session.scalars(select(models.ScoreDriftRun)).all()
        assert len(runs) == 3
        ids = sorted(run.id for run in runs)
        assert ids == list(range(ids[0], ids[0] + 3))  # the newest three survive

    def test_history_newest_first_and_limited(self, session: Session) -> None:
        """GET /score-drift/history backing query returns the series newest
        first, honoring the limit."""
        feature_sets = _varying_feature_sets(6)
        persisted = [_oracle(session, features) for features in feature_sets]
        _seed_fleet(session, feature_sets=feature_sets, persisted_risk=persisted)
        for _ in range(3):
            run_score_drift(session, settings=_settings())
            time.sleep(0.01)

        series = recent_score_drift_runs(session, limit=2)
        assert len(series) == 2
        assert [run.id for run in series] == sorted((run.id for run in series), reverse=True)
        assert recent_score_drift_runs(session, limit=10)[0].id == max(
            run.id for run in session.scalars(select(models.ScoreDriftRun))
        )


# ---------------------------------------------------------------------------
# Auto-apply rescue (--auto-apply)
# ---------------------------------------------------------------------------


class TestAutoApply:
    def _ack_alert(self, session: Session) -> int:
        alert = session.scalar(
            select(models.Alert).where(models.Alert.alert_type == SCORE_DRIFT_COMPONENT)
        )
        assert alert is not None
        alert.state = AlertState.ACKED.value
        alert.acked_at = utc_now()
        session.commit()
        return alert.id

    def test_requires_acked_alert(self, session: Session) -> None:
        """The rescue gate: no ack, no write — red drift stays untouched."""
        _red_fleet(session)
        run_score_drift(session, settings=_settings())  # opens the deduped alert

        result = rescue_drift(session, settings=_settings())
        assert result["applied"] is False
        assert "ack" in result["reason"]
        # Persisted risks are still the stale bands.
        risks = session.scalars(select(models.Score.risk)).all()
        assert sorted(risks) == [25.0, 25.0, 25.0, 80.0, 80.0, 80.0]

    def test_acked_rescue_runs_write_pass(self, session: Session) -> None:
        """Acked alert -> the real rescore write pass rewrites persisted risks
        to the live formula, closes the alert, and records the rescue."""
        live_risks = _red_fleet(session)
        run_score_drift(session, settings=_settings())
        self._ack_alert(session)

        result = rescue_drift(session, settings=_settings())
        assert result["applied"] is True
        assert result["rescore"]["updated"] == 6

        # Persisted distribution now matches the live formula.
        risks = sorted(session.scalars(select(models.Score.risk)).all())
        assert risks == pytest.approx(sorted(live_risks))
        # Alert is closed; resolution health row recorded (ok).
        alert = session.scalar(
            select(models.Alert).where(models.Alert.alert_type == SCORE_DRIFT_COMPONENT)
        )
        assert alert.state == AlertState.CLOSED.value
        assert "auto-applied" in alert.message
        latest = session.scalar(
            select(models.SystemHealth)
            .where(models.SystemHealth.component == SCORE_DRIFT_COMPONENT)
            .order_by(models.SystemHealth.ts.desc())
            .limit(1)
        )
        assert latest.state == "ok"
        assert "rescued" in (latest.message or "")
        time.sleep(0.01)  # distinct health-row timestamps (unique component+ts)
        # A fresh probe confirms the distributions now match.
        recheck = run_score_drift(session, settings=_settings())
        assert recheck["status"] == "ok"

    def test_partial_rescue_reopens_alert_and_pages(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A write pass that reports errors must never be marked resolved:
        the alert re-opens (ack cleared), a red health row records the
        failure count, a follow-up ntfy pages, and ``applied`` stays False."""
        _red_fleet(session)
        run_score_drift(session, settings=_settings())
        self._ack_alert(session)

        # The real rescore has no errors on the seeded fleet; inject a
        # write pass that partially fails so the partial path is driven.
        import scripts.rescore as rescore_mod

        def _flaky_rescore(*, session=None, dry_run=False) -> dict:
            assert not dry_run
            return {
                "updated": 4,
                "skipped": 0,
                "errors": 2,
                "distinct_risk_values": 0,
                "compare_rows": 6,
            }

        monkeypatch.setattr(rescore_mod, "rescore", _flaky_rescore)
        paged: dict = {}
        monkeypatch.setattr(
            "ops.score_drift._notify_partial_rescue",
            lambda **kw: paged.update(kw) or True,
        )

        result = rescue_drift(session, settings=_settings())
        assert result["applied"] is False
        assert result["partial"] is True
        assert result["errors"] == 2
        assert "re-opened" in result["reason"]

        # Alert re-opened: the ack is cleared, so a retry needs a fresh
        # sign-off — never closed, never silently resolved.
        alert = session.scalar(
            select(models.Alert).where(models.Alert.alert_type == SCORE_DRIFT_COMPONENT)
        )
        assert alert.state == AlertState.OPEN.value
        assert alert.acked_at is None
        assert "PARTIAL auto-apply" in alert.message
        assert "re-opened" in alert.message

        # Red health row (not the green proof-of-rescue) with the error count.
        latest = session.scalar(
            select(models.SystemHealth)
            .where(models.SystemHealth.component == SCORE_DRIFT_COMPONENT)
            .order_by(models.SystemHealth.ts.desc())
            .limit(1)
        )
        assert latest.state == "red"
        assert latest.error_count == 2
        assert "PARTIAL rescue" in (latest.message or "")

        # Follow-up page fired with the write-pass stats.
        assert paged.get("updated") == 4
        assert paged.get("errors") == 2
        assert paged.get("alert_id") == alert.id

    def test_dry_run_does_not_write(self, session: Session) -> None:
        """dry_run=True computes through rescore but writes nothing."""
        _red_fleet(session)
        run_score_drift(session, settings=_settings())
        self._ack_alert(session)

        result = rescue_drift(session, settings=_settings(), dry_run=True)
        assert result["applied"] is False
        assert result["dry_run"] is True
        assert result["rescore"]["updated"] == 6  # computed the migration
        # Nothing persisted: alert still acked (not closed), risks untouched.
        alert = session.scalar(
            select(models.Alert).where(models.Alert.alert_type == SCORE_DRIFT_COMPONENT)
        )
        assert alert.state == AlertState.ACKED.value
        risks = sorted(session.scalars(select(models.Score.risk)).all())
        assert risks == [25.0, 25.0, 25.0, 80.0, 80.0, 80.0]

    def test_disabled_refuses(self, session: Session) -> None:
        _red_fleet(session)
        run_score_drift(session, settings=_settings())
        result = rescue_drift(session, settings=_settings(score_drift_enabled=False))
        assert result["applied"] is False
        assert "disabled" in result["reason"]


class TestAutoApplyCli:
    """The real ``__main__`` wiring, driven against a patched SessionLocal:
    --auto-apply only fires on a red probe and only when the alert is acked
    (exit 2 otherwise)."""

    def _run_cli(self, engine, monkeypatch: pytest.MonkeyPatch, extra: list[str]) -> None:
        import ops.score_drift as sd

        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
        monkeypatch.setattr(
            "storage.database.SessionLocal",
            SessionLocal,  # __main__ binds this
        )
        # The probe would page ntfy under the real settings; silence the push
        # in the test (the push path itself is pinned elsewhere).
        monkeypatch.setattr("ops.score_drift._notify", lambda *a, **k: False)
        monkeypatch.setattr("sys.argv", ["ops.score_drift", "--once", *extra])
        sd.main()

    def test_cli_requires_ack_exits_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 12 assets: the real __main__ runs under production defaults where
        # score_drift_min_samples=10, so 6 would skip the probe entirely.
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
        with SessionLocal() as session:
            _red_fleet(session, n=12)

        with pytest.raises(SystemExit) as exc:
            self._run_cli(engine, monkeypatch, ["--auto-apply"])
        assert exc.value.code == 2

    def test_cli_acked_auto_apply_rewrites(self, monkeypatch: pytest.MonkeyPatch) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
        live_risks: list[float] = []
        with SessionLocal() as session:
            live_risks = _red_fleet(session, n=12)
            run_score_drift(session, settings=_settings())
            alert = session.scalar(
                select(models.Alert).where(models.Alert.alert_type == SCORE_DRIFT_COMPONENT)
            )
            assert alert is not None
            alert.state = AlertState.ACKED.value
            alert.acked_at = utc_now()
            session.commit()

        self._run_cli(engine, monkeypatch, ["--auto-apply"])
        # The real CLI's probe + rescue rewrote the persisted distribution.
        with SessionLocal() as session:
            risks = sorted(session.scalars(select(models.Score.risk)).all())
            assert risks == pytest.approx(sorted(live_risks))
            alert = session.scalar(
                select(models.Alert).where(models.Alert.alert_type == SCORE_DRIFT_COMPONENT)
            )
            assert alert.state == AlertState.CLOSED.value

    def test_cli_partial_rescue_exits_3_and_reopens_alert(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A write pass that reports errors exits 3 (distinct from the exit-2
        gate), re-opens the alert, and never claims the fleet is fixed."""
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
        with SessionLocal() as session:
            _red_fleet(session, n=12)
            run_score_drift(session, settings=_settings())
            alert = session.scalar(
                select(models.Alert).where(models.Alert.alert_type == SCORE_DRIFT_COMPONENT)
            )
            assert alert is not None
            alert.state = AlertState.ACKED.value
            alert.acked_at = utc_now()
            session.commit()

        import scripts.rescore as rescore_mod

        def _flaky_rescore(*, session=None, dry_run=False) -> dict:
            return {
                "updated": 8,
                "skipped": 0,
                "errors": 4,
                "distinct_risk_values": 0,
                "compare_rows": 12,
            }

        monkeypatch.setattr(rescore_mod, "rescore", _flaky_rescore)
        monkeypatch.setattr("ops.score_drift._notify_partial_rescue", lambda **kw: True)

        with pytest.raises(SystemExit) as exc:
            self._run_cli(engine, monkeypatch, ["--auto-apply"])
        assert exc.value.code == 3

        with SessionLocal() as session:
            alert = session.scalar(
                select(models.Alert).where(models.Alert.alert_type == SCORE_DRIFT_COMPONENT)
            )
            assert alert.state == AlertState.OPEN.value
            assert alert.acked_at is None
            assert "PARTIAL auto-apply" in alert.message


class TestHistoryCli:
    """The real ``__main__`` ``--history`` path: prints the recorded trend
    series (newest first) without running a probe — the surface behind
    ``make score-drift-history``."""

    def _run_history_cli(
        self, engine, monkeypatch: pytest.MonkeyPatch, capsys, extra: list[str] | None = None
    ) -> str:
        import ops.score_drift as sd

        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
        monkeypatch.setattr("storage.database.SessionLocal", SessionLocal)
        monkeypatch.setattr("sys.argv", ["ops.score_drift", "--history", *(extra or [])])
        sd.main()
        return capsys.readouterr().out

    def test_history_prints_recorded_runs_newest_first(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
        with SessionLocal() as session:
            _red_fleet(session, n=6)  # settings override min_samples=5, so 6 is enough
            run_score_drift(session, settings=_settings(score_drift_min_samples=5))
            run_score_drift(session, settings=_settings(score_drift_min_samples=5))
            session.commit()  # run_score_drift does not commit; persist the rows

            # Expected KS D is whatever the fixture's probe actually recorded —
            # never a hardcoded field-run number.
            runs = session.scalars(select(models.ScoreDriftRun)).all()
            assert len(runs) == 2
            expected_ks_d = runs[0].ks_d

        out = self._run_history_cli(engine, monkeypatch, capsys)
        # The probes emit structured JSON log lines to stdout; filter them out
        # so only the plain-text history table remains.
        lines = [ln for ln in out.splitlines() if ln.strip() and not ln.strip().startswith("{")]
        # Header row + two trend rows; newest first (second probe first).
        assert len(lines) == 3
        assert lines[0].startswith("run_ts")
        assert f"{expected_ks_d:>8.3f}" in lines[1]  # formatted KS D of newest row
        # Column order: run_ts (contains a space) then state, so state is index 2.
        assert lines[1].split()[2] == "red"
        # Second row is the older probe (same state for the red fleet).
        assert "red" in lines[2]

    def test_history_empty_reports_no_runs(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        out = self._run_history_cli(engine, monkeypatch, capsys)
        assert "no score_drift_runs recorded yet" in out

    def test_history_limit_caps_rows(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
        with SessionLocal() as session:
            _red_fleet(session, n=12)
            run_score_drift(session, settings=_settings(score_drift_min_samples=5))
            run_score_drift(session, settings=_settings(score_drift_min_samples=5))
            run_score_drift(session, settings=_settings(score_drift_min_samples=5))
            session.commit()

        out = self._run_history_cli(engine, monkeypatch, capsys, extra=["--limit", "2"])
        lines = [ln for ln in out.splitlines() if ln.strip() and not ln.strip().startswith("{")]
        assert len(lines) == 3  # header + 2 capped rows
        assert "--limit 2" not in out  # sanity: limit honored, not echoed
