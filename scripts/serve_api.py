from __future__ import annotations

import sys
from pathlib import Path

# Allow ``python scripts/serve_api.py`` from the project root: running the
# script puts ``scripts/`` on sys.path, not the repository that contains the
# ``api``/``trading`` packages.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> None:
    import uvicorn

    from common.config import get_settings
    from common.network import validate_network_exposure

    settings = get_settings()
    validate_network_exposure(settings)
    uvicorn.run("api.main:app", host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    main()
