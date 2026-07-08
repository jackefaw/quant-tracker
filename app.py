"""
Quant Tracker — web dashboard.

Run locally:   streamlit run app.py
Deploy free:   see DEPLOY.md (Streamlit Community Cloud)

A macro-aware, six-factor equity scorer. Reads the market regime, tilts the factor
weights to current conditions, and grades every name with full drill-down into the
raw metrics behind each grade.
"""

import copy
import os

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from market_regime import compute_regime
from pipeline import _build_peer_stats, _enrich
from providers import FMPProvider
from scoring import (FACTOR_DEFS, FACTOR_ORDER, WEIGHTS, effective_weights,
                     score_universe)
from storage import get_engine, previous_run, save_snapshot, ticker_history

load_dotenv()
st.set_page_config(page_title="Quant Tracker", page_icon="📈", layout="wide")

DEFAULT_WATCHLIST = "AMZN\nHIVE\nRXT\nICHR\nMCHP\nFRO\nGTY"
GRADE_BG = {"A+": "#15803d", "A": "#16a34a", "A-": "#22a85a", "B+": "#0e7490",
            "B": "#0891b2", "B-": "#0e98b8", "C+": "#b45309", "C": "#d97706",
            "C-": "#ca8a04", "D": "#dc2626", "F": "#991b1b", "n/a": "#4b5563"}
REGIME_COLOR = {"Risk-On": "#16a34a", "Neutral": "#d97706", "Risk-Off": "#dc2626"}


# ---- cached resources ------------------------------------------------------
@st.cache_resource
def engine():
    return get_engine()


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_bundle(tickers: tuple, key: str, use_regime: bool, sector_mode: bool, use_breadth: bool):
    """All network work, cached so moving sliders never refetches or re-bills."""
    p = FMPProvider(key)
    raw = {}
    for tk in tickers:
        m = p.get_metrics(tk)
        m.update(p.get_price_changes(tk))
        raw[tk] = m
    spy_px = p.get_price_changes("SPY")
    regime = compute_regime(p, use_breadth=use_breadth) if use_regime else None
    peer_stats = _build_peer_stats(p, raw) if sector_mode else None
    return raw, spy_px, regime, peer_stats


@st.cache_data(ttl=3600, show_spinner=False)
def run_momentum(tickers: tuple, key: str, lookback: int, rebalance: int, top: float):
    from backtest import momentum_backtest
    res = momentum_backtest(FMPProvider(key), list(tickers), lookback=lookback,
                            rebalance=rebalance, top_frac=top)
    return res.series, res.stats, res.note


@st.cache_data(ttl=600, show_spinner=False)
def run_validate(key: str):
    from backtest import validate_snapshots
    return validate_snapshots(FMPProvider(key), get_engine())


def resolve_key(typed: str) -> str:
    if typed.strip():
        return typed.strip()
    try:
        if "FMP_API_KEY" in st.secrets:
            return st.secrets["FMP_API_KEY"]
    except Exception:
        pass
    return os.getenv("FMP_API_KEY", "")


def parse_watchlist(text):
    seen, out = set(), []
    for line in text.splitlines():
        t = line.split("#")[0].strip().upper()
        if t and t not in seen:
            seen.add(t); out.append(t)
    return out


def style_grades(df):
    cols = [c for c in df.columns if c == "Grade" or c in FACTOR_ORDER]
    return df.style.map(
        lambda v: f"background-color:{GRADE_BG.get(v,'')};color:white;font-weight:600;"
        if v in GRADE_BG else "", subset=cols)


# ============================================================ SIDEBAR ========
st.sidebar.title("📈 Quant Tracker")
st.sidebar.caption("Macro-aware six-factor scoring on live fundamentals.")

sidebar_key = st.sidebar.text_input("FMP API key", type="password",
                                    help="Free key at financialmodelingprep.com. "
                                         "Leave blank if set in secrets/.env.")
watchlist_text = st.sidebar.text_area("Watchlist", value=DEFAULT_WATCHLIST, height=150)

st.sidebar.subheader("Engine")
use_regime = st.sidebar.toggle("Regime-adaptive weights", value=True,
                               help="Tilt factor weights to current market conditions.")
sector_mode = st.sidebar.toggle("Sector-relative grading", value=False,
                                help="Grade valuation/margins/leverage vs real sector peers "
                                     "(slower, more API calls).")
use_breadth = st.sidebar.toggle("Include breadth signal", value=True,
                                help="Sample sector ETFs for market breadth (11 extra calls/day).")

st.sidebar.subheader("Base factor weights")
st.sidebar.caption("Your strategy. Auto-normalized; regime then tilts these.")
base = {f: st.sidebar.slider(f, 0.0, 1.0, WEIGHTS[f], 0.05) for f in FACTOR_ORDER}
tot = sum(base.values())
base = {f: v / tot for f, v in base.items()} if tot else dict(WEIGHTS)

