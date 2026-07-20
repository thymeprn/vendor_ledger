"""
SQLite storage for vendor scorecards.

Scoring model:
- Each transaction entry rates 5 categories from 1 (terrible) to 5 (excellent):
  on_time, quality, pricing, responsiveness, compliance
- A vendor's overall score = average of all their entries' category averages
- Risk level is derived from the overall score AND from any single category
  that's consistently weak (a vendor can look fine on average but still be
  flagged if e.g. compliance is repeatedly bad).
"""
import sqlite3
from datetime import datetime
from contextlib import contextmanager
from statistics import mean

DB_PATH = "vendor_scorecard.db"

CATEGORIES = ["on_time", "quality", "pricing", "responsiveness", "compliance"]
CATEGORY_LABELS = {
    "on_time": "On-time delivery",
    "quality": "Quality / defect rate",
    "pricing": "Pricing consistency",
    "responsiveness": "Responsiveness",
    "compliance": "Compliance / documentation",
}


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vendors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vendor_id INTEGER NOT NULL,
                {" INTEGER, ".join(CATEGORIES)} INTEGER,
                notes TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (vendor_id) REFERENCES vendors(id)
            )
        """)


def get_or_create_vendor(name: str) -> int:
    name = name.strip()
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM vendors WHERE name = ?", (name,)).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO vendors (name, created_at) VALUES (?, ?)",
            (name, datetime.utcnow().isoformat())
        )
        return cur.lastrowid


def add_entry(vendor_name: str, scores: dict, notes: str = ""):
    """scores: dict with keys from CATEGORIES, values 1-5"""
    vendor_id = get_or_create_vendor(vendor_name)
    cols = ", ".join(CATEGORIES)
    placeholders = ", ".join(["?"] * len(CATEGORIES))
    values = [int(scores[c]) for c in CATEGORIES]

    with get_conn() as conn:
        conn.execute(
            f"INSERT INTO entries (vendor_id, {cols}, notes, created_at) VALUES (?, {placeholders}, ?, ?)",
            [vendor_id] + values + [notes, datetime.utcnow().isoformat()]
        )


def list_vendors():
    """Returns vendors ranked by overall score, each with risk info."""
    with get_conn() as conn:
        vendors = conn.execute("SELECT * FROM vendors").fetchall()
        results = []
        for v in vendors:
            entries = conn.execute(
                "SELECT * FROM entries WHERE vendor_id = ? ORDER BY created_at DESC", (v["id"],)
            ).fetchall()
            entries = [dict(e) for e in entries]
            summary = summarize(entries)
            results.append({
                "id": v["id"],
                "name": v["name"],
                "entry_count": len(entries),
                **summary,
            })
        # rank: highest overall score first, vendors with no entries go last
        results.sort(key=lambda r: (r["overall"] is None, -(r["overall"] or 0)))
        return results


def get_vendor(vendor_id: int):
    with get_conn() as conn:
        v = conn.execute("SELECT * FROM vendors WHERE id = ?", (vendor_id,)).fetchone()
        if not v:
            return None
        entries = conn.execute(
            "SELECT * FROM entries WHERE vendor_id = ? ORDER BY created_at DESC", (vendor_id,)
        ).fetchall()
        entries = [dict(e) for e in entries]
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
    print(f"Database initialized at {DB_PATH}")
