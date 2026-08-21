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


def _wait_until_1430():
    # Scheduled workflow starts early to reduce the chance that GitHub
    # scheduler delay pushes the actual capture past 14:30.
    if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        return

    now = datetime.now(IST)
    target = now.replace(
        hour=14, minute=30, second=0, microsecond=0
    )
    seconds = (target - now).total_seconds()
    if seconds > 0:
        print(
            "WAIT_FOR_1430 "
            f"now={now.isoformat()} "
            f"sleep_seconds={int(seconds)}"
        )
        time.sleep(seconds)
    else:
        print(
            "SCHEDULE_DELAY_WARNING "
            f"runner_started_after_1430={now.isoformat()}"
        )


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
    """Keep the canonical 14:30 decision while allowing later market data refreshes."""
    now = datetime.now(IST)
    if (now.hour, now.minute) < (14, 30):
        return live_payload

    today = now.date().isoformat()
    canonical = {}
    for row in db.canonical_research_decisions():
        key = _canonical_key(row)
        if not key[1] or not key[2]:
            continue
        # Rows are newest first, so keep the first one seen.
        canonical.setdefault(key, row)

    for record in live_payload.get("records") or []:
        if str(record.get("end_date") or "") != today:
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

    tracker_rows = db.year_model_tracker_rows(
        year=2026, limit=5000
    )
    tracker = {
        "summary":
            db.year_model_tracker_summary(2026),
        "rows": tracker_rows,
    }
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
        SITE_DATA / "audit.json", audit
    )
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
        action = rec.get("action")

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
        action = rec.get("action") or "NOT READY"
        key = _record_key(record)
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
        _log_live_rate_budget(live)
        result = {
            "mode": "live",
            "fetched_at_ist": live.get("fetched_at_ist"),
            "published_at_ist": live.get("published_at_ist"),
            "live_records": len(live.get("records") or []),
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

    if args.mode == "decision":
        if args.wait_until_1430:
            _wait_until_1430()

        live, export = _capture_and_export(
            server, db, model_audit, shadow_v2,
            recommendation, prospective_tracker,
            reason="github_action_1430",
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
        export = _export_static(
            db, model_audit, shadow_v2,
            recommendation,
            prospective_tracker,
        )
        result = {
            "mode": args.mode,
            "sync": sync,
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
        export = _export_static(
            db, model_audit, shadow_v2,
            recommendation,
            prospective_tracker,
            live_payload=live,
        )
        result = {
            "mode": args.mode,
            "sync": sync,
            "live_records":
                len((export["live"] or {}).get("records") or []),
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
