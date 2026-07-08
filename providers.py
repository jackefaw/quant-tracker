"""
Data provider layer — Financial Modeling Prep, NEW "stable" API.

FMP retired its /api/v3 and /api/v4 paths for accounts created after Aug 2025.
This provider targets the current API at:

    https://financialmodelingprep.com/stable/<endpoint>?symbol=XXX&apikey=KEY

Robustness features (why the app won't crash even if FMP shuffles things again):
- Each data need has a LIST of candidate endpoints, tried in order; the first one
  that returns usable data wins and is remembered for the rest of the run.
- Field names are looked up by alias lists (e.g. "netProfitMarginTTM" OR
  "netProfitMargin"), so renamed columns degrade to other aliases, not crashes.
- Any endpoint the plan doesn't cover returns None -> the metric simply drops out
  of its factor. Same-day disk cache, retries, and 429 rate-limit backoff included.

Public surface (unchanged — nothing else in the app needs edits):
    get_metrics(symbol), get_price_changes(symbol), get_history(symbol),
    get_price_series(symbol), get_treasury(), get_quote(symbol),
    get_peers(symbol), get_sector_pe()
"""

from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path

import requests

STABLE = "https://financialmodelingprep.com/stable"


class FMPProvider:
    def __init__(self, api_key: str, cache_dir: str = ".cache", delay: float = 0.15):
        if not api_key:
            raise ValueError("No FMP API key. Set FMP_API_KEY in Secrets/.env or paste one in the app.")
        self.api_key = api_key
        self.delay = delay
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)
        self.session = requests.Session()
        self._winner: dict[str, str] = {}  # data-need -> endpoint that worked

    # ---- low level ---------------------------------------------------------
    def _cache_path(self, tag: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in tag)
        return self.cache_dir / f"{date.today().isoformat()}__{safe}.json"

    def _http(self, endpoint: str, params: dict):
        url = f"{STABLE}/{endpoint}"
        params = {**params, "apikey": self.api_key}
        for attempt in range(3):
            try:
                r = self.session.get(url, params=params, timeout=20)
                if r.status_code == 429:
                    time.sleep(2 + 2 * attempt)
                    continue
                if r.status_code in (401, 402, 403, 404):
                    return None
                r.raise_for_status()
                data = r.json()
                # FMP signals plan/endpoint problems inside 200 bodies too
                if isinstance(data, dict) and any(k for k in data if "rror" in k):
                    return None
                time.sleep(self.delay)
                return data
            except Exception:
                time.sleep(0.8 + attempt)
        return None

    def _get_any(self, need: str, candidates: list[str], params: dict, tag: str):
        """Try candidate endpoints in order; cache result per day; remember the winner."""
        cp = self._cache_path(tag)
        if cp.exists():
            try:
                return json.loads(cp.read_text())
            except Exception:
                pass
        order = candidates
        if need in self._winner and self._winner[need] in candidates:
            order = [self._winner[need]] + [c for c in candidates if c != self._winner[need]]
        for ep in order:
            data = self._http(ep, params)
            if data not in (None, [], {}):
                self._winner[need] = ep
                try:
                    cp.write_text(json.dumps(data))
                except Exception:
                    pass
                return data
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

    # ---- fetchers (new stable endpoints, with fallbacks) --------------------
    def _profile(self, s):
        return self._first(self._get_any("profile", ["profile"], {"symbol": s}, f"profile-{s}"))

    def _ratios(self, s):
        return self._first(self._get_any("ratios", ["ratios-ttm"], {"symbol": s}, f"ratios-{s}"))

    def _keymetrics(self, s):
        return self._first(self._get_any("keymetrics", ["key-metrics-ttm"], {"symbol": s}, f"km-{s}"))

    def _growth(self, s):
        return self._first(self._get_any(
            "growth", ["financial-growth", "financial-statement-growth", "income-statement-growth"],
            {"symbol": s, "period": "annual", "limit": 1}, f"growth-{s}"))

    def _scores(self, s):
        return self._first(self._get_any("scores", ["financial-scores"], {"symbol": s}, f"scores-{s}"))

    def _pxchange(self, s):
        return self._first(self._get_any("pxchg", ["stock-price-change"], {"symbol": s}, f"pxchg-{s}"))

    def _estimates(self, s):
        return self._get_any("estimates", ["analyst-estimates"],
                             {"symbol": s, "period": "annual", "limit": 6, "page": 0}, f"est-{s}")

    def _grades(self, s):
        return self._get_any("grades", ["grades", "grades-historical", "grades-latest-news"],
                             {"symbol": s, "limit": 25}, f"grades-{s}")

    # ---- public: per-stock fundamentals ------------------------------------
    def get_metrics(self, sym: str) -> dict:
        s = sym.upper().strip()
        p, r, k, g, sc = (self._profile(s), self._ratios(s), self._keymetrics(s),
                          self._growth(s), self._scores(s))

        fwd_eps = None
        est = self._estimates(s)
        if isinstance(est, list):
            yr, fut = date.today().year, []
            for row in est:
                d = str((row or {}).get("date", "")) or str((row or {}).get("fiscalYear", ""))
                try:
                    y = int(d[:4])
                except ValueError:
                    continue
                eps = self._num(row, "estimatedEpsAvg", "epsAvg", "epsEstimatedAvg", "epsEstimated")
                if y >= yr and eps is not None:
                    fut.append((y, eps))
            fut.sort()
            fwd_eps = fut[0][1] if fut else None

        grade_net = None
        gr = self._grades(s)
        if isinstance(gr, list) and gr:
            rank = {"strong sell": 0, "sell": 1, "underperform": 1, "underweight": 1, "reduce": 1,
                    "neutral": 2, "hold": 2, "equal-weight": 2, "market perform": 2, "in-line": 2,
                    "overweight": 3, "outperform": 3, "buy": 3, "accumulate": 3, "positive": 3,
                    "strong buy": 4}
            net = 0.0
            seen = False
            for row in gr[:20]:
                n = str(row.get("newGrade", "")).strip().lower()
                o = str(row.get("previousGrade", "")).strip().lower()
                if n in rank and o in rank:
                    seen = True
                    net += 1 if rank[n] > rank[o] else (-1 if rank[n] < rank[o] else 0)
            grade_net = net if seen else None

        return {
            "name": (p.get("companyName") if p else None) or s,
            "sector": (p.get("sector") if p else None) or "Unknown",
            "industry": (p.get("industry") if p else None) or "Unknown",
            "beta": self._num(p, "beta"),
            "price": self._num(p, "price"),
            "mktcap": self._num(p, "marketCap", "mktCap"),
            # value
            "pe": self._num(r, "priceToEarningsRatioTTM", "peRatioTTM", "priceEarningsRatioTTM"),
            "ps": self._num(r, "priceToSalesRatioTTM"),
            "pb": self._num(r, "priceToBookRatioTTM"),
            "p_fcf": self._num(r, "priceToFreeCashFlowRatioTTM", "priceToFreeCashFlowsRatioTTM"),
            "ev_ebitda": self._num(k, "evToEBITDATTM", "enterpriseValueOverEBITDATTM",
                                   "enterpriseValueMultipleTTM") or
                         self._num(r, "enterpriseValueMultipleTTM"),
            # growth
            "rev_growth": self._num(g, "revenueGrowth", "growthRevenue"),
            "eps_growth": self._num(g, "epsgrowth", "epsGrowth", "growthEPS", "epsdilutedGrowth"),
            "fcf_growth": self._num(g, "freeCashFlowGrowth", "growthFreeCashFlow"),
            # profitability
            "net_margin": self._num(r, "netProfitMarginTTM", "netIncomeMarginTTM"),
            "gross_margin": self._num(r, "grossProfitMarginTTM"),
            "roe": self._num(r, "returnOnEquityTTM") or self._num(k, "returnOnEquityTTM"),
            "roic": self._num(k, "returnOnInvestedCapitalTTM", "roicTTM"),
            "fcf_yield": self._num(k, "freeCashFlowYieldTTM"),
            # quality / financial health
            "piotroski": self._num(sc, "piotroskiScore"),
            "altman_z": self._num(sc, "altmanZScore"),
            "debt_equity": self._num(r, "debtToEquityRatioTTM", "debtEquityRatioTTM"),
            "current_ratio": self._num(r, "currentRatioTTM"),
            "interest_cov": self._num(r, "interestCoverageRatioTTM", "interestCoverageTTM"),
            # revisions
            "fwd_eps": fwd_eps,
            "ttm_eps": self._num(r, "netIncomePerShareTTM", "epsTTM") or self._num(k, "netIncomePerShareTTM"),
            "grade_net": grade_net,
        }

    def get_price_changes(self, sym: str) -> dict:
        p = self._pxchange(sym.upper().strip())
        return {"px_3m": self._num(p, "3M"), "px_6m": self._num(p, "6M"),
                "px_12m": self._num(p, "1Y")}

    # ---- public: price history ----------------------------------------------
    def _history_rows(self, s, days):
        frm = (date.today() - timedelta(days=int(days * 1.5))).isoformat()
        data = self._get_any("history", ["historical-price-eod/light", "historical-price-eod/full"],
                             {"symbol": s, "from": frm, "to": date.today().isoformat()},
                             f"hist-{s}-{days}")
        if isinstance(data, dict):
            data = data.get("historical", [])
        return data if isinstance(data, list) else []

    def get_history(self, sym: str, days: int = 260):
        rows = self._history_rows(sym.upper().strip(), days)
        out = []
        for row in rows:
            c = self._num(row, "close", "price", "adjClose")
            if c is not None:
                out.append((str(row.get("date", "")), c))
        out.sort()  # chronological regardless of API order
        return [c for _, c in out][-days:]

    def get_price_series(self, sym: str, days: int = 1100) -> dict:
        rows = self._history_rows(sym.upper().strip(), days)
        series = {}
        for row in rows:
            d, c = row.get("date"), self._num(row, "close", "price", "adjClose")
            if d and c is not None:
                series[str(d)] = c
        return series

    # ---- public: macro -------------------------------------------------------
    def get_treasury(self):
        today = date.today()
        d = self._get_any("treasury", ["treasury-rates"],
                          {"from": (today - timedelta(days=14)).isoformat(),
                           "to": today.isoformat()}, "treasury")
        rows = d if isinstance(d, list) else []
        row = rows[0] if rows else {}
        # rows may be oldest-first; take the latest date
        if len(rows) > 1:
            try:
                row = max(rows, key=lambda r: str(r.get("date", "")))
            except Exception:
                pass
        return {"y2": self._num(row, "year2", "month2"), "y10": self._num(row, "year10")}

    def get_quote(self, sym: str):
        return self._first(self._get_any("quote", ["quote"], {"symbol": sym.upper()}, f"quote-{sym}"))

    # ---- public: sector-relative helpers --------------------------------------
    def get_peers(self, sym: str):
        d = self._get_any("peers", ["stock-peers"], {"symbol": sym.upper()}, f"peers-{sym}")
        if isinstance(d, list) and d:
            if isinstance(d[0], dict) and "peersList" in d[0]:
                return [p.upper() for p in d[0].get("peersList", [])]
            if isinstance(d[0], dict) and "symbol" in d[0]:
                me = sym.upper()
                return [row["symbol"].upper() for row in d if row.get("symbol") and row["symbol"].upper() != me]
        return []

    def get_sector_pe(self):
        d = self._get_any("sectorpe", ["sector-pe-snapshot"],
                          {"date": date.today().isoformat(), "exchange": "NYSE"}, "sectorpe")
        out = {}
        if isinstance(d, list):
            for row in d:
                sec, pe = row.get("sector"), self._num(row, "pe")
                if sec and pe is not None:
                    out[sec] = pe
        return out
