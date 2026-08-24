from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.config import Settings
from storage import models


def alert_generation_allowed(
    session: Session, alert_type: str, settings: Settings
) -> bool:
    """Return whether low-quality alert types should currently be emitted."""
    control = session.scalar(
        select(models.AlertTypeControl).where(models.AlertTypeControl.alert_type == alert_type)
    )
    if control is not None and control.reenabled:
        return True
    rows = session.scalars(
        select(models.Alert).where(
            models.Alert.alert_type == alert_type,
            models.Alert.acked_at.is_not(None),
        )
    ).all()
    useful = sum(row.ack_quality == "useful" for row in rows)
    noise = sum(row.ack_quality == "noise" for row in rows)
    rated = useful + noise
    if rated < settings.alert_quality_min_ratings:
        return True
    return useful / rated >= settings.alert_quality_noise_floor


def reenable_alert_type(session: Session, alert_type: str) -> models.AlertTypeControl:
    control = session.scalar(
        select(models.AlertTypeControl).where(models.AlertTypeControl.alert_type == alert_type)
    )
    if control is None:
        control = models.AlertTypeControl(alert_type=alert_type, reenabled=True)
        session.add(control)
    else:
        control.reenabled = True
    session.commit()
    session.refresh(control)
    return control
