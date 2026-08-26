"""Night Crawler GUI view functions for Streamlit."""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape as html_escape

import pandas as pd
import streamlit as st

from ui.api_client import api_get, api_post

# Source -> emoji/label mapping for badges
_SOURCE_BADGES: dict[str, str] = {
    "coingecko": "🦎 CoinGecko",
    "pump_fun": "🚀 PumpFun",
    "defillama": "🦙 DeFiLlama",
    "whale_tracker": "🐋 Whale Tracker",
    "explorer": "🔍 Explorer",
    "nitter": "🐦 Twitter/X",
    "presale": "🏷️ Presale",
    "farcaster": "🟣 Farcaster",
    "cmc": "📊 CoinMarketCap",
    "dexscreener": "📈 DexScreener",
    "birdeye": "🦅 Birdeye",
    "jupiter": "🪐 Jupiter",
    "coinmarketcap": "📊 CoinMarketCap",
    "gas_tracker": "⛽ Gas Tracker",
    "coinpaprika": "🌶️ CoinPaprika",
    "github_trending": "💻 GitHub Trending",
    "x_trends": "🐦 X Trends",
    "pump_portal": "🎯 PumpPortal",
    "dexscreener_trends": "📈 DexScreener Trends",
    "google_trends": "🔍 Google Trends",
}


def _signal_color(score: float) -> str:
    """Map signal score to a display color."""
    if score >= 80:
        return "#00ff88"
    if score >= 60:
        return "#22c55e"
    if score >= 40:
        return "#eab308"
    if score >= 20:
        return "#f97316"
    return "#6b7280"


def _signal_bg(score: float) -> str:
    """Map signal score to a subtle background tint."""
    if score >= 80:
        return "rgba(0,255,136,0.08)"
    if score >= 60:
        return "rgba(34,197,94,0.06)"
    if score >= 40:
        return "rgba(234,179,8,0.06)"
    if score >= 20:
        return "rgba(249,115,22,0.06)"
    return "rgba(107,114,128,0.04)"


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
    return _SOURCE_BADGES.get(source, f"🕷️ {source.replace('_', ' ').title()}")


def _time_ago(iso_str: str) -> str:
    """Convert an ISO timestamp to a human-readable 'time ago' string."""
    if not iso_str:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(UTC)
        delta = now - dt
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return f"{seconds}s ago"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        if seconds < 86400:
            return f"{seconds // 3600}h ago"
        return f"{seconds // 86400}d ago"
    except (ValueError, TypeError):
        return iso_str[-8:] if len(iso_str) >= 8 else iso_str


def _sparkline(values: list[float], width: int = 8) -> str:
    """Render a Unicode block-character sparkline from a list of floats.

    Uses the 8-level block elements: ``\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588``
    Right-aligns the series so the most recent week is always the
    last character. Never upsamples -- if fewer values than
    ``width``, only the trailing values are shown.
    """
    blocks = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
    if not values:
        return ""
    # Right-align: take the last ``width`` values
    if len(values) > width:
        values = values[-width:]
    lo = min(values)
    hi = max(values)
    span = hi - lo
    if span == 0:
        # All values identical -- map the absolute value to a block
        idx = min(7, int(values[0] / 100 * 7)) if values[0] > 0 else 0
        return blocks[idx] * len(values)
    return "".join(blocks[min(7, int((v - lo) / span * 7))] for v in values)


def _snr_color(snr_pct: float) -> str:
    """Map SNR percentage to a display color."""
    if snr_pct >= 60:
        return "#00ff88"
    if snr_pct >= 40:
        return "#22c55e"
    if snr_pct >= 25:
        return "#eab308"
    if snr_pct >= 10:
        return "#f97316"
    return "#6b7280"


# -- Custom CSS for the activity feed -----------------------------------------

