# Quant Tracker

A macro-aware, six-factor equity scoring engine. It reads the overall market regime,
tilts its factor weights to current conditions, grades every stock on six dimensions,
and lets you open any grade to see the raw numbers behind it. Runs as a command-line
tool and as a shareable web app off the same engine.

The design goal: the parts of a Seeking-Alpha-style quant grade that are reproducible on
accessible data — done transparently, made macro-adaptive, and fully tunable by you.

---

## What makes it more than a screener

**1. Market-regime adaptation.** Most stock graders score names in a vacuum. This one first
reads the tape — SPY trend vs its 50/200-day moving averages, 3M/6M momentum, realized
volatility, the 10Y-2Y yield curve, and sector breadth — rolls them into a 0-100 *Market
Health Score*, and classifies **Risk-On / Neutral / Risk-Off**. That regime then *tilts the
factor weights*: defensive (Quality, Value) in risk-off, offensive (Growth, Momentum,
Revisions) in risk-on. Toggle it off to see pure static-weight scores.

**2. Six factors, including financial health.**

| Factor | Metrics |
|---|---|
| Value | P/E, P/S, P/B, P/FCF, EV/EBITDA (cheaper scores higher) |
| Growth | revenue / EPS / FCF growth |
| Profitability | net & gross margin, ROE, ROIC, FCF yield |
| **Quality** | **Piotroski F-Score, Altman Z-Score**, debt/equity, current ratio, interest coverage |
| Momentum | 3M/6M/12M return, **market-relative** (minus SPY over the same window) |
| Revisions | change in forward EPS estimate, forward-EPS growth, net analyst rating actions |

**3. Full transparency.** Every factor grade opens to the underlying metrics and their
z-scores. No black box — you can see *why* a stock got a B in Quality.

**4. Revision tracking that actually persists.** Each run snapshots forward EPS estimates to
a database. The next run scores the *change* — the leading signal that front-runs sell-side
upgrades. Local runs use SQLite automatically; hosted deploys can point at a free Postgres
so the history survives (see DEPLOY.md).

**5. Two grading frames.** *Watchlist-relative* (default, fast) grades each metric across the
names you entered. *Sector-relative* mode pulls real industry peers and grades valuation,
margins and leverage against each stock's own sector — the closest a no-paid-license tool
gets to a sector grade.

---

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env        # paste your free FMP key into .env
```

Get a free key at financialmodelingprep.com. The free tier covers the core ratio, metric,
profile, score and price endpoints; a few fields (estimates, rating actions) may be gated on
the cheapest plan — the tool degrades gracefully and tells you which names ran thin.

## Command line

```bash
python tracker.py                 # regime-adaptive six-factor scores + snapshot
python tracker.py --sector        # sector-relative grading (pulls peers; more calls)
python tracker.py --no-regime     # static weights only
python tracker.py --detail MCHP   # full metric + z-score breakdown for one name
python tracker.py --history MCHP   # stored composite/grade/estimate history
python tracker.py --no-breadth     # skip the sector-ETF breadth sampling (saves calls)
```

## Web app

```bash
streamlit run app.py
```

Opens in your browser. Regime banner up top, colored grade table, live weight sliders
(re-rank instantly, no refetch), per-stock drill-down, and a movers radar. To put it online
with a public link to share, follow **DEPLOY.md**.

---

## Backtesting — does it actually predict returns?

```bash
python backtest.py              # rigorous point-in-time momentum backtest
python backtest.py --validate   # forward-validate your saved composite scores
```

Or use the **"Does the model predict returns?"** panel in the web app.

Two deliberately separate tools, because honesty depends on the data:

- **Momentum backtest** is rigorous and look-ahead-free — momentum comes purely from past
  prices, which are free and clean. Each month it buys the trailing-strongest third, holds,
  and measures the result: equity curve, CAGR vs SPY, Sharpe, max drawdown, and the rank
  Information Coefficient (did the signal order next month's winners?). Run it on a *broad*
  universe; a 7-name list is too small to mean much.
- **Forward validation** measures the *full six-factor composite* the honest way. Rebuilding
  historical fundamentals on a free plan would mean applying today's numbers to the past —
  look-ahead cheating. Instead this reads the scores you already saved on past runs and
  checks realized returns since. It starts empty and earns statistical weight every week you
  run the scorer. Zero look-ahead: the scores existed before the returns did.

A full historical backtest of the fundamental factors needs point-in-time fundamentals — a
paid dataset. This gives you a real backtest of the price factor now, plus a growing,
genuinely out-of-sample record for everything else.



Everything strategic is in `scoring.py` and `market_regime.py`:

- **`WEIGHTS`** (scoring.py) — your base factor tilt. Default leans Revisions + Quality.
- **`FACTOR_DEFS`** (scoring.py) — add/remove metrics or flip a direction.
- **Grade bands** — `letter_grade()` maps the 0-100 score to A+...F.
- **`TILTS`** (market_regime.py) — how aggressively each regime reweights the factors.
- **Regime thresholds** — the 60 / 40 cutoffs that split Risk-On / Neutral / Risk-Off.

In the web app, the base-weight sliders do this live without touching code.

---

## Architecture

```
providers.py      FMP data access (graceful, cached, swappable vendor surface)
market_regime.py  macro signals -> health score -> regime -> factor tilts
scoring.py        six-factor z-score engine, universe + sector-relative modes
backtest.py       momentum backtest + forward-validation of the composite
storage.py        snapshot persistence (SQLite local / Postgres hosted)
pipeline.py       orchestration shared by both front-ends
tracker.py        command-line interface
app.py            Streamlit web dashboard
```

Swap data vendors by reimplementing `FMPProvider` with the same method signatures; nothing
else changes.

---

## Honest limitations

- **Sector-relative grading is peer-sampled, not full-universe.** A paid point-in-time data
  license would cover every sector constituent with survivorship-free history. This samples
  live peers and z-scores against them — a real improvement over watchlist-relative, but not
  institutional point-in-time data. It's marked clearly in the UI.
- **Revisions need runtime to mature.** Day one it's a forward-vs-trailing proxy; the real
  estimate-drift signal accrues as snapshots build. Run it on a schedule.
- **Current fundamentals, not a backtest.** This is a live screen. It does not prove a
  strategy worked historically — that needs point-in-time data and a backtest harness.
- **Not investment advice.** It's model output for your own research.