run = st.sidebar.button("Score stocks", type="primary", use_container_width=True)
if run:
    st.session_state.active = True
    st.session_state.params = (tuple(parse_watchlist(watchlist_text)), use_regime,
                               sector_mode, use_breadth)
    st.session_state.save_pending = True

# ============================================================ MAIN ===========
st.title("Quant Scores")

if not st.session_state.get("active"):
    st.info("Set your API key and watchlist, then press **Score stocks**.")
    st.markdown(
        "- **Macro-aware:** reads the market regime and tilts factor weights to current conditions.\n"
        "- **Six factors:** Value · Growth · Profitability · Quality (Piotroski/Altman) · "
        "Momentum · Revisions.\n"
        "- **Transparent:** open any stock to see the raw metrics and z-scores behind every grade.\n"
        "- **Sliders are live:** re-weight the model and the board re-ranks instantly — no refetch.")
    st.stop()

key = resolve_key(sidebar_key)
tickers, p_regime, p_sector, p_breadth = st.session_state.params
if not key:
    st.error("No API key found. Paste one in the sidebar, or set FMP_API_KEY in secrets/.env.")
    st.stop()
if not tickers:
    st.error("Watchlist is empty."); st.stop()

with st.spinner(f"Fetching {len(tickers)} tickers{' + peers' if p_sector else ''} from FMP…"):
    try:
        raw_c, spy_px, regime, peer_stats = fetch_bundle(tickers, key, p_regime, p_sector, p_breadth)
    except Exception as e:
        st.error(f"Data fetch failed: {e}"); st.stop()

eng = engine()
prior = previous_run(eng, "9999")  # latest stored run, for movers
raw = _enrich(copy.deepcopy(raw_c), prior, spy_px)
eff = effective_weights(base, regime.tilts if regime else None)
res = score_universe(raw, eff, peer_stats=peer_stats)
t = res.table

# ---- regime banner ----
if regime:
    c = REGIME_COLOR[regime.label]
    st.markdown(f"### Market Regime: <span style='color:{c}'>{regime.label}</span>  "
                f"<span style='color:#888;font-size:0.7em'>health {regime.score}/100</span>",
                unsafe_allow_html=True)
    cols = st.columns(max(len(regime.signals), 1))
    for col, (name, sig) in zip(cols, regime.signals.items()):
        col.metric(name, sig["value"], help=sig["note"])
        col.progress(min(int(sig["sub_score"]), 100))
    st.caption("Regime tilts the factor weights below toward what historically works in "
               "this tape — defensive in risk-off, aggressive in risk-on.")
else:
    st.caption("Regime engine off — using your static base weights.")

# ---- scores table ----
disp = pd.DataFrame(index=t.index)
disp["Sector"] = t["sector"]
disp["Composite"] = t["Composite"].round(1)
disp["Grade"] = t["Grade"]
for f in FACTOR_ORDER:
    disp[f] = t[f + "_g"]
disp = disp.reset_index().rename(columns={"index": "Ticker"})
mode_label = "sector-relative" if p_sector else "watchlist-relative"
st.subheader(f"Scores · {mode_label}")
st.dataframe(style_grades(disp), use_container_width=True, hide_index=True)

# ---- chart + weights ----
c1, c2 = st.columns([3, 2])
with c1:
    st.subheader("Composite ranking")
    st.bar_chart(t["Composite"].dropna().sort_values(), horizontal=True, color="#16a34a")
with c2:
    st.subheader("Weights in use")
    wdf = pd.DataFrame({"Factor": FACTOR_ORDER,
                        "Base": [f"{base[f]*100:.0f}%" for f in FACTOR_ORDER],
                        "Effective": [f"{eff[f]*100:.0f}%" for f in FACTOR_ORDER]})
    st.dataframe(wdf, hide_index=True, use_container_width=True)
    if regime:
        st.caption(f"Effective = base × {regime.label} tilt, renormalized.")

# ---- movers ----
movers = [(tk, t.loc[tk, "Composite"] - prior[tk]["composite"])
          for tk in t.index if tk in prior and prior[tk].get("composite") is not None
          and t.loc[tk, "Composite"] == t.loc[tk, "Composite"]]
if movers:
    movers.sort(key=lambda x: x[1], reverse=True)
    st.subheader("Movers since last run · early-upgrade radar")
    mdf = pd.DataFrame(movers, columns=["Ticker", "Δ Composite"])
    mdf["Δ Composite"] = mdf["Δ Composite"].round(1)
    st.dataframe(mdf, hide_index=True, use_container_width=True)

# ---- per-stock transparency ----
st.subheader("Factor breakdown")
pick = st.selectbox("Inspect a stock", list(t.index),
                    format_func=lambda x: f"{x} — {raw[x]['name']}")
