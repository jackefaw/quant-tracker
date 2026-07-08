"""
Persistence layer.

Same code runs on two backends, chosen by the DATABASE_URL env/secret:
  - unset            -> local SQLite file (snapshots.db). Perfect for the CLI and local app.
  - postgres://...   -> a hosted Postgres (free tiers: Neon, Supabase). This is what makes
                        revision-tracking survive on Streamlit Cloud, whose own disk resets.

Each run stores one row per ticker: composite, grade, the six factor scores, and the
forward-EPS estimate. The forward-EPS column is the engine behind the revisions radar:
next run diffs it to measure real estimate drift before consensus moves.
"""

from __future__ import annotations

import json
import os

from sqlalchemy import (Column, Float, MetaData, String, Table, Text,
                        create_engine, select, text)

_META = MetaData()

SNAPSHOTS = Table(
    "snapshots", _META,
    Column("run_ts", String(32), primary_key=True),
    Column("ticker", String(16), primary_key=True),
    Column("composite", Float),
    Column("grade", String(4)),
    Column("fwd_eps", Float),
    Column("price", Float),
    Column("regime", String(16)),
    Column("factors", Text),  # json of the six factor scores
)


def get_engine(db_url: str | None = None):
    url = db_url or os.getenv("DATABASE_URL") or "sqlite:///snapshots.db"
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    engine = create_engine(url, pool_pre_ping=True)
    _META.create_all(engine)
    # backward-compat: add price column to a pre-existing table if missing
    try:
        with engine.begin() as con:
            con.execute(text("ALTER TABLE snapshots ADD COLUMN price FLOAT"))
    except Exception:
        pass
    return engine


def save_snapshot(engine, run_ts, regime_label, scored_table, raw):
    rows = []
    for tk in scored_table.index:
        comp = scored_table.loc[tk, "Composite"]
        rows.append({
            "run_ts": run_ts, "ticker": tk,
            "composite": float(comp) if comp == comp else None,
            "grade": scored_table.loc[tk, "Grade"],
            "fwd_eps": raw.get(tk, {}).get("fwd_eps"),
            "price": raw.get(tk, {}).get("price"),
            "regime": regime_label,
            "factors": json.dumps({f: (float(scored_table.loc[tk, f])
                                       if scored_table.loc[tk, f] == scored_table.loc[tk, f] else None)
                                   for f in ["Value", "Growth", "Profitability",
                                             "Quality", "Momentum", "Revisions"]}),
        })
    with engine.begin() as con:
        for row in rows:
            # upsert-ish: delete then insert (portable across sqlite/postgres)
            con.execute(text("DELETE FROM snapshots WHERE run_ts=:r AND ticker=:t"),
                        {"r": row["run_ts"], "t": row["ticker"]})
            con.execute(SNAPSHOTS.insert().values(**row))


def previous_run(engine, before_ts):
    with engine.connect() as con:
        r = con.execute(text(
            "SELECT run_ts FROM snapshots WHERE run_ts < :b ORDER BY run_ts DESC LIMIT 1"),
            {"b": before_ts}).fetchone()
        if not r:
            return {}
        prev_ts = r[0]
        out = {}
        for row in con.execute(select(SNAPSHOTS.c.ticker, SNAPSHOTS.c.composite,
                                      SNAPSHOTS.c.fwd_eps).where(SNAPSHOTS.c.run_ts == prev_ts)):
            out[row[0]] = {"composite": row[1], "fwd_eps": row[2]}
        return out


def ticker_history(engine, ticker):
    with engine.connect() as con:
        rows = con.execute(text(
            "SELECT run_ts, composite, grade, fwd_eps, regime FROM snapshots "
            "WHERE ticker=:t ORDER BY run_ts"), {"t": ticker.upper()}).fetchall()
    return [{"run_ts": r[0], "composite": r[1], "grade": r[2],
             "fwd_eps": r[3], "regime": r[4]} for r in rows]


def load_all_snapshots(engine):
    """Every stored row, for the forward-return validator."""
    with engine.connect() as con:
        rows = con.execute(text(
            "SELECT run_ts, ticker, composite, price FROM snapshots ORDER BY run_ts")).fetchall()
    return [{"run_ts": r[0], "ticker": r[1], "composite": r[2], "price": r[3]} for r in rows]
