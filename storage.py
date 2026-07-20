"""
Storage for vendor scorecards.

Uses SQLite locally (no setup needed) and PostgreSQL in production,
based on whether a DATABASE_URL environment variable is set.
Render sets DATABASE_URL automatically if you connect a Postgres
database's "Internal Database URL" as an env var on your web service.

Scoring model:
- Each transaction entry rates 5 categories from 1 (terrible) to 5 (excellent):
  on_time, quality, pricing, responsiveness, compliance
- A vendor's overall score = average of all their entries' category averages
- Risk level is derived from the overall score AND from any single category
  that's consistently weak (a vendor can look fine on average but still be
  flagged if e.g. compliance is repeatedly bad).
"""

import os
import sqlite3
from datetime import datetime
from contextlib import contextmanager
from statistics import mean

DB_PATH = "vendor_scorecard.db"

DATABASE_URL = os.environ.get("DATABASE_URL")
USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
    # Render (and some other hosts) give a URL starting with postgres://,
    # but psycopg2/SQLAlchemy-style drivers expect postgresql://
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

CATEGORIES = ["on_time", "quality", "pricing", "responsiveness", "compliance"]
CATEGORY_LABELS = {
    "on_time": "On-time delivery",
    "quality": "Quality / defect rate",
    "pricing": "Pricing consistency",
    "responsiveness": "Responsiveness",
    "compliance": "Compliance / documentation",
}


def _ph(n):
    """Return n placeholders appropriate for the active DB driver, comma-joined."""
    mark = "%s" if USE_POSTGRES else "?"
    return ", ".join([mark] * n)


def _q(sql: str) -> str:
    """Convert a query written with '?' placeholders into the active driver's style."""
    return sql.replace("?", "%s") if USE_POSTGRES else sql


@contextmanager
def get_conn():
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        cur = conn.cursor() if USE_POSTGRES else conn

        if USE_POSTGRES:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS vendors (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                )
            """)
            category_cols = " INTEGER, ".join(CATEGORIES)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS entries (
                    id SERIAL PRIMARY KEY,
                    vendor_id INTEGER NOT NULL,
                    {category_cols} INTEGER,
                    notes TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (vendor_id) REFERENCES vendors(id)
                )
            """)
        else:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS vendors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                )
            """)
            category_cols = " INTEGER, ".join(CATEGORIES)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vendor_id INTEGER NOT NULL,
                    {category_cols} INTEGER,
                    notes TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (vendor_id) REFERENCES vendors(id)
                )
            """)


def get_or_create_vendor(name: str) -> int:
    name = name.strip()
    with get_conn() as conn:
        cur = conn.cursor() if USE_POSTGRES else conn
        cur.execute(_q("SELECT id FROM vendors WHERE name = ?"), (name,))
        row = cur.fetchone()
        if row:
            return row["id"]

        if USE_POSTGRES:
            cur.execute(
                "INSERT INTO vendors (name, created_at) VALUES (%s, %s) RETURNING id",
                (name, datetime.utcnow().isoformat())
            )
            return cur.fetchone()["id"]
        else:
            cur.execute(
                "INSERT INTO vendors (name, created_at) VALUES (?, ?)",
                (name, datetime.utcnow().isoformat())
            )
            return cur.lastrowid


def add_entry(vendor_name: str, scores: dict, notes: str = ""):
    """scores: dict with keys from CATEGORIES, values 1-5"""
    vendor_id = get_or_create_vendor(vendor_name)
    cols = ", ".join(CATEGORIES)
    values = [int(scores[c]) for c in CATEGORIES]

    with get_conn() as conn:
        cur = conn.cursor() if USE_POSTGRES else conn
        sql = f"INSERT INTO entries (vendor_id, {cols}, notes, created_at) VALUES ({_ph(3 + len(CATEGORIES))})"
        cur.execute(sql, [vendor_id] + values + [notes, datetime.utcnow().isoformat()])


def list_vendors():
    """Returns vendors ranked by overall score, each with risk info."""
    with get_conn() as conn:
        cur = conn.cursor() if USE_POSTGRES else conn
        cur.execute("SELECT * FROM vendors")
        vendors = cur.fetchall()
        results = []
        for v in vendors:
            cur.execute(
                _q("SELECT * FROM entries WHERE vendor_id = ? ORDER BY created_at DESC"),
                (v["id"],)
            )
            entries = [dict(e) for e in cur.fetchall()]
            summary = summarize(entries)
            results.append({
                "id": v["id"],
                "name": v["name"],
                "entry_count": len(entries),
                **summary,
            })
        results.sort(key=lambda r: (r["overall"] is None, -(r["overall"] or 0)))
        return results


def get_vendor(vendor_id: int):
    with get_conn() as conn:
        cur = conn.cursor() if USE_POSTGRES else conn
        cur.execute(_q("SELECT * FROM vendors WHERE id = ?"), (vendor_id,))
        v = cur.fetchone()
        if not v:
            return None
        cur.execute(
            _q("SELECT * FROM entries WHERE vendor_id = ? ORDER BY created_at DESC"),
            (vendor_id,)
        )
        entries = [dict(e) for e in cur.fetchall()]
        summary = summarize(entries)
        return {
            "id": v["id"],
            "name": v["name"],
            "entries": entries,
            **summary,
        }


def summarize(entries: list):
    """Computes overall score, per-category averages, risk level, and flags."""
    if not entries:
        return {
            "overall": None, "category_avgs": {}, "risk": "no-data", "flags": [],
        }

    category_avgs = {}
    for c in CATEGORIES:
        vals = [e[c] for e in entries if e.get(c) is not None]
        category_avgs[c] = round(mean(vals), 2) if vals else None

    valid_avgs = [v for v in category_avgs.values() if v is not None]
    overall = round(mean(valid_avgs), 2) if valid_avgs else None

    flags = []
    for c, avg in category_avgs.items():
        if avg is not None and avg <= 2.0:
            flags.append(CATEGORY_LABELS[c])

    if overall is None:
        risk = "no-data"
    elif overall < 2.5 or len(flags) >= 2:
        risk = "high"
    elif overall < 3.5 or len(flags) == 1:
        risk = "medium"
    else:
        risk = "low"

    return {"overall": overall, "category_avgs": category_avgs, "risk": risk, "flags": flags}


if __name__ == "__main__":
    init_db()
    print(f"Database initialized ({'Postgres' if USE_POSTGRES else 'SQLite at ' + DB_PATH})")