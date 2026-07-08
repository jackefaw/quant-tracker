"""
Backtest harness — the part that asks "do the grades actually predict returns?"

Two honest tools, because the honest answer depends on the data you can get for free:

1) momentum_backtest(): a RIGOROUS, point-in-time backtest of the price-momentum factor.
   Momentum is derived purely from past prices, and deep daily price history is free and
   clean — so this one has no look-ahead bias and no survivorship hacks. Every month it
   ranks the universe by trailing return, buys the top group, holds, and measures what
   happened next. You get an equity curve, CAGR, Sharpe, max drawdown, and the rank
   Information Coefficient (how well the signal ordered next-month returns).

2) validate_snapshots(): a FORWARD validation of the full six-factor composite. We can't
   honestly rebuild historical fundamentals on a free plan (applying today's numbers to the
   past is look-ahead cheating), so instead this measures the real thing: it takes the
   composite scores you've already saved on past runs and checks the realized return since,
   using price history. It starts empty and earns statistical weight every week you run the
   tool. Zero look-ahead — the scores were genuinely computed before the returns happened.

Why split them: a price-only backtest is provably clean today; a full-model backtest needs
point-in-time fundamentals (a paid dataset). This gives you a real backtest now AND a
growing, honest out-of-sample record for the whole model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd


# ---- stats helpers ---------------------------------------------------------
def spearman_ic(signal: pd.Series, fwd: pd.Series) -> float:
    d = pd.concat([signal, fwd], axis=1).dropna()
    if len(d) < 3:
        return np.nan
    return float(d.iloc[:, 0].rank().corr(d.iloc[:, 1].rank()))


def max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    return float((equity / peak - 1.0).min() * 100)


def cagr(equity: pd.Series, index) -> float:
    if len(equity) < 2:
        return np.nan
    years = (index[-1] - index[0]).days / 365.25
    if years <= 0:
        return np.nan
    return float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1) * 100


@dataclass
class BacktestResult:
    series: pd.DataFrame              # date, strat_ret, bench_ret, strat_eq, bench_eq, ic
    stats: dict = field(default_factory=dict)
    note: str = ""


# ---- 1) point-in-time momentum backtest ------------------------------------
def _price_panel(provider, tickers, days):
    series = {}
    for tk in tickers:
        s = provider.get_price_series(tk, days)
        if s and len(s) > 50:
            series[tk] = pd.Series(s)
    if not series:
        return pd.DataFrame()
    df = pd.DataFrame(series)
    df.index = pd.to_datetime(df.index)
    return df.sort_index().ffill()


def momentum_backtest(provider, tickers, *, lookback=126, skip=21, rebalance=21,
                      top_frac=0.34, days=1100, benchmark="SPY") -> BacktestResult:
    cols = list(dict.fromkeys(list(tickers) + [benchmark]))
    panel = _price_panel(provider, cols, days)
    if panel.empty or benchmark not in panel.columns or len(panel) < lookback + skip + rebalance + 5:
        return BacktestResult(pd.DataFrame(), {}, "Not enough price history to backtest.")

    names = [c for c in panel.columns if c != benchmark]
    n = len(panel)
    rows = []
    start = lookback + skip
    for t in range(start, n - rebalance, rebalance):
        p_now, p_skip, p_back = panel.iloc[t], panel.iloc[t - skip], panel.iloc[t - skip - lookback]
        signal = (p_skip[names] / p_back[names] - 1.0).dropna()
        fwd = (panel.iloc[t + rebalance] / p_now - 1.0)
        fwd_names = fwd[names].dropna()
        common = signal.index.intersection(fwd_names.index)
        if len(common) < 3:
            continue
        signal, fwd_names = signal[common], fwd_names[common]
        k = max(1, int(round(len(common) * top_frac)))
        longs = signal.sort_values(ascending=False).head(k).index
        rows.append({
            "date": panel.index[t],
            "strat_ret": float(fwd_names[longs].mean()),
            "bench_ret": float(fwd[benchmark]) if benchmark in fwd and fwd[benchmark] == fwd[benchmark] else np.nan,
            "ic": spearman_ic(signal, fwd_names),
        })

    if not rows:
        return BacktestResult(pd.DataFrame(), {}, "No rebalance periods produced.")

    df = pd.DataFrame(rows).set_index("date")
    df["strat_eq"] = (1 + df["strat_ret"]).cumprod()
    df["bench_eq"] = (1 + df["bench_ret"].fillna(0)).cumprod()
    ann = 252 / rebalance
    s = df["strat_ret"]
    stats = {
        "periods": len(df),
        "strat_CAGR_%": round(cagr(df["strat_eq"], df.index), 1),
        "bench_CAGR_%": round(cagr(df["bench_eq"], df.index), 1),
        "strat_Sharpe": round(float(s.mean() / s.std() * np.sqrt(ann)), 2) if s.std() else np.nan,
        "strat_vol_%": round(float(s.std() * np.sqrt(ann) * 100), 1),
        "strat_maxDD_%": round(max_drawdown(df["strat_eq"]), 1),
        "beat_bench_%": round(float((df["strat_ret"] > df["bench_ret"]).mean() * 100), 0),
        "avg_rank_IC": round(float(df["ic"].mean()), 3),
    }
    note = (f"Momentum {lookback}d (skip {skip}d), rebal {rebalance}d, top {int(top_frac*100)}%, "
            f"{len(names)} names vs {benchmark}.")
    return BacktestResult(df, stats, note)


# ---- 2) forward validation of the full composite ---------------------------
def validate_snapshots(provider, engine, min_hold_days=5):
    from storage import load_all_snapshots
    rows = load_all_snapshots(engine)
    if not rows:
        return {"runs_used": 0, "note": "No snapshots yet — run the scorer a few times first."}

    # group by run
    runs = {}
    for r in rows:
        runs.setdefault(r["run_ts"], []).append(r)

    # 'now' price per ticker from one cached deep history pull
    today = pd.Timestamp(datetime.now().date())
    price_cache = {}

    def latest_price(tk):
        if tk not in price_cache:
            s = provider.get_price_series(tk, 400)
            price_cache[tk] = pd.Series({pd.to_datetime(k): v for k, v in s.items()}).sort_index() if s else None
        return price_cache[tk]

    per_run, all_ic, all_spread = [], [], []
    for run_ts, members in runs.items():
        try:
            run_date = pd.to_datetime(run_ts)
        except Exception:
            continue
        if (today - run_date).days < min_hold_days:
            continue
        comps, frets = {}, {}
        for m in members:
            tk, comp, p0 = m["ticker"], m["composite"], m["price"]
            if comp is None or not p0:
                continue
            ser = latest_price(tk)
            if ser is None or ser.empty:
                continue
            p1 = float(ser.iloc[-1])
            comps[tk] = comp
            frets[tk] = p1 / p0 - 1.0
        if len(comps) < 4:
            continue
        cs, fs = pd.Series(comps), pd.Series(frets)
        ic = spearman_ic(cs, fs)
        med = cs.median()
        top, bot = fs[cs >= med], fs[cs < med]
        spread = (top.mean() - bot.mean()) * 100 if len(top) and len(bot) else np.nan
        per_run.append({"run": run_ts, "n": len(comps), "elapsed_days": (today - run_date).days,
                        "rank_IC": round(ic, 3) if ic == ic else None,
                        "top_minus_bottom_%": round(spread, 2) if spread == spread else None})
        if ic == ic:
            all_ic.append(ic)
        if spread == spread:
            all_spread.append(spread)

    return {
        "runs_used": len(per_run),
        "avg_rank_IC": round(float(np.mean(all_ic)), 3) if all_ic else None,
        "avg_top_minus_bottom_%": round(float(np.mean(all_spread)), 2) if all_spread else None,
        "per_run": per_run,
        "note": ("Positive IC and positive top-minus-bottom mean higher composites led to "
                 "higher realized returns. Needs several runs spaced over weeks to be meaningful."),
    }


# ---- CLI -------------------------------------------------------------------
def _main():
    import argparse
    import os
    from dotenv import load_dotenv
    from rich.console import Console
    from rich.table import Table

    from providers import FMPProvider
    from storage import get_engine

    ap = argparse.ArgumentParser(description="Backtest the quant model")
    ap.add_argument("--validate", action="store_true",
                    help="forward-validate saved composite scores (default is momentum backtest)")
    ap.add_argument("-w", "--watchlist", default="watchlist.txt")
    ap.add_argument("--lookback", type=int, default=126)
    ap.add_argument("--rebalance", type=int, default=21)
    ap.add_argument("--top", type=float, default=0.34)
    args = ap.parse_args()

    load_dotenv()
    con = Console()
    provider = FMPProvider(os.getenv("FMP_API_KEY", ""))

    if args.validate:
        res = validate_snapshots(provider, get_engine())
        con.print(f"[bold]Forward validation[/] — runs used: {res['runs_used']}")
        con.print(f"  avg rank IC:           {res.get('avg_rank_IC')}")
        con.print(f"  avg top-minus-bottom:  {res.get('avg_top_minus_bottom_%')}%")
        con.print(f"[dim]{res['note']}[/]")
        return

    from pathlib import Path
    tickers = [l.split("#")[0].strip().upper() for l in Path(args.watchlist).read_text().splitlines()
               if l.split("#")[0].strip()]
    con.print(f"[dim]Backtesting momentum on {len(tickers)} names… (pulling deep price history)[/]")
    res = momentum_backtest(provider, tickers, lookback=args.lookback,
                            rebalance=args.rebalance, top_frac=args.top)
    if res.series.empty:
        con.print(f"[yellow]{res.note}[/]"); return
    con.print(f"[dim]{res.note}[/]")
    tab = Table(title="Momentum backtest", header_style="bold")
    for k in res.stats:
        tab.add_column(k)
    tab.add_row(*[str(v) for v in res.stats.values()])
    con.print(tab)
    con.print("[dim]Price-derived factor, point-in-time. Not advice; past performance is not "
              "predictive. Small universes give noisy results — backtest a broad list.[/]")


if __name__ == "__main__":
    _main()