_ACTIVITY_FEED_CSS = """
<style>
.activity-card {
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 10px;
    padding: 14px 16px;
    margin-bottom: 10px;
    transition: border-color 0.2s, box-shadow 0.2s, transform 0.15s;
    cursor: default;
}
.activity-card:hover {
    border-color: rgba(0,255,136,0.3);
    box-shadow: 0 2px 12px rgba(0,255,136,0.08);
    transform: translateY(-1px);
}
.source-badge {
    display: inline-block;
    background: rgba(255,255,255,0.07);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 20px;
    padding: 3px 10px;
    font-size: 0.82rem;
    font-weight: 600;
    white-space: nowrap;
}
.signal-bar {
    height: 4px;
    border-radius: 2px;
    margin-top: 6px;
    transition: width 0.4s ease;
}
@keyframes pulse {
    0% { opacity: 1; }
    50% { opacity: 0.4; }
    100% { opacity: 1; }
}
.refresh-pulse {
    animation: pulse 1.5s ease-in-out infinite;
    display: inline-block;
}
.token-chip {
    display: inline-block;
    background: rgba(139,92,246,0.15);
    border: 1px solid rgba(139,92,246,0.3);
    border-radius: 6px;
    padding: 2px 8px;
    font-size: 0.78rem;
    font-weight: 600;
    color: #c4b5fd;
    margin-right: 4px;
}
/* Leaderboard styles */
.lb-row {
    display: flex;
    align-items: center;
    padding: 10px 14px;
    border-bottom: 1px solid rgba(255,255,255,0.04);
    transition: background 0.15s;
}
.lb-row:hover {
    background: rgba(255,255,255,0.03);
}
.lb-rank {
    width: 36px;
    font-size: 1.1rem;
    font-weight: 700;
    text-align: center;
    flex-shrink: 0;
}
.lb-rank-1 { color: #ffd700; }
.lb-rank-2 { color: #c0c0c0; }
.lb-rank-3 { color: #cd7f32; }
.lb-sparkline {
    font-size: 1.3rem;
    letter-spacing: 1px;
    font-family: monospace;
    flex-shrink: 0;
    margin: 0 12px;
}
.lb-meta {
    font-size: 0.78rem;
    color: #9ca3af;
    margin-left: auto;
    text-align: right;
    flex-shrink: 0;
}
</style>
"""


def _cached_leaderboard(lookback: int) -> dict | None:
    """Fetch leaderboard data with 60s cache to avoid repeated queries."""

    @st.cache_data(ttl=60)
    def _fetch(d: int) -> dict | None:
        return api_get("/nightcrawlers/leaderboard", params={"days": d})

    return _fetch(lookback)


def _build_activity_card(
    source: str,
    signal_score: float,
    platform: str,
    item_count: int,
    observed: str,
    tokens: list[str],
    engagement: int,
) -> str:
    """Build HTML for a single activity card."""
    color = _signal_color(signal_score)
    bg = _signal_bg(signal_score)
    badge = _render_source_badge(source)
    label = _signal_label(signal_score)
    emoji = label.split()[0] if label else ""
    time_ago = _time_ago(observed)
    bar_width = min(signal_score, 100)

    # Token chips (escaped for XSS safety)
    token_html = ""
    for t in tokens[:5]:
        safe_t = html_escape(t)
        token_html += f'<span class="token-chip">{safe_t}</span>'
    if len(tokens) > 5:
        token_html += f'<span class="token-chip">+{len(tokens) - 5}</span>'

    # Platform span (escaped for XSS safety)
    plat_html = ""
    if platform:
        safe_plat = html_escape(platform)
        plat_html = (
            f"<span style='color:gray;font-size:0.78rem;margin-left:8px;'>{safe_plat}</span>"
        )

    parts = [
        f'<div class="activity-card" style="background:{bg};">',
        '<div style="display:flex;justify-content:space-between;align-items:center;">',
        f"<div><span class='source-badge'>{badge}</span>{plat_html}</div>",
        '<div style="text-align:right;">',
        f"<span style='color:{color};font-weight:700;font-size:0.95rem;'>{emoji} {label}</span>",
        f"<span style='color:gray;font-size:0.75rem;margin-left:8px;'>{time_ago}</span>",
        "</div>",
        "</div>",
        f"<div class=\"signal-bar\" style='width:{bar_width}%;background:{color};'></div>",
        '<div style="display:flex;justify-content:space-between;'
        'margin-top:8px;font-size:0.82rem;">',
        f"<span style='color:#9ca3af;'>"
        f"\U0001f4e5 {item_count} items "
        f"\u00b7 \U0001f4ac {engagement} engagement</span>",
        f"<span>{token_html}</span>",
        "</div>",
        "</div>",
    ]
    return "\n".join(parts)


