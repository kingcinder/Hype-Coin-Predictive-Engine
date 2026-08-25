"""Custom CSS and branding for the Serpent Circle Command Center GUI.

Dark theme with neon-green accent, monospace typography, and polished
micro-interactions. Loaded once via st.markdown in the app entrypoint.
"""

SERPENT_CSS = """
<style>
/* ── Serpent Circle Branding ─────────────────────────────────────────── */
:root {
    --sc-green: #00ff88;
    --sc-green-dim: #00cc6a;
    --sc-red: #ff4444;
    --sc-orange: #ff8800;
    --sc-yellow: #ffcc00;
    --sc-bg: #0e1117;
    --sc-surface: #1a1f2e;
    --sc-surface-hover: #252b3d;
    --sc-border: #2d3348;
    --sc-text: #e0e0e0;
    --sc-text-dim: #8b95a5;
    --sc-glow: 0 0 20px rgba(0, 255, 136, 0.15);
}

/* ── Header branding ────────────────────────────────────────────────── */
header[data-testid="stHeader"] {
    background: linear-gradient(135deg, #0e1117 0%, #1a1f2e 100%);
    border-bottom: 1px solid var(--sc-border);
}

header[data-testid="stHeader"] h1,
header[data-testid="stHeader"] h2,
header[data-testid="stHeader"] h3 {
    color: var(--sc-green) !important;
    text-shadow: 0 0 10px rgba(0, 255, 136, 0.3);
}

/* ── Sidebar branding ───────────────────────────────────────────────── */
section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #0e1117 0%, #141924 100%);
    border-right: 1px solid var(--sc-border);
}

section[data-testid="stSidebar"] .stMarkdown h1,
section[data-testid="stSidebar"] .stMarkdown h2,
section[data-testid="stSidebar"] .stMarkdown h3 {
    color: var(--sc-green) !important;
}

/* ── Metrics cards ──────────────────────────────────────────────────── */
[data-testid="stMetric"] {
    background: var(--sc-surface);
    border: 1px solid var(--sc-border);
    border-radius: 8px;
    padding: 12px 16px;
    transition: all 0.2s ease;
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.2);
}

[data-testid="stMetric"]:hover {
    border-color: var(--sc-green);
    box-shadow: var(--sc-glow);
    transform: translateY(-1px);
}

[data-testid="stMetricValue"] {
    color: var(--sc-text) !important;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
}

[data-testid="stMetricLabel"] {
    color: var(--sc-text-dim) !important;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    font-size: 0.75rem;
}

/* ── Buttons ────────────────────────────────────────────────────────── */
.stButton > button {
    background: linear-gradient(135deg, var(--sc-green-dim) 0%, var(--sc-green) 100%);
    color: #0e1117;
    border: none;
    border-radius: 6px;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-weight: 600;
    letter-spacing: 0.5px;
    transition: all 0.2s ease;
    box-shadow: 0 2px 8px rgba(0, 255, 136, 0.2);
}

.stButton > button:hover {
    transform: translateY(-1px);
    box-shadow: 0 4px 16px rgba(0, 255, 136, 0.3);
}

.stButton > button:active {
    transform: translateY(0);
    box-shadow: 0 1px 4px rgba(0, 255, 136, 0.2);
}

/* ── DataFrames ─────────────────────────────────────────────────────── */
.stDataFrame {
    border: 1px solid var(--sc-border);
    border-radius: 6px;
    overflow: hidden;
}

.stDataFrame table {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 0.8rem;
}

/* ── Expanders ──────────────────────────────────────────────────────── */
.streamlit-expanderHeader {
    background: var(--sc-surface) !important;
    border: 1px solid var(--sc-border) !important;
    border-radius: 6px;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    color: var(--sc-text) !important;
    transition: all 0.2s ease;
}

.streamlit-expanderHeader:hover {
    border-color: var(--sc-green) !important;
    box-shadow: var(--sc-glow);
}

/* ── Tabs ───────────────────────────────────────────────────────────── */
.stTabs [data-baseweb="tab-list"] {
    gap: 2px;
    background: var(--sc-surface);
    border-radius: 6px;
    padding: 4px;
}

.stTabs [data-baseweb="tab"] {
    background: transparent;
    color: var(--sc-text-dim);
    border-radius: 4px;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    transition: all 0.2s ease;
}

.stTabs [aria-selected="true"] {
    background: var(--sc-green-dim) !important;
    color: #0e1117 !important;
}

/* ── Progress bars ──────────────────────────────────────────────────── */
.stProgress > div > div {
    background: linear-gradient(90deg, var(--sc-green-dim), var(--sc-green));
}

/* ── Dividers ───────────────────────────────────────────────────────── */
hr {
    border-color: var(--sc-border) !important;
}

/* ── Alerts / info boxes ────────────────────────────────────────────── */
.stAlert {
    border-radius: 6px;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
}

/* ── Scrollbar styling ──────────────────────────────────────────────── */
::-webkit-scrollbar {
    width: 6px;
    height: 6px;
}

::-webkit-scrollbar-track {
    background: var(--sc-bg);
}

::-webkit-scrollbar-thumb {
    background: var(--sc-border);
    border-radius: 3px;
}

::-webkit-scrollbar-thumb:hover {
    background: var(--sc-green-dim);
}

/* ── Global body ────────────────────────────────────────────────────── */
.stApp {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
}

/* ── Risk band colors (applied via Streamlit column_config or JS) ────── */
/* Note: CSS :contains() is not standard. Risk band colors are applied via */
/* Streamlit's column_config or inline styling in the Python code. */

/* ── Subtle pulse animation for live elements ──────────────────────── */
@keyframes sc-pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.7; }
}

.sc-live {
    animation: sc-pulse 2s ease-in-out infinite;
}

.sc-live::before {
    content: "●";
    color: var(--sc-green);
    margin-right: 6px;
    font-size: 0.8em;
}

/* ── Fade-in animation for cards ────────────────────────────────────── */
@keyframes sc-fadein {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
}

.sc-fadein {
    animation: sc-fadein 0.3s ease-out;
}
</style>
"""


def load_branding() -> None:
    """Inject Serpent Circle branding CSS into the Streamlit page."""
    import streamlit as st

    st.markdown(SERPENT_CSS, unsafe_allow_html=True)
