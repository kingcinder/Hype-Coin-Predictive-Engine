#!/usr/bin/env python3
"""Register one webhook endpoint for EVERY engine event type.

Used by deploy/oracle-vps/install-oracle.sh when HOOK_URL is provided,
and runnable standalone with the installed venv:

    /opt/serpent/.venv/bin/python deploy/oracle-vps/register-hooks.py <url>

Registration is idempotent per URL: if a webhook with the same URL
already exists, its event-type subscription is widened to all types
instead of creating a duplicate. Uses the engine's own
``data_lake.webhooks.register_webhook``, so the URL passes the same
SSRF validation (H6) as any API-registered webhook.
"""

from __future__ import annotations

import sys

from data_lake.webhooks import DEFAULT_EVENT_TYPES, register_webhook
from storage import models
from storage.database import SessionLocal


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: register-hooks.py <webhook-url>", file=sys.stderr)
        return 2
    url = sys.argv[1]

    with SessionLocal() as session:
        existing = (
            session.query(models.WebhookConfig)
            .filter(models.WebhookConfig.url == url)
            .first()
        )
        if existing is not None:
            existing.event_types = list(DEFAULT_EVENT_TYPES)
            existing.enabled = True
            session.commit()
            print(
                f"webhook already registered for {url}; "
                f"event types widened to all {len(DEFAULT_EVENT_TYPES)}"
            )
            return 0

        register_webhook(
            session,
            url=url,
            name="francisco-build: all event types",
            event_types=list(DEFAULT_EVENT_TYPES),
        )
        session.commit()

    print(
        f"webhook registered for {url} "
        f"with all {len(DEFAULT_EVENT_TYPES)} event types: "
        + ", ".join(DEFAULT_EVENT_TYPES)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