@st.fragment(run_every=30)
def _live_activity_feed() -> None:
    """Auto-refreshing live crawler activity feed -- refreshes every 30s."""
    st.markdown(_ACTIVITY_FEED_CSS, unsafe_allow_html=True)

    col_title, col_refresh = st.columns([5, 1])
    with col_title:
        st.subheader("\U0001f4e1 Live Activity Feed")
    with col_refresh:
        st.markdown(
            "<span class='refresh-pulse' style='color:#00ff88;font-size:0.8rem'>● LIVE</span>",
            unsafe_allow_html=True,
        )

    st.caption(
        "Real-time items being collected as they arrive. "
        "Color-coded by signal strength. New items appear instantly via WebSocket."
    )

    # Inject the WebSocket bridge JavaScript for live updates
    from ui.api_client import API_BASE_URL

    st.markdown(_activity_ws_bridge_js(API_BASE_URL), unsafe_allow_html=True)

    # Fetch activity data ONCE (shared between filter and display)
    activities = api_get("/nightcrawlers/activity", params={"limit": 200}) or []

    # Merge any WebSocket-cached items from localStorage
    try:
        ws_items_raw = st.session_state.get("_ws_activity_items")
        if ws_items_raw:
            import json as _json

            ws_items = _json.loads(ws_items_raw) if isinstance(ws_items_raw, str) else ws_items_raw
            if ws_items:
                # Merge WS items into activities, deduplicating by source+observed
                seen = {(a.get("source", ""), a.get("observed_at", "")) for a in activities}
                for item in ws_items:
                    if item.get("type") == "activity":
                        key = (item.get("source", ""), item.get("observed_at", ""))
                        if key not in seen:
                            seen.add(key)
                            activities.append(
                                {
                                    "source": item.get("source", "unknown"),
                                    "platform": item.get("platform", ""),
                                    "item_count": item.get("item_count", 0),
                                    "observed_at": item.get("observed_at", ""),
                                    "signal_score": item.get("signal_score", 0),
                                    "token_mentions": item.get("token_mentions", []),
                                    "total_engagement": item.get("total_engagement", 0),
                                },
                            )
                # Sort by observed_at descending after merging
                activities.sort(key=lambda a: a.get("observed_at", ""), reverse=True)
    except Exception:  # noqa: BLE001
        pass  # WS merge is best-effort

    # Build filter options from fetched data
    source_options = ["All Sources"] + sorted(
        set(_SOURCE_BADGES.get(a.get("source", ""), a.get("source", "")) for a in activities)
    )
    platform_options = ["All Platforms"] + sorted(
        set(a.get("platform", "") for a in activities if a.get("platform"))
    )

    # Sidebar filter controls
    with st.sidebar:
        st.subheader("\U0001f50d Activity Filters")

        min_signal = st.slider(
            "Min signal score",
            min_value=0,
            max_value=100,
            value=0,
            step=10,
            key="nc_min_signal",
            help="Filter activity by minimum signal score",
        )
        source_filter = st.selectbox(
            "Source",
            options=source_options,
            key="nc_source_filter",
        )
        platform_filter = st.selectbox(
            "Platform",
            options=platform_options,
            key="nc_platform_filter",
        )
        max_items = st.selectbox(
            "Show",
            options=[10, 20, 50, 100],
            index=1,
            key="nc_max_items",
        )

        # Active filter summary
        active_filters = []
        if min_signal > 0:
            active_filters.append(f"Signal \u2265 {min_signal}")
        if source_filter != "All Sources":
            active_filters.append(f"Source: {source_filter}")
        if platform_filter != "All Platforms":
            active_filters.append(f"Platform: {platform_filter}")
        if active_filters:
            st.caption("Active: " + " \u00b7 ".join(active_filters))
        else:
            st.caption("No filters active")

    if not activities:
        st.info(
            "No activity yet. Click **\U0001f577\ufe0f "
            "Run Night Crawlers Now** below, or wait for the "
            "next scheduled crawl pass."
        )
        return

    # Apply filters
    filtered = activities
    if min_signal > 0:
        filtered = [a for a in filtered if a.get("signal_score", 0) >= min_signal]
    if source_filter != "All Sources":
        source_key = next(
            (k for k, v in _SOURCE_BADGES.items() if v == source_filter),
            source_filter,
        )
        filtered = [a for a in filtered if a.get("source", "") == source_key]
    if platform_filter != "All Platforms":
        filtered = [a for a in filtered if a.get("platform", "") == platform_filter]

    filtered = filtered[:max_items]

    if not filtered:
        st.info("No activities match the current filters.")
        return

    # Summary metrics
    total_items = sum(a.get("item_count", 0) for a in filtered)
    high_signal = sum(1 for a in filtered if a.get("signal_score", 0) >= 60)
    unique_sources = len({a.get("source", "") for a in filtered})
    unique_tokens: set[str] = set()
    for a in filtered:
        for t in a.get("token_mentions", []):
            unique_tokens.add(t)

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric("\U0001f4e5 Total Items", total_items)
    with m2:
        pct = high_signal / max(len(filtered), 1) * 100
        st.metric(
            "\U0001f525 High Signal",
            high_signal,
            delta=f"{pct:.0f}%",
            delta_color="normal",
        )
    with m3:
        st.metric("\U0001f310 Active Sources", unique_sources)
    with m4:
        st.metric("\U0001fa99 Tokens Seen", len(unique_tokens))

    st.divider()

    # Activity cards
    for _idx, activity in enumerate(filtered):
        card = _build_activity_card(
            source=activity.get("source", "unknown"),
            signal_score=activity.get("signal_score", 0),
            platform=activity.get("platform", ""),
            item_count=activity.get("item_count", 0),
            observed=activity.get("observed_at", ""),
            tokens=activity.get("token_mentions", []),
            engagement=activity.get("total_engagement", 0),
        )
        st.markdown(card, unsafe_allow_html=True)

    # Last updated footer
    now = datetime.now(UTC).strftime("%H:%M:%S UTC")
    st.caption(
        f"<span style='color:#6b7280;font-size:0.75rem;'>"
        f"Last updated: {now} "
        f"&middot; Showing {len(filtered)}/{len(activities)} items"
        f"</span>",
        unsafe_allow_html=True,
    )


