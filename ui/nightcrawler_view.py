"""Night Crawler GUI view functions for Streamlit."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ui.api_client import api_get, api_post

# Source → emoji/label mapping for badges
_SOURCE_BADGES: dict[str, str] = {
    "coingecko": "🦎 CoinGecko",
    "pump_fun": "🚀 PumpFun",
    "defillama": "🦙 DeFiLlama",
    "whale_tracker": "🐋 Whale",
    "explorer": "🔍 Explorer",
    "nitter": "🐦 Twitter",
    "presale": "🏷️ Presale",
    "farcaster": "🟣 Farcaster",
}


def _signal_color(score: float) -> str:
    """Map signal score to a display color."""
    if score >= 80:
        return "#00ff88"  # bright green — high signal
    if score >= 60:
        return "#22c55e"  # green
    if score >= 40:
        return "#eab308"  # yellow
    if score >= 20:
        return "#f97316"  # orange
    return "#6b7280"  # gray — noise


def _signal_label(score: float) -> str:
    """Map signal score to a human label."""
    if score >= 80:
        return "🔥 High Signal"
    if score >= 60:
        return "⚡ Strong"
    if score >= 40:
        return "📊 Moderate"
    if score >= 20:
        return "💨 Weak"
    return "🔇 Noise"


def _render_source_badge(source: str) -> str:
    """Render a source badge with emoji."""
    return _SOURCE_BADGES.get(source, f"🕷️ {source}")


def nightcrawler_view() -> None:
    """Night Crawlers dashboard: crawler status, live feed, heuristics, trigger."""
    st.header("Night Crawlers")
    st.caption(
        "Army of data miners and web spiders continuously feeding the engine "
        "with market data, social signals, on-chain analytics, whale movements, "
        "and narrative momentum. Self-adjusting heuristics learn which sources "
        "provide the most actionable signals."
    )

    # ── Live Activity Feed ────────────────────────────────────────────────
    st.subheader("📡 Live Activity Feed")
    st.caption(
        "Real-time items being collected as they arrive. "
        "Color-coded by signal strength. Auto-refreshes every 30s."
    )

    activities = api_get("/nightcrawlers/activity", params={"limit": 50})
    if activities:
        # Summary metrics
        total_items = sum(a.get("item_count", 0) for a in activities)
        high_signal = sum(1 for a in activities if a.get("signal_score", 0) >= 60)
        unique_sources = len({a.get("source", "") for a in activities})
        unique_tokens = set()
        for a in activities:
            for t in a.get("token_mentions", []):
                unique_tokens.add(t)

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total Items", total_items)
        m2.metric("High Signal", high_signal)
        m3.metric("Active Sources", unique_sources)
        m4.metric("Tokens Mentioned", len(unique_tokens))

        st.divider()

        # Activity cards
        for activity in activities[:20]:
            source = activity.get("source", "unknown")
            signal_score = activity.get("signal_score", 0)
            platform = activity.get("platform", "")
            item_count = activity.get("item_count", 0)
            observed = activity.get("observed_at", "")
            tokens = activity.get("token_mentions", [])
            engagement = activity.get("total_engagement", 0)

            color = _signal_color(signal_score)
            badge = _render_source_badge(source)
            label = _signal_label(signal_score)

            with st.container(border=True):
                cols = st.columns([3, 1, 1, 1])
                with cols[0]:
                    st.markdown(
                        f"**{badge}** <span style='color:{color};font-weight:bold'>{label}</span>",
                        unsafe_allow_html=True,
                    )
                    if platform:
                        st.caption(f"Platform: {platform}")
                with cols[1]:
                    st.metric("Items", item_count)
                with cols[2]:
                    st.metric("Engagement", engagement)
                with cols[3]:
                    if tokens:
                        token_str = ", ".join(f"`{t}`" for t in tokens[:4])
                        st.markdown(f"**Tokens:** {token_str}", unsafe_allow_html=True)
                    if observed:
                        st.caption(f"⏱️ {observed[-8:]}")

        st.divider()
    else:
        st.info("No activity yet. Trigger a crawl pass or wait for the next scheduled run.")

    # ── Crawler Fleet Status ──────────────────────────────────────────────
    st.subheader("🕷️ Crawler Fleet Status")
    status = api_get("/nightcrawlers/status")
    if status:
        rows = []
        for name, info in status.items():
            reliability = info.get("reliability", 0)
            if reliability >= 0.9:
                status_icon = "🟢"
            elif reliability >= 0.7:
                status_icon = "🟡"
            else:
                status_icon = "🔴"
            rows.append(
                {
                    "Status": status_icon,
                    "Crawler": _render_source_badge(name),
                    "Reliability": f"{reliability:.1%}",
                    "Total Runs": info.get("total_runs", 0),
                    "Total Items": info.get("total_items", 0),
                    "Error Rate": f"{info.get('error_rate', 0):.1%}",
                    "Last Run": str(info.get("last_run", "never"))[:19],
                    "Freq Multiplier": f"{info.get('frequency_multiplier', 1.0):.2f}x",
                }
            )
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("Crawler status unavailable. Start the engine to see fleet status.")

    st.divider()

    # ── Self-Adjusting Heuristics ─────────────────────────────────────────
    st.subheader("🧠 Self-Adjusting Heuristics")
    heuristics = api_get("/nightcrawlers/heuristics")
    if heuristics:
        col1, col2 = st.columns(2)
        with col1:
            st.metric("Sources Tracked", heuristics.get("sources_tracked", 0))
        with col2:
            st.metric("Patterns Learned", heuristics.get("patterns_learned", 0))

        source_rels = heuristics.get("source_reliabilities", {})
        if source_rels:
            st.markdown("**Source Reliability**")
            rel_rows = [
                {
                    "Source": _render_source_badge(name),
                    "Actionability": f"{info.get('actionability_rate', 0):.1%}",
                    "Recommendation": info.get("recommendation", "unknown"),
                    "Freq Multiplier": f"{info.get('frequency_multiplier', 1.0):.2f}x",
                }
                for name, info in source_rels.items()
            ]
            st.dataframe(pd.DataFrame(rel_rows), use_container_width=True, hide_index=True)

        top_patterns = heuristics.get("top_patterns", [])
        if top_patterns:
            st.markdown("**Top Learned Patterns**")
            pat_rows = [
                {
                    "Pattern": p.get("key", ""),
                    "Success Rate": f"{p.get('success_rate', 0):.1%}",
                    "Confidence": f"{p.get('confidence', 0):.1%}",
                    "Occurrences": p.get("occurrences", 0),
                }
                for p in top_patterns
            ]
            st.dataframe(pd.DataFrame(pat_rows), use_container_width=True, hide_index=True)
    else:
        st.info("Heuristics data will appear after the first crawl pass.")

    st.divider()

    # ── Night Crawler Control ─────────────────────────────────────────────
    st.subheader("⚙️ Night Crawler Control")
    st.caption("Manually trigger a full crawl pass across all sources.")
    if st.button("🕷️ Run Night Crawlers Now", type="primary", use_container_width=False):
        result = api_post("/engine/nightcrawlers")
        if result:
            if result.get("status") == "accepted":
                st.success(f"✅ {result.get('message', 'Night Crawler pipeline started')}")
            else:
                st.warning(f"⚠️ {result.get('message', 'Rejected')}")
