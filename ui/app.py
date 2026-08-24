from __future__ import annotations

import os
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st
import streamlit.components.v1 as components

from ui.api_client import API_BASE_URL, api_get, api_post  # noqa: F401

try:
    NARRATIVE_DEV_ACTIVITY_REFRESH_SECONDS = max(
        5, int(os.getenv("NARRATIVE_DEV_ACTIVITY_REFRESH_SECONDS", "30"))
    )
except ValueError:
    NARRATIVE_DEV_ACTIVITY_REFRESH_SECONDS = 30

try:
    UI_REFRESH_SECONDS = max(5, int(os.getenv("UI_REFRESH_SECONDS", "30")))
except ValueError:
    UI_REFRESH_SECONDS = 30


def _sse_bridge_js(api_base: str) -> str:
    """Return JavaScript that maintains a persistent SSE connection to the engine
    stream and writes the latest state into localStorage so Streamlit can read
    it without making a new HTTP request on every rerun."""
    return f"""
    <script>
    (function() {{
      const KEY = 'serpent_engine_sse';
      const api = '{api_base}';
      let retryMs = 1000;
      const MAX_RETRY = 30000;

      function connect() {{
        const es = new EventSource(api + '/engine/stream');

        es.onmessage = function(e) {{
          try {{
            const data = JSON.parse(e.data);
            data._ts = Date.now();
            localStorage.setItem(KEY, JSON.stringify(data));
          }} catch(err) {{ console.error('SSE parse error', err); }}
        }};

        // Named events also carry the full state snapshot
        ['init','scanning','scan_progress','forecasting','retention','completed','error','bootstrapping'].forEach(function(evt) {{
          es.addEventListener(evt, function(e) {{
            try {{
              const data = JSON.parse(e.data);
              data._event = evt;
              data._ts = Date.now();
              localStorage.setItem(KEY, JSON.stringify(data));
            }} catch(err) {{ console.error('SSE parse error', err); }}
          }});
        }});

        es.onerror = function() {{
          es.close();
          retryMs = Math.min(retryMs * 2, MAX_RETRY);
          setTimeout(connect, retryMs);
        }};

        es.onopen = function() {{ retryMs = 1000; }};
      }}

      // Only connect once per page load
      if (!window._serpentSSEConnected) {{
        window._serpentSSEConnected = true;
        connect();
      }}
    }})();
    </script>
    """


def score_frame(path: str, *, include_black: bool = False, limit: int = 25) -> pd.DataFrame:
    data = api_get(path, params={"include_black": include_black, "limit": limit})
    if not data:
        return pd.DataFrame()
    return pd.DataFrame(data)


def score_table(df: pd.DataFrame) -> None:
    if df.empty:
        st.info("No score snapshot exists yet. Run ingestion and scoring, or seed fixture data.")
        return
    columns = [
        "asset_id",
        "chain",
        "symbol",
        "hype",
        "risk_band",
        "risk",
        "liquidity_access",
        "confidence",
        "uncertainty",
        "research_priority",
    ]
    st.dataframe(df[columns], use_container_width=True, hide_index=True)


def heat_graph(df: pd.DataFrame) -> None:
    if df.empty:
        st.info("Heat graph needs scored tokens. No nodes are available yet.")
        return
    plot_df = df.copy()
    plot_df["risk_size"] = plot_df["risk"].clip(lower=8)
    fig = px.scatter(
        plot_df,
        x="liquidity_access",
        y="research_priority",
        size="confidence",
        color="hype",
        symbol="risk_band",
        hover_name="symbol",
        hover_data=["chain", "asset_id", "risk", "uncertainty"],
        color_continuous_scale="Inferno",
        labels={
            "liquidity_access": "Liquidity access",
            "research_priority": "Research priority",
            "hype": "Hype heat",
            "confidence": "Confidence halo",
            "risk_band": "Risk border",
        },
    )
    fig.update_layout(height=460, margin=dict(l=20, r=20, t=30, b=20))
    st.plotly_chart(fig, use_container_width=True)


def token_selector(df: pd.DataFrame) -> int | None:
    if df.empty:
        return None
    options = {
        f"{row.symbol} | {row.chain} | id={row.asset_id} | risk={row.risk_band}": int(row.asset_id)
        for row in df.itertuples(index=False)
    }
    label = st.selectbox("Token", list(options.keys()))
    return options[label]


def render_token_detail(asset_id: int | None) -> None:
    if asset_id is None:
        st.info("Select a scored token to inspect details.")
        return
    data = api_get(f"/tokens/{asset_id}")
    if not data:
        return
    score = data.get("latest_score")
    st.subheader(f"{data['symbol']} on {data['chain']}")
    st.caption(data["address"])
    if not score:
        st.info("This token exists in the universe but has not been scored yet.")
        return

    cols = st.columns(5)
    cols[0].metric("Hype", f"{score['hype']:.1f}")
    cols[1].metric("Risk", f"{score['risk']:.1f}", score["risk_band"])
    cols[2].metric("Liquidity", f"{score['liquidity_access']:.1f}")
    cols[3].metric("Confidence", f"{score['confidence']:.1f}")
    cols[4].metric("Research", f"{score['research_priority']:.1f}")

    explanation = data.get("explanation") or {}
    risk_reasons = explanation.get("risk_reasons") or []
    if risk_reasons:
        st.markdown("**Risk Evidence**")
        for reason in risk_reasons:
            st.write(f"- {reason}")
    else:
        st.info("No risk explanation has been generated for this score.")

    drivers = explanation.get("drivers") or {}
    if drivers:
        st.markdown("**Why It Ranked**")
        st.bar_chart(
            pd.DataFrame(
                {"driver": list(drivers.keys()), "score": list(drivers.values())}
            ).set_index("driver")
        )

    features = pd.DataFrame(data.get("features") or [])
    st.markdown("**Feature Snapshot**")
    if features.empty:
        st.info("No feature snapshot exists for this token.")
    else:
        st.dataframe(features, use_container_width=True, hide_index=True)

    st.markdown("**What Changed**")
    changed = explanation.get("changed_features") or {}
    if changed:
        rows = [
            {
                "feature": name,
                "previous": values.get("previous"),
                "current": values.get("current"),
                "delta": values.get("delta"),
                "pct_delta": values.get("pct_delta"),
            }
            for name, values in changed.items()
        ]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("No previous comparable score snapshot exists yet.")


def top_hype() -> pd.DataFrame:
    st.header("Top Hype Tokens")
    df = score_frame("/tokens/hot", include_black=True)
    heat_graph(df)
    score_table(df)
    return df


def top_research() -> pd.DataFrame:
    st.header("Top Research Candidates")
    df = score_frame("/scores/top", include_black=False)
    score_table(df)
    if not df.empty and (df["risk_band"] == "BLACK").any():
        st.error("Invariant failure: BLACK tokens appeared in research candidates.")
    return df