def _activity_ws_bridge_js(api_base: str) -> str:
    """Return JavaScript that maintains a persistent WebSocket connection to the
    activity stream and writes new items into localStorage so Streamlit can read
    them without making a new HTTP request on every rerun."""
    return (
        "<script>\n"
        "(function() {\n"
        "  const KEY = 'serpent_activity_ws';\n"
        "  let retryMs = 1000;\n"
        "  const MAX_RETRY = 30000;\n\n"
        "  function connect() {\n"
        "    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';\n"
        "    const wsUrl = protocol + '//' + window.location.host + '/ws/nightcrawlers/activity';\n"
        "    const ws = new WebSocket(wsUrl);\n\n"
        "    ws.onmessage = function(e) {\n"
        "      try {\n"
        "        const data = JSON.parse(e.data);\n"
        "        if (data.type === 'activity') {\n"
        "          const existing = JSON.parse(localStorage.getItem(KEY) || '[]');\n"
        "          const key = data.source + ':' + (data.observed_at || Date.now());\n"
        "          if (!existing.some(item => item._key === key)) {\n"
        "            data._key = key;\n"
        "            data._ts = Date.now();\n"
        "            existing.unshift(data);\n"
        "            localStorage.setItem(KEY, JSON.stringify(existing.slice(0, 200)));\n"
        "          }\n"
        "        }\n"
        "      } catch(err) { console.error('WS parse error', err); }\n"
        "    };\n\n"
        "    ws.onerror = function() {\n"
        "      ws.close();\n"
        "      retryMs = Math.min(retryMs * 2, MAX_RETRY);\n"
        "      setTimeout(connect, retryMs);\n"
        "    };\n\n"
        "    ws.onclose = function() {\n"
        "      retryMs = Math.min(retryMs * 2, MAX_RETRY);\n"
        "      setTimeout(connect, retryMs);\n"
        "    };\n\n"
        "    ws.onopen = function() { retryMs = 1000; };\n"
        "  }\n\n"
        "  if (!window._serpentActivityWSConnected) {\n"
        "    window._serpentActivityWSConnected = true;\n"
        "    connect();\n"
        "  }\n"
        "})();\n"
        "</script>"
    )


