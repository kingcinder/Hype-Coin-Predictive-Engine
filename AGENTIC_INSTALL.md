# Agentic AI Install Guide — Serpent Circle Hype-Coin Predictive Engine

> **This file is a starter prompt.** If you are a person: copy everything
> below the line into your coding agent (or hand the agent this file) and let
> it perform the install. If you are an AI agent reading this: the text below
> is your task. Execute it step by step, verify each stage before moving on,
> and report back in the format given at the end.

---

You are installing the **Serpent Circle Hype-Coin Predictive Engine**
(https://github.com/kingcinder/Hype-Coin-Predictive-Engine) on this machine.
Work through the steps in order. Do not skip the verification steps — an
install that has not passed the health check in Step 6 is not finished.
Everything here mirrors the human guide in `INSTALL.md`; if the two ever
disagree, `INSTALL.md` and the shipped scripts win.

## Ground rules

- **Default to the desktop (local) install** in Step 3A. Use the system
  install in Step 3B only if the user explicitly asked for a systemd service.
- **Never print, log, or echo the value of `ENGINE_API_TOKEN`** or any other
  secret. The API token is passed to `curl` as a stdin config (`curl -K -`)
  precisely so it never appears in a command line or the process list —
  keep it that way.
- **Exactly one engine writer per database.** The zero-container SQLite
  profile enforces a single-writer lock at boot. Never start a second
  `python -m engine` or worker loop against the same `serpent.db`; if a
  start fails with the writer-lock or "port 8000 is already in use" message,
  an engine is already running — stop and report that instead of forcing it.
- If a step fails, stop, capture the exact error output, and report it.
  Do not improvise fixes that change the install layout (paths, service
  names, ports) without telling the user.

## Choosing where the engine lives (read before Step 1)

The engine is a **server**: a worker loop that scans on an interval,
a REST API, and a web dashboard. It is designed to run continuously.
Pick the host first — it changes which install path you follow:

### Option 1 — A VPS (recommended for always-on use)

A small Ubuntu VPS is the natural home for this engine, and needs **no
code changes**: use the **system install (Step 3B)**, which registers the
engine as a systemd service with the `serpent` CLI. A free-tier ARM VPS
(e.g. Oracle Cloud Always Free, 2 OCPU / up to 12 GB RAM total) exceeds
the engine's floor (2 GB RAM, 10 GB disk). Notes for that host:

- **ARM CPUs:** most declared Python dependencies ship ARM wheels; a few
  may compile from source on first install — slower, not a blocker.
- **Skip the local LLM layer** on a small VPS: leave `llm_enabled=false`
  (the default posture without a configured provider) or point
  `LLM_PROVIDER` at an external service. Do not try to run the Compose
  stack's Ollama container on a free-tier box.
- **Reaching the dashboard:** the API and GUI bind to `127.0.0.1` by
  default, so a VPS install is not reachable from a phone or laptop
  until you add **one** of:
  - an SSH tunnel (`ssh -L 8501:localhost:8501 <vps>`), or
  - a private mesh VPN (e.g. Tailscale) and browsing to the VPS's
    address on it, or
  - the engine's own LAN mode: `ENGINE_ALLOW_LAN=1`, a chosen
    `API_HOST`/`UI_HOST`, a nonempty `ENGINE_API_TOKEN`, and
    `ENGINE_API_AUTH_BYPASS=0` (startup rejects any weaker combination).
  Prefer the tunnel or the mesh VPN; never expose the dashboard to the
  public internet without the token gate in place.

### Option 1A — Zero-config Oracle VPS (the francisco-build quick path)

On the `francisco-build` branch, Option 1 collapses to **one command**.
The branch ships `deploy/oracle-vps/install-oracle.sh`, which is the
whole install: it installs OS prerequisites, runs the system installer
(Step 3B) tracking this branch, then fills in the only settings a
phone-first deployment needs — an auto-generated `ENGINE_API_TOKEN`
(never printed), ntfy push notifications enabled with **all five alert
event types** and a generated private topic, and the LLM layer off
(a free-tier VPS has no room for a local model). If `HOOK_URL` is set,
it also registers that webhook for **every** webhook event type via
`deploy/oracle-vps/register-hooks.py`. It finishes by restarting the
service and running the authenticated health check.

```bash
git clone -b francisco-build \
  https://github.com/kingcinder/Hype-Coin-Predictive-Engine.git
cd Hype-Coin-Predictive-Engine
sudo bash deploy/oracle-vps/install-oracle.sh
# optional: NTFY_TOPIC=my-topic HOOK_URL=https://example.org/hook
```

The script's final screen is the deliverable: report the **ntfy topic**
(the user subscribes to it in the free ntfy app — that is how every
alert reaches their phone) and the dashboard instructions. Do not print
the API token; it lives only in `/opt/serpent/.env`.

**Phone GUI on this branch:** the dashboard is instantiated phone-first
— a centered single-column layout with the sidebar collapsed
(`ui/app.py` page config) and a dark high-contrast Streamlit theme
(`.streamlit/config.toml`) — because on this deployment the phone *is*
the screen. It is reached over an SSH tunnel
(`ssh -L 8501:localhost:8501 <vps>`) or a mesh VPN, never exposed raw.

### Option 2 — Use it from an Android phone (no port required)

The dashboard (port 8501) and API (port 8000) are plain web interfaces.
If the engine runs on any reachable host (home PC or VPS), an Android
phone needs **no translation at all**: open the dashboard URL in the
phone's browser. This is the supported way to "use it on a phone."

### Option 3 — Running it *on* an Android phone (experimental, unsupported)

There is no Android build of this engine, and producing a native Android
app would be a rewrite, not a conversion — do not attempt it as part of
an install. The only on-device route is Linux-on-Android (e.g. Termux):

- Python 3.12 under Termux can run much of the stack, but expect some
  declared dependencies to need Termux-provided builds or source
  compiles; budget significant troubleshooting time.
- There is **no systemd** on Android: the system install (Step 3B) does
  not apply. Only the desktop-style layout (venv + `python -m engine`)
  is even a candidate.
- Android aggressively kills long-running background processes, and the
  engine's worker is a continuous scan loop: expect to need a persistent
  foreground service / wake lock and a battery-optimization exemption,
  and expect heat and battery drain while it runs.
- The SQLite database and Parquet data lake grow on phone storage.

Treat Option 3 as a research project with a real chance of partial
success on flagship hardware — not as an install path. If the user asks
for the engine "on their phone," default to Option 1 + Option 2
(VPS-hosted, phone as the window) unless they explicitly insist on
on-device execution after hearing the above.

## Step 1 — Check the environment

Requirements (from `INSTALL.md`): Ubuntu 20.04 or newer (tested on 24.04),
**Python 3.12 or newer** (the package declares `requires-python = ">=3.12"`),
at least 2 GB RAM and 10 GB free disk.

```bash
python3 --version        # must report 3.12+
git --version
df -h .                  # confirm ~10 GB free
```

If Python 3.12 is missing, install it (this needs sudo — ask the user if
you do not have it):

```bash
sudo apt install python3.12 python3.12-venv python3.12-dev git
```

## Step 2 — Get the code

Skip this step if you are already inside a checkout of the repo.

```bash
git clone https://github.com/kingcinder/Hype-Coin-Predictive-Engine.git
cd Hype-Coin-Predictive-Engine
git branch --show-current   # expect: main
```

## Step 3A — Desktop install (default, no root)

Run the shipped installer from the repo root:

```bash
bash packaging/install-local.sh
# equivalent: make install-local
```

The installer does three things:

1. Creates the project virtualenv (`.venv`) and installs the package with
   its declared dependencies: `pip install -e ".[dev]"`. This installs
   **every** dependency declared in `pyproject.toml`
   (`[project.dependencies]`), including ones a partial manual install can
   miss — e.g. `defusedxml>=0.7.1`. Do not hand-pick a subset of packages;
   an incomplete venv fails later in confusing ways (test collection and
   some import paths need the full declared set).
2. Bootstraps the SQLite database via `scripts/bootstrap_local.py`
   (skipped automatically if `serpent.db` already exists).
3. Installs a **Serpent Circle Engine** desktop shortcut that starts the
   whole stack (worker + API + GUI).

Manual equivalent, if the installer script itself fails and you must
reproduce it by hand:

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[dev]"
.venv/bin/python scripts/bootstrap_local.py
```

Optional sanity check that the venv and the declared dependencies agree:

```bash
make check-deps                  # every third-party import is declared
make check-imports VENV=.venv    # imports resolve against this venv
```

## Step 3B — System install (only if the user asked for a service)

```bash
sudo bash packaging/install.sh
```

This creates a `serpent` system user, a venv at `/opt/serpent/.venv`,
runs migrations, registers and starts the `serpent.service` systemd unit,
and installs the `serpent` CLI. Manage it with `sudo serpent start|stop|
restart|status|logs|update|uninstall`. Config lives in `/opt/serpent/.env`.
(A second, split-services installer exists at `deploy/install.sh`; use it
only if the user explicitly asks for split `serpent-api` / `serpent-worker`
/ `serpent-ui` units.)

## Step 4 — Configuration (usually nothing to do)

A fresh checkout needs **no** `.env`: with no `.env` and no database URL
override, the engine selects `ENV=local-single` — SQLite at `serpent.db`,
zero containers. Copying `.env.example` to `.env` selects the documented
PostgreSQL profile instead; do that only if the user wants PostgreSQL.

- API and GUI bind to `127.0.0.1` by default (API port `8000`, GUI port
  `8501`). Leave them on loopback unless the user explicitly wants LAN
  access; a non-loopback bind requires **all** of `ENGINE_ALLOW_LAN=1`, a
  chosen `API_HOST`/`UI_HOST`, a nonempty `ENGINE_API_TOKEN`, and
  `ENGINE_API_AUTH_BYPASS=0`, and startup rejects anything less.
- Optional data-source keys (Helius, Etherscan, Farcaster, an LLM provider,
  etc.) go in `.env`. The engine runs without them; never invent values.

## Step 5 — Start the engine

Desktop install:

```bash
./start-engine.sh
# or, in a plain terminal:
.venv/bin/python -m engine        # equivalent: make engine
```

One process runs everything: it applies migrations, then runs the
ingestion worker loop, the REST API on http://localhost:8000, and the
Streamlit GUI on http://localhost:8501. Ctrl+C stops all three cleanly.

System install: `sudo serpent start`.

## Step 6 — Verify it is actually up (required)

`/health` sits behind the same bearer <redacted> as the rest of the API.
An unauthenticated request returns **401 — that is the gate working, not
an outage.** Verify with the token, passed via stdin config so it never
touches the process list (this is the same pattern the shipped
`start-engine.sh` launcher uses):

```bash
tok="${ENGINE_API_TOKEN:-}"
if [ -z "$tok" ] && [ -f .env ]; then
  tok="$(sed -n 's/^ENGINE_API_TOKEN=//p' .env | head -1 | tr -d '"'"'"'\r')"
fi
printf 'header = "Authorization: Bearer %s"\n' "$tok" \
  | curl -sf -K - "http://localhost:8000/health"
```

Success = a JSON health response with HTTP 200. For a desktop install with
no token configured, an authenticated request may carry an empty token —
what matters is that the endpoint answers through the shipped pattern.

Then confirm the dashboard responds:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8501   # expect 200
```

For a system install, `sudo serpent status` gives a token-free status
check.

If verification fails: check the engine's terminal/log output first, then
the troubleshooting section of `INSTALL.md` (port already in use, database
lock, service logs). The retention-wedge diagnosis lives in
`docs/runbook.md`.

## Step 7 — Report back

Tell the user, in this shape:

- Which install path was used (desktop / system), and the checkout path.
- Python version and that `.venv` was created from the declared
  dependencies (`pip install -e ".[dev]"`).
- Whether the database was bootstrapped fresh or already existed.
- The exact `/health` result (HTTP status + response body) and the
  dashboard check result — not just "it works."
- How to start/stop it from now on (`./start-engine.sh` or
  `python -m engine`; `sudo serpent …` for a system install), and where
  the GUI lives: http://localhost:8501.
- Anything that failed or was skipped, with the exact error text.
