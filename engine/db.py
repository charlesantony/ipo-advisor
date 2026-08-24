import json
import sqlite3
from pathlib import Path
from logging_utils import logger

DB_PATH = Path(__file__).with_name("ipo_advisor.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at_utc TEXT NOT NULL,
    fetched_at_ist TEXT NOT NULL,
    source TEXT NOT NULL,
    symbol TEXT,
    ipo_type TEXT,
    name TEXT,
    status TEXT,
    start_date TEXT,
    end_date TEXT,
    listing_date TEXT,
    price_high REAL,
    issue_size_cr REAL,
    fresh_issue_cr REAL,
    ofs_cr REAL,
    gmp_value REAL,
    gmp_gain_pct REAL,
    gmp_date TEXT,
    qib_x REAL,
    nii_x REAL,
    retail_x REAL,
    total_x REAL,
    is_closing_day INTEGER NOT NULL DEFAULT 0,
    is_1430_decision_snapshot INTEGER NOT NULL DEFAULT 0,
    capture_reason TEXT,
    feature_json TEXT,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS historical_ipos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    imported_at_ist TEXT NOT NULL,
    source TEXT NOT NULL,
    symbol TEXT,
    ipo_type TEXT,
    name TEXT,
    listing_date TEXT,
    issue_price REAL,
    listing_open REAL,
    listing_close REAL,
    listing_gain_pct REAL,
    target_kind TEXT,
    target_source TEXT,
    gmp_gain_pct REAL,
    qib_x REAL,
    nii_x REAL,
    retail_x REAL,
    total_x REAL,
    issue_size_cr REAL,
    ofs_ratio REAL,
    log_qib REAL,
    log_nii REAL,
    log_retail REAL,
    log_total REAL,
    log_issue_size REAL,
    diagnostic_json TEXT,
    raw_json TEXT NOT NULL,
    UNIQUE(source, symbol, listing_date)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_symbol_time
ON snapshots(symbol, fetched_at_utc);

CREATE INDEX IF NOT EXISTS idx_snapshots_end_date
ON snapshots(end_date);

CREATE INDEX IF NOT EXISTS idx_snapshots_decision
ON snapshots(is_1430_decision_snapshot, end_date);

CREATE INDEX IF NOT EXISTS idx_hist_segment
ON historical_ipos(ipo_type, listing_date);


CREATE TABLE IF NOT EXISTS historical_web_ipos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    imported_at_ist TEXT NOT NULL,
    source TEXT NOT NULL,
    source_url TEXT,
    ipo_type TEXT,
    name TEXT,
    issue_open TEXT,
    issue_close TEXT,
    listing_date TEXT,
    issue_price REAL,
    listing_close REAL,
    listing_gain_pct REAL,
    gmp_gain_pct REAL,
    qib_x REAL,
    nii_x REAL,
    retail_x REAL,
    total_x REAL,
    issue_size_cr REAL,
    log_qib REAL,
    log_nii REAL,
    log_retail REAL,
    log_total REAL,
    log_issue_size REAL,
    raw_json TEXT NOT NULL,
    UNIQUE(source, ipo_type, name, listing_date, issue_price)
);

CREATE INDEX IF NOT EXISTS idx_hist_web_segment
ON historical_web_ipos(ipo_type, listing_date);


CREATE TABLE IF NOT EXISTS historical_gmp_ipos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    imported_at_ist TEXT NOT NULL,
    source TEXT NOT NULL,
    source_url TEXT,
    ipo_type TEXT NOT NULL,
    name TEXT NOT NULL,
    open_date TEXT,
    close_date TEXT,
    issue_size_cr REAL,
    issue_price REAL,
    gmp_rupees REAL,
    gmp_gain_pct REAL,
    listing_price REAL,
    listing_gain_pct REAL,
    raw_json TEXT NOT NULL,
    UNIQUE(source, ipo_type, name, close_date)
);

CREATE INDEX IF NOT EXISTS idx_hist_gmp_segment
ON historical_gmp_ipos(ipo_type, close_date);


CREATE TABLE IF NOT EXISTS historical_market_ipos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_key TEXT,
    imported_at_ist TEXT NOT NULL,
    source TEXT NOT NULL,
    year INTEGER,
    ipo_type TEXT NOT NULL,
    name TEXT NOT NULL,
    detail_url TEXT,
    issue_open TEXT,
    issue_close TEXT,
    issue_price REAL,
    total_x REAL,
    qib_x REAL,
    nii_x REAL,
    retail_x REAL,
    gmp_rupees REAL,
    gmp_gain_pct REAL,
    listing_price REAL,
    listing_open REAL,
    listing_close REAL,
    listing_gain_pct REAL,
    log_total REAL,
    raw_json TEXT NOT NULL,
    UNIQUE(source, ipo_type, name, issue_close, issue_price)
);

CREATE INDEX IF NOT EXISTS idx_hist_market_segment
ON historical_market_ipos(ipo_type, issue_close);


