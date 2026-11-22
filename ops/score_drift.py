"""Score-distribution drift alarm: persisted vs live-formula risk.

The GUI serves ``scores.risk`` rows from the database. When the scoring
formula changes (e.g. the proportional-risk rescore, or any future formula
edit) the PERSISTED distribution is whatever the last write pass stored —
which can be quantized to a handful of bands while the live ``compute_scores``
yields hundreds of distinct values. Until a rescore lands, the GUI silently
serves stale scores.

The "live" side must reproduce the engine's full write pipeline, not just
``compute_scores``: the engine blends the rule result with the ML forecast
and heuristic layers (``scoring.ensemble``), so a rule-only recomputation
systematically disagrees with persisted scores by ~20 points (2026-11-20:
KS D 0.58, mean |delta| ~23 on 300 tokens) even when nothing is stale. There
is no stored drift baseline to re-establish — the check compares two live
computations — so the fix is to reproduce the pipeline, not to reset a
threshold. ``scripts/rescore.py`` uses the same helper, so a rescue writes
the same blended values the probe grades.

This module closes that gap: each scan it samples the most recent decision
window (exactly the rows the GUI serves), re-runs the *current engine write
pipeline* — forecast mask + ``compute_scores`` + ensemble blend — over the
same (asset, decision_ts) feature vectors, and grades the divergence of the
persisted vs live risk distributions using:

The blend re-uses each row's *write-time* ensemble weights
(``scores.ensemble_weights``, migration 0042) when present. The ensemble
recalibrates hourly from accruing scorer accuracies, so re-blending with
the current weights measures weight drift, not score corruption: a
~0.5-point shift amplifies into KS D ~ 0.5 on heavily-tied values
(2026-11-20: pinned-weight recomputation gives EXACT 300/300, mean
0.0000 with write-time weights, vs 0.5307 with DB-loaded weights). Rows
written before 0042 have NULL weights and fall back to the current
weights, exactly as the probe behaved before.

- **two-sample Kolmogorov–Smirnov D** (pure numpy — scipy is not a project
  dependency; the p-value is the standard asymptotic approximation),
- a **distinct-value ratio** (persisted distinct rounded values / live
  distinct rounded values): ≈1 means equal richness, ≪1 means stored scores
  collapsed to a handful of bands,
- the **mean |delta|** per token between persisted and live risk.

State machine (mirrors ``ops/backtest_drift.py`` / ``ops/parity.py``):

- ``ok`` — no signal cleared its warn threshold,
- ``yellow`` — KS D, distinct-ratio, or mean-delta past warn,
- ``red`` — a strong signal (or combination): records a ``score_drift``
  SystemHealth row, opens/re-arms a deduped ``score_drift`` Alert, and pages
  ntfy at most once per ``SCORE_DRIFT_ALERT_COOLDOWN_HOURS``.

A failed probe never kills the caller: it records ``red`` health and returns
``{"error": ...}``, mirroring the parity contract. Every comparable probe also
appends a ``score_drift_runs`` row (KS D/p, distinct ratio, mean |delta|) so
``GET /score-drift/history`` can chart the divergence growing before it
crosses red; the series is pruned to ``SCORE_DRIFT_RUNS_KEEP`` rows. An
operator can rescue the stale scores through the existing write pass with
``python -m ops.score_drift --once --auto-apply`` — gated on an *acked* alert
(the ack is the sign-off that applying the live formula to the stored rows is
wanted), after which the next probe confirms the distributions match.

``run_score_drift`` takes an injectable session (tests); ``maybe_run_score_drift``
owns a ``session_scope`` for the worker/engine loops; ``latest_score_drift``
parses the newest health row for the API. ``python -m ops.score_drift --once``
runs one probe from the CLI with ``--strict`` for CI integration.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TypedDict

import numpy as np
from sqlalchemy import delete, desc, func, select
from sqlalchemy.orm import Session

from common.config import Settings, get_settings
from common.enums import AlertState
from common.logging import get_logger
from common.time import ensure_utc, utc_now
from risk_engine.rules import mask_unreliable_forecast
from scoring.ensemble import ensemble_engine
from scoring.formulas import ScoreResult, clamp, compute_scores
from storage import models
from storage.repository import record_health

log = get_logger(__name__)

SCORE_DRIFT_COMPONENT = "score_drift"


# ---------------------------------------------------------------------------
# Distribution statistics (pure numpy — scipy is not a project dependency)
# ---------------------------------------------------------------------------


def _ks_2samp(x: list[float], y: list[float]) -> tuple[float, float]:
    """Two-sample Kolmogorov–Smirnov (D, approximate p-value).

    D is the supremum of the distance between the empirical CDFs. The p-value
    uses the standard asymptotic approximation from Kolmogorov's distribution
    (the same family scipy's ``ks_2samp`` asymptotic method uses)::

        λ = (sqrt(n·m/(n+m)) + 0.12 + 0.11/sqrt(n·m/(n+m))) · D
        p ≈ 2 · Σ_{k=1..∞} (-1)^{k-1} · e^{-2·k²·λ²}

    truncated once terms fall below machine-visible magnitude. Deterministic
    and identical-samples-exact (D = 0 → p = 1). Returns (D, p).

    Note: at very small λ the alternating sum is still oscillating at the
    1000-term cap, so p is only approximate there — harmless for the alarm,
    because red additionally requires D above its threshold, and small D
    fails that precondition regardless of p. Tests pin the approximation at
    legitimate sample sizes (n = m = 8+), not in the slow-convergence regime.
    """
    a = np.asarray(x, dtype=float)
    b = np.asarray(y, dtype=float)
    n, m = len(a), len(b)
    if n == 0 or m == 0:
        return 0.0, 1.0
    a = np.sort(a)
    b = np.sort(b)
    # ECDFs evaluated at every pooled observation.
    pooled = np.sort(np.concatenate([a, b]))
    cdf_a = np.searchsorted(a, pooled, side="right") / n
    cdf_b = np.searchsorted(b, pooled, side="right") / m
    d = float(np.max(np.abs(cdf_a - cdf_b)))
    if d == 0.0:
        return 0.0, 1.0
    eff = math.sqrt(n * m / (n + m))
    lam = (eff + 0.12 + 0.11 / eff) * d
    p = 0.0
    for k in range(1, 1000):
        term = 2.0 * math.exp(-2.0 * k * k * lam * lam)
        if k % 2 == 0:
            term = -term
        p += term
        if abs(term) < 1e-12:
            break
    return d, min(1.0, p)


def _distinct_ratio(persisted: list[float], live: list[float]) -> float:
    """persisted distinct values / live distinct values (quantization signal).

    ≈1.0 when stored scores are as rich as live; ≪1 when the persisted rows
    collapsed to a handful of bands (the pre-rescore signature: 2-4 values vs
    406 live). Values rounded to 2dp so near-identical floats compare as equal.
    """
    live_distinct = len({round(v, 2) for v in live})
    if live_distinct == 0:
        return 1.0
    return len({round(v, 2) for v in persisted}) / live_distinct


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------


def _sample_scores(session: Session, *, limit: int) -> list[models.Score]:
    """Sample the rows the GUI serves: the most recent decision window."""
    max_ts = session.scalar(
        select(models.Score.decision_ts).order_by(models.Score.decision_ts.desc()).limit(1)
    )
    if max_ts is None:
        return []
    return list(
        session.scalars(
            select(models.Score)
            .where(models.Score.decision_ts == max_ts)
            .order_by(models.Score.id)
            .limit(limit)
        )
    )


def _build_sample_features(
    session: Session, scores: list[models.Score]
) -> tuple[dict[tuple[int, datetime], dict[str, float]], dict[tuple[int, datetime], list[str]]]:
    """Reconstruct feature dicts + missing lists for the sampled (asset, ts).

    Only the sampled pairs are fetched (one batched query), so this is cheap
    regardless of how many total score rows exist. Missing features are
    excluded from the feature dict and recorded in the missing list, exactly
    like ``scripts/rescore.py`` so ``compute_scores`` treats them as absent.
    """
    pairs = {(s.asset_id, ensure_utc(s.decision_ts)) for s in scores}
    if not pairs:
        return {}, {}
    asset_ids = {asset_id for asset_id, _ in pairs}
    # Only the sampled decision timestamps are fetched — the latest-window
    # score rows all share one ts, so this is one tight batched query, not a
    # full historical scan for those assets.
    decision_tss = {ts for _, ts in pairs}
    features: dict[tuple[int, datetime], dict[str, float]] = {}
    missing: dict[tuple[int, datetime], list[str]] = {}
    for row in session.scalars(
        select(models.Feature).where(
            models.Feature.asset_id.in_(asset_ids),
            models.Feature.decision_ts.in_(decision_tss),
        )
    ):
        key = (row.asset_id, ensure_utc(row.decision_ts))
        if key not in pairs:
            continue
        if row.missing_flag:
            missing.setdefault(key, []).append(row.feature_name)
        else:
            features.setdefault(key, {})[row.feature_name] = float(row.feature_value)
    return features, missing


def _grade_drift(
    *,
    ks_d: float,
    ks_p: float,
    distinct_ratio: float,
    mean_delta: float,
    settings: Settings,
) -> tuple[str, list[str]]:
    """Map the signal vector to (state, reasons).

    red: strong KS divergence (D + significance), distinct-value collapse,
    or a large mean per-token delta. yellow: any warn threshold. A missing
    sample (no persisted scores yet) is ``ok`` — nothing to drift against.
    """
    reasons: list[str] = []
    red = False
    if ks_d >= settings.score_drift_ks_d_red and ks_p <= settings.score_drift_ks_p_red:
        reasons.append(f"KS D={ks_d:.3f} (p={ks_p:.2e})")
        red = True
    if distinct_ratio <= settings.score_drift_distinct_ratio_red:
        reasons.append(f"distinct ratio {distinct_ratio:.2f} (stored scores quantized)")
        red = True
    elif distinct_ratio <= settings.score_drift_distinct_ratio_warn:
        reasons.append(f"distinct ratio {distinct_ratio:.2f}")
    if mean_delta >= settings.score_drift_mean_delta_red:
        reasons.append(f"mean |delta| {mean_delta:.1f}")
        red = True
    elif mean_delta >= settings.score_drift_mean_delta_warn:
        reasons.append(f"mean |delta| {mean_delta:.1f}")
    if red:
        return "red", reasons
    if reasons:
        return "yellow", reasons
    return "ok", []


# ---------------------------------------------------------------------------
# The engine's write pipeline, reproduced for the probe (and the rescore).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineScore:
    """What the engine would persist for one (asset, decision_ts).

    ``risk`` / ``hype`` / ``confidence`` / ``research_priority`` are the
    ENSEMBLE-blended values the engine's ``score_assets`` writes; ``risk_band``
    is the RULE band (the engine's ``_upsert_score`` writes
    ``result.risk_band`` from ``compute_scores`` — the ``_replace`` that
    applies the blend does not touch the band). ``rule_result`` is the
    underlying ``compute_scores`` output, for the fields the engine does not
    blend (exit_risk, ethos, liquidity_access, ...).

    The blend's risk does not depend on the cross-source fusion boost
    (fusion feeds only the confidence input), so the probe — which grades
    risk only — reproduces it exactly with ``fusion_boost=0``. The rescore
    uses the same helper; its confidence/research_priority therefore omit
    the fusion boost the engine adds at score time (the decision-time fusion
    is not recoverable from stored data).
    """

    risk: float
    hype: float
    confidence: float
    research_priority: float
    risk_band: str
    rule_result: ScoreResult
    # The ensemble weights the blend used for this recomputation (None
    # when the blend fell back to the rule result). The rescore records
    # these on the row it rewrites.
    weights: dict[str, float] | None = None


def forecast_masked(session: Session) -> bool:
    """Whether the collapse forecast is currently masked.

    Asks the real mask function once (it reads the ``forecast_calibration``
    health row); the per-asset application is just the key pop, so the probe
    calls this once per run instead of once per token — otherwise a red
    calibration would append one ``risk_forecast_fallback`` health row per
    sampled token.
    """
    _, masked = mask_unreliable_forecast(session, {"collapse_probability_24h": 0.0})
    return masked


def engine_score(
    session: Session,
    features: dict[str, float],
    missing: list[str],
    *,
    masked: bool,
    weights: dict[str, float] | None = None,
) -> EngineScore:
    """Reproduce the engine's persisted score for one feature vector.

    Mask (if active) → ``compute_scores`` → ensemble blend, with the same
    arguments ``scoring.engine.score_assets`` passes. ``data_layer_uncertainty``
    is left at its default: it feeds only ``uncertainty``, never the blended
    fields, and the live per-asset RPC-pool value is not stored. If the blend
    raises, fall back to the rule result — mirroring the engine, whose blend
    is additive and never breaks scoring.

    ``weights`` pins the blend to an explicit weight dict — the probe
    passes the row's stored write-time weights
    (``scores.ensemble_weights``) so a recalibration since the write
    cannot masquerade as drift. When ``None`` (pre-0042 rows, or a
    caller that wants the current pipeline such as the rescore), the
    blend uses the current adaptive weights. An invalid stored dict
    falls back to the current weights inside ``blend``.
    """
    feat = features
    if masked:
        feat = {k: v for k, v in features.items() if k != "collapse_probability_24h"}
    rule = compute_scores(feat, missing, session=session)
    ml_score = feat.get("collapse_probability_24h")
    h_score = feat.get("ignition_signal")
    try:
        blended = ensemble_engine.blend(
            rule_hype=rule.hype,
            rule_risk=rule.risk,
            rule_confidence=min(100.0, rule.confidence),
            rule_research_priority=rule.research_priority,
            rule_risk_band=rule.risk_band.value,
            ml_hype=clamp(100.0 - ml_score * 100) if ml_score is not None else None,
            ml_risk=clamp(ml_score * 100) if ml_score is not None else None,
            heuristic_score=h_score if h_score is not None else None,
            weights=weights,
        )
    except Exception as exc:  # noqa: BLE001 - mirror the engine's additive blend.
        log.warning("score_drift_ensemble_failed", error=str(exc))
        return EngineScore(
            risk=rule.risk,
            hype=rule.hype,
            confidence=rule.confidence,
            research_priority=rule.research_priority,
            risk_band=rule.risk_band.value,
            rule_result=rule,
            weights=None,
        )
    return EngineScore(
        risk=blended.risk,
        hype=blended.hype,
        confidence=blended.confidence,
        research_priority=blended.research_priority,
        risk_band=rule.risk_band.value,
        rule_result=rule,
        weights={
            "rule": blended.rule_weight,
            "ml": blended.ml_weight,
            "heuristic": blended.heuristic_weight,
        },
    )


def run_score_drift(
    session: Session,
    *,
    settings: Settings | None = None,
) -> dict[str, object]:
    """Run one persisted-vs-live score-distribution drift probe.

    Samples the most recent decision window, re-runs the live formula over the
    same feature vectors, grades the divergence, records ``score_drift``
    SystemHealth, opens/re-arms a deduped Alert on red, and pages ntfy at most
    once per cooldown. Never raises — errors record red health and return
    ``{"error": ...}``. Returns ``{"skipped": True}`` when disabled or there
    are too few sampled rows to compare.
    """
    settings = settings or get_settings()
    if not settings.score_drift_enabled:
        return {"skipped": True}
    try:
        scores = _sample_scores(session, limit=settings.score_drift_sample_size)
        if len(scores) < settings.score_drift_min_samples:
            return {"skipped": True}
        features, missing = _build_sample_features(session, scores)
        masked = forecast_masked(session)

        persisted: list[float] = []
        live: list[float] = []
        no_features = 0
        errors = 0
        for score in scores:
            key = (score.asset_id, ensure_utc(score.decision_ts))
            feat = features.get(key)
            if not feat:
                no_features += 1
                continue
            try:
                live_risk = engine_score(
                    session,
                    feat,
                    missing.get(key, []),
                    masked=masked,
                    weights=score.ensemble_weights,
                ).risk
            except Exception as exc:  # noqa: BLE001 - one token must not kill the probe.
                errors += 1
                log.warning("score_drift_token_failed", asset_id=score.asset_id, error=str(exc))
                continue
            persisted.append(score.risk)
            live.append(live_risk)
        compared = len(persisted)
        if compared < 2:
            if errors:
                # Every sampled token failed formula evaluation — that is a
                # probe failure (red health + error result), not a skip:
                # the parity contract is that a failed probe is visible.
                message = (
                    f"score drift probe failed: {errors} of {len(scores)} sampled "
                    "tokens errored during formula evaluation; nothing to compare"
                )
                record_health(
                    session,
                    component=SCORE_DRIFT_COMPONENT,
                    state="red",
                    message=message,
                    error_count=errors,
                )
                log.warning("score_drift_probe_failed", error=message)
                return {"error": message, "sampled": len(scores), "errors": errors}
            return {"skipped": True}

        ks_d, ks_p = _ks_2samp(persisted, live)
        distinct_ratio = _distinct_ratio(persisted, live)
        mean_delta = float(np.mean(np.abs(np.asarray(persisted) - np.asarray(live))))
        state, reasons = _grade_drift(
            ks_d=ks_d,
            ks_p=ks_p,
            distinct_ratio=distinct_ratio,
            mean_delta=mean_delta,
            settings=settings,
        )
        self_consistent = state == "ok"
        distinct_persisted = len({round(v, 2) for v in persisted})
        distinct_live = len({round(v, 2) for v in live})
        message = (
            f"score distribution drift: sampled={len(scores)} compared={compared} "
            f"ks_D={ks_d:.3f} ks_p={ks_p:.2e} "
            f"distinct_persisted={distinct_persisted} "
            f"distinct_live={distinct_live} "
            f"distinct_ratio={distinct_ratio:.3f} mean_abs_delta={mean_delta:.2f}"
        )
        if reasons:
            message += " | " + "; ".join(reasons)
        else:
            message += " | no drift"

        _record_run(
            session,
            state=state,
            sampled=len(scores),
            compared=compared,
            ks_d=ks_d,
            ks_p=ks_p,
            distinct_ratio=distinct_ratio,
            mean_delta=mean_delta,
            distinct_persisted=distinct_persisted,
            distinct_live=distinct_live,
            no_features=no_features,
            errors=errors,
            message=message,
            settings=settings,
        )

        # Push cooldown is evaluated BEFORE this run's health row lands,
        # otherwise the gate would always see the just-written red row and
        # suppress the page (same ordering as ops/parity.py).
        push_due = state == "red" and _push_due(session, settings)
        record_health(
            session,
            component=SCORE_DRIFT_COMPONENT,
            state=state,
            message=message,
            error_count=errors,
        )
        if state == "red":
            _open_drift_alert(session, reasons=reasons, decision_ts=utc_now())
        pushed = False
        if push_due:
            pushed = _notify(
                session, reasons, ks_d, ks_p, distinct_ratio, mean_delta, compared, settings
            )
        log.info(
            "score_drift_probe",
            state=state,
            compared=compared,
            ks_d=ks_d,
            ks_p=ks_p,
            distinct_ratio=distinct_ratio,
            mean_delta=mean_delta,
            pushed=pushed,
        )
        return {
            "status": state,
            "compared": compared,
            "sampled": len(scores),
            "no_features": no_features,
            "errors": errors,
            "ks_d": ks_d,
            "ks_p": ks_p,
            "distinct_ratio": distinct_ratio,
            "mean_abs_delta": mean_delta,
            "self_consistent": self_consistent,
            "pushed": pushed,
            "reasons": reasons,
            "message": message,
        }
    except Exception as exc:  # noqa: BLE001 - the probe must never kill the caller.
        record_health(
            session,
            component=SCORE_DRIFT_COMPONENT,
            state="red",
            message=str(exc),
            error_count=1,
        )
        log.warning("score_drift_probe_failed", error=str(exc))
        return {"error": str(exc)}


def _record_run(
    session: Session,
    *,
    state: str,
    sampled: int,
    compared: int,
    ks_d: float,
    ks_p: float,
    distinct_ratio: float,
    mean_delta: float,
    distinct_persisted: int,
    distinct_live: int,
    no_features: int,
    errors: int,
    message: str,
    settings: Settings,
) -> None:
    """Append one trend-series point and prune to the bounded window.

    Only called when a comparable sample exists (skips/error-only probes leave
    the series untouched) so the table stays a clean distribution-measurement
    series. Pruning runs on the same probe to keep the table size bounded —
    the series is a trend chart, not an archive.
    """
    session.add(
        models.ScoreDriftRun(
            run_ts=utc_now(),
            state=state,
            sampled=sampled,
            compared=compared,
            ks_d=ks_d,
            ks_p=ks_p,
            distinct_ratio=distinct_ratio,
            mean_abs_delta=mean_delta,
            distinct_persisted=distinct_persisted,
            distinct_live=distinct_live,
            no_features=no_features,
            errors=errors,
            message=message,
        )
    )
    _prune_runs(session, keep=settings.score_drift_runs_keep)


def _prune_runs(session: Session, *, keep: int) -> None:
    """Delete runs beyond the newest ``keep`` (bounded trend series)."""
    if keep <= 0:
        return
    max_id = session.scalar(select(func.max(models.ScoreDriftRun.id)))
    if max_id is None or max_id <= keep:
        return
    cutoff = max_id - keep
    session.execute(delete(models.ScoreDriftRun).where(models.ScoreDriftRun.id <= cutoff))


def recent_score_drift_runs(session: Session, *, limit: int = 100) -> list[models.ScoreDriftRun]:
    """Newest-first drift-probe trend series for the API history endpoint."""
    return list(
        session.scalars(
            select(models.ScoreDriftRun)
            .order_by(models.ScoreDriftRun.run_ts.desc(), models.ScoreDriftRun.id.desc())
            .limit(limit)
        )
    )


def _push_due(session: Session, settings: Settings) -> bool:
    """True when the last red score_drift row is older than the cooldown."""
    last_red = session.scalar(
        select(models.SystemHealth)
        .where(
            models.SystemHealth.component == SCORE_DRIFT_COMPONENT,
            models.SystemHealth.state == "red",
        )
        .order_by(models.SystemHealth.ts.desc())
        .limit(1)
    )
    if last_red is None or last_red.ts is None:
        return True
    return utc_now() - ensure_utc(last_red.ts) >= timedelta(
        hours=settings.score_drift_alert_cooldown_hours
    )


def _open_drift_alert(
    session: Session,
    *,
    reasons: list[str],
    decision_ts: datetime,
) -> None:
    """Open or re-arm the single deduped score_drift Alert."""
    existing = session.scalar(
        select(models.Alert)
        .where(
            models.Alert.alert_type == SCORE_DRIFT_COMPONENT,
            # An ACKed alert is still the active sign-off for this drift
            # episode. Creating a second open alert here would leave an
            # unacked alert behind after rescue closes the operator's ACKed
            # one (the CLI probe runs before rescue).
            models.Alert.state.in_(("open", AlertState.ACKED.value)),
        )
        .order_by(desc(models.Alert.created_at))
        .limit(1)
    )
    if existing is not None:
        # Refresh the message but keep the original creation time and state —
        # the alert's age stays honest across repeated red scans, and an
        # ACKed sign-off is not silently converted back to open.
        text = "; ".join(reasons) if reasons else "distribution drifted"
        existing.message = f"Score-drift: {text}"
        return
    asset_id = _first_asset_id(session)
    if asset_id is None:
        return
    session.add(
        models.Alert(
            asset_id=asset_id,
            alert_type=SCORE_DRIFT_COMPONENT,
            threshold_version="dist-v1",
            score_snapshot_ref="score_drift",
            state="open",
            message=f"Score-drift: {'; '.join(reasons) if reasons else 'distribution drifted'}",
            created_at=decision_ts,
        )
    )


def _first_asset_id(session: Session) -> int | None:
    asset = session.scalar(select(models.Asset.id).limit(1))
    return int(asset) if asset is not None else None


def _notify(
    session: Session,
    reasons: list[str],
    ks_d: float,
    ks_p: float,
    distinct_ratio: float,
    mean_delta: float,
    compared: int,
    settings: Settings,
) -> bool:
    """Page the red drift via ntfy (monkeypatched in tests; False when off)."""
    from ops.notifier import notify_score_drift

    return notify_score_drift(
        reasons=reasons,
        ks_d=ks_d,
        ks_p=ks_p,
        distinct_ratio=distinct_ratio,
        mean_delta=mean_delta,
        compared=compared,
        settings=settings,
    )


def _notify_partial_rescue(
    *,
    updated: int,
    errors: int,
    alert_id: int,
    settings: Settings,
) -> bool:
    """Page a partial-rescue follow-up via ntfy (monkeypatched in tests)."""
    from ops.notifier import notify_score_drift_partial_rescue

    return notify_score_drift_partial_rescue(
        updated=updated,
        errors=errors,
        alert_id=alert_id,
        settings=settings,
    )


def maybe_run_score_drift() -> dict[str, object]:
    """Session-owning probe for the worker/engine loops (each scan)."""
    from storage.database import SessionLocal

    with SessionLocal() as session:
        result = run_score_drift(session)
        session.commit()
        return result


def _acked_drift_alert(session: Session) -> models.Alert | None:
    """The newest acked score_drift alert (the operator's current sign-off).

    Requires ``state == acked`` — a previously rescued alert is closed (but
    keeps its acked_at) and must never re-arm the rescue gate. Across multiple
    drift cycles, several acked alerts can accumulate; take the newest by
    creation time so the rescue acts on the *current* sign-off.
    """
    return session.scalar(
        select(models.Alert)
        .where(
            models.Alert.alert_type == SCORE_DRIFT_COMPONENT,
            models.Alert.state == AlertState.ACKED.value,
        )
        .order_by(desc(models.Alert.created_at))
        .limit(1)
    )


def rescue_drift(
    session: Session,
    *,
    settings: Settings | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    """Rescue stale persisted scores through the existing rescore write pass.

    Gate (never bypassed): an operator must have acked the open ``score_drift``
    alert — the red alarm that fired when the persisted distribution diverged.
    That ack is the sign-off that applying the current formula to the stored
    rows is wanted; without it nothing is written and ``reason`` explains how
    to proceed. ``python -m ops.score_drift --once --auto-apply`` calls this
    only when the just-run probe is red.

    On success: the acked alert is closed (``AlertState.CLOSED``), a green
    ``score_drift`` health row records the rescue (explicit ``ts`` so it never
    collides with the red probe row written moments earlier in the same
    second — ``system_health`` is unique on ``(component, ts)``), and the next
    probe confirms the distributions now match.

    On PARTIAL failure (the write pass reported ``errors > 0``): the alert is
    NOT closed — it stays acked-but-annotated with the partial outcome, a RED
    health row (not the green proof-of-rescue) records the failure count, a
    follow-up ntfy pages, and ``applied`` is False so the caller exits
    non-zero and the next probe keeps the alarm red. A half-fixed fleet is
    never silently marked resolved; retry ``--auto-apply`` after fixing the
    write failures.

    ``dry_run=True`` runs the rescore compute without writing, for smoke
    checks.

    The caller owns the commit — the injected ``rescore()`` commits its own
    write pass internally (its documented contract), leaving the alert-close
    + health-row mutations to be committed by the caller alongside the probe
    results (the CLI commits at the end of the run).
    """
    settings = settings or get_settings()
    if not settings.score_drift_enabled:
        return {"applied": False, "reason": "score_drift probe is disabled"}
    alert = _acked_drift_alert(session)
    if alert is None:
        return {
            "applied": False,
            "reason": (
                "no acked score_drift alert — run the probe (--once) so the red "
                "alarm opens, ack it via POST /alerts/{id}/ack, then retry"
            ),
        }
    from scripts.rescore import rescore  # heavy migration path; operator/CLI-only

    stats = rescore(session=session, dry_run=dry_run)
    if dry_run:
        return {"applied": False, "dry_run": True, "rescore": stats, "alert_id": alert.id}
    updated = stats.get("updated", 0)
    errors = stats.get("errors", 0)
    if errors:
        # Partial rescue: the write pass rewrote some rows but failed on
        # others — the fleet is only half-fixed. Never close the alert here:
        # RE-OPEN it (clearing the ack) with a partial annotation, record a
        # RED health row (not the green "rescued" proof), and page a
        # follow-up so the half-fixed state is never mistaken for resolved.
        # Reverting to ``open`` also re-arms the gate: a retry requires a
        # fresh operator sign-off (POST /alerts/{id}/ack) after the write
        # failures are understood — ``applied`` stays False so the CLI exits
        # non-zero and the next probe keeps the alarm red.
        alert.state = AlertState.OPEN.value
        alert.acked_at = None
        alert.ack_quality = None
        alert.message = (
            f"{alert.message} — PARTIAL auto-apply via ops.score_drift "
            f"--auto-apply: {updated} rewritten, {errors} errors; alert "
            "re-opened — fix the failures, re-ack, and retry"
        )
        record_health(
            session,
            component=SCORE_DRIFT_COMPONENT,
            state="red",
            message=(
                f"score drift PARTIAL rescue: write pass rewrote {updated} "
                f"scores with {errors} errors; alert re-opened — fix the "
                "failures, re-ack, and retry --auto-apply"
            ),
            error_count=errors,
            # Distinct ts: the probe's red health row landed moments ago in
            # this same second, and system_health is unique on (component, ts).
            ts=utc_now() + timedelta(microseconds=1),
        )
        _notify_partial_rescue(
            updated=updated,
            errors=errors,
            alert_id=alert.id,
            settings=settings,
        )
        log.warning(
            "score_drift_partial_rescue",
            updated=updated,
            errors=errors,
            alert_id=alert.id,
        )
        return {
            "applied": False,
            "partial": True,
            "updated": updated,
            "errors": errors,
            "alert_id": alert.id,
            "reason": (
                f"rescore write pass reported {errors} errors after rewriting "
                f"{updated} scores — fleet only partially fixed; alert "
                "re-opened, follow-up paged; fix the failures, then re-ack "
                "and retry --auto-apply"
            ),
            "rescore": stats,
        }
    alert.state = AlertState.CLOSED.value
    alert.message = (
        f"{alert.message} — auto-applied via ops.score_drift --auto-apply "
        f"({updated} scores rewritten)"
    )
    record_health(
        session,
        component=SCORE_DRIFT_COMPONENT,
        state="ok",
        message=(
            f"score drift rescued: rescore write pass rewrote {updated} scores; "
            "next probe confirms the distributions match"
        ),
        # Distinct ts: the probe's red health row landed moments ago in this
        # same second, and system_health is unique on (component, ts) — +2 so
        # it also stays clear of the partial-branch row at +1 (branches are
        # exclusive, but the offset documents the invariant).
        ts=utc_now() + timedelta(microseconds=2),
    )
    log.info("score_drift_rescued", updated=updated, errors=errors, alert_id=alert.id)
    return {"applied": True, "rescore": stats, "alert_id": alert.id}


# ---------------------------------------------------------------------------
# Latest-run parser (API surface)
# ---------------------------------------------------------------------------


class ScoreDriftLatest(TypedDict):
    state: str
    ts: datetime | None
    message: str | None
    error_count: int
    sampled: int
    compared: int
    ks_d: float | None
    ks_p: float | None
    distinct_ratio: float | None
    mean_abs_delta: float | None


_MSG_RE = re.compile(
    r"sampled=(\d+) compared=(\d+) ks_D=([\d.]+) ks_p=([\deE.+-]+) "
    r"distinct_persisted=\d+ distinct_live=\d+ distinct_ratio=([\d.]+) "
    r"mean_abs_delta=([\d.]+)"
)


def latest_score_drift(session: Session) -> ScoreDriftLatest | None:
    """Structured summary of the most recent probe from its health row.

    Returns ``None`` when no probe has run yet (endpoint 404s), mirroring
    ``ops.parity.latest_parity``.
    """
    row = session.scalar(
        select(models.SystemHealth)
        .where(models.SystemHealth.component == SCORE_DRIFT_COMPONENT)
        .order_by(models.SystemHealth.ts.desc())
        .limit(1)
    )
    if row is None:
        return None
    match = _MSG_RE.search(row.message or "")
    if not match:
        return {
            "state": row.state,
            "ts": row.ts,
            "message": row.message,
            "error_count": row.error_count,
            "sampled": 0,
            "compared": 0,
            "ks_d": None,
            "ks_p": None,
            "distinct_ratio": None,
            "mean_abs_delta": None,
        }
    ks_d: float | None
    ks_p: float | None
    distinct_ratio: float | None
    mean_abs_delta: float | None
    try:
        ks_d = float(match.group(3))
        ks_p = float(match.group(4))
        distinct_ratio = float(match.group(5))
        mean_abs_delta = float(match.group(6))
    except ValueError:  # pragma: no cover - regex guarantees numeric groups.
        ks_d = ks_p = distinct_ratio = mean_abs_delta = None
    return {
        "state": row.state,
        "ts": row.ts,
        "message": row.message,
        "error_count": row.error_count,
        "sampled": int(match.group(1)),
        "compared": int(match.group(2)),
        "ks_d": ks_d,
        "ks_p": ks_p,
        "distinct_ratio": distinct_ratio,
        "mean_abs_delta": mean_abs_delta,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Serpent Circle score-distribution drift probe: compare the "
        "persisted risk distribution against the live formula over the "
        "sampled latest-decision window and page divergence via ntfy"
    )
    parser.add_argument("--once", action="store_true", help="run one probe and exit")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when drift is detected (CI integration)",
    )
    parser.add_argument(
        "--auto-apply",
        action="store_true",
        help="after a red probe, run the rescore write pass to fix the persisted "
        "scores — but only when an operator has acked the score_drift alert "
        "(the ack is the sign-off); exits 2 when red but unacked, 3 when the "
        "write pass only partially landed (fleet half-fixed, alert re-opened)",
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="print the recorded probe trend series (newest first) without "
        "running a new probe — same rows the API /score-drift/history serves",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="max trend rows to print with --history (default 20)",
    )
    args = parser.parse_args()

    if args.history:
        from storage.database import SessionLocal

        with SessionLocal() as session:
            rows = recent_score_drift_runs(session, limit=max(1, args.limit))
        if not rows:
            print("no score_drift_runs recorded yet — run `python -m ops.score_drift --once`")
            return
        print(
            f"{'run_ts':<26} {'state':<8} {'sampled':>7} {'compared':>8} "
            f"{'ks_D':>8} {'ks_p':>10} {'distinct':>8} {'mean|d|':>8}"
        )
        for row in rows:
            print(
                f"{str(row.run_ts)[:26]:<26} {row.state:<8} {row.sampled:>7} "
                f"{row.compared:>8} {row.ks_d:>8.3f} {row.ks_p:>10.2e} "
                f"{row.distinct_ratio:>8.3f} {row.mean_abs_delta:>8.2f}"
            )
        return

    if not args.once:
        parser.print_help()
        return
    if args.auto_apply and not args.once:
        parser.error("--auto-apply requires --once")

    from storage.database import SessionLocal

    with SessionLocal() as session:
        result = run_score_drift(session)
        if args.auto_apply and result.get("status") == "red":
            result["auto_apply"] = rescue_drift(session)
        session.commit()
    print(json.dumps(result, default=str))

    auto_apply_raw = result.get("auto_apply")
    auto_apply: dict[str, object] = auto_apply_raw if isinstance(auto_apply_raw, dict) else {}
    rescued = bool(auto_apply.get("applied"))
    outcome = auto_apply
    if args.auto_apply and outcome.get("partial"):
        print(
            "auto-apply: PARTIAL rescue — the write pass reported errors; the "
            "fleet is only half-fixed, the alert was left open, and a follow-up "
            "was paged (exit 3 so automation never treats it as resolved)"
        )
        print(f"auto-apply: {outcome['reason']}")
        raise SystemExit(3)
    if args.auto_apply and outcome.get("applied"):
        print(
            "auto-apply: rescore write pass completed — persisted scores now "
            "match the live formula; the next probe confirms"
        )
    elif args.auto_apply and outcome.get("reason"):
        print(f"auto-apply: {outcome['reason']}")
        if result.get("status") == "red":
            raise SystemExit(2)
    elif args.auto_apply:
        print("auto-apply: probe not red — nothing to rescue")

    if args.strict and not rescued and result.get("status") in ("red", "yellow"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
