"""
Quant Tracker — command line.

    python tracker.py                 # score watchlist.txt (regime-adaptive), save snapshot
    python tracker.py --sector        # sector-relative grading (pulls peers; slower)
    python tracker.py --no-regime     # use your static weights, ignore market conditions
    python tracker.py --detail MCHP   # full factor + raw-metric breakdown for one name
    python tracker.py --history MCHP   # stored composite/grade/estimate history
    python tracker.py --no-save        # don't write a snapshot

Set FMP_API_KEY (and optionally DATABASE_URL) in a .env file. See README.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from pipeline import run
from providers import FMPProvider
from scoring import FACTOR_DEFS, FACTOR_ORDER, WEIGHTS
from storage import get_engine, save_snapshot, ticker_history

console = Console()
GRADE_STYLE = {"A+": "bold green", "A": "green", "A-": "green", "B+": "cyan", "B": "cyan",
               "B-": "cyan", "C+": "yellow", "C": "yellow", "C-": "yellow",
               "D": "red", "F": "bold red", "n/a": "dim"}


def g(grade):
    return f"[{GRADE_STYLE.get(grade,'white')}]{grade}[/]"


def load_watchlist(path):
    out = []
    for line in Path(path).read_text().splitlines():
        line = line.split("#")[0].strip().upper()
        if line:
            out.append(line)
    return out


def regime_panel(reg):
    if not reg:
        return Panel("[dim]Regime engine off — using your static weights.[/]", title="Market")
    color = {"Risk-On": "green", "Neutral": "yellow", "Risk-Off": "red"}[reg.label]
    lines = [f"[bold {color}]{reg.label}[/]   market health [bold]{reg.score}/100[/]  "
             f"({reg.coverage} signals)\n"]
    for name, s in reg.signals.items():
        lines.append(f"  {name:<12} {s['value']:<26} [dim]{s['note']}[/]")
    return Panel("\n".join(lines), title="Market Regime", border_style=color)


def scores_table(res):
    t = res.scored.table
    tab = Table(title=f"Quant Scores  ·  {'sector-relative' if res.sector_mode else 'watchlist-relative'}",
                header_style="bold")
    tab.add_column("Ticker", style="bold")
    tab.add_column("Sector", style="dim")
    tab.add_column("Comp", justify="right")
    tab.add_column("Grade", justify="center")
    for f in FACTOR_ORDER:
        tab.add_column(f[:4], justify="center")
    for tk in t.index:
        comp = t.loc[tk, "Composite"]
        sec = str(t.loc[tk, "sector"])[:14]
        tab.add_row(tk, sec, f"{comp:5.1f}" if comp == comp else "  n/a", g(t.loc[tk, "Grade"]),
                    *[g(t.loc[tk, f + "_g"]) for f in FACTOR_ORDER])
    return tab


def weights_line(res):
    parts = []
    for f in FACTOR_ORDER:
        b, e = res.base_weights[f], res.eff_weights[f]
        arrow = "→" if abs(e - b) < 0.005 else ("↑" if e > b else "↓")
        parts.append(f"{f[:4]} {e*100:.0f}%{arrow}")
    return "[dim]Effective weights (regime-adjusted): " + "  ".join(parts) + "[/]"


def movers_table(res):
    if not res.movers:
        return None
    tab = Table(title="Movers since last run (composite Δ — early-upgrade radar)", header_style="bold")
    tab.add_column("Ticker", style="bold"); tab.add_column("Δ", justify="right")
    for tk, d in res.movers:
        c = "green" if d > 0 else ("red" if d < 0 else "dim")
        tab.add_row(tk, f"[{c}]{d:+.1f}[/]")
    return tab


def show_detail(res, ticker):
    ticker = ticker.upper()
    if ticker not in res.raw:
        console.print(f"[yellow]{ticker} not in this run.[/]"); return
    t = res.scored.table
    console.print(Panel(f"[bold]{res.raw[ticker]['name']}[/]  ({ticker})  ·  "
                        f"{res.raw[ticker]['sector']}\nComposite [bold]{t.loc[ticker,'Composite']:.1f}[/] "
                        f"{g(t.loc[ticker,'Grade'])}", title="Detail"))
    mz = res.scored.metric_z
    for f in FACTOR_ORDER:
        sc = t.loc[ticker, f]
        rows = []
        for m in FACTOR_DEFS[f]:
            if m in mz.columns and ticker in mz.index:
                z = mz.loc[ticker, m]
                raw_v = res.raw[ticker].get(m)
                rv = f"{raw_v:.2f}" if isinstance(raw_v, (int, float)) else "n/a"
                rows.append(f"{m}={rv} (z{z:+.1f})" if z == z else f"{m}={rv}")
        console.print(f"  {f:<14} {g(t.loc[ticker, f+'_g'])}  [dim]{'  '.join(rows)}[/]")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-w", "--watchlist", default="watchlist.txt")
    ap.add_argument("--sector", action="store_true", help="sector-relative grading (slower)")
    ap.add_argument("--no-regime", action="store_true", help="ignore market regime")
    ap.add_argument("--no-breadth", action="store_true", help="skip sector-ETF breadth (saves calls)")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--detail", metavar="TICKER")
    ap.add_argument("--history", metavar="TICKER")
    args = ap.parse_args()

    load_dotenv()
    engine = get_engine()

    if args.history:
        hist = ticker_history(engine, args.history)
        if not hist:
            console.print(f"[yellow]No history for {args.history.upper()}.[/]"); return
        tab = Table(title=f"History — {args.history.upper()}", header_style="bold")
        for c in ("Run", "Composite", "Grade", "Fwd EPS", "Regime"):
            tab.add_column(c)
        for h in hist:
            tab.add_row(h["run_ts"], f"{h['composite']:.1f}" if h["composite"] else "n/a",
                        h["grade"] or "n/a", f"{h['fwd_eps']:.2f}" if h["fwd_eps"] else "n/a",
                        h["regime"] or "-")
        console.print(tab); return

    provider = FMPProvider(os.getenv("FMP_API_KEY", ""))
    tickers = load_watchlist(args.watchlist)
    console.print(f"[dim]Scoring {len(tickers)} tickers"
                  f"{' · sector-relative' if args.sector else ''}"
                  f"{' · no regime' if args.no_regime else ''}…[/]")

    res = run(provider, tickers, WEIGHTS, use_regime=not args.no_regime,
              sector_mode=args.sector, engine=engine, use_breadth=not args.no_breadth)

    console.print(regime_panel(res.regime))
    console.print(scores_table(res))
    if not args.no_regime:
        console.print(weights_line(res))
    mt = movers_table(res)
    if mt:
        console.print(mt)
    if args.detail:
        console.print()
        show_detail(res, args.detail)

    if not args.no_save:
        save_snapshot(engine, res.run_ts, res.regime.label if res.regime else "n/a",
                      res.scored.table, res.raw)
        console.print(f"\n[dim]Snapshot saved ({res.run_ts}).[/]")

    miss = [tk for tk in tickers if res.scored.table.loc[tk, "Composite"] != res.scored.table.loc[tk, "Composite"]]
    if miss:
        console.print(f"[yellow]No score for: {', '.join(miss)} (data unavailable on your plan).[/]")
    console.print("[dim]Research tool, not investment advice.[/]")


if __name__ == "__main__":
    main()