CREATE TABLE IF NOT EXISTS research_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at_utc TEXT NOT NULL,
    created_at_ist TEXT NOT NULL,
    source TEXT NOT NULL,
    symbol TEXT,
    ipo_type TEXT,
    name TEXT,
    status TEXT,
    end_date TEXT,
    listing_date TEXT,
    is_closing_day INTEGER NOT NULL DEFAULT 0,
    is_1430_decision_snapshot INTEGER NOT NULL DEFAULT 0,
    capture_reason TEXT,
    policy_version TEXT,
    action TEXT,
    research_confidence TEXT,
    finality_code TEXT,
    primary_prediction_pct REAL,
    gmp_input_pct REAL,
    total_subscription_x REAL,
    gmp_prediction_pct REAL,
    subscription_prediction_pct REAL,
    signal_conflict INTEGER NOT NULL DEFAULT 0,
    recommendation_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_research_decisions_symbol_time
ON research_decisions(symbol, created_at_utc);

CREATE INDEX IF NOT EXISTS idx_research_decisions_canonical
ON research_decisions(is_1430_decision_snapshot, end_date);


CREATE TABLE IF NOT EXISTS year_model_tracker (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tracker_key TEXT NOT NULL UNIQUE,
    year INTEGER NOT NULL,
    ipo_type TEXT NOT NULL,
    name TEXT NOT NULL,
    detail_url TEXT,
    provider_status TEXT,
    issue_open TEXT,
    issue_close TEXT,
    issue_price REAL,
    total_x REAL,
    gmp_used_pct REAL,
    gmp_used_rupees REAL,
    gmp_used_at_ist TEXT,
    gmp_quality TEXT,
    decision_source TEXT,
    model_policy_version TEXT,
    model_action TEXT,
    model_confidence TEXT,
    primary_prediction_pct REAL,
    gmp_prediction_pct REAL,
    subscription_prediction_pct REAL,
    signal_conflict INTEGER NOT NULL DEFAULT 0,
    listing_price REAL,
    actual_listing_gain_pct REAL,
    outcome_vs_call TEXT,
    shadow_v2_version TEXT,
    shadow_v2_triggered INTEGER NOT NULL DEFAULT 0,
    shadow_v2_action TEXT,
    shadow_v2_strength TEXT,
    shadow_v2_outcome TEXT,
    shadow_v2_reason TEXT,
    last_updated_ist TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_year_tracker_year_status
ON year_model_tracker(year, provider_status);

CREATE INDEX IF NOT EXISTS idx_year_tracker_year_action
ON year_model_tracker(year, model_action);





"""

def _canon_name(value):
    import re
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())

def _market_record_key(source, year, ipo_type, name):
    return "|".join([
        str(source or ""),
        str(year or ""),
        str(ipo_type or "").upper(),
        _canon_name(name),
    ])

_MARKET_MERGE_FIELDS = [
    "detail_url", "issue_open", "issue_close", "issue_price",
    "total_x", "qib_x", "nii_x", "retail_x",
    "gmp_rupees", "gmp_gain_pct", "listing_price",
    "listing_open", "listing_close", "listing_gain_pct",
    "log_total", "raw_json", "imported_at_ist",
]

def _completeness(row):
    priority = [
        "listing_gain_pct", "gmp_gain_pct", "total_x", "issue_price",
        "listing_price", "issue_close", "issue_open", "listing_open",
        "listing_close", "qib_x", "nii_x", "retail_x", "detail_url",
    ]
    return sum(1 for f in priority if row[f] is not None)

def _migrate_market_integrity(conn):
    """
    V0.3.6 migration:
    SQLite UNIQUE constraints treat NULL values as distinct. Older builds used
    (source, ipo_type, name, issue_close, issue_price), so rows whose date/price
    was NULL could be inserted again on every refresh.

    We assign a stable non-NULL record_key based on source/year/segment/company,
    merge duplicate information, delete duplicate rows, then add a unique index.
    """
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(historical_market_ipos)").fetchall()]
    if "record_key" not in cols:
        conn.execute("ALTER TABLE historical_market_ipos ADD COLUMN record_key TEXT")

    rows = conn.execute(
        "SELECT * FROM historical_market_ipos ORDER BY id"
    ).fetchall()

    groups = {}
    for row in rows:
        key = _market_record_key(
            row["source"], row["year"], row["ipo_type"], row["name"]
        )
        groups.setdefault(key, []).append(row)

    duplicate_groups = 0
    duplicate_rows_removed = 0
    duplicate_examples = []

    for key, group in groups.items():
        if len(group) == 1:
            conn.execute(
                "UPDATE historical_market_ipos SET record_key=? WHERE id=?",
                (key, group[0]["id"]),
            )
            continue

        duplicate_groups += 1
        duplicate_rows_removed += len(group) - 1
        if len(duplicate_examples) < 20:
            duplicate_examples.append({
                "record_key": key,
                "name": group[0]["name"],
                "ipo_type": group[0]["ipo_type"],
                "ids": [r["id"] for r in group],
            })

        # Keep the most complete row and merge all non-null values into it.
        keeper = max(group, key=_completeness)
        merged = {f: keeper[f] for f in _MARKET_MERGE_FIELDS}

        # Prefer the keeper value; otherwise recover information from duplicates.
        for row in sorted(group, key=_completeness, reverse=True):
            for f in _MARKET_MERGE_FIELDS:
                if merged.get(f) is None and row[f] is not None:
                    merged[f] = row[f]

        assignments = ", ".join([f"{f}=?" for f in _MARKET_MERGE_FIELDS])
        values = [merged[f] for f in _MARKET_MERGE_FIELDS]
        conn.execute(
            f"UPDATE historical_market_ipos SET record_key=?, {assignments} WHERE id=?",
            [key] + values + [keeper["id"]],
        )

        delete_ids = [r["id"] for r in group if r["id"] != keeper["id"]]
        placeholders = ",".join("?" for _ in delete_ids)
        conn.execute(
            f"DELETE FROM historical_market_ipos WHERE id IN ({placeholders})",
            delete_ids,
        )

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_hist_market_record_key "
        "ON historical_market_ipos(record_key)"
    )
    conn.commit()

    after = conn.execute("SELECT COUNT(*) AS n FROM historical_market_ipos").fetchone()["n"]
    if duplicate_rows_removed:
        logger.warning(
            "DATA_MIGRATION_DEDUP table=historical_market_ipos "
            "duplicate_groups=%s rows_removed=%s rows_after=%s examples=%s",
            duplicate_groups, duplicate_rows_removed, after, duplicate_examples,
        )
    return {
        "duplicate_groups": duplicate_groups,
        "duplicate_rows_removed": duplicate_rows_removed,
        "rows_after": after,
        "examples": duplicate_examples,
    }

def _migrate_v043_shadow_columns(conn):
    cols = {
        r["name"]
        for r in conn.execute("PRAGMA table_info(year_model_tracker)").fetchall()
    }
    additions = [
        ("shadow_v2_version", "TEXT"),
        ("shadow_v2_triggered", "INTEGER NOT NULL DEFAULT 0"),
        ("shadow_v2_action", "TEXT"),
        ("shadow_v2_strength", "TEXT"),
        ("shadow_v2_outcome", "TEXT"),
        ("shadow_v2_reason", "TEXT"),
    ]
    added = []
    for name, sql_type in additions:
        if name not in cols:
            conn.execute(
                f"ALTER TABLE year_model_tracker ADD COLUMN {name} {sql_type}"
            )
            added.append(name)
    if added:
        conn.commit()
        logger.info("DATA_MIGRATION_V043_SHADOW_COLUMNS added=%s", added)

def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate_market_integrity(conn)
    _migrate_v043_shadow_columns(conn)
    return conn

def previous_snapshot(symbol):
    if not symbol:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM snapshots WHERE symbol=? ORDER BY fetched_at_utc DESC LIMIT 1",
            (symbol,),
        ).fetchone()
    return dict(row) if row else None

def latest_subscription_snapshot(symbol=None, name=None, end_date=None):
    # Newest snapshot for this IPO whose total subscription is non-null.
    clauses = ["total_x IS NOT NULL"]
    params = []

    if symbol:
        clauses.append("symbol=?")
        params.append(symbol)
    elif name:
        clauses.append("name=?")
        params.append(name)
    else:
        return None

    if end_date:
        clauses.append("end_date=?")
        params.append(end_date)

    where = " AND ".join(clauses)
    sql = (
        "SELECT * FROM snapshots WHERE "
        + where
        + " ORDER BY fetched_at_utc DESC LIMIT 1"
    )
    with connect() as conn:
        row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None

def save_snapshots(records, fetched_at_utc, fetched_at_ist, source="finapi",
                   capture_reason="manual", local_date=None, decision_window=False):
    with connect() as conn:
        for n in records:
            is_closing = int(bool(local_date and n.get("end_date") == local_date))
            finality = (
                ((n.get("recommendation") or {}).get("finality") or {})
            )
            is_decision = int(bool(
                is_closing
                and decision_window
                and finality.get("canonical")
            ))
            conn.execute(
                """
                INSERT INTO snapshots (
                    fetched_at_utc, fetched_at_ist, source,
                    symbol, ipo_type, name, status, start_date, end_date, listing_date,
                    price_high, issue_size_cr, fresh_issue_cr, ofs_cr,
                    gmp_value, gmp_gain_pct, gmp_date,
                    qib_x, nii_x, retail_x, total_x,
                    is_closing_day, is_1430_decision_snapshot,
                    capture_reason, feature_json, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fetched_at_utc, fetched_at_ist, source,
                    n.get("symbol"), n.get("type"), n.get("name"), n.get("status"),
                    n.get("start_date"), n.get("end_date"), n.get("listing_date"),
                    n.get("price_high"), n.get("issue_size_cr"), n.get("fresh_issue_cr"),
                    n.get("ofs_cr"), n.get("gmp_value"), n.get("gmp_gain_pct"),
                    n.get("gmp_date"), n.get("qib_x"), n.get("nii_x"),
                    n.get("retail_x"), n.get("total_x"),
                    is_closing, is_decision, capture_reason,
                    json.dumps(n.get("features") or {}, ensure_ascii=False),
                    json.dumps(n.get("raw") or {}, ensure_ascii=False),
                ),
            )
        conn.commit()

