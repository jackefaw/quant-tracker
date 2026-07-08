"""
Pipeline — the orchestration shared by the CLI (tracker.py) and the web app (app.py).

run() does the whole job end to end:
  1. fetch fundamentals + price windows for each ticker (and SPY for relative momentum)
  2. optionally read the market regime and tilt the factor weights
  3. optionally build sector peer statistics for sector-relative grading
  4. enrich raw data with market-relative momentum and revision deltas (vs last snapshot)
  5. score, then persist a snapshot and compute movers vs the previous run

Keeping this in one place means the terminal tool and the website always agree.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from market_regime import RegimeReport, compute_regime
from scoring import SECTOR_SENSITIVE, effective_weights, score_universe


@dataclass
class PipelineResult:
    scored: object                       # ScoreResult
    raw: dict
    regime: RegimeReport | None
    base_weights: dict
    eff_weights: dict
    movers: list = field(default_factory=list)   # [(ticker, delta_composite)]
    run_ts: str = ""
    sector_mode: bool = False


def _enrich(raw, prior, spy_px):
    for tk, m in raw.items():
        for win, key in (("3m", "px_3m"), ("6m", "px_6m"), ("12m", "px_12m")):
            stock, mkt = m.get(key), spy_px.get(key)
            m[f"rel_{win}"] = (stock - mkt) if (stock is not None and mkt is not None) else None
        fwd, ttm = m.get("fwd_eps"), m.get("ttm_eps")
        m["fwd_growth"] = ((fwd - ttm) / abs(ttm)) if (fwd is not None and ttm not in (None, 0)) else None
        prev = prior.get(tk, {}).get("fwd_eps")
        m["eps_rev"] = ((fwd - prev) / abs(prev)) if (fwd is not None and prev not in (None, 0)) else None
    return raw


def _build_peer_stats(provider, raw, max_peers=10):
    """For sector-relative mode: pull peers for each sector and compute (mean,std) per metric."""
    # gather a peer set per sector present in the watchlist
    sector_syms: dict[str, set] = {}
    for tk, m in raw.items():
        sec = m.get("sector", "Unknown")
        sector_syms.setdefault(sec, set())
        for peer in provider.get_peers(tk)[:max_peers]:
            sector_syms[sec].add(peer)

    # fetch peer fundamentals (cached daily by the provider)
    peer_metrics: dict[str, dict] = {}
    for sec, syms in sector_syms.items():
        for sym in syms:
            if sym not in peer_metrics and sym not in raw:
                peer_metrics[sym] = provider.get_metrics(sym)

    # also let watchlist names inform their own sector stats
    by_sector: dict[str, list] = {}
    for sym, m in {**peer_metrics, **raw}.items():
        by_sector.setdefault(m.get("sector", "Unknown"), []).append(m)

    stats: dict[str, dict] = {}   # metric -> {sector: (mean,std)}
    for metric in SECTOR_SENSITIVE:
        stats[metric] = {}
        for sec, members in by_sector.items():
            vals = [mm.get(metric) for mm in members if mm.get(metric) is not None]
            if len(vals) >= 4:
                arr = np.array(vals, dtype=float)
                lo, hi = np.percentile(arr, 5), np.percentile(arr, 95)
                arr = np.clip(arr, lo, hi)
                sd = float(np.std(arr, ddof=0))
                if sd:
                    stats[metric][sec] = (float(np.mean(arr)), sd)
    return stats


def run(provider, tickers, base_weights, *, use_regime=True, sector_mode=False,
        engine=None, use_breadth=True) -> PipelineResult:
    run_ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 1. fetch
    raw = {}
    for tk in tickers:
        m = provider.get_metrics(tk)
        m.update(provider.get_price_changes(tk))
        raw[tk] = m
    spy_px = provider.get_price_changes("SPY")

    # 2. regime
    regime = compute_regime(provider, use_breadth=use_breadth) if use_regime else None
    eff = effective_weights(base_weights, regime.tilts if regime else None)

    # 3. sector peer stats (optional)
    peer_stats = _build_peer_stats(provider, raw) if sector_mode else None

    # 4. prior snapshot + enrich
    prior = {}
    if engine is not None:
        from storage import previous_run
        prior = previous_run(engine, run_ts)
    raw = _enrich(copy.deepcopy(raw), prior, spy_px)

    # 5. score
    scored = score_universe(raw, eff, peer_stats=peer_stats)

    # movers vs previous run
    movers = []
    for tk in scored.table.index:
        now = scored.table.loc[tk, "Composite"]
        prev = prior.get(tk, {}).get("composite")
        if now == now and prev is not None:
            movers.append((tk, now - prev))
    movers.sort(key=lambda x: x[1], reverse=True)

    return PipelineResult(scored=scored, raw=raw, regime=regime, base_weights=base_weights,
                          eff_weights=eff, movers=movers, run_ts=run_ts, sector_mode=sector_mode)
