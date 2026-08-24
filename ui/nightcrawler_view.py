"""Night Crawler GUI view functions for Streamlit."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from ui.api_client import api_get, api_post


def nightcrawler_view() -> None:
    """Night Crawlers dashboard: crawler status, heuristics, trigger."""
    st.header("Night Crawlers")
    st.caption(
        "Army of data miners and web spiders continuously feeding the engine "
        "with market data, social signals, on-chain analytics, whale movements, "
        "and narrative momentum. Self-adjusting heuristics learn which sources "
        "provide the most actionable signals."
    )

    # Crawler status
    st.subheader("Crawler Fleet Status")
    status = api_get("/nightcrawlers/status")
    if status:
        rows = []
        for name, info in status.items():
            rows.append({
                "Crawler": name,
                "Reliability": f"{info.get('reliability', 0):.1%}",
                "Total Runs": info.get("total_runs", 0),
                "Total Items": info.get("total_items", 0),
                "Error Rate": f"{info.get('error_rate', 0):.1%}",
                "Last Run": str(info.get("last_run", "never"))[:19],
                "Freq Multiplier": f"{info.get('frequency_multiplier', 1.0):.2f}x",
            })
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("Crawler status unavailable. Start the engine to see fleet status.")

    st.divider()

    # Heuristics
    st.subheader("Self-Adjusting Heuristics")
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
                    "Source": name,
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

    # Trigger
    st.subheader("Night Crawler Control")
    st.caption("Manually trigger a full crawl pass across all sources.")
    if st.button("🕷️ Run Night Crawlers Now", type="primary", use_container_width=False):
        result = api_post("/engine/nightcrawlers")
        if result:
            if result.get("status") == "accepted":
                st.success(f"✅ {result.get('message', 'Night Crawler pipeline started')}")
            else:
                st.warning(f"⚠️ {result.get('message', 'Rejected')}")
