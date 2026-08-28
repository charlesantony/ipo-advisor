#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine"
STATE = ROOT / "state"
SITE_DATA = ROOT / "site" / "data"
DB_STATE = STATE / "ipo_advisor.db"
DB_ENGINE = ENGINE / "ipo_advisor.db"
SENT_LEDGER = STATE / "email_sent.json"
IST = ZoneInfo("Asia/Kolkata")
STALE_SCHEDULE_MARKER = ROOT / ".stale_schedule_skip"

sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ROOT / "scripts"))


def _json_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(value, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temp.replace(path)


def _restore_state():
    STATE.mkdir(parents=True, exist_ok=True)
    SITE_DATA.mkdir(parents=True, exist_ok=True)
    if DB_STATE.exists():
        shutil.copy2(DB_STATE, DB_ENGINE)


def _persist_state():
    if DB_ENGINE.exists():
        shutil.copy2(DB_ENGINE, DB_STATE)


def _wait_until_time(
    hour,
    minute,
    label,
    max_schedule_early_minutes=30,
):
    # Primary cron entries start only shortly before the intended checkpoint.
    # If a scheduled occurrence reaches a runner many hours later (for example
    # after midnight), its target on the current date is in the future. The
    # old implementation then slept until that evening. Treat that as stale.
    force_wait = str(
        os.environ.get("FORCE_CHECKPOINT_WAIT") or ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    event_name = str(
        os.environ.get("GITHUB_EVENT_NAME") or ""
    ).strip().lower()

    if event_name != "schedule" and not force_wait:
        print(
            f"{label}_MANUAL_NO_WAIT "
            f"event={event_name or 'local'}"
        )
        return True

    now = datetime.now(IST)
    target = now.replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    seconds = (target - now).total_seconds()

    if (
        event_name == "schedule"
        and seconds > max_schedule_early_minutes * 60
    ):
        print(
            f"{label}_STALE_SCHEDULE_SKIP "
            f"now={now.isoformat()} "
            f"target={target.isoformat()} "
            f"seconds_until_current_day_target={int(seconds)}"
        )
        return False

    if seconds > 0:
        print(
            f"WAIT_FOR_{label} "
            f"now={now.isoformat()} "
            f"sleep_seconds={int(seconds)}"
        )
        time.sleep(seconds)
        return True

    print(
        "SCHEDULE_DELAY_WARNING "
        f"checkpoint={label} "
        f"runner_started_after_target={now.isoformat()} "
        "running_immediately=true"
    )
    return True


def _wait_until_1430():
    return _wait_until_time(14, 30, "1430")


def _wait_until_2030():
    return _wait_until_time(20, 30, "2030")


def _ensure_training(server, db):
    rows = db.historical_market_rows(limit=20000)
    n2024 = sum(
        1 for r in rows
        if int(r.get("year") or 0) == 2024
    )
    n2025 = sum(
        1 for r in rows
        if int(r.get("year") or 0) == 2025
    )

    if n2024 < 100:
        print(
            "BOOTSTRAP_HISTORY year=2024 "
            f"existing_rows={n2024}"
        )
        server.import_market_year_history(
            2024, target_gmp_per_segment=25
        )

    if n2025 < 300:
        print(
            "BOOTSTRAP_HISTORY year=2025 "
            f"existing_rows={n2025}"
        )
        server.import_market_year_history(
            2025, target_gmp_per_segment=25
        )


def _audit_payload(db, model_audit, shadow_v2, recommendation):
    rows = db.year_model_tracker_rows(
        year=2026, limit=5000
    )
    audit = model_audit.build_model_audit(
        rows, year=2026
    )
    hist = db.historical_market_rows(limit=20000)
    engine = recommendation.ResearchDecisionEngine(hist)
    audit["shadow_v2"] = {
        "discovery_2026":
            shadow_v2.audit_tracker_shadow(rows),
        "historical_crosscheck_2025":
            shadow_v2.historical_crosscheck_2025(
                hist, engine
            ),
        "threshold_sensitivity_2025":
            shadow_v2.threshold_grid_2025(
                hist, engine
            ),
    }
    return audit


def _prospective_payload(db, prospective_tracker):
    return prospective_tracker.build_prospective_experiment(
        db.canonical_research_decisions(),
        db.year_model_tracker_rows(
            year=2026, limit=5000
        ),
        year=2026,
    )


def _canonical_key(value):
    symbol = str(value.get("symbol") or "").strip().upper()
    name = str(value.get("name") or "").strip().upper()
    segment = str(
        value.get("ipo_type") or value.get("type") or ""
    ).strip().upper()
    end_date = str(value.get("end_date") or "").strip()
    identity = symbol or name
    return (segment, identity, end_date)


def _retain_1430_display_decisions(db, live_payload):
    """Keep every captured 14:30 decision immutable after its checkpoint."""
    now = datetime.now(IST)
    today = now.date()
    canonical = {}
    for row in db.canonical_research_decisions():
        key = _canonical_key(row)
        if not key[1] or not key[2]:
            continue
        canonical.setdefault(key, row)

    for record in live_payload.get("records") or []:
        end = _parse_iso_date(record.get("end_date"))
        if not end or end > today:
            continue
        if end == today and (now.hour, now.minute) < (14, 30):
            continue

        row = canonical.get(_canonical_key(record))
        if not row:
            continue

        rec = record.get("recommendation") or {}
        rec["action"] = row.get("action")
        rec["research_confidence"] = row.get("research_confidence")
        rec["primary_prediction_pct"] = row.get("primary_prediction_pct")
        rec["policy_version"] = row.get("policy_version")
        rec["signal_conflict"] = bool(row.get("signal_conflict"))
        rec["finality"] = {
            "canonical": True,
            "code": "CAPTURED_1430_IST",
            "label": "CANONICAL_1430_RETAINED",
            "captured_at_ist": row.get("created_at_ist"),
        }
        rec["display_decision_source"] = "CAPTURED_1430_IST"
        record["recommendation"] = rec

    return live_payload


def _previous_live_payload():
    live_path = SITE_DATA / "live.json"
    if not live_path.exists():
        return None
    try:
        return json.loads(live_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _filter_closed_public_live(live_payload):
    """Remove records whose actual issue end date has passed."""
    today = datetime.now(IST).date()
    kept = []
    removed = []
    for record in live_payload.get("records") or []:
        end = _parse_iso_date(record.get("end_date"))
        if end and end < today:
            removed.append(record.get("name") or record.get("symbol"))
            continue
        kept.append(record)
    live_payload["records"] = kept
    live_payload["public_filtered_closed_count"] = len(removed)
    if removed:
        live_payload["public_filtered_closed_names"] = removed
    return live_payload


PUBLIC_SIGNAL_ACTIONS = {
    "STRONG SUBSCRIBE",
    "SUBSCRIBE",
    "BORDERLINE",
    "AVOID",
}


def _public_live_signal_state(record):
    """Return whether a model signal is suitable for public display.

    The model may still calculate and retain its raw internal action. Public
    display is deliberately more conservative: the IPO must be live/open and
    the current market snapshot must contain enough market evidence to make a
    sufficiently grounded public call. The UI receives only a generic reason
    when this gate is not satisfied.
    """
    rec = record.get("recommendation") or {}
    preds = rec.get("predictions") or {}
    action = str(rec.get("action") or "").strip().upper()
    status = str(record.get("status") or "").strip().upper()
    today = datetime.now(IST).date()
    start = _parse_iso_date(record.get("start_date"))

    if status != "LIVE":
        return {
            "locked": True,
            "action": "LOCKED",
            "reason": "RELEVANT_DATA_NOT_AVAILABLE",
        }
    if start and today < start:
        return {
            "locked": True,
            "action": "LOCKED",
            "reason": "RELEVANT_DATA_NOT_AVAILABLE",
        }
    if action not in PUBLIC_SIGNAL_ACTIONS:
        return {
            "locked": True,
            "action": "LOCKED",
            "reason": "RELEVANT_DATA_NOT_AVAILABLE",
        }

    # A subscription-only V1 signal may be published only after the missing
    # GMP validation itself has completed.
    gmp_validation = record.get("gmp_validation") or {}
    if (
        preds.get("gmp_input_pct") is None
        and not bool(gmp_validation.get("complete"))
    ):
        return {
            "locked": True,
            "action": "LOCKED",
            "reason": "RELEVANT_DATA_NOT_AVAILABLE",
        }

    # Current V1 can calculate a raw action from GMP alone. Do not expose that
    # publicly until a current demand observation is also available.
    if preds.get("total_subscription_x") is None:
        return {
            "locked": True,
            "action": "LOCKED",
            "reason": "RELEVANT_DATA_NOT_AVAILABLE",
        }

    return {
        "locked": False,
        "action": action,
        "reason": None,
    }


def _apply_public_live_signal_gate(live_payload):
    """Sanitize public live JSON while preserving raw model state in the DB."""
    for record in live_payload.get("records") or []:
        rec = dict(record.get("recommendation") or {})
        # V2 remains available through Model Audit, not the normal public feed.
        rec.pop("shadow_v2", None)
        state = _public_live_signal_state(record)
        rec["public_signal"] = state

        if state["locked"]:
            rec["action"] = "LOCKED"
            rec["action_priority"] = 0
            rec["research_confidence"] = None
            rec["primary_prediction_pct"] = None
            rec["ranking_score"] = -999.0
            rec["signal_conflict"] = False
            rec["reason"] = ["Relevant data not available."]

            preds = dict(rec.get("predictions") or {})
            # Keep observed market inputs visible, but hide derived model
            # outputs so a locked call cannot be inferred from public JSON.
            preds["gmp_prediction_pct"] = None
            preds["subscription_prediction_pct"] = None
            preds["log_total"] = None
            rec["predictions"] = preds

        record["recommendation"] = rec

    live_payload["public_signal_policy"] = {
        "version": "public-signal-gate-v1",
        "locked_label": "Relevant data not available",
    }
    return live_payload


def _public_tracker_row(row):
    """Return a sanitized tracker row for the public site."""
    item = dict(row)
    # Keep experimental V2 fields internal to the normal tracker feed.
    for key in list(item):
        if key.startswith("shadow_v2_"):
            item.pop(key, None)

    action = str(item.get("model_action") or "").strip().upper()
    today = datetime.now(IST).date()
    open_date = _parse_iso_date(item.get("issue_open"))

    locked = False
    if open_date and today < open_date:
        locked = True
    if item.get("total_x") is None:
        locked = True
    if action not in PUBLIC_SIGNAL_ACTIONS:
        locked = True

    item["public_signal"] = {
        "locked": locked,
        "action": "LOCKED" if locked else action,
        "reason": (
            "RELEVANT_DATA_NOT_AVAILABLE"
            if locked else None
        ),
    }

    if locked:
        item["model_action"] = "LOCKED"
        item["model_confidence"] = None
        item["primary_prediction_pct"] = None
        item["gmp_prediction_pct"] = None
        item["subscription_prediction_pct"] = None
        item["signal_conflict"] = 0
        item["outcome_vs_call"] = (
            "NOT EVALUABLE"
            if item.get("actual_listing_gain_pct") is not None
            else None
        )

    return item


def _public_tracker_rows(rows):
    return [_public_tracker_row(row) for row in rows or []]


def _prepare_light_live_payload(db, live_payload):
    """Prepare a public live snapshot without recording a research checkpoint."""
    if (
        not (live_payload.get("records") or [])
        and (live_payload.get("errors") or [])
    ):
        previous = _previous_live_payload()
        if previous and (previous.get("records") or []):
            previous["refresh_warning"] = {
                "at_ist": datetime.now(IST).isoformat(),
                "errors": live_payload.get("errors") or [],
            }
            live_payload = previous

    live_payload = _retain_1430_display_decisions(
        db, live_payload
    )
    live_payload = _filter_closed_public_live(live_payload)
    live_payload = _apply_public_live_signal_gate(live_payload)
    live_payload["refresh_kind"] = "LIGHT_LIVE"
    live_payload["published_at_ist"] = datetime.now(IST).isoformat()
    return live_payload


def _log_live_rate_budget(live_payload):
    endpoint_values = []
    global_values = []
    for meta in live_payload.get("source_meta") or []:
        endpoint = meta.get("rate_remaining_endpoint")
        global_remaining = meta.get("rate_remaining_global")
        if endpoint is not None:
            endpoint_values.append(endpoint)
        if global_remaining is not None:
            global_values.append(global_remaining)
    print(
        "LIVE_REFRESH_RATE "
        f"endpoint_remaining={endpoint_values or 'unknown'} "
        f"global_remaining={global_values or 'unknown'}"
    )


def _export_static(
    db, model_audit, shadow_v2,
    recommendation, prospective_tracker,
    live_payload=None,
    listed_payload=None,
):
    if live_payload is None:
        live_payload = _previous_live_payload()
        if live_payload is None:
            live_payload = {
                "records": [],
                "errors": [],
            }

    # If a live refresh failed completely, retain the previous public snapshot
    # rather than replacing the dashboard with an empty error response.
    if (
        not (live_payload.get("records") or [])
        and (live_payload.get("errors") or [])
    ):
        previous = _previous_live_payload()
        if previous and (previous.get("records") or []):
            previous["refresh_warning"] = {
                "at_ist": datetime.now(IST).isoformat(),
                "errors": live_payload.get("errors") or [],
            }
            live_payload = previous

    live_payload = _retain_1430_display_decisions(
        db, live_payload
    )
    live_payload = _filter_closed_public_live(live_payload)
    live_payload = _apply_public_live_signal_gate(live_payload)

    tracker_rows = db.year_model_tracker_rows(
        year=2026, limit=5000
    )
    tracker = {
        "summary":
            db.year_model_tracker_summary(2026),
        "rows": _public_tracker_rows(tracker_rows),
    }

    if listed_payload is None:
        import listed_tracker
        listed_payload = listed_tracker.build_listed_payload(
            tracker_rows,
            deep_refresh=False,
            persist=False,
        )

    prospective = _prospective_payload(
        db, prospective_tracker
    )
    audit = _audit_payload(
        db, model_audit, shadow_v2,
        recommendation
    )
    health = db.dataset_summary()
    health["generated_at_ist"] = (
        datetime.now(IST).isoformat()
    )
    health["v1_policy"] = "FROZEN"
    health["v2_policy"] = "SHADOW_ONLY"

    _json_write(
        SITE_DATA / "live.json", live_payload
    )
    _json_write(
        SITE_DATA / "year_tracker.json", tracker
    )
    _json_write(
        SITE_DATA / "prospective.json", prospective
    )
    _json_write(
        SITE_DATA / "listed.json", listed_payload
    )

    # Model audit remains generated internally, but it is not a public-site
    # artifact. Remove any old public copy left by earlier releases.
    public_audit = SITE_DATA / "audit.json"
    if public_audit.exists():
        public_audit.unlink()

    _json_write(
        SITE_DATA / "health.json", health
    )
    _json_write(
        SITE_DATA / "config.json",
        {
            "subscription_endpoint": os.environ.get(
                "SUBSCRIBE_ENDPOINT", ""
            ).strip(),
        },
    )

    return {
        "tracker": tracker,
        "prospective": prospective,
        "audit": audit,
        "listed": listed_payload,
        "health": health,
        "live": live_payload,
    }


def _load_ledger():
    base = {
        "sent": {},
        "day2": {},
        "closing": {},
    }
    if not SENT_LEDGER.exists():
        return base
    try:
        value = json.loads(
            SENT_LEDGER.read_text(encoding="utf-8")
        )
        if not isinstance(value, dict):
            return base
        value.setdefault("sent", {})
        value.setdefault("day2", {})
        value.setdefault("closing", {})
        return value
    except Exception:
        return base


def _parse_iso_date(value):
    try:
        return datetime.strptime(
            str(value or ""), "%Y-%m-%d"
        ).date()
    except (TypeError, ValueError):
        return None


def _bidding_day_number(start_date, current_date):
    """Weekday-based bidding-day number; LIVE provider status is also required."""
    if not start_date or current_date < start_date:
        return 0
    cursor = start_date
    count = 0
    while cursor <= current_date:
        if cursor.weekday() < 5:
            count += 1
        cursor += timedelta(days=1)
    return count


def _record_key(record):
    segment = str(record.get("type") or "").strip().upper()
    identity = str(
        record.get("symbol")
        or record.get("name")
        or "ipo"
    ).strip().upper()
    start_date = str(record.get("start_date") or "").strip()
    return f"{segment}|{identity}|{start_date}"


def _alert_from_record(
    record, alert_kind, previous_signal=""
):
    rec = record.get("recommendation") or {}
    preds = rec.get("predictions") or {}
    return {
        "name": record.get("name"),
        "symbol": record.get("symbol"),
        "segment": record.get("type"),
        "action": rec.get("action") or "NOT READY",
        "predicted_gain_pct":
            rec.get("primary_prediction_pct"),
        "gmp_gain_pct":
            preds.get("gmp_input_pct"),
        "total_x":
            preds.get("total_subscription_x"),
        "start_date": record.get("start_date"),
        "end_date": record.get("end_date"),
        "alert_kind": alert_kind,
        "previous_signal": previous_signal,
    }


def _send_day2_email(live_payload):
    from email_alert_sender import send_research_alerts

    today = datetime.now(IST).date()
    ledger = _load_ledger()
    sent_map = ledger.setdefault("day2", {})
    candidates = []

    for record in live_payload.get("records") or []:
        start = _parse_iso_date(record.get("start_date"))
        end = _parse_iso_date(record.get("end_date"))
        rec = record.get("recommendation") or {}
        public_signal = rec.get("public_signal") or {}
        action = rec.get("action")

        if public_signal.get("locked") or action == "LOCKED":
            continue

        # Day-2 alert is useful only before the actual closing day.
        if not start or not end or not (start <= today < end):
            continue
        if _bidding_day_number(start, today) != 2:
            continue
        if action not in {"STRONG SUBSCRIBE", "SUBSCRIBE"}:
            continue

        key = _record_key(record)
        if key in sent_map:
            continue
        candidates.append((key, record))

    if not candidates:
        print(
            "EMAIL_DAY2_NO_ALERTS "
            "No new Day-2 STRONG SUBSCRIBE/SUBSCRIBE signals."
        )
        return {
            "eligible_alerts": 0,
            "new_alerts": 0,
            "sent_alerts": 0,
        }

    alerts = [
        _alert_from_record(record, "DAY2_EARLY")
        for _, record in candidates
    ]
    result = send_research_alerts(
        alerts,
        dashboard_url=os.environ.get(
            "PUBLIC_DASHBOARD_URL", ""
        ).strip(),
    )

    if result.get("configured") and (
        result.get("failed_alerts") == 0
    ):
        now = datetime.now(IST).isoformat()
        for key, record in candidates:
            action = (
                (record.get("recommendation") or {})
                .get("action")
            )
            sent_map[key] = {
                "sent_at_ist": now,
                "action": action,
                "name": record.get("name"),
                "segment": record.get("type"),
                "start_date": record.get("start_date"),
                "end_date": record.get("end_date"),
            }
        _json_write(SENT_LEDGER, ledger)

    return {
        "eligible_alerts": len(candidates),
        "new_alerts": len(candidates),
        "email": result,
    }


def _promote_emailed_manual_recoveries(db):
    """Promote manual closing-day recovery batches that actually sent email."""
    ledger = _load_ledger()
    closing = ledger.get("closing") or {}
    seen = set()
    promoted = 0

    for entry in closing.values():
        if not isinstance(entry, dict):
            continue
        end_date = str(entry.get("end_date") or "").strip()
        sent_at = str(entry.get("sent_at_ist") or "").strip()
        if not end_date or not sent_at:
            continue

        batch_key = (end_date, sent_at)
        if batch_key in seen:
            continue
        seen.add(batch_key)

        try:
            sent_dt = datetime.fromisoformat(sent_at)
            if sent_dt.tzinfo is None:
                sent_dt = sent_dt.replace(tzinfo=IST)
            sent_dt = sent_dt.astimezone(IST)
        except (TypeError, ValueError):
            continue

        # Research decisions are saved immediately before the email is sent.
        # Promote the whole closing-day batch, not just positive emailed IPOs,
        # so the prospective sample remains unbiased.
        start = (sent_dt - timedelta(minutes=20)).isoformat()
        finish = (sent_dt + timedelta(minutes=2)).isoformat()
        promoted += db.promote_manual_checkpoint_batch(
            end_date=end_date,
            start_ist=start,
            end_ist=finish,
        )

    if promoted:
        print(
            "MANUAL_CHECKPOINT_BACKFILL "
            f"promoted_rows={promoted}"
        )
    return promoted


def _send_closing_email(live_payload):
    from email_alert_sender import send_research_alerts

    today = datetime.now(IST).date().isoformat()
    ledger = _load_ledger()
    early_map = ledger.setdefault("day2", {})
    closing_map = ledger.setdefault("closing", {})
    candidates = []

    for record in live_payload.get("records") or []:
        if str(record.get("end_date") or "") != today:
            continue

        rec = record.get("recommendation") or {}
        public_signal = rec.get("public_signal") or {}
        action = rec.get("action") or "NOT READY"
        key = _record_key(record)

        if public_signal.get("locked") or action == "LOCKED":
            continue
        early = early_map.get(key)
        early_action = (
            early.get("action")
            if isinstance(early, dict)
            else ""
        )

        # Normal closing-day email is sent only for a positive V1 signal.
        # If a Day-2 early alert was sent, always send the closing-day update
        # so the subscriber sees whether that early signal strengthened,
        # weakened, or reversed.
        qualifies_now = action in {
            "STRONG SUBSCRIBE", "SUBSCRIBE"
        }
        if not qualifies_now and not early:
            continue
        if key in closing_map:
            continue

        alert_kind = (
            "CLOSING_UPDATE" if early
            else "CLOSING_DAY"
        )
        alert = _alert_from_record(
            record,
            alert_kind,
            previous_signal=early_action,
        )
        candidates.append((key, record, alert))

    if not candidates:
        print(
            "EMAIL_CLOSING_NO_ALERTS "
            "No new closing-day email is required."
        )
        return {
            "eligible_alerts": 0,
            "new_alerts": 0,
            "sent_alerts": 0,
        }

    result = send_research_alerts(
        [item[2] for item in candidates],
        dashboard_url=os.environ.get(
            "PUBLIC_DASHBOARD_URL", ""
        ).strip(),
    )

    if result.get("configured") and (
        result.get("failed_alerts") == 0
    ):
        now = datetime.now(IST).isoformat()
        for key, record, alert in candidates:
            closing_map[key] = {
                "sent_at_ist": now,
                "action": alert.get("action"),
                "previous_signal":
                    alert.get("previous_signal") or "",
                "name": record.get("name"),
                "segment": record.get("type"),
                "end_date": record.get("end_date"),
            }
        _json_write(SENT_LEDGER, ledger)

    return {
        "eligible_alerts": len(candidates),
        "new_alerts": len(candidates),
        "email": result,
    }


def _capture_and_export(
    server, db, model_audit, shadow_v2,
    recommendation, prospective_tracker,
    reason,
):
    live = server.capture_live(reason=reason)
    export = _export_static(
        db, model_audit, shadow_v2,
        recommendation,
        prospective_tracker,
        live_payload=live,
    )
    return export["live"], export


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        choices=[
            "decision", "day2", "refresh", "live",
            "daily", "bootstrap"
        ],
    )
    parser.add_argument(
        "--wait-until-1430",
        action="store_true",
    )
    parser.add_argument(
        "--wait-until-2030",
        action="store_true",
    )
    parser.add_argument(
        "--phase",
        default="manual",
        choices=[
            "manual", "rollover", "morning",
            "sme_close", "mainboard_close"
        ],
    )
    args = parser.parse_args()

    _restore_state()

    # Import after state is restored.
    import db
    import listed_tracker
    import model_audit
    import prospective_tracker
    import recommendation
    import server
    import shadow_v2
    import year_tracker

    _ensure_training(server, db)

    if args.mode == "live":
        # Lightweight public-dashboard refresh. This intentionally does not
        # call capture_live(), save snapshots/research decisions, persist the
        # database, update the experiment report, or send email.
        live = server.fetch_normalized(
            status="LIVE",
            ipo_type="ALL",
        )
        live = _prepare_light_live_payload(db, live)
        _json_write(SITE_DATA / "live.json", live)

        listed = listed_tracker.build_listed_payload(
            db.year_model_tracker_rows(
                year=2026, limit=5000
            ),
            deep_refresh=False,
            persist=False,
        )
        _json_write(SITE_DATA / "listed.json", listed)

        _log_live_rate_budget(live)
        result = {
            "mode": "live",
            "fetched_at_ist": live.get("fetched_at_ist"),
            "published_at_ist": live.get("published_at_ist"),
            "live_records": len(live.get("records") or []),
            "listed_records": len(listed.get("records") or []),
            "listed_prices": (
                (listed.get("summary") or {})
                .get("current_price_available")
            ),
            "errors": live.get("errors") or [],
        }
        print(
            json.dumps(
                result, indent=2,
                ensure_ascii=False,
                default=str,
            )
        )
        return

    # Recover prior manual closing-day runs that genuinely sent closing email.
    # This is idempotent and persists with the next stateful job.
    _promote_emailed_manual_recoveries(db)

    if args.mode == "decision":
        if (
            args.wait_until_1430
            and not _wait_until_1430()
        ):
            STALE_SCHEDULE_MARKER.write_text(
                "decision stale schedule\n",
                encoding="utf-8",
            )
            print(json.dumps({
                "mode": "decision",
                "status": "SKIPPED_STALE_SCHEDULE",
                "completed_at_ist": datetime.now(IST).isoformat(),
            }, indent=2))
            return

        event_name = str(
            os.environ.get("GITHUB_EVENT_NAME") or ""
        ).strip().lower()
        schedule_recovery = str(
            os.environ.get("CHECKPOINT_RECOVERY") or ""
        ).strip() == "1"
        manual_recovery = str(
            os.environ.get("MANUAL_CHECKPOINT_RECOVERY") or ""
        ).strip() == "1"

        if event_name == "schedule":
            checkpoint_reason = (
                "github_action_1430_recovery"
                if schedule_recovery
                else "github_action_1430"
            )
        elif manual_recovery:
            checkpoint_reason = "manual_1430_recovery"
        else:
            checkpoint_reason = "manual_1430"
        live, export = _capture_and_export(
            server, db, model_audit, shadow_v2,
            recommendation, prospective_tracker,
            reason=checkpoint_reason,
        )
        email_alert = _send_closing_email(live)
        result = {
            "mode": args.mode,
            "fetched_at_ist":
                live.get("fetched_at_ist"),
            "live_records":
                len(live.get("records") or []),
            "decision_saved_count":
                live.get("decision_saved_count"),
            "email_alert": email_alert,
            "prospective_status":
                export["prospective"].get("status"),
        }

    elif args.mode == "day2":
        if (
            args.wait_until_2030
            and not _wait_until_2030()
        ):
            STALE_SCHEDULE_MARKER.write_text(
                "day2 stale schedule\n",
                encoding="utf-8",
            )
            print(json.dumps({
                "mode": "day2",
                "status": "SKIPPED_STALE_SCHEDULE",
                "completed_at_ist": datetime.now(IST).isoformat(),
            }, indent=2))
            return

        live, export = _capture_and_export(
            server, db, model_audit, shadow_v2,
            recommendation, prospective_tracker,
            reason="github_action_day2_2030",
        )
        email_alert = _send_day2_email(live)
        result = {
            "mode": args.mode,
            "fetched_at_ist":
                live.get("fetched_at_ist"),
            "live_records":
                len(live.get("records") or []),
            "email_alert": email_alert,
            "prospective_status":
                export["prospective"].get("status"),
        }

    elif args.mode == "refresh":
        reason = f"github_action_refresh_{args.phase}"
        live, export = _capture_and_export(
            server, db, model_audit, shadow_v2,
            recommendation, prospective_tracker,
            reason=reason,
        )
        result = {
            "mode": args.mode,
            "phase": args.phase,
            "fetched_at_ist":
                live.get("fetched_at_ist"),
            "live_records":
                len(live.get("records") or []),
            "prospective_status":
                export["prospective"].get("status"),
        }

    elif args.mode == "daily":
        sync = year_tracker.sync_year_tracker(
            year=2026,
            force_detail_refresh=False,
        )

        # Never let the tracker/outcome job republish an older checked-in
        # live.json over a newer Pages-only Live Refresh. Re-fetch current
        # live data (including GMP/subscription fallbacks) before deployment.
        live = server.fetch_normalized(
            status="LIVE",
            ipo_type="ALL",
        )
        listed = listed_tracker.build_listed_payload(
            db.year_model_tracker_rows(
                year=2026, limit=5000
            ),
            deep_refresh=True,
            persist=True,
        )
        export = _export_static(
            db, model_audit, shadow_v2,
            recommendation,
            prospective_tracker,
            live_payload=live,
            listed_payload=listed,
        )
        result = {
            "mode": args.mode,
            "sync": sync,
            "live_fetched_at_ist":
                (export.get("live") or {}).get("fetched_at_ist"),
            "live_records":
                len((export.get("live") or {}).get("records") or []),
            "prospective_status":
                export["prospective"].get("status"),
        }

    else:
        sync = year_tracker.sync_year_tracker(
            year=2026,
            force_detail_refresh=False,
        )
        live = server.fetch_normalized(
            status="LIVE",
            ipo_type="ALL",
        )
        listed = listed_tracker.build_listed_payload(
            db.year_model_tracker_rows(
                year=2026, limit=5000
            ),
            deep_refresh=True,
            persist=True,
        )
        export = _export_static(
            db, model_audit, shadow_v2,
            recommendation,
            prospective_tracker,
            live_payload=live,
            listed_payload=listed,
        )
        result = {
            "mode": args.mode,
            "sync": sync,
            "live_records":
                len((export["live"] or {}).get("records") or []),
            "listed_records":
                len((export["listed"] or {}).get("records") or []),
            "prospective_status":
                export["prospective"].get("status"),
        }

    _persist_state()
    _json_write(
        SITE_DATA / "last_job.json",
        {
            "completed_at_ist":
                datetime.now(IST).isoformat(),
            "result": result,
        },
    )
    print(
        json.dumps(
            result, indent=2,
            ensure_ascii=False,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