def upsert_historical(records, imported_at_ist, source="finapi_listed"):
    inserted = 0
    with connect() as conn:
        for r in records:
            f = r.get("features") or {}
            o = r.get("outcome") or {}
            symbol = r.get("symbol")
            listing_date = r.get("listing_date") or ""
            before = conn.total_changes
            conn.execute(
                """
                INSERT INTO historical_ipos (
                    imported_at_ist, source, symbol, ipo_type, name, listing_date,
                    issue_price, listing_open, listing_close, listing_gain_pct,
                    target_kind, target_source,
                    gmp_gain_pct, qib_x, nii_x, retail_x, total_x,
                    issue_size_cr, ofs_ratio, log_qib, log_nii, log_retail,
                    log_total, log_issue_size, diagnostic_json, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, symbol, listing_date) DO UPDATE SET
                    imported_at_ist=excluded.imported_at_ist,
                    name=excluded.name,
                    issue_price=excluded.issue_price,
                    listing_open=excluded.listing_open,
                    listing_close=excluded.listing_close,
                    listing_gain_pct=excluded.listing_gain_pct,
                    target_kind=excluded.target_kind,
                    target_source=excluded.target_source,
                    gmp_gain_pct=excluded.gmp_gain_pct,
                    qib_x=excluded.qib_x,
                    nii_x=excluded.nii_x,
                    retail_x=excluded.retail_x,
                    total_x=excluded.total_x,
                    issue_size_cr=excluded.issue_size_cr,
                    ofs_ratio=excluded.ofs_ratio,
                    log_qib=excluded.log_qib,
                    log_nii=excluded.log_nii,
                    log_retail=excluded.log_retail,
                    log_total=excluded.log_total,
                    log_issue_size=excluded.log_issue_size,
                    diagnostic_json=excluded.diagnostic_json,
                    raw_json=excluded.raw_json
                """,
                (
                    imported_at_ist, source, symbol, r.get("type"), r.get("name"),
                    listing_date, o.get("issue_price"), o.get("listing_open"),
                    o.get("listing_close"), o.get("listing_gain_pct"),
                    o.get("target_kind"), o.get("target_source"),
                    r.get("gmp_gain_pct"), r.get("qib_x"), r.get("nii_x"),
                    r.get("retail_x"), r.get("total_x"), r.get("issue_size_cr"),
                    f.get("ofs_ratio"), f.get("log_qib"), f.get("log_nii"),
                    f.get("log_retail"), f.get("log_total"), f.get("log_issue_size"),
                    json.dumps(o, ensure_ascii=False),
                    json.dumps(r.get("raw") or {}, ensure_ascii=False),
                ),
            )
            if conn.total_changes > before:
                inserted += 1
        conn.commit()
    return inserted

