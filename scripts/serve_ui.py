from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    from common.config import get_settings
    from common.network import validate_network_exposure

    settings = get_settings()
    validate_network_exposure(settings)
    ui_path = Path(__file__).resolve().parents[1] / "ui" / "app.py"
    raise SystemExit(
        subprocess.call(
            [
                sys.executable,
                "-m",
                "streamlit",
                "run",
                str(ui_path),
                "--server.address",
                settings.ui_host,
                "--server.port",
                str(settings.ui_port),
            ],
            env={**os.environ, "API_BASE_URL": settings.api_base_url},
        )
    )


if __name__ == "__main__":
    main()
