from __future__ import annotations

import os

# Zero-container profile by default: SQLite + local Parquet archive, so a bare
# ``python -m engine`` runs the whole stack on one machine with no containers.
# This must happen before any settings-dependent import (storage.database reads
# settings at import time), so it lives at the very top of the entrypoint.
os.environ.setdefault("ENV", "local-single")

from engine.run import main  # noqa: E402

if __name__ == "__main__":
    main()