def risk_console() -> None:
    st.header("Risk Console")
    df = score_frame("/tokens/hot", include_black=True, limit=100)
    if df.empty:
        st.info("No risk rows exist yet. Run scoring to populate the console.")
        return
    risk_order = ["BLACK", "RED", "ORANGE", "YELLOW", "GREEN"]
    filtered = df[df["risk_band"].isin(risk_order)].copy()
    filtered["risk_band"] = pd.Categorical(
        filtered["risk_band"], categories=risk_order, ordered=True
    )
    filtered = filtered.sort_values(["risk_band", "risk"], ascending=[True, False])
    st.dataframe(
        filtered[
            [
                "asset_id",
                "chain",
                "symbol",
                "risk_band",
                "risk",
                "hype",
                "liquidity_access",
                "confidence",
            ]
        ],
        use_container_width=True,
        hide_index=True,
    )
    asset_id = token_selector(filtered)
    if asset_id:
        risk = api_get(f"/risk/{asset_id}")
        if risk:
            st.markdown("**Risk Reasons**")
            for reason in risk.get("reasons") or []:
                st.write(f"- {reason}")


def alerts_view() -> None:
    st.header("Alerts")
    data = api_get("/alerts", params={"limit": 100})
    if not data:
        st.info("No alerts have fired yet.")
        return
    st.dataframe(pd.DataFrame(data), use_container_width=True, hide_index=True)


