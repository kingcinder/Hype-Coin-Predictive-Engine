"""Bootstrap the zero-container ``local-single`` profile.

Creates the SQLite database, seeds chains/sources/venues, and prepares the
local Parquet archive directory. No Postgres, Redis, or MinIO containers.

Usage:
    python scripts/bootstrap_local.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Must be set before any settings are read (settings are lru-cached).
os.environ.setdefault("ENV", "local-single")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.config import get_settings
from storage.database import Base, SessionLocal, engine
from storage.seed import seed_reference_data

ARCHIVE_HELP = """\
Serpent Circle zero-container profile is ready.

  Database : {database_url}
  Archive  : {archive_backend} -> {archive_dir}

Run the stack (three terminals, or use `make local-worker` / `local-api` / `local-ui`):

  python -m ingestion.worker --loop            # scanner + radar + forecast (no archive)
  uvicorn api.main:app --host 0.0.0.0 --port 8000
  streamlit run ui/app.py --server.port=8501

Ad-hoc jobs:

  python -m ops.archive --once                 # compact raw evidence -> Parquet + prune
  python -m ops.archive --query "SELECT source_type, count(*) AS n FROM evidence GROUP BY 1"

Optional extras (all free):

  NTFY_ENABLED=True NTFY_TOPIC=<unique> python -m ingestion.worker --loop   # phone push
  python scripts/telegram_auth.py                                           # Telegram channels
"""


def main() -> None:
    settings = get_settings()
    archive_dir = Path(settings.archive_local_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)

    Base.metadata.create_all(bind=engine)
    seed_reference_data()
    with SessionLocal() as session:
        session.commit()

    print(
        ARCHIVE_HELP.format(
            database_url=settings.database_url,
            archive_backend=settings.archive_backend,
            archive_dir=archive_dir.resolve(),
        )
    )


if __name__ == "__main__":
    main()
