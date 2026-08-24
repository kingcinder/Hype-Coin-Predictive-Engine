from __future__ import annotations

# Importing the archive module here would trigger a runpy warning when the CLI
# runs `python -m ops.archive` (the package __init__ eagerly imports the target
# module). Consumers import from ops.archive / ops.notifier directly.
from ops.notifier import NtfyNotifier, run_notifier

__all__ = ["NtfyNotifier", "run_notifier"]