def live_ops_console() -> None:
    st.header("Live Ops Console")
    st.caption(
        "Last scan's pipeline stage counts, notifier health, and most recent "
        "pushed alerts with timestamps."
    )
    data = api_get("/ops/console")
    if not data:
        st.info("No ops data available yet. Run ingestion to populate the console.")
        return

    last_scan = data.get("last_scan")
    notifier_health = data.get("notifier_health")
    recent_alerts = data.get("recent_alerts") or []

    st.subheader("Last Scan")
    if last_scan:
        scan_state = last_scan["state"]
        if scan_state == "ok":
            st.success(f"Scan completed successfully at {last_scan['ts']}")
        elif scan_state == "yellow":
            st.warning(f"Scan completed with warnings at {last_scan['ts']}")
        else:
            error_msg = last_scan.get('error_message', 'unknown error')
            st.error(f"Scan failed at {last_scan['ts']}: {error_msg}")

        duration = last_scan.get("duration_sec")
        if duration is not None:
            st.metric("Duration", f"{duration:.1f}s")

        st.markdown("**Pipeline Stage Counts**")
        stage_cols = st.columns(4)
        stage_data = [
            ("Profiles", last_scan["profiles"]),
            ("Pairs", last_scan["pairs"]),
            ("Mempool", last_scan["mempool"]),
            ("LP Removals", last_scan["lp_removals"]),
            ("Prelaunch", last_scan["prelaunch"]),
            ("Narrative", last_scan["narrative"]),
            ("Catalysts", last_scan["catalysts"]),
            ("Ignitions", last_scan["ignition_events"]),
            ("Fingerprints", last_scan["fingerprints"]),
            ("Lifecycle", last_scan["lifecycle"]),
            ("Forecasts", last_scan["forecasts"]),
            ("Scores", last_scan["scores"]),
            ("Archive", last_scan["archive"]),
            ("NTFY Sent", last_scan["ntfy_sent"]),
            ("RPC Pool Notify", last_scan["rpc_pool_notifications"]),
            ("RPC Snapshots", last_scan["rpc_pool_snapshots"]),
        ]
        for i, (label, count) in enumerate(stage_data):
            col_idx = i % 4
            stage_cols[col_idx].metric(label, count)
    else:
        st.info("No scan has completed yet.")

    st.subheader("Notifier Health")
    if notifier_health:
        state = notifier_health["state"]
        if state == "ok":
            st.success(f"Notifier: {state}")
        elif state == "yellow":
            st.warning(f"Notifier: {state}")
        else:
            st.error(f"Notifier: {state}")
        st.caption(f"Last check: {notifier_health['ts']}")
        if notifier_health.get("message"):
            st.text(notifier_health["message"])
        if notifier_health.get("error_count", 0) > 0:
            st.metric("Errors", notifier_health["error_count"])
    else:
        st.info("Notifier health not yet recorded.")

    st.subheader("Recent Pushed Alerts")
    if recent_alerts:
        alerts_df = pd.DataFrame(recent_alerts)
        st.dataframe(
            alerts_df[["created_at", "alert_type", "symbol", "state", "message"]],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No alerts have been pushed yet.")


def historical_setups() -> None:
    st.header("Historical Similar Setups")
    df = score_frame("/tokens/hot", include_black=True, limit=100)
    asset_id = token_selector(df)
    if asset_id is None:
        st.info("No scored token exists yet. Run ingestion and scoring first.")
        return
    data = api_get(f"/tokens/{asset_id}/similar", params={"limit": 20, "min_features": 6})
    if not data:
        st.info(
            "No comparable historical feature vectors exist yet. "
            "This view fills in as repeated scans accumulate."
        )
        return
    st.dataframe(pd.DataFrame(data), use_container_width=True, hide_index=True)


def ignition_radar() -> None:
    st.header("Ignition Radar")
    data = api_get("/radar/ignitions", params={"limit": 200})
    if not data:
        st.info("No ignition events yet. Radar runs after each ingestion scan.")
        return
    df = pd.DataFrame(data)
    st.caption(
        "t0 ignition signals: first liquidity injections, sniper bursts, and LP withdrawals."
    )
    st.dataframe(
        df[["ts", "event_type", "symbol", "chain", "confidence"]],
        use_container_width=True,
        hide_index=True,
    )
    if df.empty:
        return
    labels = {
        f"{row.ts} | {row.event_type} | {row.symbol} | id={row.asset_id}": int(row.id)
        for row in df.itertuples(index=False)
    }
    label = st.selectbox("Event", list(labels.keys()))
    event = next((row for row in data if row["id"] == labels[label]), None)
    if event:
        st.json(event.get("details"))


def syndicate_fingerprint() -> None:
    st.header("Syndicate Fingerprint")
    data = api_get("/fingerprint/top", params={"limit": 200})
    if not data:
        st.info(
            "No fingerprint assessments yet. Run ingestion so holder evidence can be "
            "clustered into syndicates."
        )
        return
    df = pd.DataFrame(data)
    st.caption(
        "Recidivism score: how much of a token's launch wallet set overlaps known "
        "pump-and-dump clusters. 60+ raises RiskScore; 70+ fires an alert."
    )
    st.dataframe(
        df[
            [
                "symbol",
                "chain",
                "recidivism_score",
                "matched_cluster_count",
                "matched_wallet_count",
                "matched_roles",
                "decision_ts",
            ]
        ],
        use_container_width=True,
        hide_index=True,
    )
    if not df.empty:
        labels = {
            f"{row.symbol} | recidivism={row.recidivism_score:.0f} | id={row.asset_id}": int(
                row.asset_id
            )
            for row in df.itertuples(index=False)
        }
        label = st.selectbox("Assessment detail", list(labels.keys()))
        detail = api_get(f"/fingerprint/{labels[label]}")
        if detail:
            st.json(detail)


def prelaunch_queue() -> None:
    st.header("Prelaunch Queue")
    data = api_get("/radar/prelaunch", params={"limit": 200})
    if not data:
        st.info(
            "No prelaunch candidates yet. Tokens with contracts or catalysts but no "
            "tradable pool get ranked here."
        )
        return
    df = pd.DataFrame(data)
    st.caption("Tokens ranked before their pool exists — the radar is already watching them at t0.")
    st.dataframe(
        df[["symbol", "chain", "priority_score", "decision_ts"]],
        use_container_width=True,
        hide_index=True,
    )
    if not df.empty:
        labels = {
            f"{row.symbol} | {row.priority_score:.0f} | id={row.asset_id}": int(row.id)
            for row in df.itertuples(index=False)
        }
        label = st.selectbox("Candidate", list(labels.keys()))
        candidate = next((row for row in data if row["id"] == labels[label]), None)
        if candidate:
            st.json(candidate.get("drivers"))


def narrative_radar() -> None:
    st.header("Narrative Radar")
    data = api_get("/narrative/clusters", params={"limit": 200})
    if not data:
        st.info("No narrative clusters yet. Crawlers run after each ingestion scan.")
        return
    df = pd.DataFrame(data)
    st.caption("Mention clusters grouped by shared vocabulary (hand-built minhash, offline).")
    st.dataframe(
        df[["last_seen_at", "mention_count", "seed_topic"]],
        use_container_width=True,
        hide_index=True,
    )


def catalyst_timetable() -> None:
    st.header("Catalyst Timetable")
    data = api_get("/catalysts", params={"limit": 200})
    if not data:
        st.info(
            "No scheduled catalysts yet. News items with TGE/airdrop/unlock/listing "
            "terms feed this."
        )
        return
    df = pd.DataFrame(data)
    st.dataframe(
        df[["symbol", "catalyst_type", "scheduled_at", "published_at", "confidence"]],
        use_container_width=True,
        hide_index=True,
    )


def forecast_view() -> None:
    st.header("Forecast")
    data = api_get("/forecasts", params={"limit": 200})
    if not data:
        st.info(
            "No forecasts yet. The engine needs labeled history to train; "
            "it degrades honestly until then."
        )
        return
    df = pd.DataFrame(data)
    st.caption(
        "Calibrated 24h phase-transition probabilities. P(collapse) feeds RiskScore "
        "and ExitRisk."
    )
    st.dataframe(
        df[
            [
                "symbol",
                "chain",
                "p_ignition_24h",
                "p_collapse_24h",
                "expected_hours_to_peak",
                "expected_hours_to_collapse",
                "calibration_bucket",
                "calibrated",
                "decision_ts",
            ]
        ],
        use_container_width=True,
        hide_index=True,
    )

    options = {
        f"{row.symbol} | {row.chain} | collapse={row.p_collapse_24h:.1%} | id={row.id}": int(
            row.id
        )
        for row in df.itertuples(index=False)
    }
    selected_label = st.selectbox("Prediction explanation", list(options.keys()))
    selected = next((row for row in data if row["id"] == options[selected_label]), None)
    details = (selected or {}).get("details") or {}
    contributions = details.get("feature_contributions") or {}
    if not contributions:
        st.info(
            "This forecast predates local feature explanations. The next scheduled "
            "training run will persist per-prediction feature impacts."
        )
        return

    rows: list[dict[str, Any]] = []
    for name, contribution in contributions.items():
        if not isinstance(contribution, dict):
            continue
        ignition_impact = float(contribution.get("p_ignition_delta") or 0.0) * 100.0
        collapse_impact = float(contribution.get("p_collapse_delta") or 0.0) * 100.0
        rows.append(
            {
                "feature": name.replace("_", " ").title(),
                "feature_key": name,
                "value": contribution.get("value"),
                "status": "missing baseline" if contribution.get("missing") else "observed",
                "ignition impact (pp)": round(ignition_impact, 2),
                "collapse impact (pp)": round(collapse_impact, 2),
                "absolute impact": max(abs(ignition_impact), abs(collapse_impact)),
            }
        )
    contribution_df = pd.DataFrame(rows).sort_values("absolute impact", ascending=False)
    if contribution_df.empty:
        st.info("No feature contribution rows were persisted for this forecast.")
        return

    st.subheader("What drove this prediction")
    st.caption(
        "Local impact versus a neutral/missing feature baseline, in probability points. "
        "Positive values increase a probability; negative values reduce it. These are "
        "row-level effects, not global model importances."
    )
    velocity_keys = {"kol_velocity", "github_star_velocity", "hf_download_velocity"}
    velocity_rows = contribution_df[contribution_df["feature_key"].isin(velocity_keys)]
    if not velocity_rows.empty:
        st.markdown("**Velocity feature contribution**")
        st.dataframe(
            velocity_rows[
                [
                    "feature",
                    "value",
                    "status",
                    "ignition impact (pp)",
                    "collapse impact (pp)",
                ]
            ],
            use_container_width=True,
            hide_index=True,
        )

    st.markdown("**Top feature impacts**")
    top = contribution_df.head(12)
    st.dataframe(
        top[
            [
                "feature",
                "value",
                "status",
                "ignition impact (pp)",
                "collapse impact (pp)",
            ]
        ],
        use_container_width=True,
        hide_index=True,
    )
    chart = top.set_index("feature")[["ignition impact (pp)", "collapse impact (pp)"]]
    st.bar_chart(chart, use_container_width=True)


def lifecycle_radar() -> None:
    st.header("Lifecycle Radar")
    data = api_get("/lifecycle/current", params={"limit": 200})
    if not data:
        st.info(
            "No lifecycle phases yet. The state machine runs after each ingestion "
            "scan and advances SEEDING -> IGNITION -> PARABOLIC -> SATURATION -> "
            "COLLAPSE (exits: DEAD, RUGGED, SURVIVOR)."
        )
        return
    df = pd.DataFrame(data)
    st.caption("Current hype-lifecycle phase per asset (monotonic state machine).")
    st.dataframe(
        df[["symbol", "chain", "phase", "ts", "confidence"]],
        use_container_width=True,
        hide_index=True,
    )
    events = api_get("/lifecycle/events", params={"limit": 100})
    if events:
        st.markdown("**Recent transitions**")
        st.dataframe(
            pd.DataFrame(events)[["ts", "symbol", "phase", "confidence"]],
            use_container_width=True,
            hide_index=True,
        )

    alerts = api_get("/lifecycle/alerts", params={"limit": 100})
    if alerts:
        st.markdown("**Terminal transition alerts**")
        st.caption(
            "COLLAPSE, RUGGED, and DEAD alerts include the exact phase evidence used by "
            "the state machine; SURVIVOR is intentionally not alerted."
        )
        for alert in alerts:
            st.error(
                f"{alert['symbol'] or 'UNKNOWN'} · {alert['chain']} · "
                f"{alert['phase'].upper()} · {alert['state']}"
            )
            st.caption(
                f"Alerted {alert['created_at']} · transition {alert['event_ts']} · "
                f"confidence={alert['confidence']:.2f}"
            )
            st.write(alert["message"])
            evidence = alert.get("evidence") or {}
            if evidence:
                evidence_rows = [
                    {"evidence": key, "value": value} for key, value in evidence.items()
                ]
                st.dataframe(
                    pd.DataFrame(evidence_rows),
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.info("No terminal-phase evidence was persisted for this alert.")


def backtest_results() -> None:
    st.header("Backtest & Drift")
    data = api_get("/backtest/results", params={"limit": 20})
    if not data:
        st.info(
            "No backtest runs yet. Run `python -m backtest.runner` or let forecast "
            "training persist its metrics."
        )
        return
    for run in data:
        st.subheader(f"Run {run['run_id']} · {run['model_version']}")
        st.caption(f"status={run['status']} · started={run['started_at']}")
        metrics = run.get("metrics") or {}
        if metrics:
            st.dataframe(
                pd.DataFrame(
                    [{"metric": name, "value": value} for name, value in metrics.items()]
                ),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("This run has no metrics yet.")


def archive_retention() -> None:
    st.header("Archive & Retention")
    st.caption(
        "Raw evidence is compacted into partitioned Parquet (source/year/month) after "
        "ARCHIVE_COMPACT_AFTER_HOURS and pruned from the hot DB after "
        "ARCHIVE_RETENTION_DAYS. The retention autopilot runs this on a cadence "
        "(RETENTION_CADENCE_HOURS) and reports lake growth in Feed Health as the "
        "`lake` component. Zero-container profile keeps the lake on local disk; "
        "the docker profile uses MinIO. Query the lake with `python -m ops.archive "
        "--query \"SELECT ...\"`."
    )
    runs = api_get("/retention/runs", params={"limit": 30})
    if runs:
        latest = runs[0]
        st.metric("Lake bytes", f"{int(latest['byte_size']):,}")
        st.metric("Growth since last pass", f"{int(latest['growth_bytes']):,} B")
        st.metric(
            "Growth %",
            f"{latest['growth_pct']:.2f}%" if latest["growth_pct"] is not None else "—",
        )
        st.subheader("Retention passes")
        st.dataframe(
            pd.DataFrame(runs)[
                [
                    "ts",
                    "partitions",
                    "archived_rows",
                    "byte_size",
                    "growth_bytes",
                    "growth_pct",
                    "compacted",
                    "pruned",
                ]
            ],
            use_container_width=True,
            hide_index=True,
        )
    data = api_get("/archive/manifests", params={"limit": 200})
    if not data:
        st.info(
            "No archive manifests yet. Run ingestion so raw evidence accumulates, then "
            "`python -m ops.archive --once` (or let the worker run it each scan)."
        )
        return
    df = pd.DataFrame(data)
    st.metric("Parquet partitions", len(df))
    st.metric("Archived rows", int(df["row_count"].sum()))
    st.metric("Lake bytes", f"{int(df['byte_size'].sum()):,}")
    st.dataframe(
        df[
            [
                "source_name",
                "partition_year",
                "partition_month",
                "row_count",
                "byte_size",
                "last_observed_at",
                "object_key",
            ]
        ],
        use_container_width=True,
        hide_index=True,
    )


def feed_health() -> None:
    st.header("Feed Health")
    data = api_get("/health")
    if data:
        st.metric("API Status", data.get("status", "unknown"))
        components = pd.DataFrame(data.get("components") or [])
        if components.empty:
            st.info("No component health rows exist yet. Worker runs will populate this table.")
        else:
            st.dataframe(components, use_container_width=True, hide_index=True)

    pool_data = api_get("/rpc/pool")
    if not pool_data:
        st.info("No live RPC pool state is available in this API process yet.")
        return

    st.subheader("Live RPC Pool")
    st.caption(
        "Endpoint state comes from the latest persisted worker snapshot, with a local "
        "fallback before the first scan. Probe history records background and scan-time "
        "liveness checks; a missing probe means that endpoint has not been probed yet."
    )
    for chain in pool_data:
        st.markdown(f"**{chain['chain'].upper()} · {chain['state'].upper()}**")
        endpoint_rows = []
        history_rows = []
        for endpoint in chain.get("endpoints") or []:
            endpoint_rows.append(
                {
                    "endpoint": endpoint["url"],
                    "status": "DOWN" if endpoint["down"] else "up",
                    "health": f"{endpoint['health'] * 100:.0f}%",
                    "failures": endpoint["consecutive_failures"],
                    "last probe": endpoint["last_probe_at"] or "—",
                    "probe": (
                        "ok"
                        if endpoint["last_probe_ok"] is True
                        else "failed"
                        if endpoint["last_probe_ok"] is False
                        else "—"
                    ),
                    "probes": endpoint["probe_count"],
                }
            )
            for probe in endpoint.get("probe_history") or []:
                history_rows.append(
                    {
                        "endpoint": endpoint["url"],
                        "ts": probe["ts"],
                        "result": "ok" if probe["ok"] else "failed",
                    }
                )
        st.dataframe(pd.DataFrame(endpoint_rows), use_container_width=True, hide_index=True)
        with st.expander(f"{chain['chain'].upper()} probe history", expanded=False):
            if history_rows:
                st.dataframe(
                    pd.DataFrame(history_rows).sort_values("ts", ascending=False),
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.info("No probes recorded yet.")


def rpc_pool_status() -> None:
    st.header("RPC Pool Status")
    st.caption(
        "Live per-chain endpoint health: a failed request decays an endpoint's health, "
        "two consecutive failures take it down, and background probes recover it. "
        "The latest worker snapshot is persisted for cross-process API/UI reads; "
        "aggregate chain health also remains visible under Feed Health."
    )
    data = api_get("/rpc/pool")
    if not data:
        st.info("No RPC pool state available. The pool is initialized on first use.")
        return
    for chain in data:
        st.subheader(f"{chain['chain']} · {chain['state'].upper()}")
        col_a, col_b, col_c = st.columns(3)
        col_a.metric("Endpoints", len(chain["endpoints"]))
        col_b.metric("Down", chain["down_count"])
        col_c.metric("Degraded", chain["degraded_count"])
        rows = []
        for endpoint in chain["endpoints"]:
            rows.append(
                {
                    "endpoint": endpoint["url"],
                    "health": f"{endpoint['health'] * 100:.0f}%",
                    "health_score": endpoint["health"],
                    "consecutive_failures": endpoint["consecutive_failures"],
                    "status": "DOWN" if endpoint["down"] else "up",
                }
            )
        df = pd.DataFrame(rows)
        st.dataframe(
            df[["endpoint", "status", "health", "consecutive_failures"]],
            use_container_width=True,
            hide_index=True,
        )
        st.progress(min(1.0, max(0.0, 1.0 - chain["down_count"] / max(1, len(chain["endpoints"])))),
            text="pool availability")


@st.fragment(run_every=NARRATIVE_DEV_ACTIVITY_REFRESH_SECONDS)
def narrative_dev_activity() -> None:
    st.header("Narrative Dev-Activity")
    st.caption(
        "Live metrics refresh every "
        f"{NARRATIVE_DEV_ACTIVITY_REFRESH_SECONDS}s. Dev-activity proxies from the "
        "crawler metrics: kol_velocity (distinct KOL channels mentioning the token in "
        "24h), github_star_velocity and hf_download_velocity (per-day star/download "
        "growth from raw-evidence crawl history). Missing means the evidence base isn't "
        "there yet — never a fake zero."
    )
    data = api_get("/features/velocity", params={"limit": 200})
    if not data:
        st.info(
            "No velocity features yet. Run ingestion so the narrative crawlers persist "
            "mention metrics, then score the assets."
        )
        return
    df = pd.DataFrame(data)
    kol_values = df.loc[~df["kol_velocity_missing"], "kol_velocity"].dropna()
    star_values = df.loc[~df["github_star_velocity_missing"], "github_star_velocity"].dropna()
    metric_cols = st.columns(3)
    metric_cols[0].metric("Tokens tracked", len(df))
    metric_cols[1].metric(
        "Max KOL breadth",
        f"{kol_values.max():.1f}" if not kol_values.empty else "missing",
    )
    metric_cols[2].metric(
        "Max GitHub stars/day",
        f"{star_values.max():.1f}" if not star_values.empty else "missing",
    )
    latest_snapshot = df["decision_ts"].max() if "decision_ts" in df else None
    if latest_snapshot:
        st.caption(f"Latest feature snapshot: {latest_snapshot}")
    display = df.copy()
    display["kol"] = display.apply(
        lambda row: (
            "missing"
            if row["kol_velocity_missing"]
            else f"{row['kol_velocity']:.1f}"
        ),
        axis=1,
    )
    display["stars/day"] = display.apply(
        lambda row: (
            "missing"
            if row["github_star_velocity_missing"]
            else f"{row['github_star_velocity']:.1f}"
        ),
        axis=1,
    )
    display["downloads/day"] = display.apply(
        lambda row: (
            "missing"
            if row["hf_download_velocity_missing"]
            else f"{row['hf_download_velocity']:.0f}"
        ),
        axis=1,
    )
    st.dataframe(
        display[["symbol", "chain", "kol", "stars/day", "downloads/day", "decision_ts"]],
        use_container_width=True,
        hide_index=True,
    )
    if df.empty:
        return
    chart = df[df["github_star_velocity_missing"] == False]  # noqa: E712 - pandas bool
    if not chart.empty:
        st.markdown("**GitHub star velocity (stars/day)**")
        st.bar_chart(
            chart.set_index("symbol")["github_star_velocity"],
            use_container_width=True,
        )
    else:
        st.info(
            "No star velocity yet — needs at least two crawls of a repo that mentions "
            "a tracked token (roughly two scans apart)."
        )


def engine_control() -> None:
    """Engine control panel: status, trigger actions, seed data."""
    st.header("Engine Control")
    st.caption(
        "Real-time engine status via SSE stream. Data updates instantly on phase "
        "changes — no 30-second polling delay."
    )

    # ── Live engine status ──────────────────────────────────────────────────
    engine = api_get("/engine/status")
    if engine:
        status = engine.get("status", "unknown")
        uptime = engine.get("uptime_sec")
        iterations = engine.get("total_iterations", 0)
        interval = engine.get("scan_interval_seconds", 0)
        scan = engine.get("scan", {})
        phase = scan.get("phase", "idle")

        st.subheader("Runtime Status")
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Engine", status.upper())
        col2.metric("Phase", phase.upper())
        col3.metric("Iterations", iterations)
        col4.metric(
            "Uptime",
            f"{uptime:.0f}s" if uptime else "—",
            help=f"Scan interval: {interval}s",
        )

        if phase in ("scanning", "forecasting", "retention", "bootstrapping"):
            st.info(f"⏳ {scan.get('phase_message', 'Working...')}")
            dur = scan.get("duration_sec")
            if dur is not None:
                st.progress(
                    min(1.0, dur / max(1, interval)),
                    text=f"Scan duration: {dur:.1f}s",
                )
        elif phase == "error":
            st.error(f"❌ {scan.get('error_message', 'Unknown error')}")
        elif phase == "completed":
            st.success("✅ Last scan completed successfully")
        else:
            st.info("💤 Engine is idle — waiting for next scan cycle")

        # Pipeline stage counts from last scan
        stage_data = [
            ("Pairs", scan.get("pairs", 0)),
            ("Scores", scan.get("scores", 0)),
            ("Forecasts", scan.get("forecasts", 0)),
            ("Lifecycle", scan.get("lifecycle", 0)),
            ("Narrative", scan.get("narrative", 0)),
            ("Catalysts", scan.get("catalysts", 0)),
            ("Ignitions", scan.get("ignition_events", 0)),
            ("Fingerprints", scan.get("fingerprints", 0)),
            ("Archive", scan.get("archive", 0)),
            ("NTFY Sent", scan.get("ntfy_sent", 0)),
            ("RPC Snapshots", scan.get("rpc_pool_snapshots", 0)),
        ]
        if any(v > 0 for _, v in stage_data):
            st.subheader("Last Scan Pipeline")
            cols = st.columns(4)
            for i, (label, count) in enumerate(stage_data):
                cols[i % 4].metric(label, count)
    else:
        st.warning("Engine status unavailable — is the API running?")

    st.divider()

    # ── Trigger actions ──────────────────────────────────────────────────────
    st.subheader("Trigger Actions")
    st.caption("Manually trigger engine operations. These run in background threads.")

    act_cols = st.columns(3)
    with act_cols[0]:
        if st.button("🔄 Trigger Scan", type="primary", use_container_width=True):
            result = api_post("/engine/scan")
            if result:
                if result.get("status") == "accepted":
                    st.success(f"✅ {result.get('message', 'Scan started')}")
                else:
                    st.warning(f"⚠️ {result.get('message', 'Rejected')}")
    with act_cols[1]:
        if st.button("🧠 Train Forecast", use_container_width=True):
            result = api_post("/engine/forecast")
            if result:
                if result.get("status") == "accepted":
                    st.success(f"✅ {result.get('message', 'Forecast started')}")
                else:
                    st.warning(f"⚠️ {result.get('message', 'Rejected')}")
    with act_cols[2]:
        if st.button("📦 Run Retention", use_container_width=True):
            result = api_post("/engine/retention")
            if result:
                if result.get("status") == "accepted":
                    st.success(f"✅ {result.get('message', 'Retention started')}")
                else:
                    st.warning(f"⚠️ {result.get('message', 'Rejected')}")

    st.divider()

    # ── Seed data ────────────────────────────────────────────────────────────
    st.subheader("First-Run Setup")
    st.caption(
        "Seed reference data (chains, sources, fixture tokens) into the database. "
        "Safe to run multiple times — idempotent."
    )
    if st.button("🌱 Seed Fixture Data", use_container_width=False):
        with st.spinner("Seeding database..."):
            result = api_post("/engine/seed")
        if result:
            st.success(f"✅ {result.get('message', 'Done')}")



def data_lake_dashboard() -> None:
    """Data Lake dashboard: signal scoring, label progress, archive stats."""
    st.header("Data Lake Dashboard")
    st.caption(
        "Signal scoring sieves actionable data from noise. Label densification "
        "accelerates forecast training. Archive compacts raw evidence into Parquet."
    )

    # Label generation progress
    st.subheader("Label Generation Progress")
    progress = api_get("/data/labels/progress")
    if progress:
        cols = st.columns(4)
        cols[0].metric("Total Labels", progress.get("total_labels", 0))
        cols[1].metric("Required", progress.get("min_samples_required", 30))
        cols[2].metric("Progress", f"{progress.get('progress_pct', 0):.0f}%")
        cols[3].metric(
            "Ready to Train",
            "✅ Yes" if progress.get("ready_to_train") else f"❌ Short {progress.get('shortfall', 0)}",
        )
        label_cols = st.columns(5)
        label_cols[0].metric("Ignition +", progress.get("ignition_positive", 0))
        label_cols[1].metric("Ignition -", progress.get("ignition_negative", 0))
        label_cols[2].metric("Collapse +", progress.get("collapse_positive", 0))
        label_cols[3].metric("Collapse -", progress.get("collapse_negative", 0))
        label_cols[4].metric("Assets Labeled", progress.get("unique_assets_labeled", 0))

        # Progress bar
        st.progress(
            min(1.0, progress.get("progress_pct", 0) / 100.0),
            text=f"{progress.get('total_labels', 0)} / {progress.get('min_samples_required', 30)} labels",
        )

        if st.button("⚡ Densify Labels Now", use_container_width=False):
            with st.spinner("Densifying labels from market snapshots..."):
                result = api_post("/data/densify-labels")
            if result:
                st.success(f"Generated labels: {result}")
                st.rerun()
    else:
        st.info("Label progress data unavailable.")

    st.divider()

    # Signal scoring
    st.subheader("Signal Scoring")
    if st.button("📊 Score Recent Signals", use_container_width=False):
        with st.spinner("Scoring recent data points..."):
            signal_data = api_post("/data/signal/score")
        if signal_data:
            sig_cols = st.columns(4)
            sig_cols[0].metric("Total Scored", signal_data.get("total_scored", 0))
            sig_cols[1].metric("Actionable", signal_data.get("actionable_count", 0))
            sig_cols[2].metric("Noise", signal_data.get("noise_count", 0))
            sig_cols[3].metric("Avg Signal", f"{signal_data.get('avg_signal', 0):.3f}")

            top_signals = signal_data.get("top_signals") or []
            if top_signals:
                st.markdown("**Top Signals**")
                signal_df = pd.DataFrame(top_signals)
                st.dataframe(
                    signal_df[["source_table", "signal_score", "novelty_score", "magnitude_score", "actionable", "reasons"]],
                    use_container_width=True,
                    hide_index=True,
                )
    else:
        st.info("Click 'Score Recent Signals' to analyze recent data for signal strength.")

    st.divider()

    # Trigger data lake pass
    st.subheader("Data Lake Pass")
    st.caption("Run a full data lake pass: signal scoring + label densification + webhook dispatch.")
    if st.button("🚀 Run Data Lake Pass", type="primary", use_container_width=False):
        result = api_post("/engine/data-lake")
        if result:
            if result.get("status") == "accepted":
                st.success(f"✅ {result.get('message', 'Data lake pass started')}")
            else:
                st.warning(f"⚠️ {result.get('message', 'Rejected')}")


def webhook_manager() -> None:
    """Webhook Manager: register, list, delete webhooks and view dispatch history."""
    st.header("Webhook Manager")
    st.caption(
        "Register HTTP endpoints to receive real-time alerts when high-signal events "
        "are detected. Supports custom HTTP POST, Telegram bots, and Discord webhooks."
    )

    # Register new webhook
    st.subheader("Register Webhook")
    with st.form("register_webhook"):
        col1, col2 = st.columns(2)
        with col1:
            webhook_url = st.text_input("Webhook URL", placeholder="https://hooks.example.com/alerts")
            webhook_name = st.text_input("Name", placeholder="my-alerts")
        with col2:
            webhook_events = st.multiselect(
                "Event Types",
                ["ignition_detected", "liquidity_withdrawal_warning", "syndicate_recidivism", "lifecycle_transition", "high_signal_scan"],
                default=["ignition_detected", "lifecycle_transition"],
            )


        if st.form_submit_button("Register Webhook"):
            if webhook_url and webhook_name:
                events_str = ",".join(webhook_events)
                result = api_get(f"/webhooks/register/custom?webhook_url={webhook_url}&webhook_name={webhook_name}&webhook_events={events_str}")
                if result and result.get("status") == "registered":
                    st.success(f"✅ Webhook registered: {result.get('name')}")
                    st.rerun()
                else:
                    st.error("Failed to register webhook")
            else:
                st.warning("URL and Name are required")

    st.divider()

    # List existing webhooks
    st.subheader("Registered Webhooks")
    webhooks = api_get("/webhooks")
    if webhooks:
        for wh in webhooks:
            with st.expander(f"{wh.get('name', 'unnamed')} — {'✅ enabled' if wh.get('enabled') else '❌ disabled'}"):
                st.write(f"**URL:** {wh.get('url', '')}")
                st.write(f"**Events:** {', '.join(wh.get('event_types', []))}")
                st.write(f"**Cooldown:** {wh.get('cooldown_seconds', 300)}s")
                st.write(f"**Last dispatched:** {wh.get('last_dispatched_at', 'never')}")
                if st.button(f"Delete webhook {wh.get('id')}", key=f"del_{wh.get('id')}"):
                    result = api_post(f"/webhooks/{wh.get('id')}/delete")
                    if result and result.get("status") == "deleted":
                        st.success(f"✅ Webhook {wh.get('id')} deleted")
                    st.rerun()
    else:
        st.info("No webhooks registered yet. Register one above.")

    st.divider()

    # Dispatch history
    st.subheader("Dispatch History")
    dispatches = api_get("/webhooks/dispatches?limit=30")
    if dispatches:
        df = pd.DataFrame(dispatches)
        st.dataframe(
            df[["dispatched_at", "event_type", "success", "status_code", "duration_ms", "error_message"]],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No dispatches yet.")


def confidence_dashboard() -> None:
    """Confidence Dashboard: scoring formula breakdown, feature importance, scan history."""
    st.header("Confidence Dashboard")
    st.caption(
        "Understand WHY tokens are ranked the way they are. Scoring formula breakdown, "
        "feature importance for top-ranked tokens, and scan history over time."
    )

    data = api_get("/data/confidence")
    if not data:
        st.info("Confidence dashboard data unavailable. Run ingestion first.")
        return

    # Label progress
    progress = data.get("label_progress", {})
    if progress:
        st.subheader("ML Model Readiness")
        ready = progress.get("ready_to_train", False)
        if ready:
            st.success(f"✅ Model ready to train — {progress.get('total_labels', 0)} labels available")
        else:
            st.warning(
                f"⚠️ Need {progress.get('shortfall', 0)} more labels to train "
                f"({progress.get('total_labels', 0)}/{progress.get('min_samples_required', 30)})"
            )
        st.progress(
            min(1.0, progress.get("progress_pct", 0) / 100.0),
            text=f"{progress.get('progress_pct', 0):.0f}% complete",
        )

    st.divider()

    # Scoring breakdown
    st.subheader("Scoring Formula Breakdown")
    breakdown = data.get("scoring_breakdown") or []
    if breakdown:
        rows = []
        for item in breakdown:
            row = {
                "Symbol": item.get("symbol"),
                "Chain": item.get("chain"),
                "Hype": item.get("hype"),
                "Ethos": item.get("ethos"),
                "Risk": item.get("risk"),
                "Liquidity": item.get("liquidity_access"),
                "Confidence": item.get("confidence"),
                "Priority": item.get("research_priority"),
                "Band": item.get("risk_band"),
            }
            # Add top feature values
            features = item.get("feature_importance", {})
            for fname, fval in features.items():
                short = fname.replace("_", " ").title()[:20]
                row[short] = fval
            rows.append(row)
        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True, hide_index=True)

        # Feature importance chart for first token
        if breakdown:
            first = breakdown[0]
            features = first.get("feature_importance", {})
            if features:
                st.markdown(f"**Top Feature Values for {first.get('symbol', 'UNKNOWN')}**")
                feat_df = pd.DataFrame(
                    [{"feature": k.replace("_", " ").title(), "value": v} for k, v in features.items()]
                ).sort_values("value", ascending=False)
                st.bar_chart(feat_df.set_index("feature"), use_container_width=True)
    else:
        st.info("No scoring data available yet.")

    st.divider()

    # Scan history
    st.subheader("Scan History")
    scan_history = data.get("scan_history") or []
    if scan_history:
        df = pd.DataFrame(scan_history)
        st.dataframe(df, use_container_width=True, hide_index=True)
        if "duration_sec" in df.columns and df["duration_sec"].notna().any():
            st.line_chart(df.set_index("ts")[["duration_sec"]], use_container_width=True)
    else:
        st.info("No scan history yet.")


def command_center() -> None:
    """Compact live overview: engine status, top scores, forecasts, lifecycle, alerts."""
    st.header("Command Center")
    st.caption(
        f"Compact live overview — auto-refreshes every {UI_REFRESH_SECONDS}s. "
        "All dedicated views stay available in the sidebar."
    )

    health = api_get("/health")
    engine = api_get("/engine/status")
    ops = api_get("/ops/console")
    hot = score_frame("/tokens/hot", include_black=True, limit=100)
    research = score_frame("/scores/top", include_black=False, limit=100)
    forecasts = api_get("/forecasts", params={"limit": 100}) or []
    lifecycle = api_get("/lifecycle/current", params={"limit": 100}) or []
    pool = api_get("/rpc/pool") or []


    last_scan = (ops or {}).get("last_scan")
    pool_state = max((chain.get("state") for chain in pool), default="—")

    # ── Engine status banner ────────────────────────────────────────────────
    eng_status = (engine or {}).get("status", "unknown")
    scan_info = (engine or {}).get("scan", {})
    phase = scan_info.get("phase", "idle")
    uptime = engine.get("uptime_sec") if engine else None
    iterations = engine.get("total_iterations", 0) if engine else 0

    phase_colors = {
        "idle": "🟢", "completed": "🟢", "scanning": "🔵",
        "forecasting": "🔵", "retention": "🔵",
        "bootstrapping": "🟡", "error": "🔴",
    }
    phase_emoji = phase_colors.get(phase, "⚪")

    eng_cols = st.columns(4)
    eng_cols[0].metric(
        "Engine",
        eng_status.upper(),
        help=f"{phase_emoji} Phase: {phase} · Iterations: {iterations}"
            + (f" · Uptime: {uptime:.0f}s" if uptime else ""),
    )
    if phase in ("scanning", "forecasting", "retention", "bootstrapping"):
        eng_cols[1].metric("Phase", phase.upper(), help=scan_info.get("phase_message", ""))
        dur = scan_info.get("duration_sec")
        if dur is not None:
            eng_cols[2].metric("Scan Duration", f"{dur:.1f}s")
    elif phase == "error":
        eng_cols[1].metric("Phase", "ERROR", help=scan_info.get("error_message", ""))
    else:
        eng_cols[1].metric("Phase", phase.upper())
    eng_cols[2].metric("Iterations", iterations)
    next_scan = engine.get("scan_interval_seconds") if engine else None
    if next_scan:
        eng_cols[3].metric("Scan interval", f"{next_scan}s")

    status_cols = st.columns(5)
    status_cols[0].metric("API", (health or {}).get("status", "down"))
    if last_scan:
        duration = last_scan.get("duration_sec")
        help_text = (
            f"{last_scan.get('ts')} · {duration:.1f}s" if duration is not None else "—"
        )
        status_cols[1].metric("Last Scan", last_scan.get("state", "?"), help=help_text)
    else:
        status_cols[1].metric("Last Scan", "never")
    status_cols[2].metric("Tokens", len(hot))
    status_cols[3].metric("Forecasts", len(forecasts))
    status_cols[4].metric("RPC pool", pool_state)

    left, right = st.columns(2)
    with left:
        st.markdown("**Top Hype**")
        if hot.empty:
            st.info("No scored tokens yet — run ingestion or seed fixtures.")
        else:
            st.dataframe(
                hot[["symbol", "chain", "hype", "risk_band", "research_priority"]].head(6),
                use_container_width=True,
                hide_index=True,
                height=220,
            )
    with right:
        st.markdown("**Top Research**")
        if research.empty:
            st.info("No research candidates yet.")
        else:
            st.dataframe(
                research[
                    ["symbol", "chain", "research_priority", "risk_band", "hype"]
                ].head(6),
                use_container_width=True,
                hide_index=True,
                height=220,
            )

    f_left, f_right = st.columns(2)
    with f_left:
        st.markdown("**Forecasts · by P(collapse 24h)**")
        if not forecasts:
            st.info("No forecasts yet — needs labeled history to train.")
        else:
            fdf = pd.DataFrame(forecasts)[
                [
                    "symbol",
                    "chain",
                    "p_ignition_24h",
                    "p_collapse_24h",
                    "expected_hours_to_collapse",
                ]
            ].head(6)
            fdf = fdf.round(3)
            st.dataframe(fdf, use_container_width=True, hide_index=True, height=220)
    with f_right:
        st.markdown("**Lifecycle · current phase**")
        if not lifecycle:
            st.info("No lifecycle phases yet.")
        else:
            ldf = pd.DataFrame(lifecycle)[["symbol", "chain", "phase", "ts"]].head(8)
            st.dataframe(ldf, use_container_width=True, hide_index=True, height=220)

    st.markdown("**Last scan pipeline**")
    if not last_scan:
        st.info("No scan has completed yet — the worker loop starts one immediately.")
    else:
        stage_data = [
            ("Profiles", last_scan["profiles"]),
            ("Pairs", last_scan["pairs"]),
            ("Mempool", last_scan["mempool"]),
            ("LP Removals", last_scan["lp_removals"]),
            ("Prelaunch", last_scan["prelaunch"]),
            ("Narrative", last_scan["narrative"]),
            ("Catalysts", last_scan["catalysts"]),
            ("Ignitions", last_scan["ignition_events"]),
            ("Fingerprints", last_scan["fingerprints"]),
            ("Lifecycle", last_scan["lifecycle"]),
            ("Forecasts", last_scan["forecasts"]),
            ("Scores", last_scan["scores"]),
            ("Archive", last_scan["archive"]),
            ("NTFY", last_scan["ntfy_sent"]),
            ("RPC Notify", last_scan["rpc_pool_notifications"]),
            ("RPC Snapshots", last_scan["rpc_pool_snapshots"]),
        ]
        stage_cols = st.columns(8)
        for i, (label, count) in enumerate(stage_data):
            stage_cols[i % 8].metric(label, count)

    st.markdown("**Recent alerts**")
    recent = (ops or {}).get("recent_alerts") or []
    if recent:
        alerts_df = pd.DataFrame(recent)[
            ["created_at", "alert_type", "symbol", "state"]
        ].head(6)
        st.dataframe(alerts_df, use_container_width=True, hide_index=True, height=220)
    else:
        st.info("No alerts have been pushed yet.")


@st.fragment(run_every=UI_REFRESH_SECONDS)
def render_active_view(view: str) -> None:
    """Render the selected view; the whole active view re-runs on the refresh cadence.

    ``Narrative Dev-Activity`` is intentionally excluded: it is itself a
    ``run_every`` fragment, and Streamlit fragments cannot be nested.
    ``Engine Control`` is also excluded because it has interactive buttons.
    """
    if view == "Command Center":
        command_center()
    elif view == "Top Hype Tokens":
        top_hype()
    elif view == "Top Research Candidates":
        top_research()
    elif view == "Risk Console":
        risk_console()
    elif view in {"Token Detail Page", "Why It Ranked", "What Changed"}:
        hot_df = score_frame("/tokens/hot", include_black=True)
        render_token_detail(token_selector(hot_df))
    elif view == "Historical Similar Setups":
        historical_setups()
    elif view == "Ignition Radar":
        ignition_radar()
    elif view == "Prelaunch Queue":
        prelaunch_queue()
    elif view == "Narrative Radar":
        narrative_radar()
    elif view == "Catalyst Timetable":
        catalyst_timetable()
    elif view == "Forecast":
        forecast_view()
    elif view == "Syndicate Fingerprint":
        syndicate_fingerprint()
    elif view == "Lifecycle Radar":
        lifecycle_radar()
    elif view == "RPC Pool Status":
        rpc_pool_status()
    elif view == "Archive & Retention":
        archive_retention()
    elif view == "Backtest & Drift":
        backtest_results()
    elif view == "Feed Health":
        feed_health()
    elif view == "Live Ops Console":
        live_ops_console()
    elif view == "Alerts":
        alerts_view()


# ── SSE-injected live banner (shown at top of every page) ─────────────────

def main() -> None:
    st.set_page_config(page_title="Serpent Circle Hype-Coin Engine", layout="wide")
    st.title("Serpent Circle Hype-Coin Engine")
    st.caption("Research-only local intelligence. Hype and risk stay separate.")

    # Inject the persistent SSE JavaScript connection (reconnects automatically)
    components.html(_sse_bridge_js(API_BASE_URL), height=0)

    view = st.sidebar.radio(
        "View",
        [
            "Command Center",
            "Engine Control",
            "Data Lake",
            "Confidence Dashboard",
            "Webhook Manager",
            "Top Hype Tokens",
            "Top Research Candidates",
            "Risk Console",
            "Token Detail Page",
            "Why It Ranked",
            "What Changed",
            "Historical Similar Setups",
            "Ignition Radar",
            "Prelaunch Queue",
            "Narrative Radar",
            "Catalyst Timetable",
            "Forecast",
            "Syndicate Fingerprint",
            "Lifecycle Radar",
            "Narrative Dev-Activity",
            "RPC Pool Status",
            "Archive & Retention",
            "Backtest & Drift",
            "Feed Health",
            "Live Ops Console",
            "Alerts",
            "Night Crawlers",
        ],
    )

    if view == "Narrative Dev-Activity":
        # Own run_every fragment; Streamlit cannot nest fragments, so it renders
        # outside the refresh wrapper.
        narrative_dev_activity()
    elif view == "Engine Control":
        engine_control()
    elif view == "Night Crawlers":
        from ui.nightcrawler_view import nightcrawler_view
        nightcrawler_view()
    elif view == "Data Lake":
        data_lake_dashboard()
    elif view == "Confidence Dashboard":
        confidence_dashboard()
    elif view == "Webhook Manager":
        webhook_manager()
    else:
        render_active_view(view)


if __name__ == "__main__":
    main()
