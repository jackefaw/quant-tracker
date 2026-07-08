"""
Market Regime Engine.

This is the piece that makes the tool macro-aware instead of grading stocks in a vacuum.
It reads the overall market with a handful of well-established, data-driven signals,
rolls them into a 0-100 Market Health Score, classifies a regime, and emits factor-weight
*tilts* that the scoring engine applies to every stock.

Signals (each normalized to a 0-100 sub-score, higher = healthier/risk-on):
  1. Trend      - SPY vs its 50- and 200-day moving averages (the classic regime filter).
  2. Momentum   - SPY 3M / 6M total return.
  3. Volatility - SPY 20-day realized volatility (annualized); low vol = risk-on.
  4. Yield curve- 10Y minus 2Y Treasury spread; inversion = late-cycle / risk-off.
  5. Breadth    - share of major sector ETFs trading above their own 50-day MA.

Why tilts and not just a label: in risk-off tape, quality/value/profitability historically
hold up while high-beta growth and momentum get punished; risk-on flips that. So the engine
nudges the composite toward what tends to work *now*. You can switch this off in the UI to
see pure static-weight scores.

Every signal degrades gracefully: if a data series is unavailable on your plan, that
sub-score is dropped and the health score is computed from the rest.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

SECTOR_ETFS = ["XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLB", "XLU", "XLRE", "XLC"]


@dataclass
class RegimeReport:
    score: float                       # 0-100 market health
    label: str                         # "Risk-On" | "Neutral" | "Risk-Off"
    signals: dict = field(default_factory=dict)   # name -> {value, sub_score, note}
    tilts: dict = field(default_factory=dict)     # factor -> multiplier
    coverage: int = 0                  # how many signals actually computed


# ----- helpers --------------------------------------------------------------
def _sma(closes, n):
    return float(np.mean(closes[-n:])) if len(closes) >= n else None


def _ret(closes, n):
    if len(closes) > n and closes[-n - 1]:
        return (closes[-1] / closes[-n - 1] - 1.0) * 100.0
    return None


def _realized_vol(closes, n=20):
    if len(closes) < n + 1:
        return None
    arr = np.array(closes[-(n + 1):], dtype=float)
    rets = np.diff(np.log(arr))
    return float(np.std(rets, ddof=0) * math.sqrt(252) * 100.0)


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


# ----- the engine -----------------------------------------------------------
def compute_regime(provider, use_breadth: bool = True) -> RegimeReport:
    signals = {}

    spy = provider.get_history("SPY", 260)

    # 1. Trend
    if len(spy) >= 200:
        last, s50, s200 = spy[-1], _sma(spy, 50), _sma(spy, 200)
        sub = 50.0
        if s200:
            sub = 80.0 if last > s200 else 25.0
            if s50 and last > s50:
                sub += 12
            elif s50 and last < s50:
                sub -= 12
        note = "above 200DMA" if (s200 and last > s200) else "below 200DMA"
        signals["Trend"] = {"value": f"{(last/s200-1)*100:+.1f}% vs 200DMA" if s200 else "n/a",
                            "sub_score": _clamp(sub), "note": note}

    # 2. Momentum
    r3, r6 = _ret(spy, 63), _ret(spy, 126)
    moms = [r for r in (r3, r6) if r is not None]
    if moms:
        avg = np.mean(moms)
        sub = _clamp(50 + avg * 2.5)   # +20% ~ score 100, -20% ~ 0
        signals["Momentum"] = {"value": f"3M {r3:+.1f}%  6M {r6:+.1f}%" if r3 is not None and r6 is not None
                               else f"{avg:+.1f}%", "sub_score": sub, "note": "trailing SPY return"}

    # 3. Volatility (inverse)
    vol = _realized_vol(spy, 20)
    if vol is not None:
        # ~10% vol -> calm (high score), ~35%+ -> stressed (low score)
        sub = _clamp(100 - (vol - 10) * (100 / 25))
        signals["Volatility"] = {"value": f"{vol:.0f}% annualized",
                                 "sub_score": sub, "note": "SPY 20d realized vol"}

    # 4. Yield curve
    t = provider.get_treasury()
    if t.get("y10") is not None and t.get("y2") is not None:
        spread = t["y10"] - t["y2"]
        sub = _clamp(50 + spread * 30)  # +1.0% -> 80, inverted -1% -> 20
        signals["Yield Curve"] = {"value": f"10Y-2Y {spread:+.2f}%",
                                  "sub_score": sub,
                                  "note": "inverted" if spread < 0 else "normal"}

    # 5. Breadth
    if use_breadth:
        above = total = 0
        for etf in SECTOR_ETFS:
            closes = provider.get_history(etf, 60)
            s50 = _sma(closes, 50)
            if s50:
                total += 1
                above += 1 if closes[-1] > s50 else 0
        if total >= 5:
            pct = above / total * 100
            signals["Breadth"] = {"value": f"{above}/{total} sectors > 50DMA",
                                  "sub_score": _clamp(pct), "note": "sector participation"}

    # ----- aggregate ------------------------------------------------------
    subs = [s["sub_score"] for s in signals.values()]
    health = float(np.mean(subs)) if subs else 50.0

    if health >= 60:
        label = "Risk-On"
    elif health >= 40:
        label = "Neutral"
    else:
        label = "Risk-Off"

    return RegimeReport(score=round(health, 1), label=label, signals=signals,
                        tilts=regime_tilts(label), coverage=len(signals))


# ----- regime -> factor tilts ----------------------------------------------
# Multipliers applied to the user's base weights, then renormalized in the pipeline.
TILTS = {
    "Risk-On":  {"Value": 0.80, "Growth": 1.30, "Profitability": 1.00,
                 "Quality": 0.80, "Momentum": 1.35, "Revisions": 1.25},
    "Neutral":  {"Value": 1.00, "Growth": 1.00, "Profitability": 1.00,
                 "Quality": 1.00, "Momentum": 1.00, "Revisions": 1.00},
    "Risk-Off": {"Value": 1.20, "Growth": 0.70, "Profitability": 1.20,
                 "Quality": 1.50, "Momentum": 0.70, "Revisions": 0.90},
}


def regime_tilts(label: str) -> dict:
    return dict(TILTS.get(label, TILTS["Neutral"]))
