#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime
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

def _export_static(
    db, model_audit, shadow_v2,
    recommendation, prospective_tracker,
    live_payload=None,
):
    if live_payload is None:
        live_path = SITE_DATA / "live.json"
        if live_path.exists():
            try:
                live_payload = json.loads(
                    live_path.read_text(
                        encoding="utf-8"
                    )
                )
            except Exception:
                live_payload = {
                    "records": [],
                    "errors": [
                        "Could not read previous live.json"
                    ],
                }
        else:
            live_payload = {
                "records": [],
                "errors": [],
            }

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
    }

def _load_ledger():
    if not SENT_LEDGER.exists():
        return {"sent": {}}
    try:
        return json.loads(
            SENT_LEDGER.read_text(encoding="utf-8")
        )
    except Exception:
        return {"sent": {}}

def _eligible_alerts(live_payload):
    out = []
    for n in live_payload.get("records") or []:
        rec = n.get("recommendation") or {}
        if not n.get("is_closing_today"):
            continue
        if rec.get("action") not in {
            "STRONG SUBSCRIBE", "SUBSCRIBE"
        }:
            continue
        preds = rec.get("predictions") or {}
        out.append({
            "name": n.get("name"),
            "symbol": n.get("symbol"),
            "segment": n.get("type"),
            "action": rec.get("action"),
            "predicted_gain_pct":
                rec.get("primary_prediction_pct"),
            "gmp_gain_pct":
                preds.get("gmp_input_pct"),
            "total_x":
                preds.get("total_subscription_x"),
        })
    return out

def _send_email_once(live_payload):
    from email_alert_sender import send_research_alerts

    alerts = _eligible_alerts(live_payload)
    if not alerts:
        print(
            "EMAIL_NO_ALERTS "
            "No STRONG SUBSCRIBE/SUBSCRIBE "
            "IPO closes today."
        )
        return {
            "eligible_alerts": 0,
            "new_alerts": 0,
            "sent_alerts": 0,
        }

    today = datetime.now(IST).date().isoformat()
    ledger = _load_ledger()
    sent_map = ledger.setdefault("sent", {})

    new_alerts = []
    keys = []
    for alert in alerts:
        identity = (
            alert.get("symbol")
            or alert.get("name")
            or "ipo"
        )
        key = (
            f"{today}|{alert.get('segment')}|"
            f"{identity}|{alert.get('action')}"
        )
        if key not in sent_map:
            new_alerts.append(alert)
            keys.append(key)

    if not new_alerts:
        print(
            "EMAIL_DUPLICATE_GUARD "
            "All today's eligible alerts were already sent."
        )
        return {
            "eligible_alerts": len(alerts),
            "new_alerts": 0,
            "sent_alerts": 0,
        }

    result = send_research_alerts(
        new_alerts,
        dashboard_url=os.environ.get(
            "PUBLIC_DASHBOARD_URL", ""
        ).strip(),
    )

    if result.get("configured") and (
        result.get("failed_alerts") == 0
    ):
        now = datetime.now(IST).isoformat()
        for key in keys:
            sent_map[key] = now
        _json_write(SENT_LEDGER, ledger)

    return {
        "eligible_alerts": len(alerts),
        "new_alerts": len(new_alerts),
        "email": result,
    }

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        choices=[
            "decision", "daily", "bootstrap"
        ],
    )
    parser.add_argument(
        "--wait-until-1430",
        action="store_true",
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

    if args.mode == "decision":
        if args.wait_until_1430:
            _wait_until_1430()

        live = server.capture_live(
            reason="github_action_1430"
        )
        export = _export_static(
            db, model_audit, shadow_v2,
            recommendation,
            prospective_tracker,
            live_payload=live,
        )
        email_alert = _send_email_once(live)
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
                len(live.get("records") or []),
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
