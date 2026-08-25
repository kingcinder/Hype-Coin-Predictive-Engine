"""Serpent Circle — Dark theme, branding, and micro-interactions for Streamlit GUI.

Inject via ``inject_theme()`` at the top of app.py before any Streamlit calls.
"""

from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

# ── Brand assets ────────────────────────────────────────────────────────────
BRAND_NAME = "Serpent Circle"
BRAND_TAGLINE = "Hype-Coin Predictive Engine"
BRAND_EMOJI = "🐍"
BRAND_COLOR = "#00ff88"  # venom green
BRAND_ACCENT = "#00d4ff"  # cyan accent
BRAND_DANGER = "#ff4444"
BRAND_WARN = "#ffaa00"
BRAND_SUCCESS = "#00ff88"
BRAND_SURFACE = "#1a1f2e"
BRAND_SURFACE_HOVER = "#232940"
BRAND_BORDER = "#2a3050"
BRAND_BORDER_LIGHT = "#3a4060"
BRAND_TEXT = "#e0e0e0"
BRAND_TEXT_DIM = "#8890a0"
BRAND_GLOW = "0 0 20px rgba(0, 255, 136, 0.15)"


# ── Plotly dark theme ───────────────────────────────────────────────────────
def plotly_dark_layout() -> dict:
    """Return a Plotly layout dict that matches the Serpent Circle dark theme."""
    return dict(
        paper_bgcolor="#0e1117",
        plot_bgcolor="#0e1117",
        font=dict(color=BRAND_TEXT, family="monospace"),
        xaxis=dict(gridcolor=BRAND_BORDER, zerolinecolor=BRAND_BORDER),
        yaxis=dict(gridcolor=BRAND_BORDER, zerolinecolor=BRAND_BORDER),
        margin=dict(l=20, r=20, t=40, b=20),
    )


def apply_dark_theme(fig: go.Figure) -> go.Figure:
    """Apply the Serpent Circle dark theme to a Plotly figure in-place."""
    fig.update_layout(**plotly_dark_layout())
    return fig


# ── Cached CSS injection ───────────────────────────────────────────────────
@st.cache_resource
def _cached_css() -> str:
    return _CSS


def inject_theme() -> None:
    """Inject Serpent Circle CSS into the Streamlit page. Cached across reruns."""
    st.markdown(_cached_css(), unsafe_allow_html=True)


