"""Forecast Model Persistence — save/load trained models to disk.

Enables model version comparison and automatic rollback when a newly
trained model performs worse than its predecessor.
"""

from __future__ import annotations

import json
import os
import pickle
from pathlib import Path
from typing import Any

from common.config import get_settings
from common.logging import get_logger
from common.time import utc_now

log = get_logger(__name__)

# Model artifact directory
_MODELS_DIR = "models/forecast"


def _models_dir() -> Path:
    """Return the models directory, creating it if needed."""
    settings = get_settings()
    base = Path(getattr(settings, "model_artifact_dir", _MODELS_DIR))
    base.mkdir(parents=True, exist_ok=True)
    return base


def save_model(
    model: Any,
    *,
    version: str,
    metrics: dict[str, float] | None = None,
    max_versions: int | None = None,
) -> str:
    """Save a trained forecast model to disk.

    Returns the path to the saved model file.
    Prunes old versions beyond ``max_versions`` (default from Settings).
    """
    settings = get_settings()
    model_dir = _models_dir()
    timestamp = utc_now().strftime("%Y%m%d_%H%M%S")
    filename = f"forecast_{version}_{timestamp}.pkl"
    filepath = model_dir / filename

    artifact = {
        "version": version,
        "timestamp": utc_now().isoformat(),
        "metrics": metrics or {},
        "model": model,
    }

    with open(filepath, "wb") as f:
        pickle.dump(artifact, f, protocol=pickle.HIGHEST_PROTOCOL)

    # Also save a metadata JSON for quick inspection
    meta_path = model_dir / f"forecast_{version}_{timestamp}.json"
    meta = {
        "version": version,
        "timestamp": utc_now().isoformat(),
        "metrics": metrics or {},
        "filename": filename,
        "size_bytes": os.path.getsize(filepath),
    }
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    log.info("forecast_model_saved", version=version, path=str(filepath))

    # Prune old model versions beyond max_versions to prevent disk bloat
    _prune_old_versions(
        model_dir,
        keep=(
            max_versions
            if max_versions is not None
            else int(getattr(settings, "model_max_versions", 5))
        ),
    )

    return str(filepath)


def _prune_old_versions(model_dir: Path, *, keep: int = 5) -> None:
    """Remove old model artifacts, keeping only the newest ``keep`` versions.

    Operates on both ``.pkl`` and ``.json`` metadata files.  Best-effort:
    errors are logged but never raised.  Guard against ``keep <= 0`` to
    prevent accidental deletion of all versions.
    """
    if keep <= 0:
        keep = 1  # always retain at least the latest version
    try:
        pkl_files = sorted(model_dir.glob("forecast_*.pkl"), key=os.path.getmtime, reverse=True)
        if len(pkl_files) <= keep:
            return
        for old_file in pkl_files[keep:]:
            try:
                old_file.unlink()
                # Also remove the matching metadata JSON
                meta = model_dir / old_file.with_suffix(".json").name
                if meta.exists():
                    meta.unlink()
            except OSError as exc:  # noqa: BLE001
                log.debug("model_prune_failed", path=str(old_file), error=str(exc))
        log.info("model_versions_pruned", removed=len(pkl_files) - keep, kept=keep)
    except Exception:  # noqa: BLE001
        pass  # pruning is best-effort


def load_model(version: str | None = None) -> Any | None:
    """Load a trained forecast model from disk.

    If version is None, loads the latest model. Returns None if no model exists.
    """
    model_dir = _models_dir()
    if not model_dir.exists():
        return None

    # Find all model files
    pkl_files = sorted(model_dir.glob("forecast_*.pkl"), key=os.path.getmtime, reverse=True)
    if not pkl_files:
        return None

    # Filter by version if specified
    if version:
        version_files = [f for f in pkl_files if f.name.startswith(f"forecast_{version}_")]
        if not version_files:
            return None
        target_file = version_files[0]
    else:
        target_file = pkl_files[0]

    try:
        with open(target_file, "rb") as f:
            artifact = pickle.load(f)  # noqa: S301 - trusted local model file
        log.info(
            "forecast_model_loaded",
            version=artifact.get("version"),
            path=str(target_file),
        )
        return artifact.get("model")
    except Exception as exc:  # noqa: BLE001
        log.warning("forecast_model_load_failed", path=str(target_file), error=str(exc))
        return None


def get_model_versions() -> list[dict[str, Any]]:
    """List all saved model versions with their metadata."""
    model_dir = _models_dir()
    if not model_dir.exists():
        return []

    versions = []
    for json_file in sorted(model_dir.glob("forecast_*.json"), reverse=True):
        try:
            with open(json_file) as f:
                meta = json.load(f)
            versions.append(meta)
        except Exception:  # noqa: BLE001
            continue

    return versions


def compare_models(
    new_metrics: dict[str, float],
    old_metrics: dict[str, float],
    *,
    settings: Any | None = None,
) -> dict[str, Any]:
    """Compare two model versions based on their metrics.

    Returns a comparison result with verdict (deploy/rollback) and reasons.
    Thresholds are configurable via Settings.
    """
    if settings is None:
        settings = get_settings()
    comparison: dict[str, Any] = {
        "new_metrics": new_metrics,
        "old_metrics": old_metrics,
        "verdict": "deploy",
        "reasons": [],
        "improvements": {},
        "regressions": {},
    }

    # Key metrics to compare — thresholds sourced from Settings
    precision_delta = getattr(settings, "model_compare_precision_delta", 0.05)
    cal_delta = getattr(settings, "model_compare_cal_delta", 0.05)
    key_metrics = {
        "precision_at_10": {"higher_is_better": True, "threshold": precision_delta},
        "calibration_error": {"higher_is_better": False, "threshold": cal_delta},
        "precision_at_10_real": {"higher_is_better": True, "threshold": precision_delta},
        "calibration_error_real": {"higher_is_better": False, "threshold": cal_delta},
    }

    for metric_name, config in key_metrics.items():
        new_val = new_metrics.get(metric_name)
        old_val = old_metrics.get(metric_name)

        if new_val is None or old_val is None:
            continue

        delta = new_val - old_val
        threshold = config["threshold"]

        if config["higher_is_better"]:
            if delta > threshold:
                comparison["improvements"][metric_name] = round(delta, 4)
            elif delta < -threshold:
                comparison["regressions"][metric_name] = round(delta, 4)
        else:
            if delta < -threshold:
                comparison["improvements"][metric_name] = round(-delta, 4)
            elif delta > threshold:
                comparison["regressions"][metric_name] = round(delta, 4)

    # Decision logic: rollback if there are significant regressions
    severe_threshold = getattr(settings, "model_compare_severe_delta", 0.1)
    if comparison["regressions"]:
        # Check if any regression is severe (> threshold degradation)
        severe_regressions = {
            k: v for k, v in comparison["regressions"].items() if abs(v) > severe_threshold
        }
        if severe_regressions:
            comparison["verdict"] = "rollback"
            comparison["reasons"].append(
                f"Severe regressions in: {', '.join(severe_regressions.keys())}"
            )
        else:
            comparison["verdict"] = "deploy_with_caution"
            comparison["reasons"].append(
                f"Minor regressions in: {', '.join(comparison['regressions'].keys())}"
            )
    elif comparison["improvements"]:
        comparison["reasons"].append(
            f"Improvements in: {', '.join(comparison['improvements'].keys())}"
        )

    return comparison