if pick:
    st.markdown(f"**{raw[pick]['name']}** ({pick}) · {raw[pick]['sector']} · "
                f"composite **{t.loc[pick,'Composite']:.1f}** ({t.loc[pick,'Grade']})")
    rows = []
    for f in FACTOR_ORDER:
        for m in FACTOR_DEFS[f]:
            if m in res.metric_z.columns:
                z = res.metric_z.loc[pick, m]
                rv = raw[pick].get(m)
                rows.append({"Factor": f, "Metric": m,
                             "Value": round(rv, 3) if isinstance(rv, (int, float)) else "n/a",
                             "z-score": round(z, 2) if z == z else None,
                             "Factor grade": t.loc[pick, f + "_g"]})
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    hist = ticker_history(eng, pick)
    if len(hist) > 1:
        hdf = pd.DataFrame(hist)
        st.line_chart(hdf.set_index("run_ts")["composite"], color="#16a34a")

# ---- backtest section ----
st.divider()
st.subheader("🔬 Does the model predict returns?")
bc1, bc2 = st.columns(2)

with bc1.expander("Momentum factor backtest (rigorous, point-in-time)"):
    st.caption("Buys the trailing-strongest third each month, holds, measures what happened "
               "next. Uses only past prices, so no look-ahead. Broader watchlists = less noise.")
    lb = st.select_slider("Lookback", options=[63, 126, 189, 252], value=126)
    rb = st.select_slider("Rebalance (days)", options=[10, 21, 42, 63], value=21)
    if st.button("Run momentum backtest"):
        with st.spinner("Pulling deep price history and backtesting…"):
            series, stats, note = run_momentum(tickers, key, lb, rb, 0.34)
        if series is None or series.empty:
            st.warning(note)
        else:
            st.caption(note)
            m = st.columns(4)
            m[0].metric("Strategy CAGR", f"{stats['strat_CAGR_%']}%", f"vs {stats['bench_CAGR_%']}% SPY")
            m[1].metric("Sharpe", stats["strat_Sharpe"])
            m[2].metric("Max drawdown", f"{stats['strat_maxDD_%']}%")
            m[3].metric("Avg rank IC", stats["avg_rank_IC"],
                        help="Correlation between signal and next-month return. >0 means it ordered winners correctly.")
            eq = series[["strat_eq", "bench_eq"]].rename(
                columns={"strat_eq": "Momentum strategy", "bench_eq": "SPY"})
            st.line_chart(eq)
            st.caption(f"Beat SPY in {stats['beat_bench_%']:.0f}% of {stats['periods']} periods. "
                       "Past performance is not predictive.")

with bc2.expander("Forward validation of the full composite"):
    st.caption("Takes the composite scores you've saved on past runs and checks realized "
               "returns since — a clean, look-ahead-free record of the whole model that "
               "grows every week you run the scorer.")
    if st.button("Run forward validation"):
        with st.spinner("Measuring realized returns on saved snapshots…"):
            v = run_validate(key)
        if v.get("runs_used", 0) == 0:
            st.info(v.get("note", "No snapshots yet."))
        else:
            vc = st.columns(3)
            vc[0].metric("Runs used", v["runs_used"])
            vc[1].metric("Avg rank IC", v.get("avg_rank_IC"))
            vc[2].metric("Top-minus-bottom", f"{v.get('avg_top_minus_bottom_%')}%")
            if v.get("per_run"):
                st.dataframe(pd.DataFrame(v["per_run"]), hide_index=True, use_container_width=True)
            st.caption(v["note"])

# ---- data diagnostics ----
with st.expander("🔧 Data diagnostics — what is FMP returning?"):
    st.caption("Hits each FMP endpoint once from this server and reports the outcome. "
               "Use this if grades look flat or data seems missing.")
    if st.button("Run diagnostics"):
        with st.spinner("Testing endpoints…"):
            try:
                report = FMPProvider(key).diagnose(tickers[0] if tickers else "AAPL")
                ddf = pd.DataFrame([{"Endpoint": ep, "Status": v["status"],
                                     "Sample fields": v["fields"]} for ep, v in report.items()])
                st.dataframe(ddf, hide_index=True, use_container_width=True)
            except Exception as e:
                st.error(f"Diagnostics failed: {e}")

# ---- save snapshot once per fetch ----
if st.session_state.get("save_pending"):
    try:
        save_snapshot(eng, __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"),
                      regime.label if regime else "n/a", t, raw)
    except Exception as e:
        st.caption(f"(snapshot not saved: {e})")
    st.session_state.save_pending = False

miss = [tk for tk in tickers if t.loc[tk, "Composite"] != t.loc[tk, "Composite"]]
if miss:
    st.warning(f"No score for: {', '.join(miss)} — data unavailable on your plan.")
st.caption("Research tool, not investment advice. Grades are model output, not recommendations.")
