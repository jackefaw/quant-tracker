"""
Scoring engine. This is the strategy you own.

Six factors, each a basket of metrics:
  Value         - P/E, P/S, P/B, P/FCF, EV/EBITDA           (cheaper scores higher)
  Growth        - revenue / EPS / FCF growth
  Profitability - net & gross margin, ROE, ROIC, FCF yield
  Quality       - Piotroski F, Altman Z, debt/equity, current ratio, interest coverage
  Momentum      - 3M/6M/12M return, market-relative (computed in the pipeline)
  Revisions     - change in forward EPS estimate, forward-EPS growth, net rating actions

Scoring mechanics:
- Each metric is z-scored, value/leverage metrics inverted, winsorized to tame outliers.
- Per-factor z = mean of available metric z-scores -> normal CDF -> 0-100 -> A+..F.
- Composite = weighted blend over available factors (weights renormalized per row so a
  missing factor never silently zeroes a stock).

Two grading frames:
- UNIVERSE (default): metrics z-scored across the watchlist you entered. Fast, free,
  fully data-driven, but only as meaningful as the comparison set.
- SECTOR-RELATIVE (optional): the pipeline supplies real peer statistics, and the
  sector-sensitive metrics (valuation, margins, leverage) are z-scored against each
  stock's own sector instead of the watchlist. Growth/momentum/revisions stay
  cross-sectional because they compare cleanly across sectors. This is the closest a
  no-paid-license tool gets to a Seeking-Alpha-style sector grade.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

# metric -> direction (+1 higher better, -1 lower better)
FACTOR_DEFS = {
    "Value":         {"pe": -1, "ps": -1, "pb": -1, "p_fcf": -1, "ev_ebitda": -1},
    "Growth":        {"rev_growth": 1, "eps_growth": 1, "fcf_growth": 1},
    "Profitability": {"net_margin": 1, "gross_margin": 1, "roe": 1, "roic": 1, "fcf_yield": 1},
    "Quality":       {"piotroski": 1, "altman_z": 1, "debt_equity": -1,
                      "current_ratio": 1, "interest_cov": 1},
    "Momentum":      {"rel_3m": 1, "rel_6m": 1, "rel_12m": 1},
    "Revisions":     {"eps_rev": 1, "fwd_growth": 1, "grade_net": 1},
}

FACTOR_ORDER = ["Value", "Growth", "Profitability", "Quality", "Momentum", "Revisions"]

# Default base weights (sum to 1.0). Tilted toward revisions + quality.
WEIGHTS = {"Value": 0.15, "Growth": 0.15, "Profitability": 0.18,
           "Quality": 0.17, "Momentum": 0.15, "Revisions": 0.20}

# Metrics whose scale is genuinely sector-dependent -> eligible for sector-relative z.
SECTOR_SENSITIVE = {"pe", "ps", "pb", "p_fcf", "ev_ebitda",
                    "net_margin", "gross_margin", "roe", "roic", "fcf_yield",
                    "debt_equity", "current_ratio", "interest_cov"}


def _winsorize(s: pd.Series, p=0.05):
    if s.notna().sum() < 4:
        return s
    return s.clip(s.quantile(p), s.quantile(1 - p))


def _universe_z(s: pd.Series) -> pd.Series:
    s = _winsorize(s.astype(float))
    mu, sd = s.mean(), s.std(ddof=0)
    if not sd or np.isnan(sd):
        return pd.Series(0.0, index=s.index)
    return (s - mu) / sd


def _sector_z(values: pd.Series, sectors: pd.Series, peer_stats_metric: dict,
              fallback: pd.Series) -> pd.Series:
    """z each value against its own sector's (mean,std); fall back to universe z per-row."""
    out = {}
    for tk in values.index:
        v = values[tk]
        sec = sectors.get(tk)
        stat = peer_stats_metric.get(sec) if peer_stats_metric else None
        if v is not None and v == v and stat and stat[1]:
            out[tk] = (v - stat[0]) / stat[1]
        else:
            out[tk] = fallback[tk]
    return pd.Series(out)


def _cdf_score(z):
    if pd.isna(z):
        return np.nan
    return 100.0 * 0.5 * (1.0 + math.erf(z / math.sqrt(2)))


def letter_grade(score):
    if pd.isna(score):
        return "n/a"
    for cut, g in [(97, "A+"), (90, "A"), (80, "A-"), (70, "B+"), (60, "B"),
                   (50, "B-"), (40, "C+"), (30, "C"), (20, "C-"), (10, "D")]:
        if score >= cut:
            return g
    return "F"


@dataclass
class ScoreResult:
    table: pd.DataFrame
    factor_scores: pd.DataFrame
    metric_z: pd.DataFrame   # per-metric z-scores, for the transparency drill-down


def score_universe(raw: dict, weights: dict | None = None, peer_stats: dict | None = None):
    weights = weights or WEIGHTS
    df = pd.DataFrame(raw).T
    sectors = df["sector"] if "sector" in df.columns else pd.Series("Unknown", index=df.index)
    factor_scores = pd.DataFrame(index=df.index)
    metric_z = pd.DataFrame(index=df.index)

    for factor, metrics in FACTOR_DEFS.items():
        z_cols = []
        for m, direction in metrics.items():
            if m not in df.columns:
                continue
            base = _universe_z(df[m])
            if peer_stats and m in SECTOR_SENSITIVE and m in peer_stats:
                z = _sector_z(df[m], sectors, peer_stats[m], base) * direction
            else:
                z = base * direction
            metric_z[m] = z
            z_cols.append(z)
        factor_scores[factor] = (pd.concat(z_cols, axis=1).mean(axis=1, skipna=True).apply(_cdf_score)
                                 if z_cols else np.nan)

    comp = []
    for _, row in factor_scores.iterrows():
        num = den = 0.0
        for f in FACTOR_ORDER:
            v = row.get(f)
            if pd.notna(v):
                num += v * weights[f]
                den += weights[f]
        comp.append(num / den if den else np.nan)
    factor_scores["Composite"] = comp

    out = factor_scores.copy()
    for f in FACTOR_ORDER:
        out[f + "_g"] = out[f].apply(letter_grade)
    out["Grade"] = out["Composite"].apply(letter_grade)
    out["sector"] = sectors
    out = out.sort_values("Composite", ascending=False)
    return ScoreResult(table=out, factor_scores=factor_scores, metric_z=metric_z)


def effective_weights(base: dict, tilts: dict | None) -> dict:
    """Apply regime tilts to base weights and renormalize to sum 1.0."""
    if not tilts:
        w = dict(base)
    else:
        w = {f: base[f] * tilts.get(f, 1.0) for f in base}
    total = sum(w.values())
    return {f: v / total for f, v in w.items()} if total else dict(base)
