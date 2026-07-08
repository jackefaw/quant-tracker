"""
Data provider — FMP "stable" API, self-sufficient edition.

Strategy: two layers.
  LAYER 1 (fast): the precomputed endpoints (ratios-ttm, key-metrics-ttm,
    financial-growth, financial-scores, stock-price-change).
  LAYER 2 (fallback): if a Layer-1 endpoint returns nothing, derive the same
    metrics from primitives that are free on every plan — the raw financial
    statements (income / balance sheet / cash flow), the company profile, and
    daily price history. P/E, margins, ROE, ROIC, growth, leverage, EV/EBITDA
    and momentum are all computed in-house from first principles.

So even if FMP gates every convenience endpoint, the app still scores stocks —
it just does the arithmetic itself. There's also a diagnose() method that reports
exactly what each endpoint returned, surfaced in the app's Data Diagnostics panel.

Public surface (unchanged):
    get_metrics, get_price_changes, get_history, get_price_series,
    get_treasury, get_quote, get_peers, get_sector_pe, diagnose
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
        self.last_status: dict[str, str] = {}   # endpoint -> human-readable outcome

    # ---- low level ---------------------------------------------------------
    def _cache_path(self, tag: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in tag)
        return self.cache_dir / f"{date.today().isoformat()}__{safe}.json"

    def _http(self, endpoint: str, params: dict):
        url = f"{STABLE}/{endpoint}"
        params = {**params, "apikey": self.api_key}
        outcome = "no response"
        for attempt in range(4):
            try:
                r = self.session.get(url, params=params, timeout=25)
                if r.status_code == 429:
                    outcome = "429 rate-limited"
                    time.sleep(2.5 * (attempt + 1))
                    continue
                if r.status_code in (401, 402, 403, 404):
                    outcome = f"HTTP {r.status_code} (blocked/unavailable)"
                    self.last_status[endpoint] = outcome
                    return None
                r.raise_for_status()
                data = r.json()
                if isinstance(data, dict):
                    errkey = next((k for k in data if "rror" in k or "message" in k.lower()), None)
                    if errkey and len(data) <= 2:
                        self.last_status[endpoint] = f"API said: {str(data[errkey])[:120]}"
                        return None
                if data in ([], {}):
                    self.last_status[endpoint] = "empty response"
                    return None
                self.last_status[endpoint] = "OK"
                time.sleep(self.delay)
                return data
            except Exception as e:
                outcome = f"{type(e).__name__}"
                time.sleep(0.8 + attempt)
        self.last_status[endpoint] = outcome
        return None

    def _get(self, endpoint: str, params: dict, tag: str):
        cp = self._cache_path(tag)
        if cp.exists():
            try:
                data = json.loads(cp.read_text())
                self.last_status[endpoint] = "OK (cached)"
                return data
            except Exception:
                pass
        data = self._http(endpoint, params)
        if data is not None:
            try:
                cp.write_text(json.dumps(data))
            except Exception:
                pass
        return data

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

    @staticmethod
    def _div(a, b):
        try:
            if a is None or b in (None, 0):
                return None
            return a / b
        except Exception:
            return None

    # ---- primitive fetchers --------------------------------------------------
    def _profile(self, s):
        return self._first(self._get("profile", {"symbol": s}, f"profile-{s}"))

    def _income(self, s, limit=2):
        d = self._get("income-statement", {"symbol": s, "period": "annual", "limit": limit}, f"inc-{s}")
        return d if isinstance(d, list) else []

    def _balance(self, s):
        return self._first(self._get("balance-sheet-statement",
                                     {"symbol": s, "period": "annual", "limit": 1}, f"bal-{s}"))

    def _cashflow(self, s, limit=2):
        d = self._get("cash-flow-statement", {"symbol": s, "period": "annual", "limit": limit}, f"cf-{s}")
        return d if isinstance(d, list) else []

    def _history_rows(self, s, days):
        frm = (date.today() - timedelta(days=int(days * 1.6))).isoformat()
        for ep in ("historical-price-eod/light", "historical-price-eod/full"):
            data = self._get(ep, {"symbol": s, "from": frm, "to": date.today().isoformat()},
                             f"hist-{s}-{days}-{ep[-5:]}")
            if isinstance(data, dict):
                data = data.get("historical", [])
            if isinstance(data, list) and data:
                return data
        return []

    # ---- public: history -----------------------------------------------------
    def get_history(self, sym: str, days: int = 260):
        rows = self._history_rows(sym.upper().strip(), days)
        out = []
        for row in rows:
            c = self._num(row, "close", "price", "adjClose")
            d = str(row.get("date", ""))
            if c is not None and d:
                out.append((d, c))
        out.sort()
        return [c for _, c in out][-days:]

    def get_price_series(self, sym: str, days: int = 1100) -> dict:
        rows = self._history_rows(sym.upper().strip(), days)
        series = {}
        for row in rows:
            d, c = row.get("date"), self._num(row, "close", "price", "adjClose")
            if d and c is not None:
                series[str(d)] = c
        return series

    # ---- public: price changes (endpoint, else derived from history) ----------
    def get_price_changes(self, sym: str) -> dict:
        s = sym.upper().strip()
        p = self._first(self._get("stock-price-change", {"symbol": s}, f"pxchg-{s}"))
        out = {"px_3m": self._num(p, "3M"), "px_6m": self._num(p, "6M"),
               "px_12m": self._num(p, "1Y")}
        if all(v is None for v in out.values()):
            closes = self.get_history(s, 260)
            def ret(n):
                if len(closes) > n and closes[-n - 1]:
                    return (closes[-1] / closes[-n - 1] - 1.0) * 100.0
                return None
            out = {"px_3m": ret(63), "px_6m": ret(126), "px_12m": ret(252)}
            if any(v is not None for v in out.values()):
                self.last_status["momentum"] = "derived from price history"
        return out

    # ---- public: per-stock fundamentals ---------------------------------------
    def get_metrics(self, sym: str) -> dict:
        s = sym.upper().strip()
        p = self._profile(s)

        # ----- Layer 1: precomputed endpoints -----
        r = self._first(self._get("ratios-ttm", {"symbol": s}, f"ratios-{s}"))
        k = self._first(self._get("key-metrics-ttm", {"symbol": s}, f"km-{s}"))
        g = self._first(self._get("financial-growth",
                                  {"symbol": s, "period": "annual", "limit": 1}, f"growth-{s}"))
        sc = self._first(self._get("financial-scores", {"symbol": s}, f"scores-{s}"))

        m = {
            "name": (p.get("companyName") if p else None) or s,
            "sector": (p.get("sector") if p else None) or "Unknown",
            "industry": (p.get("industry") if p else None) or "Unknown",
            "beta": self._num(p, "beta"),
            "price": self._num(p, "price"),
            "mktcap": self._num(p, "marketCap", "mktCap"),
            "pe": self._num(r, "priceToEarningsRatioTTM", "peRatioTTM", "priceEarningsRatioTTM"),
            "ps": self._num(r, "priceToSalesRatioTTM"),
            "pb": self._num(r, "priceToBookRatioTTM"),
            "p_fcf": self._num(r, "priceToFreeCashFlowRatioTTM", "priceToFreeCashFlowsRatioTTM"),
            "ev_ebitda": self._num(k, "evToEBITDATTM", "enterpriseValueOverEBITDATTM")
                         or self._num(r, "enterpriseValueMultipleTTM"),
            "rev_growth": self._num(g, "revenueGrowth", "growthRevenue"),
            "eps_growth": self._num(g, "epsgrowth", "epsGrowth", "growthEPS", "epsdilutedGrowth"),
            "fcf_growth": self._num(g, "freeCashFlowGrowth", "growthFreeCashFlow"),
            "net_margin": self._num(r, "netProfitMarginTTM", "netIncomeMarginTTM"),
            "gross_margin": self._num(r, "grossProfitMarginTTM"),
            "roe": self._num(r, "returnOnEquityTTM") or self._num(k, "returnOnEquityTTM"),
            "roic": self._num(k, "returnOnInvestedCapitalTTM", "roicTTM"),
            "fcf_yield": self._num(k, "freeCashFlowYieldTTM"),
            "piotroski": self._num(sc, "piotroskiScore"),
            "altman_z": self._num(sc, "altmanZScore"),
            "debt_equity": self._num(r, "debtToEquityRatioTTM", "debtEquityRatioTTM"),
            "current_ratio": self._num(r, "currentRatioTTM"),
            "interest_cov": self._num(r, "interestCoverageRatioTTM", "interestCoverageTTM"),
            "fwd_eps": None,
            "ttm_eps": self._num(r, "netIncomePerShareTTM", "epsTTM")
                       or self._num(k, "netIncomePerShareTTM"),
            "grade_net": None,
        }

        # ----- Layer 2: derive missing fundamentals from raw statements -----
        core = ("pe", "ps", "net_margin", "roe", "rev_growth", "debt_equity")
        if sum(1 for c in core if m[c] is None) >= 4:
            self._derive_from_statements(s, m)

        # ----- estimates & rating actions (nice-to-have; degrade silently) -----
        est = self._get("analyst-estimates",
                        {"symbol": s, "period": "annual", "page": 0, "limit": 6}, f"est-{s}")
        if isinstance(est, list):
            yr, fut = date.today().year, []
            for row in est:
                dstr = str((row or {}).get("date", "")) or str((row or {}).get("fiscalYear", ""))
                try:
                    y = int(dstr[:4])
                except ValueError:
                    continue
                eps = self._num(row, "estimatedEpsAvg", "epsAvg", "epsEstimatedAvg", "epsEstimated")
                if y >= yr and eps is not None:
                    fut.append((y, eps))
            fut.sort()
            m["fwd_eps"] = fut[0][1] if fut else None

        gr = self._get("grades", {"symbol": s, "limit": 25}, f"grades-{s}")
        if not isinstance(gr, list):
            gr = self._get("grades-historical", {"symbol": s, "limit": 25}, f"gradesh-{s}")
        if isinstance(gr, list) and gr:
            rank = {"strong sell": 0, "sell": 1, "underperform": 1, "underweight": 1, "reduce": 1,
                    "neutral": 2, "hold": 2, "equal-weight": 2, "market perform": 2, "in-line": 2,
                    "overweight": 3, "outperform": 3, "buy": 3, "accumulate": 3, "positive": 3,
                    "strong buy": 4}
            net, seen = 0.0, False
            for row in gr[:20]:
                n = str(row.get("newGrade", "")).strip().lower()
                o = str(row.get("previousGrade", "")).strip().lower()
                if n in rank and o in rank:
                    seen = True
                    net += 1 if rank[n] > rank[o] else (-1 if rank[n] < rank[o] else 0)
            m["grade_net"] = net if seen else None

        return m

    def _derive_from_statements(self, s: str, m: dict):
        """Compute fundamentals in-house from raw statements + profile."""
        inc = self._income(s, 2)
        bal = self._balance(s)
        cfs = self._cashflow(s, 2)
        i0 = inc[0] if inc else {}
        i1 = inc[1] if len(inc) > 1 else {}
        c0 = cfs[0] if cfs else {}
        c1 = cfs[1] if len(cfs) > 1 else {}

        rev = self._num(i0, "revenue")
        rev_prev = self._num(i1, "revenue")
        ni = self._num(i0, "netIncome")
        gp = self._num(i0, "grossProfit")
        opinc = self._num(i0, "operatingIncome")
        ebitda = self._num(i0, "ebitda")
        eps = self._num(i0, "epsdiluted", "epsDiluted", "eps")
        eps_prev = self._num(i1, "epsdiluted", "epsDiluted", "eps")
        shares = self._num(i0, "weightedAverageShsOutDil", "weightedAverageShsOut")
        interest = self._num(i0, "interestExpense")

        equity = self._num(bal, "totalStockholdersEquity", "totalEquity")
        debt = self._num(bal, "totalDebt")
        cash = self._num(bal, "cashAndCashEquivalents", "cashAndShortTermInvestments")
        cur_assets = self._num(bal, "totalCurrentAssets")
        cur_liab = self._num(bal, "totalCurrentLiabilities")

        fcf = self._num(c0, "freeCashFlow")
        fcf_prev = self._num(c1, "freeCashFlow")

        mktcap, price = m.get("mktcap"), m.get("price")
        if mktcap is None and price is not None and shares:
            mktcap = price * shares
            m["mktcap"] = mktcap

        def setif(key, val):
            if m.get(key) is None and val is not None:
                m[key] = val

        setif("net_margin", self._div(ni, rev))
        setif("gross_margin", self._div(gp, rev))
        setif("roe", self._div(ni, equity))
        setif("pe", self._div(price, eps) if (price is not None and eps not in (None, 0)) else None)
        setif("ps", self._div(mktcap, rev))
        setif("pb", self._div(mktcap, equity))
        setif("p_fcf", self._div(mktcap, fcf))
        setif("fcf_yield", self._div(fcf, mktcap))
        if m.get("ev_ebitda") is None and mktcap is not None and ebitda not in (None, 0):
            ev = mktcap + (debt or 0) - (cash or 0)
            m["ev_ebitda"] = self._div(ev, ebitda)
        setif("debt_equity", self._div(debt, equity))
        setif("current_ratio", self._div(cur_assets, cur_liab))
        if m.get("interest_cov") is None and opinc is not None and interest not in (None, 0):
            m["interest_cov"] = abs(self._div(opinc, interest) or 0)
        if m.get("roic") is None and opinc is not None:
            invested = (debt or 0) + (equity or 0)
            if invested:
                m["roic"] = opinc * 0.79 / invested  # NOPAT approx at 21% tax
        setif("rev_growth", self._div((rev - rev_prev) if (rev is not None and rev_prev not in (None, 0)) else None, abs(rev_prev) if rev_prev else None))
        setif("eps_growth", self._div((eps - eps_prev) if (eps is not None and eps_prev not in (None, 0)) else None, abs(eps_prev) if eps_prev else None))
        setif("fcf_growth", self._div((fcf - fcf_prev) if (fcf is not None and fcf_prev not in (None, 0)) else None, abs(fcf_prev) if fcf_prev else None))
        setif("ttm_eps", eps)
        self.last_status["fundamentals"] = "derived from raw statements (in-house)"

    # ---- public: macro ---------------------------------------------------------
    def get_treasury(self):
        today = date.today()
        d = self._get("treasury-rates",
                      {"from": (today - timedelta(days=14)).isoformat(),
                       "to": today.isoformat()}, "treasury")
        rows = d if isinstance(d, list) else []
        row = rows[0] if rows else {}
        if len(rows) > 1:
            try:
                row = max(rows, key=lambda r: str(r.get("date", "")))
            except Exception:
                pass
        return {"y2": self._num(row, "year2", "month2"), "y10": self._num(row, "year10")}

    def get_quote(self, sym: str):
        return self._first(self._get("quote", {"symbol": sym.upper()}, f"quote-{sym}"))

    # ---- public: sector-relative helpers ----------------------------------------
    def get_peers(self, sym: str):
        d = self._get("stock-peers", {"symbol": sym.upper()}, f"peers-{sym}")
        if isinstance(d, list) and d:
            if isinstance(d[0], dict) and "peersList" in d[0]:
                return [p.upper() for p in d[0].get("peersList", [])]
            if isinstance(d[0], dict) and "symbol" in d[0]:
                me = sym.upper()
                return [row["symbol"].upper() for row in d
                        if row.get("symbol") and row["symbol"].upper() != me]
        return []

    def get_sector_pe(self):
        d = self._get("sector-pe-snapshot",
                      {"date": date.today().isoformat(), "exchange": "NYSE"}, "sectorpe")
        out = {}
        if isinstance(d, list):
            for row in d:
                sec, pe = row.get("sector"), self._num(row, "pe")
                if sec and pe is not None:
                    out[sec] = pe
        return out

    # ---- diagnostics --------------------------------------------------------------
    def diagnose(self, sym: str = "AAPL") -> dict:
        """Hit each endpoint once (bypassing cache) and report exactly what came back."""
        s = sym.upper().strip()
        checks = [
            ("profile", {"symbol": s}),
            ("ratios-ttm", {"symbol": s}),
            ("key-metrics-ttm", {"symbol": s}),
            ("financial-growth", {"symbol": s, "period": "annual", "limit": 1}),
            ("financial-scores", {"symbol": s}),
            ("stock-price-change", {"symbol": s}),
            ("income-statement", {"symbol": s, "period": "annual", "limit": 1}),
            ("balance-sheet-statement", {"symbol": s, "period": "annual", "limit": 1}),
            ("cash-flow-statement", {"symbol": s, "period": "annual", "limit": 1}),
            ("historical-price-eod/light", {"symbol": s,
                                            "from": (date.today() - timedelta(days=30)).isoformat(),
                                            "to": date.today().isoformat()}),
            ("analyst-estimates", {"symbol": s, "period": "annual", "page": 0, "limit": 2}),
            ("grades", {"symbol": s, "limit": 5}),
            ("treasury-rates", {"from": (date.today() - timedelta(days=14)).isoformat(),
                                "to": date.today().isoformat()}),
        ]
        report = {}
        for ep, params in checks:
            data = self._http(ep, params)
            status = self.last_status.get(ep, "?")
            sample = ""
            if data is not None:
                head = data[0] if isinstance(data, list) and data else data
                if isinstance(head, dict):
                    sample = ", ".join(list(head.keys())[:6])
            report[ep] = {"status": status, "fields": sample}
        return report
