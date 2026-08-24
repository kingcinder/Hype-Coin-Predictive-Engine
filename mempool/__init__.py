from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from mempool.evm import run_evm_watch
from mempool.solana import run_solana_watch


def run_mempool(
    session: Session, *, decision_ts: datetime | None = None
) -> dict[str, Any]:
    """Run the Solana signature watcher and EVM factory watcher for this scan."""
    solana = run_solana_watch(session, decision_ts=decision_ts)
    evm = run_evm_watch(session, decision_ts=decision_ts)
    return {"solana": solana, "evm": evm}


__all__ = ["run_mempool", "run_evm_watch", "run_solana_watch"]
