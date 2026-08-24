from __future__ import annotations

import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from common.config import Settings, get_settings
from common.logging import get_logger
from common.time import ensure_utc, utc_now
from features.definitions import FEATURE_NAMES
from forecast.hazard import DiscreteHazardModel, HazardFit
from forecast.labels import LABEL_COLLAPSE, LABEL_IGNITION, LabelEngine
from ingestion.rpc_pool import get_rpc_pool
from storage import models
from storage.repository import record_health, upsert_forecast

log = get_logger(__name__)

# The forecast model trains on the full persisted point-in-time feature set.
# This includes the narrative dev-activity proxies that feed the hype and
# catalyst scores: kol_velocity (distinct KOL channels mentioning the token in
# the trailing 24h), github_star_velocity, and hf_download_velocity (per-day
# star/download growth from the raw-evidence crawl history). It also includes
# rpc_pool_health, the effective live health of the asset's chain RPC pool, so
# degraded data access pulls probabilities toward an honest 50/50 baseline.
# lifecycle_phase (0=seeding..4=collapse, 5=terminal) — the asset's position in
# the hype-lifecycle state machine at the label's decision time — which the
# survival layer conditions on so hazards are phase-dependent. Missing features
# at a label's decision time enter the matrix as 0.0 — never fabricated.
FORECAST_FEATURE_NAMES: tuple[str, ...] = FEATURE_NAMES
VELOCITY_FEATURE_NAMES: tuple[str, ...] = (
    "kol_velocity",
    "github_star_velocity",
    "hf_download_velocity",
)

# Numeric lifecycle_phase feature value -> phase slug for hazard conditioning.
# Rank 5 covers the terminal exits (DEAD / RUGGED / SURVIVOR), which share the
# same rank in the feature factory; a missing value means the asset had no
# lifecycle events yet, i.e. SEEDING by the state machine's own definition.
_PHASE_SLUGS: dict[int, str] = {
    0: "seeding",
    1: "ignition",
    2: "parabolic",
    3: "saturation",
    4: "collapse",
}


def _feature_default(name: str) -> float:
    """Use a neutral default for live data-layer health when history is absent."""
    return 1.0 if name == "rpc_pool_health" else 0.0


def _widen_probability(probability: float, rpc_pool_health: float) -> float:
    """Shrink a forecast toward 0.5 as the chain data layer degrades."""
    health = max(0.0, min(1.0, float(rpc_pool_health)))
    return 0.5 + (float(probability) - 0.5) * health


def _phase_slug(phase_value: float | None) -> str:
    if phase_value is None:
        return "seeding"
    rank = int(round(phase_value))
    return _PHASE_SLUGS.get(rank, "terminal")


def _git_sha() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:  # noqa: BLE001 - not a git checkout; None is honest.
        return None


def _latest_training_run(session: Session, settings: Settings) -> models.BacktestRun | None:
    """Return the latest completed run owned by the forecast trainer."""
    return session.scalar(
        select(models.BacktestRun)
        .where(
            models.BacktestRun.model_version == settings.forecast_model_version,
            models.BacktestRun.status == "completed",
        )
        .order_by(models.BacktestRun.started_at.desc())
        .limit(1)
    )