def _leaderboard_section() -> None:
    """Crawler performance leaderboard with sparkline trend charts."""
    st.subheader("\U0001f3c6 Crawler Performance Leaderboard")

    # Lookback selector
    lcol1, lcol2, _ = st.columns([1, 1, 4])
    with lcol1:
        lookback = st.selectbox(
            "Lookback",
            options=[7, 14, 30, 60, 90],
            index=2,
            key="lb_lookback",
            label_visibility="collapsed",
            help="Days to look back for SNR calculation",
        )
    with lcol2:
        st.caption(f"Last {lookback} days")

    data = _cached_leaderboard(lookback)
    if not isinstance(data, dict) or not data.get("entries"):
        st.info(
            "No leaderboard data yet. Run the Night Crawlers "
            "to start collecting performance metrics."
        )
        return

    entries = data["entries"]

    # Summary metrics
    total_sources = data.get("total_sources", len(entries))
    avg_snr = sum(e.get("snr_pct", 0) for e in entries) / max(total_sources, 1)
    best_source = entries[0] if entries else None

    scol1, scol2, scol3 = st.columns(3)
    with scol1:
        st.metric("🌐 Sources Ranked", total_sources)
    with scol2:
        st.metric(
            "📈 Avg SNR",
            f"{avg_snr:.1f}%",
            help="Average signal-to-noise ratio across all sources",
        )
    with scol3:
        if best_source:
            badge = _render_source_badge(best_source["source"])
            st.metric(
                "🏆 Top Source",
                badge,
                delta=f"{best_source['snr_pct']:.1f}% SNR",
            )

    st.divider()

    # Leaderboard table as styled HTML rows
    html_parts = [
        '<div style="border:1px solid rgba(255,255,255,0.06);border-radius:10px;overflow:hidden;">'
    ]

    # Header
    html_parts.append(
        '<div style="display:flex;padding:8px 14px;'
        "background:rgba(255,255,255,0.04);"
        "font-size:0.75rem;color:#9ca3af;"
        'text-transform:uppercase;letter-spacing:0.05em;">'
        '<span style="width:36px;text-align:center;">#</span>'
        '<span style="flex:1;">Source</span>'
        '<span style="width:120px;text-align:center;">SNR</span>'
        '<span style="width:140px;text-align:center;">'
        "30d Trend</span>"
        '<span style="width:90px;text-align:right;">Items</span>'
        '<span style="width:70px;text-align:right;">Reliability</span>'
        "</div>"
    )

    for entry in entries:
        rank = entry.get("rank", 0)
        source = entry.get("source", "unknown")
        snr_pct = entry.get("snr_pct", 0)
        sparkline_vals = entry.get("sparkline", [])
        total_items = entry.get("total_items", 0)
        reliability = entry.get("reliability", 0)

        badge = _render_source_badge(source)
        snr_c = _snr_color(snr_pct)
        spark_str = _sparkline(sparkline_vals, width=8)

        rank_cls = f"lb-rank-{rank}" if rank <= 3 else ""
        rank_display = (
            ["\U0001f947", "\U0001f948", "\U0001f949"][rank - 1] if rank <= 3 else str(rank)
        )

        # Reliability color
        if reliability >= 0.9:
            rel_color = "#00ff88"
        elif reliability >= 0.7:
            rel_color = "#eab308"
        else:
            rel_color = "#f97316"

        row = (
            f'<div class="lb-row">'
            f'<div class="lb-rank {rank_cls}">{rank_display}</div>'
            f'<div style="flex:1;"><span class="source-badge">'
            f"{badge}</span></div>"
            f'<div style="width:120px;text-align:center;">'
            f"<span style='color:{snr_c};font-weight:700;"
            f"font-size:0.95rem;'>{snr_pct:.1f}%</span></div>"
            f'<div class="lb-sparkline" '
            f"style='color:{snr_c};'>{spark_str}</div>"
            f'<div class="lb-meta" style="width:90px;">'
            f"{total_items:,}</div>"
            f'<div class="lb-meta" style="width:70px;">'
            f"<span style='color:{rel_color};'>"
            f"{reliability:.0%}</span></div>"
            f"</div>"
        )
        html_parts.append(row)

    html_parts.append("</div>")
    st.markdown("\n".join(html_parts), unsafe_allow_html=True)

    st.caption(
        f"<span style='color:#6b7280;font-size:0.75rem;'>"
        f"SNR = actionable items / total items. "
        f"Sparkline shows weekly SNR trend over the last "
        f"{lookback} days.</span>",
        unsafe_allow_html=True,
    )


