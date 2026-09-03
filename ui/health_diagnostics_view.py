"""Health & Diagnostics dashboard — one-stop view for operational health.

Aggregates RPC pool status, database size, memory usage, ensemble weight
distribution, risk calibration state, LLM health, and recent error logs.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import psutil
import streamlit as st

from ui.api_client import api_get, api_get_silent
from ui.theme import apply_dark_theme


def _fmt_bytes(n: float) -> str:
    """Human-readable byte size."""
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def _state_color(state: str) -> str:
    return {"ok": "#00ff88", "yellow": "#eab308", "red": "#f97316"}.get(state, "#9ca3af")


def _render_score_drift_panel() -> None:
    """Score-distribution drift card: persisted risk vs the live formula.

    Newest probe from ``/score-drift/latest`` (the alarm compares the stored
    distribution the GUI serves against what the *current* ``compute_scores``
    yields over the same feature vectors). ``red`` is unmissable and points at
    the operator review workflow; the deduped ``score_drift`` alert — message
    plus sign-off (ack) state — is surfaced so the card carries the ack
    context for the ``--auto-apply`` rescue.

    ``api_get_silent`` is used because a 404 before the first probe is an
    expected state, not an API error — the card renders an informative note
    instead of flashing a red error banner.
    """
    latest = api_get_silent("/score-drift/latest")
    if latest is None:
        st.info(
            "No score-drift probe yet (or the API is unreachable — check the other "
            "panels for a connectivity failure). The alarm arms on the next scan "
            "once at least `score_drift_min_samples` scores are sampled."
        )
        return

    state = latest.get("state") or "unknown"
    ks_d = latest.get("ks_d")
    ks_p = latest.get("ks_p")
    distinct_ratio = latest.get("distinct_ratio")
    mean_delta = latest.get("mean_abs_delta")
    compared = latest.get("compared", 0)
    error_count = latest.get("error_count", 0)
    ts = latest.get("ts")
    stamp = str(ts)[:19] if ts else "n/a"

    titles = {
        "ok": ("✅", "Persisted risk matches the live formula"),
        "yellow": ("⚠️", "Persisted risk is drifting from the live formula"),
        "red": ("🚨", "STALE SCORES — won't match the live formula"),
    }
    icon, title = titles.get(state, ("❓", f"Score-drift state: {state}"))
    st.markdown(f"**{icon} {title}**")

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        ks_label = "—"
        if ks_d is not None:
            ks_label = f"{ks_d:.3f}"
            if ks_p is not None:
                ks_label += f" (p={ks_p:.1e})"
        st.metric(
            "KS D",
            ks_label,
            help=(
                "two-sample Kolmogorov–Smirnov D between persisted and live "
                "risk distributions (p is the asymptotic significance)"
            ),
        )
    with m2:
        st.metric(
            "Distinct ratio",
            f"{distinct_ratio:.2f}" if distinct_ratio is not None else "—",
            help=(
                "stored distinct risk values ÷ live distinct values; ≈1 equal "
                "richness, ≪1 means the served scores collapsed to bands"
            ),
        )
    with m3:
        st.metric(
            "Mean |Δ|",
            f"{mean_delta:.1f}" if mean_delta is not None else "—",
            help="mean per-token risk delta between persisted and live",
        )
    with m4:
        st.metric("Compared", compared, help="tokens compared by the last probe")

    # ── Drift trend series: divergence visible growing before it crosses red ──
    history = api_get_silent("/score-drift/history", params={"limit": 50}) or []
    if history:
        trend_rows = [
            {
                "ts": row.get("run_ts"),
                "KS D": row.get("ks_d"),
                "Distinct ratio": row.get("distinct_ratio"),
                "state": row.get("state"),
            }
            for row in reversed(history)  # API returns newest-first
        ]
        trend_df = pd.DataFrame(trend_rows)
        trend_df["ts"] = pd.to_datetime(trend_df["ts"], errors="coerce")
        trend_df = trend_df.dropna(subset=["ts"])
        if not trend_df.empty:
            melted = trend_df.melt(
                id_vars=["ts", "state"],
                value_vars=["KS D", "Distinct ratio"],
                var_name="metric",
                value_name="value",
            )
            melted = melted.dropna(subset=["value"])
            if melted.empty:
                st.caption("Trend series has no comparable probe signals yet.")
            else:
                fig = px.line(
                    melted,
                    x="ts",
                    y="value",
                    color="metric",
                    markers=True,
                    labels={"value": "", "metric": "", "ts": ""},
                )
                # Mark probes that crossed into red on both series (shared legend).
                red_pts = melted[melted["state"] == "red"]
                for metric in ("KS D", "Distinct ratio"):
                    sub = red_pts[red_pts["metric"] == metric]
                    if sub.empty:
                        continue
                    fig.add_trace(
                        go.Scatter(
                            x=sub["ts"],
                            y=sub["value"],
                            mode="markers",
                            marker={"color": "#f97316", "size": 11, "symbol": "x"},
                            name="red probe",
                            legendgroup="red",
                            hovertemplate=f"{metric}: %{{y:.3f}}<extra></extra>",
                        )
                    )
                fig.update_layout(
                    height=210,
                    margin=dict(l=10, r=10, t=10, b=10),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
                )
                # KS D is bounded [0, 1]; distinct_ratio can exceed 1 when the
                # persisted distribution is richer than the live one, so scale
                # the axis to the data rather than clipping at 1.
                y_max = max(1.0, float(melted["value"].max()))
                fig.update_yaxes(range=[0, y_max])
                apply_dark_theme(fig)
                st.plotly_chart(fig, width="stretch")
                st.caption(
                    "KS D and persisted÷live distinct-ratio per probe (newest at "
                    "right); red × marks probes that crossed into red. A line "
                    "trending up is divergence growing before it alarms."
                )
        else:
            st.caption("Trend series has no timestamped probes yet.")
    else:
        st.caption("Trend series fills in as probes run.")

    if state == "red":
        st.error(
            "**Stale-score alarm**: the distribution the GUI serves no longer "
            "matches the current scoring formula. Review the movers before "
            "trusting served scores:\n\n"
            "```bash\nmake rescore-compare\n```\n"
            "When the diff looks right, ack the alert and rescue the persisted "
            "scores with `python -m ops.score_drift --once --auto-apply`."
        )
    elif state == "yellow":
        st.warning(
            "Divergence trending — run `make rescore-compare` to size the gap "
            "before it crosses red."
        )
    elif error_count:
        st.warning(f"Last probe errored on {error_count} token(s) — see the message below.")
    else:
        st.caption(f"Last probe: {stamp} · no drift")

    if latest.get("message"):
        st.caption(f"{latest['message'][:160]}")

    # Deduped score_drift alert: message + sign-off state (the rescue gate).
    alerts = api_get_silent("/alerts", params={"limit": 50}) or []
    drift_alerts = [a for a in alerts if a.get("alert_type") == "score_drift"]
    if drift_alerts:
        alert = drift_alerts[0]
        a_state = alert.get("state", "?")
        acked = (
            "✅ acked"
            if alert.get("acked_at")
            else "open — not yet acknowledged (required before --auto-apply)"
        )
        with st.expander(f"score_drift alert · {a_state} · {acked}"):
            st.markdown(alert.get("message") or "(no message)")
    else:
        st.caption("No score_drift alert open.")


def _db_size_bytes() -> int:
    """Return the SQLite database file size in bytes, or 0 if unavailable."""
    for candidate in ("serpent.db", "/app/data/serpent.db"):
        if os.path.exists(candidate):
            return os.path.getsize(candidate)
    return 0


def health_diagnostics_view() -> None:
    """Health & Diagnostics dashboard with an auto-refresh toggle.

    The heavy body runs inside an ``@st.fragment(run_every=30)`` when
    auto-refresh is enabled (session_state ``hd_auto_refresh``), so the data
    refreshes every 30s without a full page rerun; the toggle itself lives
    outside the fragment so changing it re-registers the cadence.
    """
    st.header("\U0001fa7a Health & Diagnostics")
    st.caption(
        "Operational health at a glance: RPC pools, database, memory, "
        "ensemble weights, risk calibration, LLM status, and recent errors."
    )
    if "hd_auto_refresh" not in st.session_state:
        st.session_state.hd_auto_refresh = True
    auto = st.toggle(
        "Auto-refresh every 30s",
        key="hd_auto_refresh",
        help="Re-fetches health data on a 30s cadence without rerunning the page.",
    )
    run_every = 30 if auto else None

    @st.fragment(run_every=run_every)
    def _body() -> None:
        _render_health_body()

    _body()


def _render_health_body() -> None:
    """Fragment-wrapped body of the Health & Diagnostics dashboard."""
    # ── Top-row summary metrics ──────────────────────────────────────────
    health = api_get("/health")
    engine = api_get("/engine/status")
    rpc = api_get("/rpc/pool")
    llm = api_get("/llm/health")
    calibration = api_get("/llm/calibration")
    ensemble = api_get("/ensemble/state")
    fusion = api_get("/fusion/recent")

    process = psutil.Process()
    mem = process.memory_info()
    db_size = _db_size_bytes()

    # Summary row
    c1, c2, c3, c4, c4b = st.columns(5)
    with c1:
        st.metric(
            "\U0001f5a5\ufe0f DB Size",
            _fmt_bytes(db_size),
        )
    with c2:
        st.metric(
            "\U0001f4be Memory",
            _fmt_bytes(mem.rss),
            delta=f"VMS {_fmt_bytes(mem.vms)}",
        )
    with c3:
        uptime = engine.get("uptime_sec", 0) if engine else 0
        st.metric(
            "\u23f1\ufe0f Uptime",
            f"{uptime / 3600:.1f}h" if uptime else "N/A",
        )
    with c4:
        if calibration and calibration.get("current_weight") is not None:
            llm_w = calibration["current_weight"]
            share = (1.0 - llm_w) / 3.0
            st.metric(
                "Ensemble (est.)",
                f"R:{share:.0%} M:{share:.0%} H:{share:.0%}",
                delta=f"LLM {llm_w:.0%}",
                help=(
                    "Even split of the non-LLM share — persisted per-scorer "
                    "weights may differ. LLM weight comes from calibration."
                ),
            )
        else:
            st.metric("Ensemble (est.)", "uncalibrated")
    with c4b:
        llm_w = calibration.get("current_weight", 0) if calibration else 0
        st.metric(
            "LLM Weight",
            f"{llm_w:.1%}",
            delta=f"prev {calibration.get('previous_weight', 0):.1%}" if calibration else None,
        )

    # ── Historical memory/CPU (accumulated per refresh) ──────────────────
    history = st.session_state.setdefault("hd_resource_history", [])
    process = psutil.Process()
    history.append(
        {
            "ts": datetime.now(UTC),
            "rss_mb": round(process.memory_info().rss / (1024 * 1024), 1),
            "cpu_pct": round(process.cpu_percent(interval=None), 1),
        }
    )
    history = history[-200:]
    st.session_state["hd_resource_history"] = history
    if len(history) >= 2:
        hist_df = pd.DataFrame(history)
        fig = px.line(
            hist_df,
            x="ts",
            y=["rss_mb", "cpu_pct"],
            labels={"value": "value", "variable": "metric", "ts": "time"},
            title="Memory & CPU history (this GUI process)",
        )
        fig.update_layout(height=260, legend=dict(orientation="h", yanchor="bottom", y=1.02))
        apply_dark_theme(fig)
        st.plotly_chart(fig, width="stretch")
    else:
        st.caption("Resource history fills in as this view refreshes (one sample per refresh).")

    st.divider()

    # ── System Health ────────────────────────────────────────────────────
    st.subheader("\U0001f3e0 System Health")

    if health:
        status = health.get("status", "unknown")
        db_status = health.get("database", "unknown")
        components = health.get("components", [])

        hc1, hc2 = st.columns([1, 3])
        with hc1:
            st.markdown(f"**Overall:** :{'green' if status == 'ok' else 'orange'}[{status}]")
            st.markdown(f"**Database:** :{'green' if db_status == 'ok' else 'orange'}[{db_status}]")
        with hc2:
            if components:
                comp_html = '<div style="display:flex;flex-wrap:wrap;gap:6px;">'
                for comp in components:
                    c_state = comp.get("state", "unknown")
                    color = _state_color(c_state)
                    name = comp.get("component", "?")
                    msg = comp.get("message", "")[:60]
                    comp_html += (
                        f'<div style="background:rgba(255,255,255,0.05);'
                        f"border-left:3px solid {color};padding:4px 10px;"
                        f'border-radius:4px;font-size:0.78rem;">'
                        f'<b>{name}</b> <span style="color:{color};">{c_state}</span>'
                        f'<br><span style="color:#9ca3af;">{msg}</span></div>'
                    )
                comp_html += "</div>"
                st.markdown(comp_html, unsafe_allow_html=True)
    else:
        st.warning("Health data unavailable — is the API running?")

    st.divider()

    # ── Score-Distribution Drift ─────────────────────────────────────────
    st.subheader("\U0001f4ca Score-Distribution Drift")
    _render_score_drift_panel()

    st.divider()

    # ── Risk Calibration (ML band boundaries) ────────────────────────────
    st.subheader("🧮 Risk Calibration — ML Band Boundaries")
    risk_cal = api_get("/risk/calibration")
    if risk_cal:
        st.caption(
            "Collapse-probability cutoffs currently used by the ML band mapper. "
            "Learned from ML scorer outcomes; BLACK is structurally fixed at 0.75."
        )
        version = risk_cal.get("version")
        calibrated_at = risk_cal.get("calibrated_at")
        sample_size = risk_cal.get("sample_size", 0)
        if version:
            st.markdown(
                f"**Version:** `{version}` · **Last calibrated:** "
                f"{str(calibrated_at)[:19] if calibrated_at else 'n/a'} · "
                f"**Samples:** {sample_size}"
            )
        else:
            st.info(
                "No calibration data yet — defaults in effect "
                "(YELLOW ≥ 0.10, ORANGE ≥ 0.30, RED ≥ 0.50)."
            )

        ml_bands = [
            ("YELLOW", risk_cal.get("ml_yellow_threshold", 0.10), "#eab308"),
            ("ORANGE", risk_cal.get("ml_orange_threshold", 0.30), "#f97316"),
            ("RED", risk_cal.get("ml_red_threshold", 0.50), "#ff4444"),
            ("BLACK", risk_cal.get("ml_black_threshold", 0.75), "#7f1d1d"),
        ]
        rows_html = "".join(
            f'<div style="display:flex;align-items:center;gap:10px;margin:4px 0;">'
            f'<span style="width:70px;font-weight:700;color:{color};">{name}</span>'
            f'<span style="font-size:0.8rem;">≥ {boundary:.2f}</span>'
            f"</div>"
            for name, boundary, color in ml_bands
        )
        st.markdown(rows_html, unsafe_allow_html=True)

        ml_precisions = risk_cal.get("ml_band_precisions") or {}
        if ml_precisions:
            st.caption("Band precision (collapsed / flagged) from the last calibration window:")
            st.dataframe(
                [{"band": name, "precision": value} for name, value in ml_precisions.items()],
                width="stretch",
                hide_index=True,
            )
    else:
        st.info("Risk calibration data unavailable.")

    st.divider()

    # ── RPC Pool Status ──────────────────────────────────────────────────
    st.subheader("\U0001f310 RPC Pool Status")

    if rpc:
        for chain in rpc:
            chain_name = chain.get("chain", "?")
            chain_state = chain.get("state", "unknown")
            endpoints = chain.get("endpoints", [])
            down = chain.get("down_count", 0)
            degraded = chain.get("degraded_count", 0)

            label = (
                f"**{chain_name.upper()}** \u2014 "
                f":{'green' if chain_state == 'ok' else 'orange'}[{chain_state}] "
                f"({len(endpoints)} endpoints, {down} down, {degraded} degraded)"
            )
            with st.expander(label, expanded=True):
                if endpoints:
                    ep_data = []
                    for ep in endpoints:
                        health_val = ep.get("health", 0)
                        ep_data.append(
                            {
                                "URL": ep.get("url", "")[:50],
                                "Health": f"**{health_val:.0f}**",
                                "Failures": ep.get("consecutive_failures", 0),
                                "Down": "\u274c" if ep.get("down") else "\u2705",
                                "Probes": ep.get("probe_count", 0),
                                "Success": (
                                    f"{ep.get('probe_successes', 0)}/{ep.get('probe_count', 0)}"
                                ),
                            }
                        )
                    st.dataframe(ep_data, width="stretch", hide_index=True)
                else:
                    st.info("No endpoint data yet — run an ingestion scan first.")
    else:
        st.info("RPC pool data unavailable.")

    st.divider()

    # ── Ensemble & LLM ───────────────────────────────────────────────────
    st.subheader("\U0001f9e0 Ensemble & LLM Status")

    ec1, ec2 = st.columns(2)

    with ec1:
        st.markdown("**Ensemble Weights (rule / ml / heuristic / LLM)**")
        weights = {}
        if ensemble and (ensemble.get("current_weights") or {}):
            persisted = ensemble["current_weights"]
            llm_w = calibration.get("current_weight", 0) if calibration else 0
            weights = {
                "Rule": float(persisted.get("rule", 0)),
                "ML": float(persisted.get("ml", 0)),
                "Heuristic": float(persisted.get("heuristic", 0)),
                "LLM": llm_w,
            }
        elif calibration:
            llm_w = calibration.get("current_weight", 0)
            share = (1 - llm_w) / 3
            weights = {"Rule": share, "ML": share, "Heuristic": share, "LLM": llm_w}
        if weights:
            for name, w in weights.items():
                bar_width = min(w * 100, 100)
                color = "#00ff88" if name == "LLM" else "#3b82f6"
                st.markdown(
                    f'<div style="display:flex;align-items:center;gap:8px;margin:4px 0;">'
                    f'<span style="width:80px;font-size:0.8rem;">{name}</span>'
                    f'<div style="background:#1e293b;border-radius:4px;flex:1;height:12px;">'
                    f'<div style="background:{color};width:{bar_width}%;height:100%;'
                    f'border-radius:4px;"></div></div>'
                    f'<span style="font-size:0.8rem;width:50px;text-align:right;">{w:.1%}</span>'
                    f"</div>",
                    unsafe_allow_html=True,
                )
            st.caption(
                "Persisted adaptive weights from the ensemble engine"
                " (rule/ml/heuristic sum to 1; LLM blends on top)."
            )
        else:
            st.info("No ensemble state yet — weights appear after outcomes are recorded.")

        # Per-scorer accuracy
        scorers = (ensemble or {}).get("scorer_accuracy") or []
        if scorers:
            st.markdown("**Per-scorer accuracy**")
            scorer_rows = []
            for s in scorers:
                acc = s.get("accuracy")
                scorer_rows.append(
                    {
                        "scorer": s.get("scorer_name", "?"),
                        "correct": round(float(s.get("correct_predictions", 0)), 2),
                        "total": round(float(s.get("total_predictions", 0)), 2),
                        "accuracy": f"{acc:.1%}" if acc is not None else "—",
                    }
                )
            st.dataframe(pd.DataFrame(scorer_rows), width="stretch", hide_index=True)

    with ec2:
        st.markdown("**LLM Calibration**")
        if calibration:
            enabled = calibration.get("enabled", False)
            total = calibration.get("total_predictions", 0)
            improved = calibration.get("total_improved", 0)
            degraded = calibration.get("total_degraded", 0)
            rate = calibration.get("improvement_rate", 0)
            last_ts = calibration.get("last_calibration_ts")

            st.markdown(f"**Enabled:** {'Yes' if enabled else 'No'}")
            st.markdown(f"**Total Predictions:** {total}")
            st.markdown(f"**Improved:** :green[{improved}] | **Degraded:** :red[{degraded}]")
            st.markdown(f"**Improvement Rate:** {rate:.1%}")
            if last_ts:
                st.markdown(f"**Last Calibration:** {last_ts[:19]}")

            history = calibration.get("weight_history", [])
            if history:
                st.caption("Weight History (recent 20 calibrations)")
                hist_vals = [h.get("new_weight", 0) for h in history if isinstance(h, dict)]
                if hist_vals:
                    st.code(" ".join(f"{v:.3f}" for v in hist_vals[-10:]))
        else:
            st.info("LLM calibration data unavailable.")

    # ── Weight evolution across runs ────────────────────────────────────
    weight_history = (ensemble or {}).get("weight_history") or []
    llm_history = (calibration or {}).get("weight_history") or []
    if weight_history or llm_history:
        st.markdown("**Weight evolution across recalibrations**")
        rows: list[dict[str, Any]] = []
        for h in weight_history:
            if not isinstance(h, dict):
                continue
            weights = h.get("weights") or {}
            rows.append(
                {
                    "ts": h.get("ts"),
                    "Rule": float(weights.get("rule", 0)),
                    "ML": float(weights.get("ml", 0)),
                    "Heuristic": float(weights.get("heuristic", 0)),
                    "LLM": None,
                }
            )
        for h in llm_history:
            if not isinstance(h, dict):
                continue
            rows.append(
                {
                    "ts": h.get("ts"),
                    "Rule": None,
                    "ML": None,
                    "Heuristic": None,
                    "LLM": h.get("new_weight"),
                }
            )
        if rows:
            ev_df = pd.DataFrame(rows)
            ev_df["ts"] = pd.to_datetime(ev_df["ts"], errors="coerce")
            ev_df = ev_df.dropna(subset=["ts"]).sort_values("ts")
            ev_df = ev_df.set_index("ts")
            for col in ("Rule", "ML", "Heuristic", "LLM"):
                ev_df[col] = ev_df[col].ffill()
            if not ev_df.empty:
                fig = px.line(ev_df, labels={"value": "weight", "variable": "scorer"})
                fig.update_layout(height=280, legend=dict(orientation="h", y=1.02))
                apply_dark_theme(fig)
                st.plotly_chart(fig, width="stretch")

    # ── Cross-source fusion activity ─────────────────────────────────────
    if fusion:
        st.markdown("**Cross-source fusion activity (recent)**")
        fusion_df = pd.DataFrame(fusion)
        display_cols = [
            c
            for c in (
                "symbol",
                "source_count",
                "fusion_score",
                "confidence_boost",
                "signal_agreement",
                "observed_at",
            )
            if c in fusion_df.columns
        ]
        if display_cols:
            st.dataframe(fusion_df[display_cols].head(20), width="stretch", hide_index=True)
            fusion_plot = fusion_df[fusion_df["fusion_score"].notna()].copy()
            if not fusion_plot.empty:
                fig = px.bar(
                    fusion_plot.sort_values("fusion_score").tail(15),
                    x="symbol" if "symbol" in fusion_plot.columns else "asset_id",
                    y="fusion_score",
                    color="confidence_boost" if "confidence_boost" in fusion_plot.columns else None,
                )
                fig.update_layout(height=240)
                apply_dark_theme(fig)
                st.plotly_chart(fig, width="stretch")

    st.divider()

    # ── LLM Health ───────────────────────────────────────────────────────
    st.subheader("\U0001f916 LLM (Ollama) Health")

    if llm:
        connected = llm.get("connected", False)
        model = llm.get("model", "unknown")
        available = llm.get("available", False)
        last_check = llm.get("last_check")
        error = llm.get("error")
        enabled = llm.get("enabled", False)

        llm_c1, llm_c2 = st.columns(2)
        with llm_c1:
            st.markdown(f"**Connected:** {'Yes' if connected else 'No'}")
            st.markdown(f"**Model:** {model}")
            st.markdown(f"**Available:** {'Yes' if available else 'No'}")
            st.markdown(f"**Enabled:** {'Yes' if enabled else 'No'}")
        with llm_c2:
            if last_check:
                st.markdown(f"**Last Check:** {last_check[:19]}")
            if error:
                st.error(f"Error: {error}")
    else:
        st.info("LLM health data unavailable.")

    st.divider()

    # ── Engine Status ────────────────────────────────────────────────────
    st.subheader("\u2699\ufe0f Engine Runtime")

    if engine:
        scan = engine.get("scan", {})
        phase = scan.get("phase", "unknown")
        iteration = scan.get("iteration", 0)
        duration = scan.get("duration_sec", 0)
        error_msg = scan.get("error_message")
        phase_color = (
            "green" if phase in ("idle", "completed") else "orange" if phase == "running" else "red"
        )

        st.markdown(
            f"**Phase:** :{phase_color}[{phase}] "
            f"| **Iteration:** {iteration} "
            f"| **Duration:** {duration:.1f}s"
        )

        if error_msg:
            st.error(f"Error: {error_msg}")

        # Scan metrics
        mc1, mc2, mc3, mc4 = st.columns(4)
        with mc1:
            st.metric("Pairs", scan.get("pairs", 0))
        with mc2:
            st.metric("Scores", scan.get("scores", 0))
        with mc3:
            st.metric("Forecasts", scan.get("forecasts", 0))
        with mc4:
            st.metric("Lifecycle", scan.get("lifecycle", 0))

        mc5, mc6, mc7, mc8 = st.columns(4)
        with mc5:
            st.metric("Ignition", scan.get("ignition_events", 0))
        with mc6:
            st.metric("Fingerprints", scan.get("fingerprints", 0))
        with mc7:
            st.metric("Catalysts", scan.get("catalysts", 0))
        with mc8:
            st.metric("Archive", scan.get("archive", 0))
    else:
        st.info("Engine status unavailable.")

    st.divider()

    # ── Recent Errors ────────────────────────────────────────────────────
    st.subheader("\u26a0\ufe0f Recent Errors")

    if health and health.get("components"):
        error_comps = [
            c
            for c in health["components"]
            if c.get("state") in ("red",) or (c.get("error_count", 0) or 0) > 0
        ]
        if error_comps:
            for comp in error_comps:
                st.error(
                    f"**{comp.get('component', '?')}**: {comp.get('message', 'no message')} "
                    f"(errors: {comp.get('error_count', 0)})"
                )
        else:
            st.success("No recent errors detected.")
    else:
        st.info("Unable to fetch error data.")

    # Footer
    now = datetime.now(UTC).strftime("%H:%M:%S UTC")
    st.caption(
        f"<span style='color:#6b7280;font-size:0.75rem;'>Last updated: {now}</span>",
        unsafe_allow_html=True,
    )