def recent_snapshots(limit=100, symbol=None):
    with connect() as conn:
        if symbol:
            rows = conn.execute(
                "SELECT * FROM snapshots WHERE symbol=? ORDER BY fetched_at_utc DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM snapshots ORDER BY fetched_at_utc DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]

def historical_rows(limit=1000):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM historical_ipos ORDER BY listing_date DESC, name LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]

def save_research_decisions(
    records,
    created_at_utc,
    created_at_ist,
    source="finapi",
    capture_reason="manual",
):
    saved = 0
    with connect() as conn:
        for n in records:
            rec = n.get("recommendation") or {}
            if not rec:
                continue
            preds = rec.get("predictions") or {}
            finality = rec.get("finality") or {}
            conn.execute(
                """
                INSERT INTO research_decisions (
                    created_at_utc, created_at_ist, source,
                    symbol, ipo_type, name, status, end_date, listing_date,
                    is_closing_day, is_1430_decision_snapshot, capture_reason,
                    policy_version, action, research_confidence, finality_code,
                    primary_prediction_pct, gmp_input_pct, total_subscription_x,
                    gmp_prediction_pct, subscription_prediction_pct,
                    signal_conflict, recommendation_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at_utc, created_at_ist, source,
                    n.get("symbol"), n.get("type"), n.get("name"), n.get("status"),
                    n.get("end_date"), n.get("listing_date"),
                    int(bool(n.get("is_closing_today"))),
                    int(bool(finality.get("canonical"))),
                    capture_reason,
                    rec.get("policy_version"), rec.get("action"),
                    rec.get("research_confidence"), finality.get("code"),
                    rec.get("primary_prediction_pct"),
                    preds.get("gmp_input_pct"), preds.get("total_subscription_x"),
                    preds.get("gmp_prediction_pct"),
                    preds.get("subscription_prediction_pct"),
                    int(bool(rec.get("signal_conflict"))),
                    json.dumps(rec, ensure_ascii=False),
                ),
            )
            saved += 1
        conn.commit()
    logger.info(
        "RESEARCH_DECISIONS_SAVED count=%s capture_reason=%s at_ist=%s",
        saved, capture_reason, created_at_ist
    )
    return saved

def recent_research_decisions(limit=100, symbol=None):
    with connect() as conn:
        if symbol:
            rows = conn.execute(
                """
                SELECT * FROM research_decisions
                WHERE symbol=?
                ORDER BY created_at_utc DESC LIMIT ?
                """,
                (symbol, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM research_decisions
                ORDER BY created_at_utc DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]

def upsert_year_model_tracker(records):
    with connect() as conn:
        for r in records:
            conn.execute(
                """
                INSERT INTO year_model_tracker (
                    tracker_key, year, ipo_type, name, detail_url, provider_status,
                    issue_open, issue_close, issue_price, total_x,
                    gmp_used_pct, gmp_used_rupees, gmp_used_at_ist, gmp_quality,
                    decision_source, model_policy_version, model_action,
                    model_confidence, primary_prediction_pct, gmp_prediction_pct,
                    subscription_prediction_pct, signal_conflict,
                    listing_price, actual_listing_gain_pct, outcome_vs_call,
                    shadow_v2_version, shadow_v2_triggered, shadow_v2_action,
                    shadow_v2_strength, shadow_v2_outcome, shadow_v2_reason,
                    last_updated_ist, raw_json
                ) VALUES (
                    :tracker_key, :year, :ipo_type, :name, :detail_url, :provider_status,
                    :issue_open, :issue_close, :issue_price, :total_x,
                    :gmp_used_pct, :gmp_used_rupees, :gmp_used_at_ist, :gmp_quality,
                    :decision_source, :model_policy_version, :model_action,
                    :model_confidence, :primary_prediction_pct, :gmp_prediction_pct,
                    :subscription_prediction_pct, :signal_conflict,
                    :listing_price, :actual_listing_gain_pct, :outcome_vs_call,
                    :shadow_v2_version, :shadow_v2_triggered, :shadow_v2_action,
                    :shadow_v2_strength, :shadow_v2_outcome, :shadow_v2_reason,
                    :last_updated_ist, :raw_json
                )
                ON CONFLICT(tracker_key) DO UPDATE SET
                    detail_url=excluded.detail_url,
                    provider_status=excluded.provider_status,
                    issue_open=excluded.issue_open,
                    issue_close=excluded.issue_close,
                    issue_price=excluded.issue_price,
                    total_x=excluded.total_x,
                    gmp_used_pct=excluded.gmp_used_pct,
                    gmp_used_rupees=excluded.gmp_used_rupees,
                    gmp_used_at_ist=excluded.gmp_used_at_ist,
                    gmp_quality=excluded.gmp_quality,
                    decision_source=excluded.decision_source,
                    model_policy_version=excluded.model_policy_version,
                    model_action=excluded.model_action,
                    model_confidence=excluded.model_confidence,
                    primary_prediction_pct=excluded.primary_prediction_pct,
                    gmp_prediction_pct=excluded.gmp_prediction_pct,
                    subscription_prediction_pct=excluded.subscription_prediction_pct,
                    signal_conflict=excluded.signal_conflict,
                    listing_price=COALESCE(excluded.listing_price, year_model_tracker.listing_price),
                    actual_listing_gain_pct=COALESCE(excluded.actual_listing_gain_pct, year_model_tracker.actual_listing_gain_pct),
                    outcome_vs_call=excluded.outcome_vs_call,
                    shadow_v2_version=excluded.shadow_v2_version,
                    shadow_v2_triggered=excluded.shadow_v2_triggered,
                    shadow_v2_action=excluded.shadow_v2_action,
                    shadow_v2_strength=excluded.shadow_v2_strength,
                    shadow_v2_outcome=excluded.shadow_v2_outcome,
                    shadow_v2_reason=excluded.shadow_v2_reason,
                    last_updated_ist=excluded.last_updated_ist,
                    raw_json=excluded.raw_json
                """,
                r,
            )
        conn.commit()
    logger.info("YEAR_TRACKER_DB_UPSERT rows=%s", len(records))
    return len(records)

def year_model_tracker_rows(year=2026, limit=1000):
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM year_model_tracker
            WHERE year=?
            ORDER BY
              CASE WHEN actual_listing_gain_pct IS NULL THEN 0 ELSE 1 END,
              issue_close DESC,
              name
            LIMIT ?
            """,
            (int(year), int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]

def year_model_tracker_cache(year=2026):
    return {
        r["tracker_key"]: r
        for r in year_model_tracker_rows(year=year, limit=5000)
    }

def year_model_tracker_summary(year=2026):
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
              COUNT(*) AS rows,
              SUM(CASE WHEN actual_listing_gain_pct IS NOT NULL THEN 1 ELSE 0 END) AS listed_with_gain,
              SUM(CASE WHEN model_action='STRONG SUBSCRIBE' THEN 1 ELSE 0 END) AS strong_subscribe,
              SUM(CASE WHEN model_action='SUBSCRIBE' THEN 1 ELSE 0 END) AS subscribe,
              SUM(CASE WHEN model_action='BORDERLINE' THEN 1 ELSE 0 END) AS borderline,
              SUM(CASE WHEN model_action='AVOID' THEN 1 ELSE 0 END) AS avoid,
              MAX(last_updated_ist) AS last_updated_ist
            FROM year_model_tracker
            WHERE year=?
            """,
            (int(year),),
        ).fetchone()
    return dict(row) if row else {}

def canonical_research_decisions():
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM research_decisions
            WHERE is_1430_decision_snapshot=1
            ORDER BY created_at_utc DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]

def dataset_summary():
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()["n"]
        symbols = conn.execute(
            "SELECT COUNT(DISTINCT symbol) AS n FROM snapshots WHERE symbol IS NOT NULL"
        ).fetchone()["n"]
        decision = conn.execute(
            "SELECT COUNT(*) AS n FROM snapshots WHERE is_1430_decision_snapshot=1"
        ).fetchone()["n"]
        closing = conn.execute(
            "SELECT COUNT(*) AS n FROM snapshots WHERE is_closing_day=1"
        ).fetchone()["n"]
        hist = conn.execute("SELECT COUNT(*) AS n FROM historical_ipos").fetchone()["n"]
        hist_targets = conn.execute(
            "SELECT COUNT(*) AS n FROM historical_ipos WHERE listing_gain_pct IS NOT NULL"
        ).fetchone()["n"]
        web_hist = conn.execute(
            "SELECT COUNT(*) AS n FROM historical_web_ipos"
        ).fetchone()["n"]
        web_targets = conn.execute(
            "SELECT COUNT(*) AS n FROM historical_web_ipos WHERE listing_gain_pct IS NOT NULL"
        ).fetchone()["n"]
        gmp_hist = conn.execute(
            "SELECT COUNT(*) AS n FROM historical_gmp_ipos"
        ).fetchone()["n"]
        gmp_hist_complete = conn.execute(
            "SELECT COUNT(*) AS n FROM historical_gmp_ipos WHERE listing_gain_pct IS NOT NULL AND gmp_gain_pct IS NOT NULL"
        ).fetchone()["n"]
        market_hist = conn.execute(
            "SELECT COUNT(*) AS n FROM historical_market_ipos"
        ).fetchone()["n"]
        market_gmp = conn.execute(
            "SELECT COUNT(*) AS n FROM historical_market_ipos WHERE listing_gain_pct IS NOT NULL AND gmp_gain_pct IS NOT NULL"
        ).fetchone()["n"]
        market_total = conn.execute(
            "SELECT COUNT(*) AS n FROM historical_market_ipos WHERE listing_gain_pct IS NOT NULL AND log_total IS NOT NULL"
        ).fetchone()["n"]
        market_targets = conn.execute(
            "SELECT COUNT(*) AS n FROM historical_market_ipos WHERE listing_gain_pct IS NOT NULL"
        ).fetchone()["n"]
        research_decisions = conn.execute(
            "SELECT COUNT(*) AS n FROM research_decisions"
        ).fetchone()["n"]
        canonical_research_decisions = conn.execute(
            "SELECT COUNT(*) AS n FROM research_decisions WHERE is_1430_decision_snapshot=1"
        ).fetchone()["n"]
        tracker_rows = conn.execute(
            "SELECT COUNT(*) AS n FROM year_model_tracker WHERE year=2026"
        ).fetchone()["n"]
        tracker_listed = conn.execute(
            "SELECT COUNT(*) AS n FROM year_model_tracker WHERE year=2026 AND actual_listing_gain_pct IS NOT NULL"
        ).fetchone()["n"]
        tracker_last = conn.execute(
            "SELECT MAX(last_updated_ist) AS t FROM year_model_tracker WHERE year=2026"
        ).fetchone()["t"]
        exact_1430_listed = conn.execute(
            """
            SELECT COUNT(*) AS n FROM year_model_tracker
            WHERE year=2026
              AND decision_source='CAPTURED_1430_IST'
              AND actual_listing_gain_pct IS NOT NULL
            """
        ).fetchone()["n"]
        exact_1430_pending = conn.execute(
            """
            SELECT COUNT(*) AS n FROM year_model_tracker
            WHERE year=2026
              AND decision_source='CAPTURED_1430_IST'
              AND actual_listing_gain_pct IS NULL
            """
        ).fetchone()["n"]
        first = conn.execute("SELECT MIN(fetched_at_ist) AS t FROM snapshots").fetchone()["t"]
        last = conn.execute("SELECT MAX(fetched_at_ist) AS t FROM snapshots").fetchone()["t"]
    return {
        "snapshots": total,
        "unique_ipos": symbols,
        "closing_day_snapshots": closing,
        "decision_1430_snapshots": decision,
        "historical_ipos": hist,
        "historical_targets": hist_targets,
        "web_historical_ipos": web_hist,
        "web_historical_targets": web_targets,
        "gmp_historical_ipos": gmp_hist,
        "gmp_historical_complete": gmp_hist_complete,
        "market_historical_ipos": market_hist,
        "market_historical_gmp_complete": market_gmp,
        "market_historical_total_complete": market_total,
        "market_historical_targets": market_targets,
        "research_decisions": research_decisions,
        "canonical_research_decisions": canonical_research_decisions,
        "year_tracker_rows": tracker_rows,
        "year_tracker_listed": tracker_listed,
        "year_tracker_last_updated_ist": tracker_last,
        "exact_1430_listed_rows": exact_1430_listed,
        "exact_1430_pending_rows": exact_1430_pending,
        "exact_1430_target_rows": 20,
        "first_capture_ist": first,
        "last_capture_ist": last,
    }


def upsert_historical_web(records, imported_at_ist, source="ipodhan_web"):
    import math
    def lg(v):
        try:
            v = float(v)
            return math.log1p(v) if v >= 0 else None
        except Exception:
            return None

    changed = 0
    with connect() as conn:
        for r in records:
            before = conn.total_changes
            conn.execute(
                """
                INSERT INTO historical_web_ipos (
                    imported_at_ist, source, source_url, ipo_type, name,
                    issue_open, issue_close, listing_date, issue_price,
                    listing_close, listing_gain_pct, gmp_gain_pct,
                    qib_x, nii_x, retail_x, total_x, issue_size_cr,
                    log_qib, log_nii, log_retail, log_total, log_issue_size,
                    raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, ipo_type, name, listing_date, issue_price) DO UPDATE SET
                    imported_at_ist=excluded.imported_at_ist,
                    source_url=excluded.source_url,
                    issue_open=excluded.issue_open,
                    issue_close=excluded.issue_close,
                    listing_close=excluded.listing_close,
                    listing_gain_pct=excluded.listing_gain_pct,
                    gmp_gain_pct=excluded.gmp_gain_pct,
                    qib_x=excluded.qib_x,
                    nii_x=excluded.nii_x,
                    retail_x=excluded.retail_x,
                    total_x=excluded.total_x,
                    issue_size_cr=excluded.issue_size_cr,
                    log_qib=excluded.log_qib,
                    log_nii=excluded.log_nii,
                    log_retail=excluded.log_retail,
                    log_total=excluded.log_total,
                    log_issue_size=excluded.log_issue_size,
                    raw_json=excluded.raw_json
                """,
                (
                    imported_at_ist, source, r.get("source_url"), r.get("ipo_type"),
                    r.get("name"), r.get("issue_open"), r.get("issue_close"),
                    r.get("listing_date"), r.get("issue_price"), r.get("listing_close"),
                    r.get("listing_gain_pct"), r.get("gmp_gain_pct"),
                    r.get("qib_x"), r.get("nii_x"), r.get("retail_x"), r.get("total_x"),
                    r.get("issue_size_cr"), lg(r.get("qib_x")), lg(r.get("nii_x")),
                    lg(r.get("retail_x")), lg(r.get("total_x")), lg(r.get("issue_size_cr")),
                    json.dumps(r.get("raw_row") or {}, ensure_ascii=False),
                ),
            )
            if conn.total_changes > before:
                changed += 1
        conn.commit()
    return changed

def historical_web_rows(limit=5000):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM historical_web_ipos ORDER BY listing_date DESC, name LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_historical_gmp(records, imported_at_ist, source="ipowatch_2025"):
    changed = 0
    with connect() as conn:
        for r in records:
            before = conn.total_changes
            conn.execute(
                """
                INSERT INTO historical_gmp_ipos (
                    imported_at_ist, source, source_url, ipo_type, name,
                    open_date, close_date, issue_size_cr, issue_price,
                    gmp_rupees, gmp_gain_pct, listing_price, listing_gain_pct,
                    raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, ipo_type, name, close_date) DO UPDATE SET
                    imported_at_ist=excluded.imported_at_ist,
                    source_url=excluded.source_url,
                    issue_size_cr=excluded.issue_size_cr,
                    issue_price=excluded.issue_price,
                    gmp_rupees=excluded.gmp_rupees,
                    gmp_gain_pct=excluded.gmp_gain_pct,
                    listing_price=excluded.listing_price,
                    listing_gain_pct=excluded.listing_gain_pct,
                    raw_json=excluded.raw_json
                """,
                (
                    imported_at_ist, source, r.get("source_url"), r.get("ipo_type"),
                    r.get("name"), r.get("open_date"), r.get("close_date"),
                    r.get("issue_size_cr"), r.get("issue_price"), r.get("gmp_rupees"),
                    r.get("gmp_gain_pct"), r.get("listing_price"),
                    r.get("listing_gain_pct"),
                    json.dumps(r.get("raw_row") or {}, ensure_ascii=False),
                ),
            )
            if conn.total_changes > before:
                changed += 1
        conn.commit()
    return changed

def historical_gmp_rows(limit=5000):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM historical_gmp_ipos ORDER BY close_date DESC, name LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_historical_market(records, imported_at_ist, source="ipomarkets_2025"):
    import math
    changed = 0
    with connect() as conn:
        for r in records:
            total = r.get("total_x")
            try:
                log_total = math.log1p(float(total)) if total is not None and float(total) >= 0 else None
            except Exception:
                log_total = None

            record_key = _market_record_key(
                source, r.get("year"), r.get("ipo_type"), r.get("name")
            )

            existing = conn.execute(
                "SELECT id FROM historical_market_ipos WHERE record_key=?",
                (record_key,),
            ).fetchone()

            params = {
                "record_key": record_key,
                "imported_at_ist": imported_at_ist,
                "source": source,
                "year": r.get("year"),
                "ipo_type": r.get("ipo_type"),
                "name": r.get("name"),
                "detail_url": r.get("detail_url"),
                "issue_open": r.get("issue_open"),
                "issue_close": r.get("issue_close"),
                "issue_price": r.get("issue_price"),
                "total_x": r.get("total_x"),
                "qib_x": r.get("qib_x"),
                "nii_x": r.get("nii_x"),
                "retail_x": r.get("retail_x"),
                "gmp_rupees": r.get("gmp_rupees"),
                "gmp_gain_pct": r.get("gmp_gain_pct"),
                "listing_price": r.get("listing_price"),
                "listing_open": r.get("listing_open"),
                "listing_close": r.get("listing_close"),
                "listing_gain_pct": r.get("listing_gain_pct"),
                "log_total": log_total,
                "raw_json": json.dumps(r.get("raw_index") or {}, ensure_ascii=False),
            }

            if existing:
                # Preserve richer information already captured by earlier detail-page
                # enrichment when the current annual-index refresh lacks that field.
                conn.execute(
                    """
                    UPDATE historical_market_ipos SET
                        imported_at_ist=:imported_at_ist,
                        detail_url=COALESCE(:detail_url, detail_url),
                        issue_open=COALESCE(:issue_open, issue_open),
                        issue_close=COALESCE(:issue_close, issue_close),
                        issue_price=COALESCE(:issue_price, issue_price),
                        total_x=COALESCE(:total_x, total_x),
                        qib_x=COALESCE(:qib_x, qib_x),
                        nii_x=COALESCE(:nii_x, nii_x),
                        retail_x=COALESCE(:retail_x, retail_x),
                        gmp_rupees=COALESCE(:gmp_rupees, gmp_rupees),
                        gmp_gain_pct=COALESCE(:gmp_gain_pct, gmp_gain_pct),
                        listing_price=COALESCE(:listing_price, listing_price),
                        listing_open=COALESCE(:listing_open, listing_open),
                        listing_close=COALESCE(:listing_close, listing_close),
                        listing_gain_pct=COALESCE(:listing_gain_pct, listing_gain_pct),
                        log_total=COALESCE(:log_total, log_total),
                        raw_json=CASE
                            WHEN :raw_json IS NOT NULL AND :raw_json != '{}' THEN :raw_json
                            ELSE raw_json
                        END
                    WHERE record_key=:record_key
                    """,
                    params,
                )
            else:
                conn.execute(
                    """
                    INSERT INTO historical_market_ipos (
                        record_key, imported_at_ist, source, year, ipo_type, name,
                        detail_url, issue_open, issue_close, issue_price, total_x,
                        qib_x, nii_x, retail_x, gmp_rupees, gmp_gain_pct,
                        listing_price, listing_open, listing_close,
                        listing_gain_pct, log_total, raw_json
                    ) VALUES (
                        :record_key, :imported_at_ist, :source, :year, :ipo_type, :name,
                        :detail_url, :issue_open, :issue_close, :issue_price, :total_x,
                        :qib_x, :nii_x, :retail_x, :gmp_rupees, :gmp_gain_pct,
                        :listing_price, :listing_open, :listing_close,
                        :listing_gain_pct, :log_total, :raw_json
                    )
                    """,
                    params,
                )
            changed += 1
        conn.commit()

        integrity = market_integrity_summary(conn=conn)
        logger.info(
            "HISTORICAL_MARKET_UPSERT source=%s input_records=%s database_integrity=%s",
            source, len(records), integrity,
        )
    return changed


def market_integrity_summary(conn=None):
    owns = conn is None
    if owns:
        conn = connect()

    rows = conn.execute(
        "SELECT id, record_key, source, year, ipo_type, name FROM historical_market_ipos"
    ).fetchall()
    keys = {}
    missing_key = 0
    for row in rows:
        key = row["record_key"]
        if not key:
            missing_key += 1
            key = _market_record_key(
                row["source"], row["year"], row["ipo_type"], row["name"]
            )
        keys.setdefault(key, []).append(row["id"])

    duplicates = {k: ids for k, ids in keys.items() if len(ids) > 1}
    result = {
        "database_rows": len(rows),
        "unique_record_keys": len(keys),
        "duplicate_groups": len(duplicates),
        "duplicate_rows": sum(len(ids)-1 for ids in duplicates.values()),
        "missing_record_key_rows": missing_key,
        "ok": len(duplicates) == 0 and missing_key == 0,
    }
    if owns:
        conn.close()
    return result

def historical_market_rows(limit=10000):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM historical_market_ipos ORDER BY issue_close DESC, name LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
