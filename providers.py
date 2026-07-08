"""
Data provider layer (Financial Modeling Prep).

Everything else talks to this through a small surface:
    get_metrics(symbol)        -> per-stock fundamentals + profile (sector, beta, ...)
    get_price_changes(symbol)  -> 3M / 6M / 12M price change windows
    get_history(symbol)        -> chronological closes (regime trend / vol / returns)
    get_treasury()             -> latest 2Y / 10Y yields (yield-curve signal)
    get_quote(symbol)          -> live quote
    get_peers(symbol)          -> close industry peers (optional sector-relative mode)
    get_sector_pe()            -> sector median P/E benchmarks

Production-grade rules:
- Every endpoint call is isolated and returns None on any failure (plan gating, 4xx,
  timeout, bad JSON). A missing metric drops out of its factor; nothing crashes.
- Same-day disk cache keyed on (endpoint, symbol): re-runs and slider moves never
  re-bill your quota within a day.
- Retry/backoff with 429 (rate-limit) awareness.

Swapping vendors = reimplement this one class with the same method signatures.
"""

from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path

import requests

V3 = "https://financialmodelingprep.com/api/v3"
V4 = "https://financialmodelingprep.com/api/v4"


class FMPProvider:
    def __init__(self, api_key: str, cache_dir: str = ".cache", delay: float = 0.2):
        if not api_key:
            raise ValueError("No FMP API key. Set FMP_API_KEY in .env or paste one in the app.")
        self.api_key = api_key
        self.delay = delay
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)
        self.session = requests.Session()

    # ---- low level ---------------------------------------------------------
    def _cache_path(self, tag: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in tag)
        return self.cache_dir / f"{date.today().isoformat()}__{safe}.json"

    def _get(self, url: str, params: dict, tag: str):
        cp = self._cache_path(tag)
        if cp.exists():
            try:
                return json.loads(cp.read_text())
            except Exception:
                pass
        params = {**params, "apikey": self.api_key}
        for attempt in range(3):
            try:
                r = self.session.get(url, params=params, timeout=20)
                if r.status_code == 429:
                    time.sleep(2 + 2 * attempt)
                    continue
                if r.status_code in (401, 402, 403):
                    return None
                r.raise_for_status()
                data = r.json()
                cp.write_text(json.dumps(data))
                time.sleep(self.delay)
                return data
            except Exception:
                time.sleep(1 + attempt)
        return None

    @staticmethod
    def _first(data) -> dict:
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _num(d, *keys):
        if not isinstance(d, dict):
            return None
        for k in keys:
            v = d.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return None

    # ---- endpoint fetchers -------------------------------------------------
    def _ratios(self, s):     return self._first(self._get(f"{V3}/ratios-ttm/{s}", {}, f"ratios-ttm-{s}"))
    def _keymetrics(self, s): return self._first(self._get(f"{V3}/key-metrics-ttm/{s}", {}, f"key-metrics-ttm-{s}"))
    def _growth(self, s):     return self._first(self._get(f"{V3}/financial-growth/{s}", {"period": "annual", "limit": 1}, f"growth-{s}"))
    def _profile(self, s):    return self._first(self._get(f"{V3}/profile/{s}", {}, f"profile-{s}"))
    def _pxchange(self, s):   return self._first(self._get(f"{V3}/stock-price-change/{s}", {}, f"pxchg-{s}"))

    def _scores(self, s):
        d = self._get(f"{V4}/score", {"symbol": s}, f"score-{s}")
        if not d:
            d = self._get(f"{V3}/score/{s}", {}, f"scorev3-{s}")
        return self._first(d)

    def _fwd_eps(self, s):
        data = self._get(f"{V3}/analyst-estimates/{s}", {"period": "annual", "limit": 6}, f"estimates-{s}")
        if not isinstance(data, list):
            return None
        yr, fut = date.today().year, []
        for row in data:
            try:
                y = int(str((row or {}).get("date", ""))[:4])
            except ValueError:
                continue
            eps = self._num(row, "estimatedEpsAvg")
            if y >= yr and eps is not None:
                fut.append((y, eps))
        fut.sort()
        return fut[0][1] if fut else None

    def _grade_net(self, s):
        data = self._get(f"{V3}/grade/{s}", {"limit": 25}, f"grade-{s}")
        if not isinstance(data, list):
            return None
        rank = {"strong sell": 0, "sell": 1, "underperform": 1, "underweight": 1, "reduce": 1,
                "neutral": 2, "hold": 2, "equal-weight": 2, "market perform": 2, "in-line": 2,
                "overweight": 3, "outperform": 3, "buy": 3, "accumulate": 3, "positive": 3,
                "strong buy": 4}
        net = 0.0
        for row in data[:20]:
            n = str(row.get("newGrade", "")).strip().lower()
            o = str(row.get("previousGrade", "")).strip().lower()
            if n in rank and o in rank:
                net += 1 if rank[n] > rank[o] else (-1 if rank[n] < rank[o] else 0)
        return net

    # ---- public: per-stock fundamentals ------------------------------------
    def get_metrics(self, sym: str) -> dict:
        s = sym.upper().strip()
        r, k, g, p, sc = (self._ratios(s), self._keymetrics(s), self._growth(s),
                          self._profile(s), self._scores(s))
        return {
            "name": (p.get("companyName") if p else None) or s,
            "sector": (p.get("sector") if p else None) or "Unknown",
            "industry": (p.get("industry") if p else None) or "Unknown",
            "beta": self._num(p, "beta"),
            "price": self._num(p, "price"),
            "mktcap": self._num(p, "mktCap", "marketCap"),
            # value
            "pe": self._num(r, "peRatioTTM", "priceEarningsRatioTTM"),
            "ps": self._num(r, "priceToSalesRatioTTM"),
            "pb": self._num(r, "priceToBookRatioTTM"),
            "p_fcf": self._num(r, "priceToFreeCashFlowsRatioTTM"),
            "ev_ebitda": self._num(k, "enterpriseValueOverEBITDATTM"),
            # growth
            "rev_growth": self._num(g, "revenueGrowth"),
            "eps_growth": self._num(g, "epsgrowth", "epsdilutedGrowth"),
            "fcf_growth": self._num(g, "freeCashFlowGrowth"),
            # profitability
            "net_margin": self._num(r, "netProfitMarginTTM"),
            "gross_margin": self._num(r, "grossProfitMarginTTM"),
            "roe": self._num(r, "returnOnEquityTTM"),
            "roic": self._num(k, "roicTTM"),
            "fcf_yield": self._num(k, "freeCashFlowYieldTTM"),
            # quality / financial health
            "piotroski": self._num(sc, "piotroskiScore"),
            "altman_z": self._num(sc, "altmanZScore"),
            "debt_equity": self._num(r, "debtEquityRatioTTM", "debtToEquityTTM"),
            "current_ratio": self._num(r, "currentRatioTTM"),
            "interest_cov": self._num(r, "interestCoverageTTM"),
            # revisions
            "fwd_eps": self._fwd_eps(s),
            "ttm_eps": self._num(r, "netIncomePerShareTTM", "epsTTM"),
            "grade_net": self._grade_net(s),
        }

    def get_price_changes(self, sym: str) -> dict:
        p = self._pxchange(sym.upper().strip())
        return {"px_3m": self._num(p, "3M"), "px_6m": self._num(p, "6M"),
                "px_12m": self._num(p, "1Y")}

    # ---- public: history ---------------------------------------------------
    def get_history(self, sym: str, days: int = 260):
        data = self._get(f"{V3}/historical-price-full/{sym.upper()}",
                         {"timeseries": days}, f"hist-{sym}-{days}")
        hist = data.get("historical", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        closes = [self._num(row, "close", "adjClose") for row in hist]
        closes = [c for c in closes if c is not None]
        closes.reverse()  # newest-first -> chronological
        return closes

    # ---- public: macro -----------------------------------------------------
    def get_treasury(self):
        today = date.today()
        d = self._get(f"{V4}/treasury",
                      {"from": (today - timedelta(days=12)).isoformat(), "to": today.isoformat()},
                      "treasury")
        row = d[0] if isinstance(d, list) and d else {}
        return {"y2": self._num(row, "year2", "month2"), "y10": self._num(row, "year10")}

    def get_quote(self, sym: str):
        return self._first(self._get(f"{V3}/quote/{sym.upper()}", {}, f"quote-{sym}"))

    def get_price_series(self, sym: str, days: int = 1100) -> dict:
        """{date_str: close} for backtesting. Deep history; cached daily."""
        data = self._get(f"{V3}/historical-price-full/{sym.upper()}",
                         {"timeseries": days}, f"hist-{sym}-{days}")
        hist = data.get("historical", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        out = {}
        for row in hist:
            d, c = row.get("date"), self._num(row, "close", "adjClose")
            if d and c is not None:
                out[d] = c
        return out

    # ---- public: sector-relative helpers -----------------------------------
    def get_peers(self, sym: str):
        d = self._get(f"{V4}/stock_peers", {"symbol": sym.upper()}, f"peers-{sym}")
        if isinstance(d, list) and d and isinstance(d[0], dict):
            return [p.upper() for p in d[0].get("peersList", [])]
        return []

    def get_sector_pe(self):
        d = self._get(f"{V4}/sector_price_earning_ratio",
                      {"date": date.today().isoformat(), "exchange": "NYSE"}, "sectorpe")
        out = {}
        if isinstance(d, list):
            for row in d:
                sec, pe = row.get("sector"), self._num(row, "pe")
                if sec and pe is not None:
                    out[sec] = pe
        return out