def nightcrawler_view() -> None:
    """Night Crawlers dashboard: crawler status, live feed, heuristics, trigger."""
    st.header("Night Crawlers")
    st.caption(
        "Army of data miners and web spiders continuously feeding the engine "
        "with market data, social signals, on-chain analytics, whale "
        "movements, and narrative momentum. Self-adjusting heuristics learn "
        "which sources provide the most actionable signals."
    )

    # Live Activity Feed (auto-refreshing fragment)
    _live_activity_feed()

    st.divider()

    # Crawler Performance Leaderboard
    _leaderboard_section()

    st.divider()

    # Crawler Fleet Status
    st.subheader("\U0001f577\ufe0f Crawler Fleet Status")
    status = api_get("/nightcrawlers/status")
    if status:
        rows = []
        for name, info in status.items():
            reliability = info.get("reliability", 0)
            if reliability >= 0.9:
                status_icon = "\U0001f7e2"
            elif reliability >= 0.7:
                status_icon = "\U0001f7e1"
            else:
                status_icon = "\U0001f534"
            rows.append(
                {
                    "Status": status_icon,
                    "Crawler": _render_source_badge(name),
                    "Reliability": f"{reliability:.1%}",
                    "Total Runs": info.get("total_runs", 0),
                    "Total Items": info.get("total_items", 0),
                    "Error Rate": f"{info.get('error_rate', 0):.1%}",
                    "Last Run": str(info.get("last_run", "never"))[:19],
                    "Freq Multiplier": (f"{info.get('frequency_multiplier', 1.0):.2f}x"),
                }
            )
        if rows:
            st.dataframe(
                pd.DataFrame(rows),
                width="stretch",
                hide_index=True,
            )
    else:
        st.info("Crawler status unavailable. Start the engine to see fleet status.")

    st.divider()

    # Self-Adjusting Heuristics
    st.subheader("\U0001f9e0 Self-Adjusting Heuristics")
    heuristics = api_get("/nightcrawlers/heuristics")
    if heuristics:
        col1, col2 = st.columns(2)
        with col1:
            st.metric(
                "Sources Tracked",
                heuristics.get("sources_tracked", 0),
            )
        with col2:
            st.metric(
                "Patterns Learned",
                heuristics.get("patterns_learned", 0),
            )

        source_rels = heuristics.get("source_reliabilities", {})
        if source_rels:
            st.markdown("**Source Reliability**")
            rel_rows = [
                {
                    "Source": _render_source_badge(name),
                    "Actionability": (f"{info.get('actionability_rate', 0):.1%}"),
                    "Recommendation": info.get("recommendation", "unknown"),
                    "Freq Multiplier": (f"{info.get('frequency_multiplier', 1.0):.2f}x"),
                }
                for name, info in source_rels.items()
            ]
            st.dataframe(
                pd.DataFrame(rel_rows),
                width="stretch",
                hide_index=True,
            )

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
            st.dataframe(
                pd.DataFrame(pat_rows),
                width="stretch",
                hide_index=True,
            )
    else:
        st.info("Heuristics data will appear after the first crawl pass.")

    st.divider()

    # Night Crawler Control
    st.subheader("\u2699\ufe0f Night Crawler Control")
    st.caption("Manually trigger a full crawl pass across all sources.")
    if st.button(
        "\U0001f577\ufe0f Run Night Crawlers Now",
        type="primary",
        width="stretch",
    ):
        result = api_post("/engine/nightcrawlers")
        if result:
            if result.get("status") == "accepted":
                msg = result.get("message", "Night Crawler pipeline started")
                st.success(f"\u2705 {msg}")
            else:
                msg = result.get("message", "Rejected")
                st.warning(f"\u26a0\ufe0f {msg}")