def forecast_due(
    session: Session,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> bool:
    """Return whether the forecast model should be retrained now.

    The first training pass is due immediately. Subsequent passes use the
    persisted completed training run rather than process-local state, so the
    APScheduler process and a separate worker agree on the cadence.
    """
    settings = settings or get_settings()
    if not settings.forecast_enabled:
        return False
    now = ensure_utc(now or utc_now())
    latest = _latest_training_run(session, settings)
    if latest is None:
        return True
    return now - ensure_utc(latest.started_at) >= timedelta(
        hours=settings.forecast_train_frequency_hours
    )


def run_forecast_if_due(
    session: Session,
    *,
    decision_ts: datetime | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Train once when the persisted forecast cadence has elapsed."""
    settings = settings or get_settings()
    if not forecast_due(session, settings=settings):
        return {"status": "skipped", "reason": "not_due"}
    result = ForecastEngine().run(session, decision_ts=decision_ts)
    result["scheduled"] = True
    return result


def maybe_run_forecast() -> dict[str, Any]:
    """Cadence-gated forecast pass for worker loops and APScheduler."""
    from storage.database import SessionLocal

    with SessionLocal() as session:
        if not forecast_due(session):
            return {"status": "skipped", "reason": "not_due"}
        try:
            result = ForecastEngine().run(session)
            session.commit()
            result["scheduled"] = True
            return result
        except Exception as exc:  # noqa: BLE001 - scheduled jobs must not kill the worker.
            session.rollback()
            with session.begin():
                record_health(
                    session,
                    component="forecast",
                    state="red",
                    message=str(exc),
                    error_count=1,
                )
            log.exception("forecast_training_failed", error=str(exc))
            return {"status": "error", "error": str(exc)}


@dataclass(frozen=True)
class Sample:
    asset_id: int
    ts: datetime
    features: dict[str, float]
    y_ignition: int
    y_collapse: int


@dataclass(frozen=True)
class ForecastModel:
    ignition: HistGradientBoostingClassifier | None
    collapse: HistGradientBoostingClassifier | None
    ignition_calibrator: IsotonicRegression | None
    collapse_calibrator: IsotonicRegression | None
    hazard: HazardFit
    peak_hazard: HazardFit
    # Phase-conditioned survival fits: time-to-collapse / time-to-peak learned
    # separately for samples observed in each lifecycle phase, so an asset in
    # COLLAPSE gets a fast-collapse curve while a SEEDING asset gets the
    # long-tailed one. Predict falls back to the global fits for phases with no
    # training data.
    hazards_by_phase: dict[str, HazardFit]
    peak_hazards_by_phase: dict[str, HazardFit]
    calibrated: bool


class ForecastEngine:
    """Phase-3 prediction layer.

    Labels outcomes from market history, trains gradient-boosting classifiers on
    persisted point-in-time features with a purged time split, calibrates
    probabilities with isotonic regression, fits a hand-built discrete hazard
    model, and writes per-asset Forecast rows. Degrades honestly: with too few
    labeled samples it trains nothing and says so.
    """

    def __init__(self) -> None:
        self.settings = get_settings()

    def run(self, session: Session, *, decision_ts: datetime | None = None) -> dict[str, Any]:
        decision_ts = ensure_utc(decision_ts or utc_now())
        if not self.settings.forecast_enabled:
            return {"status": "disabled"}
        label_counts = LabelEngine().generate(session, decision_ts=decision_ts)
        # Also generate dense labels from interpolated market snapshots to
        # accelerate training data accumulation (unblocks the ML model faster).
        try:
            from data_lake.labels import generate_dense_labels
            dense_counts = generate_dense_labels(session, decision_ts=decision_ts)
            label_counts["dense_ignition"] = dense_counts.get("ignition", 0)
            label_counts["dense_collapse"] = dense_counts.get("collapse", 0)
            label_counts["total_decision_points"] = dense_counts.get("total_decision_points", 0)
        except Exception:  # noqa: BLE001 - dense labels are additive, never block training.
            pass
        samples = self._collect_samples(session, decision_ts)
        if len(samples) < self.settings.forecast_min_samples:
            record_health(
                session,
                component="forecast",
                state="yellow",
                message=(
                    f"{len(samples)} labeled samples < min "
                    f"{self.settings.forecast_min_samples}; model not trained"
                ),
            )
            return {
                "status": "insufficient_data",
                "samples": len(samples),
                "labels": label_counts,
            }
        model = self._train(session, samples, decision_ts)
        if model is None:
            record_health(
                session,
                component="forecast",
                state="yellow",
                message="train/test split too small; model not trained",
            )
            return {"status": "insufficient_data", "samples": len(samples)}
        predicted = self._predict(session, model, decision_ts)
        record_health(
            session,
            component="forecast",
            state="ok",
            message=f"{len(predicted)} forecasts, {len(samples)} labeled samples",
        )
        return {
            "status": "ok",
            "samples": len(samples),
            "forecasts": len(predicted),
            "labels": label_counts,
        }

    # ------------------------------------------------------------------ dataset

    def _collect_samples(self, session: Session, decision_ts: datetime) -> list[Sample]:
        forward = timedelta(hours=self.settings.forecast_forward_hours)
        targets: dict[tuple[int, datetime], dict[str, int]] = defaultdict(dict)
        for label in session.scalars(
            select(models.Label).where(models.Label.observed_at <= decision_ts)
        ).all():
            if label.label_type not in (LABEL_IGNITION, LABEL_COLLAPSE):
                continue
            targets[(label.asset_id, ensure_utc(label.ts))][label.label_type] = (
                1 if label.label_value == "1" else 0
            )
        samples: list[Sample] = []
        for (asset_id, ts), values in targets.items():
            if ts + forward > decision_ts:
                continue
            if LABEL_IGNITION not in values or LABEL_COLLAPSE not in values:
                continue
            features = self._features_at(session, asset_id, ts)
            if not features:
                continue
            samples.append(
                Sample(
                    asset_id=asset_id,
                    ts=ts,
                    features=features,
                    y_ignition=values[LABEL_IGNITION],
                    y_collapse=values[LABEL_COLLAPSE],
                )
            )
        return samples

    def _features_at(
        self, session: Session, asset_id: int, ts: datetime
    ) -> dict[str, float]:
        rows = session.scalars(
            select(models.Feature).where(
                models.Feature.asset_id == asset_id,
                models.Feature.decision_ts == ts,
                models.Feature.missing_flag.is_(False),
            )
        ).all()
        return {row.feature_name: float(row.feature_value) for row in rows}

    # ------------------------------------------------------------------- train

    def _training_split(self, samples: list[Sample]) -> tuple[list[Sample], list[Sample]] | None:
        samples = sorted(samples, key=lambda sample: sample.ts)
        split = max(1, int(len(samples) * 0.7))
        train, test = samples[:split], samples[split:]
        if len(train) < 5 or not test:
            return None
        forward = timedelta(hours=self.settings.forecast_forward_hours)
        purged = [sample for sample in test if sample.ts >= train[-1].ts + forward]
        return train, purged or test

    def _train(
        self, session: Session, samples: list[Sample], decision_ts: datetime
    ) -> ForecastModel | None:
        split = self._training_split(samples)
        if split is None:
            return None
        train, test = split

        train_x = self._matrix(train)
        test_x = self._matrix(test)
        train_ignition = np.array([sample.y_ignition for sample in train])
        train_collapse = np.array([sample.y_collapse for sample in train])
        test_ignition = np.array([sample.y_ignition for sample in test])
        test_collapse = np.array([sample.y_collapse for sample in test])

        ignition_model = self._fit_classifier(train_x, train_ignition)
        collapse_model = self._fit_classifier(train_x, train_collapse)
        if ignition_model is None or collapse_model is None:
            return None

        ignition_probs = ignition_model.predict_proba(test_x)[:, 1]
        collapse_probs = collapse_model.predict_proba(test_x)[:, 1]
        ignition_calibrator, calibrated_ignition = self._calibrate(
            ignition_probs, test_ignition
        )
        collapse_calibrator, calibrated_collapse = self._calibrate(
            collapse_probs, test_collapse
        )

        hazard = self._fit_hazard(session, samples)
        peak_hazard = self._fit_peak_hazard(session, samples)
        hazards_by_phase = self._fit_phase_hazards(session, samples)
        peak_hazards_by_phase = self._fit_phase_peak_hazards(session, samples)
        metrics = {
            "precision_at_10": self._precision_at_k(calibrated_collapse, test_collapse, 10),
            "calibration_error": self._calibration_error(calibrated_collapse, test_collapse),
            "collapse_rate_test": float(test_collapse.mean()) if len(test_collapse) else 0.0,
            "median_lead_time_hours": self._median_lead_time(
                session, samples=test, probs=calibrated_collapse, labels=test_collapse
            ),
            "samples": float(len(samples)),
            "test_samples": float(len(test)),
            "hazard.phases_fit": float(len(hazards_by_phase)),
        }
        # Persist the phase-conditioned survival curves so the Backtest & Drift
        # UI can show how expected time-to-collapse / time-to-peak differs by
        # lifecycle phase.
        for slug, fit in sorted(hazards_by_phase.items()):
            if fit.mean_time_to_collapse is not None:
                metrics[f"hazard.{slug}.mean_hours_to_collapse"] = fit.mean_time_to_collapse
        for slug, fit in sorted(peak_hazards_by_phase.items()):
            if fit.mean_time_to_collapse is not None:
                metrics[f"hazard.{slug}.mean_hours_to_peak"] = fit.mean_time_to_collapse
        drift = self._assess_drift(
            session,
            samples=test,
            probs=calibrated_collapse,
            labels=test_collapse,
            decision_ts=decision_ts,
        )
        for key, value in drift["measures"].items():
            if isinstance(value, (int, float)):
                metrics[f"drift.{key}"] = float(value)
        metrics["drift.status"] = {
            "insufficient_trailing": -1.0,
            "ok": 0.0,
            "drift": 1.0,
            "severe_drift": 2.0,
        }[drift["status"]]
        self._persist_metrics(session, samples=samples, decision_ts=decision_ts, metrics=metrics)
        return ForecastModel(
            ignition=ignition_model,
            collapse=collapse_model,
            ignition_calibrator=ignition_calibrator,
            collapse_calibrator=collapse_calibrator,
            hazard=hazard,
            peak_hazard=peak_hazard,
            hazards_by_phase=hazards_by_phase,
            peak_hazards_by_phase=peak_hazards_by_phase,
            calibrated=True,
        )

    def _evaluate_ab_variant(
        self,
        session: Session,
        *,
        train: list[Sample],
        test: list[Sample],
        masked_features: set[str] | frozenset[str],
    ) -> dict[str, float] | None:
        """Fit one A/B classifier variant on an identical purged split."""
        train_x = self._matrix(train, masked_features=masked_features)
        test_x = self._matrix(test, masked_features=masked_features)
        train_labels = np.array([sample.y_collapse for sample in train])
        test_labels = np.array([sample.y_collapse for sample in test])
        ignition_classifier = self._fit_classifier(
            train_x, np.array([sample.y_ignition for sample in train])
        )
        classifier = self._fit_classifier(train_x, train_labels)
        if ignition_classifier is None or classifier is None:
            return None
        probabilities = classifier.predict_proba(test_x)[:, 1]
        _, calibrated = self._calibrate(probabilities, test_labels)
        return {
            "precision_at_10": self._precision_at_k(
                calibrated, test_labels, min(10, len(test_labels))
            ),
            "calibration_error": self._calibration_error(calibrated, test_labels),
            "median_lead_time_hours": self._median_lead_time(
                session, samples=test, probs=calibrated, labels=test_labels
            ),
        }

    def _persist_velocity_ab_metrics(
        self,
        session: Session,
        *,
        samples: list[Sample],
        decision_ts: datetime,
        variants: dict[str, dict[str, float]],
        delta: dict[str, float],
    ) -> int:
        model_version = f"{self.settings.forecast_model_version}-velocity-ab"
        run = models.BacktestRun(
            cutoff_start=min(sample.ts for sample in samples),
            cutoff_end=decision_ts,
            config_json={
                "experiment": "velocity_features_ab",
                "masked_features": list(VELOCITY_FEATURE_NAMES),
                "forward_hours": self.settings.forecast_forward_hours,
            },
            git_sha=_git_sha(),
            model_version=model_version,
            status="completed",
        )
        session.add(run)
        session.flush()
        for variant, metrics in variants.items():
            for name, value in metrics.items():
                session.add(
                    models.BacktestResult(
                        run_id=run.id,
                        metric_name=f"forecast_ab.{variant}.{name}",
                        metric_value=float(value),
                        chain_slug=None,
                        details_json={},
                    )
                )
        for name, value in delta.items():
            session.add(
                models.BacktestResult(
                    run_id=run.id,
                    metric_name=f"forecast_ab.delta.{name}",
                    metric_value=float(value),
                    chain_slug=None,
                    details_json={"direction": "velocity_masked_minus_full"},
                )
            )
        session.flush()
        return run.id

    def run_velocity_ab_experiment(
        self, session: Session, *, decision_ts: datetime | None = None
    ) -> dict[str, Any]:
        """Compare full versus velocity-masked training on one labeled corpus.

        Both variants use the same point-in-time samples, chronological split,
        purge window, classifier settings, calibration procedure, and metrics.
        The experiment never changes production Forecast rows; it persists an
        auditable ``forecast_ab`` BacktestRun and metric set instead.
        """
        decision_ts = ensure_utc(decision_ts or utc_now())
        if not self.settings.forecast_enabled:
            return {"status": "disabled"}
        label_counts = LabelEngine().generate(session, decision_ts=decision_ts)
        samples = self._collect_samples(session, decision_ts)
        if len(samples) < self.settings.forecast_min_samples:
            return {
                "status": "insufficient_data",
                "samples": len(samples),
                "labels": label_counts,
            }
        split = self._training_split(samples)
        if split is None:
            return {"status": "insufficient_data", "samples": len(samples)}
        train, test = split
        variants = {
            "full": self._evaluate_ab_variant(
                session, train=train, test=test, masked_features=frozenset()
            ),
            "velocity_masked": self._evaluate_ab_variant(
                session,
                train=train,
                test=test,
                masked_features=frozenset(VELOCITY_FEATURE_NAMES),
            ),
        }
        if variants["full"] is None or variants["velocity_masked"] is None:
            return {
                "status": "insufficient_data",
                "samples": len(samples),
                "test_samples": len(test),
            }
        full = variants["full"]
        masked = variants["velocity_masked"]
        delta = {name: masked[name] - full[name] for name in full}
        run_id = self._persist_velocity_ab_metrics(
            session,
            samples=samples,
            decision_ts=decision_ts,
            variants={"full": full, "velocity_masked": masked},
            delta=delta,
        )
        return {
            "status": "ok",
            "run_id": run_id,
            "samples": len(samples),
            "train_samples": len(train),
            "test_samples": len(test),
            "masked_features": list(VELOCITY_FEATURE_NAMES),
            "full": full,
            "velocity_masked": masked,
            "delta": delta,
        }

    @staticmethod
    def _fit_classifier(
        x: np.ndarray, y: np.ndarray
    ) -> HistGradientBoostingClassifier | None:
        if len(np.unique(y)) < 2:
            return None
        model = HistGradientBoostingClassifier(
            max_iter=120, learning_rate=0.08, max_depth=3, random_state=42
        )
        model.fit(x, y)
        return model

    @staticmethod
    def _calibrate(
        probs: np.ndarray, labels: np.ndarray
    ) -> tuple[IsotonicRegression, np.ndarray]:
        calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        calibrator.fit(probs, labels)
        return calibrator, calibrator.predict(probs)

    def _matrix(
        self,
        samples: list[Sample],
        *,
        masked_features: set[str] | frozenset[str] = frozenset(),
    ) -> np.ndarray:
        return np.array(
            [
                [
                    _feature_default(name)
                    if name in masked_features
                    else sample.features.get(name, _feature_default(name))
                    for name in FORECAST_FEATURE_NAMES
                ]
                for sample in samples
            ],
            dtype=float,
        )

    def _fit_hazard(self, session: Session, samples: list[Sample]) -> HazardFit:
        forward_hours = float(self.settings.forecast_forward_hours)
        times: list[float] = []
        events: list[bool] = []
        for sample in samples:
            events.append(sample.y_collapse == 1)
            if sample.y_collapse == 1:
                trough_hours = self._trough_hours(session, sample.asset_id, sample.ts)
                times.append(trough_hours if trough_hours is not None else forward_hours)
            else:
                times.append(forward_hours)
        return DiscreteHazardModel(self.settings.forecast_forward_hours).fit(times, events)

    def _fit_phase_hazards(
        self, session: Session, samples: list[Sample]
    ) -> dict[str, HazardFit]:
        """One time-to-collapse survival fit per lifecycle phase.

        Samples are bucketed by the asset's lifecycle phase at the label's
        decision time (``lifecycle_phase`` feature; missing = SEEDING), so each
        phase learns its own empirical hazard curve: a token already in COLLAPSE
        decays on a fast curve, a SEEDING token on the long tail.
        """
        buckets: dict[str, list[Sample]] = defaultdict(list)
        for sample in samples:
            buckets[_phase_slug(sample.features.get("lifecycle_phase"))].append(sample)
        return {slug: self._fit_hazard(session, bucket) for slug, bucket in buckets.items()}

    def _fit_phase_peak_hazards(
        self, session: Session, samples: list[Sample]
    ) -> dict[str, HazardFit]:
        """Same phase conditioning for the time-to-peak survival fit."""
        buckets: dict[str, list[Sample]] = defaultdict(list)
        for sample in samples:
            buckets[_phase_slug(sample.features.get("lifecycle_phase"))].append(sample)
        return {
            slug: self._fit_peak_hazard(session, bucket) for slug, bucket in buckets.items()
        }

    def _peak_hours(self, session: Session, asset_id: int, ts: datetime) -> float | None:
        forward = timedelta(hours=self.settings.forecast_forward_hours)
        from backtest.runner import point_in_time_market_rows

        rows = [
            row
            for row in point_in_time_market_rows(
                session, asset_id=asset_id, decision_ts=ts + forward
            )
            if row.price_usd is not None
            and ensure_utc(row.ts) > ts
            and ensure_utc(row.ts) <= ts + forward
        ]
        if not rows:
            return None
        peak = max(rows, key=lambda row: float(row.price_usd or 0.0))
        return max(0.0, (ensure_utc(peak.ts) - ts).total_seconds() / 3600.0)

    def _fit_peak_hazard(self, session: Session, samples: list[Sample]) -> HazardFit:
        """Time-to-peak survival fit: when does the pump peak inside the window."""
        forward_hours = float(self.settings.forecast_forward_hours)
        times: list[float] = []
        events: list[bool] = []
        for sample in samples:
            events.append(sample.y_ignition == 1)
            if sample.y_ignition == 1:
                peak_hours = self._peak_hours(session, sample.asset_id, sample.ts)
                times.append(peak_hours if peak_hours is not None else forward_hours)
            else:
                times.append(forward_hours)
        return DiscreteHazardModel(self.settings.forecast_forward_hours).fit(times, events)

    def _trough_hours(
        self, session: Session, asset_id: int, ts: datetime
    ) -> float | None:
        forward = timedelta(hours=self.settings.forecast_forward_hours)
        from backtest.runner import point_in_time_market_rows

        rows = [
            row
            for row in point_in_time_market_rows(
                session, asset_id=asset_id, decision_ts=ts + forward
            )
            if row.price_usd is not None
            and ensure_utc(row.ts) > ts
            and ensure_utc(row.ts) <= ts + forward
        ]
        if not rows:
            return None
        trough = min(rows, key=lambda row: float(row.price_usd or 0.0))
        return max(0.0, (ensure_utc(trough.ts) - ts).total_seconds() / 3600.0)

    @staticmethod
    def _precision_at_k(probs: np.ndarray, labels: np.ndarray, k: int) -> float:
        if len(labels) == 0:
            return 0.0
        order = np.argsort(probs)[::-1][: min(k, len(probs))]
        return float(labels[order].mean()) if len(order) else 0.0

    @staticmethod
    def _calibration_error(probs: np.ndarray, labels: np.ndarray) -> float:
        if len(labels) == 0:
            return 1.0
        buckets = np.linspace(0.0, 1.0, 11)
        errors: list[float] = []
        for index in range(len(buckets) - 1):
            mask = (probs >= buckets[index]) & (probs < buckets[index + 1])
            if not mask.any():
                continue
            mean_pred = float(probs[mask].mean())
            fraction = float(labels[mask].mean())
            errors.append(abs(mean_pred - fraction))
        return float(np.mean(errors)) if errors else 1.0

    def _median_lead_time(
        self,
        session: Session,
        *,
        samples: list[Sample],
        probs: np.ndarray,
        labels: np.ndarray,
    ) -> float:
        lead_times: list[float] = []
        for sample, prob, label in zip(samples, probs, labels, strict=True):
            if prob >= 0.5 and label == 1:
                trough = self._trough_hours(session, sample.asset_id, sample.ts)
                if trough is not None:
                    lead_times.append(trough)
        if not lead_times:
            return 0.0
        return float(np.median(lead_times))

    # ------------------------------------------------------------------- drift

    def _assess_drift(
        self,
        session: Session,
        *,
        samples: list[Sample],
        probs: np.ndarray,
        labels: np.ndarray,
        decision_ts: datetime,
    ) -> dict[str, Any]:
        """Compare trailing-window performance against the baseline and history.

        The market's hype mechanics shift when the model's recent test performance
        degrades relative to (a) older test samples in the same run and (b) the
        persisted historical metrics. Emits a ``forecast_drift`` health warning
        (yellow for drift, red when trailing precision collapses).
        """
        cutoff = decision_ts - timedelta(hours=self.settings.forecast_drift_trailing_hours)
        trailing_idx = [index for index, sample in enumerate(samples) if sample.ts >= cutoff]
        baseline_idx = [index for index, sample in enumerate(samples) if sample.ts < cutoff]
        measures: dict[str, Any] = {}
        if len(trailing_idx) < self.settings.forecast_drift_min_samples:
            self._record_drift_health(session, "insufficient_trailing", measures, decision_ts)
            return {"status": "insufficient_trailing", "measures": measures}

        trailing = np.array(trailing_idx)
        trailing_precision = self._precision_at_k(
            probs[trailing], labels[trailing], min(10, len(trailing_idx))
        )
        trailing_cal = self._calibration_error(probs[trailing], labels[trailing])
        measures["trailing_samples"] = float(len(trailing_idx))
        measures["trailing_precision_at_10"] = round(trailing_precision, 4)
        measures["trailing_calibration_error"] = round(trailing_cal, 4)

        baseline_precision: float | None = None
        baseline_cal: float | None = None
        if baseline_idx:
            baseline = np.array(baseline_idx)
            baseline_precision = self._precision_at_k(
                probs[baseline], labels[baseline], min(10, len(baseline_idx))
            )
            baseline_cal = self._calibration_error(probs[baseline], labels[baseline])
            measures["baseline_samples"] = float(len(baseline_idx))
            measures["baseline_precision_at_10"] = round(baseline_precision, 4)
            measures["baseline_calibration_error"] = round(baseline_cal, 4)

        history_precision = self._latest_metric(session, "forecast.precision_at_10")
        history_cal = self._latest_metric(session, "forecast.calibration_error")
        measures["historical_precision_at_10"] = (
            round(history_precision, 4) if history_precision is not None else None
        )
        measures["historical_calibration_error"] = (
            round(history_cal, 4) if history_cal is not None else None
        )

        reasons: list[str] = []
        if baseline_precision is not None and (
            trailing_precision
            < baseline_precision - self.settings.forecast_drift_precision_margin
            and trailing_precision < self.settings.forecast_drift_min_precision
        ):
            reasons.append("trailing precision collapsed vs baseline")
        if (
            history_precision is not None
            and trailing_precision
            < history_precision * self.settings.forecast_drift_precision_fraction
        ):
            reasons.append("trailing precision below historical performance")
        if baseline_cal is not None and (
            trailing_cal > baseline_cal + self.settings.forecast_drift_cal_margin
            and trailing_cal > self.settings.forecast_drift_max_cal_error
        ):
            reasons.append("trailing calibration error above baseline")
        if (
            history_cal is not None
            and trailing_cal > history_cal + self.settings.forecast_drift_cal_margin
        ):
            reasons.append("trailing calibration error above historical")

        if not reasons:
            status = "ok"
        elif trailing_precision < self.settings.forecast_drift_severe_precision:
            status = "severe_drift"
            measures["reasons"] = reasons
        else:
            status = "drift"
            measures["reasons"] = reasons
        self._record_drift_health(session, status, measures, decision_ts)
        return {"status": status, "measures": measures}

    def _record_drift_health(
        self,
        session: Session,
        status: str,
        measures: dict[str, Any],
        decision_ts: datetime,
    ) -> None:
        state = {
            "ok": "ok",
            "insufficient_trailing": "yellow",
            "drift": "yellow",
            "severe_drift": "red",
        }[status]
        message = f"drift={status}"
        for key in (
            "trailing_precision_at_10",
            "baseline_precision_at_10",
            "historical_precision_at_10",
            "trailing_calibration_error",
            "baseline_calibration_error",
        ):
            if measures.get(key) is not None:
                message += f"; {key}={measures[key]}"
        if measures.get("reasons"):
            message += "; reasons=" + ",".join(measures["reasons"])
        record_health(
            session,
            component="forecast_drift",
            state=state,
            message=message,
        )

    def _latest_metric(self, session: Session, metric_name: str) -> float | None:
        value = session.scalar(
            select(models.BacktestResult.metric_value)
            .join(models.BacktestRun, models.BacktestRun.id == models.BacktestResult.run_id)
            .where(
                models.BacktestResult.metric_name == metric_name,
                models.BacktestRun.model_version == self.settings.forecast_model_version,
            )
            .order_by(models.BacktestRun.started_at.desc())
            .limit(1)
        )
        return float(value) if value is not None else None

    def _persist_metrics(
        self,
        session: Session,
        *,
        samples: list[Sample],
        decision_ts: datetime,
        metrics: dict[str, float],
    ) -> None:
        run = models.BacktestRun(
            cutoff_start=min(sample.ts for sample in samples),
            cutoff_end=decision_ts,
            config_json={
                "forward_hours": self.settings.forecast_forward_hours,
                "ignition_threshold": self.settings.forecast_ignition_threshold,
                "collapse_threshold": self.settings.forecast_collapse_threshold,
            },
            git_sha=_git_sha(),
            model_version=self.settings.forecast_model_version,
            status="completed",
        )
        session.add(run)
        session.flush()
        for name, value in metrics.items():
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            session.add(
                models.BacktestResult(
                    run_id=run.id,
                    metric_name=f"forecast.{name}",
                    metric_value=numeric,
                    chain_slug=None,
                    details_json={},
                )
            )

    # ---------------------------------------------------------------- predict

    def _live_rpc_pool_health(self, session: Session, asset_id: int) -> float:
        if not self.settings.rpc_pool_enabled:
            return 1.0
        asset = session.get(models.Asset, asset_id)
        chain = session.get(models.Chain, asset.chain_id) if asset else None
        if chain is None:
            return 1.0
        states = get_rpc_pool(chain.slug).snapshot()
        if not states:
            return 0.0
        return sum(
            0.0 if state.down else max(0.0, min(1.0, state.health)) for state in states
        ) / len(states)

    @staticmethod
    def _model_probability(
        model: ForecastModel, row: np.ndarray, *, target: str
    ) -> float:
        classifier = model.ignition if target == "ignition" else model.collapse
        calibrator = (
            model.ignition_calibrator if target == "ignition" else model.collapse_calibrator
        )
        if classifier is None or calibrator is None:
            probability = 0.0
        else:
            probability = float(
                calibrator.predict(classifier.predict_proba(row)[:, 1])[0]
            )
        rpc_index = FORECAST_FEATURE_NAMES.index("rpc_pool_health")
        return _widen_probability(probability, float(row[0, rpc_index]))

    def _feature_contributions(
        self,
        model: ForecastModel,
        row: np.ndarray,
        features: dict[str, float],
    ) -> dict[str, dict[str, float | bool]]:
        """Estimate local feature impact by replacing each value with neutral.

        These are prediction deltas, not global model importances: each row says
        how much the current value moved the two probabilities versus the
        model's neutral/missing baseline. This keeps the explanation honest for
        nonlinear interactions and makes missing velocity evidence explicit.
        """
        current_ignition = self._model_probability(model, row, target="ignition")
        current_collapse = self._model_probability(model, row, target="collapse")
        contributions: dict[str, dict[str, float | bool]] = {}
        for index, name in enumerate(FORECAST_FEATURE_NAMES):
            neutral_row = row.copy()
            neutral_row[0, index] = _feature_default(name)
            neutral_ignition = self._model_probability(
                model, neutral_row, target="ignition"
            )
            neutral_collapse = self._model_probability(
                model, neutral_row, target="collapse"
            )
            contributions[name] = {
                "value": float(row[0, index]),
                "baseline": _feature_default(name),
                "missing": name not in features,
                "p_ignition_delta": round(current_ignition - neutral_ignition, 6),
                "p_collapse_delta": round(current_collapse - neutral_collapse, 6),
            }
        return contributions

    def _predict(
        self, session: Session, model: ForecastModel, decision_ts: datetime
    ) -> list[models.Forecast]:
        latest_rows = session.execute(
            select(
                models.Feature.asset_id,
                func.max(models.Feature.decision_ts).label("latest_ts"),
            )
            .where(models.Feature.decision_ts <= decision_ts)
            .group_by(models.Feature.asset_id)
        ).all()
        output: list[models.Forecast] = []
        for asset_id, latest_ts in latest_rows:
            features = self._features_at(session, int(asset_id), latest_ts)
            if not features:
                continue
            # Forecast runs before the next scoring pass, so refresh this
            # chain-level feature directly from the live pool instead of using
            # a stale persisted snapshot.
            features["rpc_pool_health"] = self._live_rpc_pool_health(session, int(asset_id))
            row = np.array(
                [
                    [features.get(name, _feature_default(name)) for name in FORECAST_FEATURE_NAMES]
                ],
                dtype=float,
            )
            p_ignition = self._model_probability(model, row, target="ignition")
            p_collapse = self._model_probability(model, row, target="collapse")
            feature_contributions = self._feature_contributions(model, row, features)
            # Time-to-collapse / time-to-peak conditioned on the asset's current
            # lifecycle phase: the survival model learned a separate hazard curve
            # per phase, and the phase-specific fit takes priority. A phase with
            # no usable fit (no training samples, or samples but no collapse
            # events to learn a curve from) falls back to the global aggregate.
            phase_slug = _phase_slug(features.get("lifecycle_phase"))
            phase_hazard = model.hazards_by_phase.get(phase_slug)
            hazard = (
                phase_hazard
                if phase_hazard is not None and phase_hazard.mean_time_to_collapse is not None
                else model.hazard
            )
            phase_peak = model.peak_hazards_by_phase.get(phase_slug)
            peak_hazard = (
                phase_peak
                if phase_peak is not None and phase_peak.mean_time_to_collapse is not None
                else model.peak_hazard
            )
            expected_collapse = DiscreteHazardModel.expected_hours(hazard, p_collapse)
            expected_peak = DiscreteHazardModel.expected_hours(peak_hazard, p_ignition)
            bucket = f"{int(p_collapse * 10) * 10}-{int(p_collapse * 10) * 10 + 10}%"
            forecast = upsert_forecast(
                session,
                asset_id=int(asset_id),
                decision_ts=decision_ts,
                p_ignition_24h=round(p_ignition, 4),
                p_collapse_24h=round(p_collapse, 4),
                expected_hours_to_peak=(
                    round(expected_peak, 2) if expected_peak is not None else None
                ),
                expected_hours_to_collapse=(
                    round(expected_collapse, 2) if expected_collapse is not None else None
                ),
                calibration_bucket=bucket,
                calibrated=model.calibrated,
                details={
                    "source_features_ts": latest_ts.isoformat(),
                    "hazard_phase": phase_slug,
                    "feature_contributions": feature_contributions,
                },
                model_version=self.settings.forecast_model_version,
            )
            output.append(forecast)
        return output