_CSS = f"""
<style>
/* ═══════════════════════════════════════════════════════════════════════════
   Serpent Circle — Dark Theme & Micro-Interactions
   ═══════════════════════════════════════════════════════════════════════════ */

/* ── Global foundations ─────────────────────────────────────────────────── */
:root {{
    --sc-brand: {BRAND_COLOR};
    --sc-accent: {BRAND_ACCENT};
    --sc-danger: {BRAND_DANGER};
    --sc-warn: {BRAND_WARN};
    --sc-success: {BRAND_SUCCESS};
    --sc-surface: {BRAND_SURFACE};
    --sc-surface-hover: {BRAND_SURFACE_HOVER};
    --sc-border: {BRAND_BORDER};
    --sc-border-light: {BRAND_BORDER_LIGHT};
    --sc-text: {BRAND_TEXT};
    --sc-text-dim: {BRAND_TEXT_DIM};
    --sc-glow: {BRAND_GLOW};
    --sc-radius: 10px;
    --sc-transition: 0.2s cubic-bezier(0.4, 0, 0.2, 1);
}}

html {{ scroll-behavior: smooth; }}

.stApp {{
    font-family: 'JetBrains Mono', 'Fira Code', 'SF Mono', 'Cascadia Code', monospace;
}}

/* ── Header bar ────────────────────────────────────────────────────────── */
header[data-testid="stHeader"] {{
    background: linear-gradient(135deg, #0e1117 0%, #1a1f2e 100%);
    border-bottom: 1px solid var(--sc-border);
    box-shadow: 0 1px 8px rgba(0, 0, 0, 0.3);
}}

/* ── Sidebar ───────────────────────────────────────────────────────────── */
section[data-testid="stSidebar"] {{
    background: linear-gradient(180deg, #0e1117 0%, #111827 100%);
    border-right: 1px solid var(--sc-border);
}}

/* Sidebar brand header + footer */
.sc-sidebar-brand {{
    display: flex; align-items: center; gap: 10px;
    padding: 12px 8px 14px 8px;
    border-bottom: 1px solid var(--sc-border);
    margin-bottom: 6px;
}}
.sc-sidebar-brand .sc-logo {{
    font-size: 1.8rem; animation: serpentPulse 3s ease-in-out infinite;
}}
.sc-sidebar-brand-name {{
    font-weight: 800; font-size: 1.05rem; letter-spacing: 0.06em;
    background: linear-gradient(135deg, {BRAND_COLOR}, {BRAND_ACCENT});
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    background-clip: text;
}}
.sc-sidebar-brand-tag {{
    font-size: 0.68rem; color: var(--sc-text-dim); letter-spacing: 0.1em;
    text-transform: uppercase; margin-top: 2px;
}}

/* Nav section headers */
.sc-nav-section {{
    margin: 14px 4px 4px 4px;
    padding: 4px 10px;
    font-size: 0.66rem; font-weight: 700; letter-spacing: 0.14em;
    text-transform: uppercase; color: var(--sc-text-dim);
    border-left: 2px solid var(--sc-brand);
    background: linear-gradient(90deg, rgba(0,255,136,0.06), transparent);
}}

/* Sidebar footer */
.sc-sidebar-footer {{
    display: flex; align-items: center; justify-content: space-between;
    margin-top: 18px; padding: 10px 8px;
    border-top: 1px solid var(--sc-border);
}}
.sc-sidebar-ver {{
    font-size: 0.7rem; color: var(--sc-text-dim); letter-spacing: 0.08em;
}}

/* Status pills */
.sc-pill {{
    display: inline-block; padding: 3px 10px; border-radius: 20px;
    font-size: 0.66rem; font-weight: 700; letter-spacing: 0.08em;
    text-transform: uppercase;
}}
.sc-pill-ok {{ background: rgba(0,255,136,0.14); color: {BRAND_SUCCESS}; }}
.sc-pill-warn {{ background: rgba(255,170,0,0.14); color: {BRAND_WARN}; }}
.sc-pill-error {{ background: rgba(255,68,68,0.14); color: {BRAND_DANGER}; }}

section[data-testid="stSidebar"] .stRadio > div > label {{
    transition: all var(--sc-transition);
    border-radius: var(--sc-radius);
    padding: 6px 10px;
    margin: 1px 0;
}}

section[data-testid="stSidebar"] .stRadio > div > label:hover {{
    background: var(--sc-surface-hover);
    box-shadow: inset 2px 0 0 var(--sc-brand);
}}

section[data-testid="stSidebar"] .stRadio > div > label[data-checked="true"] {{
    background: rgba(0, 255, 136, 0.08);
    box-shadow: inset 2px 0 0 var(--sc-brand);
    border-left: none;
}}

/* ── Main content ──────────────────────────────────────────────────────── */
.stApp {{ background: #0e1117; }}
.block-container {{ padding-top: 2rem; max-width: 1200px; }}

/* ── Headers ───────────────────────────────────────────────────────────── */
h1, h2, h3 {{ color: var(--sc-text) !important; font-weight: 600; letter-spacing: -0.02em; }}
h1 {{ border-bottom: 2px solid var(--sc-brand); padding-bottom: 8px; text-shadow: 0 0 30px rgba(0, 255, 136, 0.1); }}

/* ── Metric cards ──────────────────────────────────────────────────────── */
[data-testid="stMetric"] {{
    background: var(--sc-surface);
    border: 1px solid var(--sc-border);
    border-radius: var(--sc-radius);
    padding: 16px 20px;
    transition: all var(--sc-transition);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
}}
[data-testid="stMetric"]:hover {{
    border-color: var(--sc-brand);
    box-shadow: var(--sc-glow);
    transform: translateY(-1px);
}}
[data-testid="stMetricLabel"] {{ color: var(--sc-text-dim) !important; font-size: 0.8rem !important; text-transform: uppercase; letter-spacing: 0.08em; }}
[data-testid="stMetricValue"] {{ color: var(--sc-text) !important; font-weight: 700 !important; font-size: 1.5rem !important; }}

/* ── DataFrames ────────────────────────────────────────────────────────── */
.stDataFrame {{ border: 1px solid var(--sc-border); border-radius: var(--sc-radius); overflow: hidden; }}
.stDataFrame th {{ background: var(--sc-surface) !important; color: var(--sc-text-dim) !important; text-transform: uppercase; font-size: 0.7rem !important; letter-spacing: 0.1em; border-bottom: 2px solid var(--sc-brand) !important; padding: 10px 12px !important; }}
.stDataFrame td {{ border-bottom: 1px solid var(--sc-border) !important; padding: 8px 12px !important; transition: background var(--sc-transition); }}
.stDataFrame tr:hover td {{ background: var(--sc-surface-hover) !important; }}

/* ── Buttons ───────────────────────────────────────────────────────────── */
.stButton > button {{ border-radius: var(--sc-radius); font-weight: 600; letter-spacing: 0.03em; transition: all var(--sc-transition); border: 1px solid var(--sc-border); background: var(--sc-surface); color: var(--sc-text); padding: 8px 20px; }}
.stButton > button:hover {{ border-color: var(--sc-brand); box-shadow: var(--sc-glow); transform: translateY(-1px); color: var(--sc-brand); }}
.stButton > button:active {{ transform: translateY(0); box-shadow: none; }}

.stButton > button[kind="primary"],
.stButton > button[data-testid="stBaseButton-primary"] {{
    background: linear-gradient(135deg, {BRAND_COLOR}, #00cc6a);
    color: #0e1117; border: none; font-weight: 700;
    box-shadow: 0 2px 12px rgba(0, 255, 136, 0.3);
}}
.stButton > button[kind="primary"]:hover,
.stButton > button[data-testid="stBaseButton-primary"]:hover {{
    box-shadow: 0 4px 20px rgba(0, 255, 136, 0.4); transform: translateY(-2px);
}}

.stFormSubmitButton > button {{
    background: linear-gradient(135deg, {BRAND_COLOR}, #00cc6a);
    color: #0e1117; border: none; font-weight: 700;
    box-shadow: 0 2px 12px rgba(0, 255, 136, 0.3);
}}
.stFormSubmitButton > button:hover {{ box-shadow: 0 4px 20px rgba(0, 255, 136, 0.4); transform: translateY(-2px); }}

/* ── Alerts ────────────────────────────────────────────────────────────── */
.stAlert {{ border-radius: var(--sc-radius); border-left: 4px solid; animation: slideIn 0.3s cubic-bezier(0.4, 0, 0.2, 1); }}
@keyframes slideIn {{ from {{ opacity: 0; transform: translateX(-10px); }} to {{ opacity: 1; transform: translateX(0); }} }}
.stAlert[data-type="info"] {{ background: rgba(0, 212, 255, 0.08); border-color: {BRAND_ACCENT}; }}
.stAlert[data-type="success"] {{ background: rgba(0, 255, 136, 0.08); border-color: {BRAND_SUCCESS}; }}
.stAlert[data-type="warning"] {{ background: rgba(255, 170, 0, 0.08); border-color: {BRAND_WARN}; }}
.stAlert[data-type="error"] {{ background: rgba(255, 68, 68, 0.08); border-color: {BRAND_DANGER}; }}

/* ── Expanders ─────────────────────────────────────────────────────────── */
[data-testid="stExpander"] {{ border: 1px solid var(--sc-border); border-radius: var(--sc-radius); background: var(--sc-surface); transition: all var(--sc-transition); }}
[data-testid="stExpander"]:hover {{ border-color: var(--sc-border-light); }}

/* ── Progress bars ─────────────────────────────────────────────────────── */
.stProgress > div > div {{ background: var(--sc-surface); border-radius: 6px; overflow: hidden; }}
.stProgress > div > div > div {{ background: linear-gradient(90deg, {BRAND_COLOR}, {BRAND_ACCENT}) !important; border-radius: 6px; box-shadow: 0 0 10px rgba(0, 255, 136, 0.3); }}

/* ── Tabs ──────────────────────────────────────────────────────────────── */
.stTabs [data-baseweb="tab-list"] {{ gap: 4px; background: var(--sc-surface); border-radius: var(--sc-radius); padding: 4px; }}
.stTabs [data-baseweb="tab"] {{ border-radius: 8px; color: var(--sc-text-dim); font-weight: 500; transition: all var(--sc-transition); border: none; }}
.stTabs [data-baseweb="tab"]:hover {{ color: var(--sc-text); background: var(--sc-surface-hover); }}
.stTabs [aria-selected="true"] {{ color: var(--sc-brand) !important; background: rgba(0, 255, 136, 0.1) !important; border-bottom: none !important; font-weight: 700; }}

/* ── Select boxes ──────────────────────────────────────────────────────── */
.stSelectbox > div > div {{ border-color: var(--sc-border); border-radius: var(--sc-radius); transition: border-color var(--sc-transition); }}
.stSelectbox > div > div:hover {{ border-color: var(--sc-brand); }}

/* ── Text / number / date / textarea inputs ───────────────────────────── */
.stTextInput input, .stNumberInput input, .stDateInput input, .stTextArea textarea {{
    background: var(--sc-surface) !important;
    border: 1px solid var(--sc-border) !important;
    border-radius: var(--sc-radius) !important;
    color: var(--sc-text) !important;
    transition: all var(--sc-transition);
}}
.stTextInput input:focus, .stNumberInput input:focus, .stDateInput input:focus,
.stTextArea textarea:focus {{
    border-color: var(--sc-brand) !important;
    box-shadow: 0 0 0 3px rgba(0, 255, 136, 0.15) !important;
}}
.stTextInput input::placeholder, .stTextArea textarea::placeholder {{
    color: var(--sc-text-dim) !important;
}}

/* ── Multiselect ──────────────────────────────────────────────────────── */
.stMultiSelect [data-baseweb="select"] {{ background: var(--sc-surface) !important; border-color: var(--sc-border) !important; border-radius: var(--sc-radius) !important; }}
.stMultiSelect [data-baseweb="select"]:hover {{ border-color: var(--sc-brand) !important; }}

/* ── Radio & checkbox ─────────────────────────────────────────────────── */
.stRadio label, .stCheckbox label {{ color: var(--sc-text) !important; }}
.stCheckbox [data-testid="stCheckbox"] {{ accent-color: var(--sc-brand); }}

/* ── Code blocks ──────────────────────────────────────────────────────── */
.stCodeBlock pre {{ background: var(--sc-surface) !important; border: 1px solid var(--sc-border); border-radius: var(--sc-radius); color: var(--sc-text) !important; }}

/* ── Spinner ──────────────────────────────────────────────────────────── */
[data-testid="stSpinner"] {{ color: var(--sc-brand); }}
[data-testid="stSpinner"] svg {{ stroke: var(--sc-brand) !important; }}

/* ── Info/Success/Warning/Error inside bordered containers ────────────── */
[data-testid="stAlert"] {{ border-left-width: 4px; }}

/* ── Plotly charts ─────────────────────────────────────────────────────── */
.stPlotlyChart {{ border: 1px solid var(--sc-border); border-radius: var(--sc-radius); overflow: hidden; }}

/* ── JSON display ──────────────────────────────────────────────────────── */
.stJson {{ background: var(--sc-surface) !important; border: 1px solid var(--sc-border); border-radius: var(--sc-radius); padding: 16px; }}

/* ── Dividers ──────────────────────────────────────────────────────────── */
hr {{ border: none; border-top: 1px solid var(--sc-border); margin: 1.5rem 0; }}

/* ── Scrollbar ─────────────────────────────────────────────────────────── */
::-webkit-scrollbar {{ width: 8px; height: 8px; }}
::-webkit-scrollbar-track {{ background: #0e1117; }}
::-webkit-scrollbar-thumb {{ background: var(--sc-border-light); border-radius: 4px; }}
::-webkit-scrollbar-thumb:hover {{ background: var(--sc-text-dim); }}

/* ── Animations ────────────────────────────────────────────────────────── */
@keyframes serpentPulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.6; }} }}
.sc-logo {{ font-size: 1.4rem; animation: serpentPulse 3s ease-in-out infinite; display: inline-block; margin-right: 8px; }}

@keyframes glowPulse {{ 0%, 100% {{ box-shadow: 0 0 4px currentColor; }} 50% {{ box-shadow: 0 0 12px currentColor; }} }}
.sc-badge {{ display: inline-block; padding: 2px 10px; border-radius: 20px; font-size: 0.75rem; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; animation: glowPulse 2s ease-in-out infinite; }}
.sc-badge-ok {{ background: rgba(0,255,136,0.15); color: {BRAND_SUCCESS}; }}
.sc-badge-warn {{ background: rgba(255,170,0,0.15); color: {BRAND_WARN}; }}
.sc-badge-error {{ background: rgba(255,68,68,0.15); color: {BRAND_DANGER}; }}

@keyframes fadeInUp {{ from {{ opacity: 0; transform: translateY(12px); }} to {{ opacity: 1; transform: translateY(0); }} }}
.stContainer > div {{ animation: fadeInUp 0.4s ease-out; animation-fill-mode: both; }}

@keyframes scanPulse {{ 0% {{ box-shadow: 0 0 0 0 rgba(0, 255, 136, 0.4); }} 70% {{ box-shadow: 0 0 0 10px rgba(0, 255, 136, 0); }} 100% {{ box-shadow: 0 0 0 0 rgba(0, 255, 136, 0); }} }}
.sc-scanning {{ animation: scanPulse 1.5s infinite; border-color: {BRAND_COLOR} !important; }}

/* ── Risk band coloring ────────────────────────────────────────────────── */
.sc-risk-black {{ color: #ff4444; font-weight: 700; }}
.sc-risk-red {{ color: #ff6644; font-weight: 700; }}
.sc-risk-orange {{ color: {BRAND_WARN}; font-weight: 700; }}
.sc-risk-yellow {{ color: #ffdd44; font-weight: 700; }}
.sc-risk-green {{ color: {BRAND_SUCCESS}; font-weight: 700; }}

/* ── Hide Streamlit chrome ─────────────────────────────────────────────── */
.stDeployButton {{ display: none; }}
#MainMenu {{ visibility: hidden; }}
footer {{ visibility: hidden; }}

/* ═══════════════════════════════════════════════════════════════════════════
   Mobile Responsive — Tablet & Phone Breakpoints
   ═══════════════════════════════════════════════════════════════════════════ */

/* ── Tablet (≤ 992px) ─────────────────────────────────────────────────── */
@media (max-width: 992px) {{
    .block-container {{ padding-top: 1rem; padding-left: 1rem; padding-right: 1rem; max-width: 100%; }}

    [data-testid="stMetric"] {{ padding: 12px 14px; }}
    [data-testid="stMetricValue"] {{ font-size: 1.2rem !important; }}

    h1 {{ font-size: 1.5rem; }}
    h2 {{ font-size: 1.2rem; }}
}}

/* ── Mobile (≤ 768px) ─────────────────────────────────────────────────── */
@media (max-width: 768px) {{
    .block-container {{ padding-top: 0.75rem; padding-left: 0.75rem; padding-right: 0.75rem; }}

    /* Stack columns vertically */
    [data-testid="stHorizontalBlock"] {{
        flex-wrap: wrap;
    }}

    [data-testid="stHorizontalBlock"] > div {{
        min-width: 100% !important;
        flex: 1 1 100% !important;
    }}

    /* Smaller metric cards */
    [data-testid="stMetric"] {{ padding: 10px 12px; }}
    [data-testid="stMetricValue"] {{ font-size: 1.1rem !important; }}
    [data-testid="stMetricLabel"] {{ font-size: 0.7rem !important; }}

    /* Smaller headers */
    h1 {{ font-size: 1.3rem; }}
    h2 {{ font-size: 1.1rem; }}
    h3 {{ font-size: 1rem; }}

    /* Sidebar becomes full-width overlay on mobile */
    section[data-testid="stSidebar"] {{
        width: 85vw;
        min-width: 85vw;
    }}

    /* Buttons full width */
    .stButton > button {{ width: 100%; }}

    /* Tables horizontal scroll */
    .stDataFrame {{ overflow-x: auto; }}

    /* Compact alerts */
    .stAlert {{ padding: 8px 12px; font-size: 0.85rem; }}

    /* Branding header compact */
    .sc-logo {{ font-size: 1.1rem; }}
}}

/* ── Small phone (≤ 480px) ────────────────────────────────────────────── */
@media (max-width: 480px) {{
    .block-container {{ padding-top: 0.5rem; padding-left: 0.5rem; padding-right: 0.5rem; }}

    [data-testid="stMetric"] {{ padding: 8px 10px; border-radius: 6px; }}
    [data-testid="stMetricValue"] {{ font-size: 1rem !important; }}

    .stTabs [data-baseweb="tab-list"] {{ flex-wrap: wrap; }}
    .stTabs [data-baseweb="tab"] {{ font-size: 0.75rem; padding: 6px 10px; }}
}}

/* ── Print ─────────────────────────────────────────────────────────────── */
@media print {{
    .stApp {{ background: #0e1117 !important; }}
}}
</style>

<!-- Serpent Circle branding header -->
<div style="
    text-align: center;
    padding: 1.5rem 0 0.5rem 0;
    margin-bottom: 0.5rem;
    border-bottom: 1px solid {BRAND_BORDER};
">
    <span class="sc-logo">🐍</span>
    <span style="
        font-size: 1.3rem;
        font-weight: 800;
        background: linear-gradient(135deg, {BRAND_COLOR}, {BRAND_ACCENT});
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        background-clip: text;
        letter-spacing: 0.04em;
    ">{BRAND_NAME}</span>
    <span style="
        font-size: 0.8rem;
        color: {BRAND_TEXT_DIM};
        margin-left: 10px;
        letter-spacing: 0.1em;
        text-transform: uppercase;
    ">{BRAND_TAGLINE}</span>
</div>
"""
