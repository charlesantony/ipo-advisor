import json
import re
from statistics import mean, median

from shadow_v2 import shadow_signal_from_v1, shadow_outcome

PROSPECTIVE_VERSION = "prospective-experiment-v1"
EXACT_LISTED_TARGET = 20
SME_SHADOW_TRIGGER_TARGET = 5
SELECTED_ACTIONS = {"STRONG SUBSCRIBE", "SUBSCRIBE"}

def _canon(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())

def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def _key(segment, name):
    return f"{str(segment or '').upper()}|{_canon(name)}"

def _distance_from_1430(row):
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(str(row.get("created_at_ist")))
    except Exception:
        return 10**12
    target = dt.replace(hour=14, minute=30, second=0, microsecond=0)
    return abs((dt - target).total_seconds())

def _quality(gain):
    gain = _f(gain)
    if gain is None:
        return "PENDING"
    if gain >= 20:
        return "EXCELLENT"
    if gain >= 10:
        return "GOOD"
    if gain >= 5:
        return "MARGINAL"
    if gain >= 0:
        return "WEAK"
    return "LOSS"

def _dedupe_canonical(decisions):
    groups = {}
    for r in decisions or []:
        if not bool(r.get("is_1430_decision_snapshot")):
            continue
        if not _canon(r.get("name")):
            continue
        groups.setdefault(_key(r.get("ipo_type"), r.get("name")), []).append(r)

    out = {}
    for key, rows in groups.items():
        rows = sorted(
            rows,
            key=lambda r: (
                _distance_from_1430(r),
                str(r.get("created_at_ist") or ""),
            ),
        )
        out[key] = rows[0]
    return out

def _tracker_map(rows, year):
    return {
        _key(r.get("ipo_type"), r.get("name")): r
        for r in (rows or [])
        if int(r.get("year") or 0) == int(year)
    }

def _metric(rows):
    gains = [_f(r.get("actual_listing_gain_pct")) for r in rows]
    gains = [g for g in gains if g is not None]
    if not gains:
        return {
            "count": 0,
            "positive_rate_pct": None,
            "ge_10_rate_pct": None,
            "ge_20_rate_pct": None,
            "avg_gain_pct": None,
            "median_gain_pct": None,
            "worst_gain_pct": None,
            "best_gain_pct": None,
            "losses": 0,
        }

    def pct(n):
        return round(n / len(gains) * 100.0, 1)

    return {
        "count": len(gains),
        "positive_rate_pct": pct(sum(g > 0 for g in gains)),
        "ge_10_rate_pct": pct(sum(g >= 10 for g in gains)),
        "ge_20_rate_pct": pct(sum(g >= 20 for g in gains)),
        "avg_gain_pct": round(mean(gains), 2),
        "median_gain_pct": round(median(gains), 2),
        "worst_gain_pct": round(min(gains), 2),
        "best_gain_pct": round(max(gains), 2),
        "losses": sum(g < 0 for g in gains),
    }

def _capture_rate(selected_rows, listed):
    winners = {
        _key(r.get("ipo_type"), r.get("name"))
        for r in listed
        if _f(r.get("actual_listing_gain_pct")) is not None
        and _f(r.get("actual_listing_gain_pct")) >= 20
    }
    selected = {
        _key(r.get("ipo_type"), r.get("name"))
        for r in selected_rows
    }
    captured = winners & selected
    return {
        "all_ge_20_winners": len(winners),
        "captured_ge_20_winners": len(captured),
        "ge_20_winner_capture_rate_pct": (
            round(len(captured) / len(winners) * 100.0, 1)
            if winners else None
        ),
    }

def _v1_outcome(action, gain):
    if _f(gain) is None:
        return "PENDING"
    prefix = "V1 SELECTED" if action in SELECTED_ACTIONS else "V1 NOT SELECTED"
    return f"{prefix} — {_quality(gain)}"

def build_prospective_experiment(decisions, tracker_rows, year=2026):
    canonical = _dedupe_canonical(decisions)
    tracker = _tracker_map(tracker_rows, year)
    samples = []

    for key, d in canonical.items():
        t = tracker.get(key) or {}
        try:
            rec = json.loads(d.get("recommendation_json") or "{}")
        except Exception:
            rec = {}

        if not rec:
            rec = {
                "segment": d.get("ipo_type"),
                "action": d.get("action"),
                "research_confidence": d.get("research_confidence"),
                "primary_prediction_pct": d.get("primary_prediction_pct"),
                "predictions": {
                    "gmp_input_pct": d.get("gmp_input_pct"),
                    "total_subscription_x": d.get("total_subscription_x"),
                    "gmp_prediction_pct": d.get("gmp_prediction_pct"),
                    "subscription_prediction_pct": d.get(
                        "subscription_prediction_pct"
                    ),
                },
            }

        shadow = rec.get("shadow_v2") or shadow_signal_from_v1(rec)
        actual = _f(t.get("actual_listing_gain_pct"))
        samples.append({
            "name": d.get("name"),
            "ipo_type": d.get("ipo_type"),
            "captured_at_ist": d.get("created_at_ist"),
            "closing_date": d.get("end_date"),
            "tracker_status": t.get("provider_status"),
            "v1_action": d.get("action"),
            "v1_confidence": d.get("research_confidence"),
            "v1_primary_prediction_pct": d.get("primary_prediction_pct"),
            "gmp_input_pct": d.get("gmp_input_pct"),
            "total_subscription_x": d.get("total_subscription_x"),
            "gmp_prediction_pct": d.get("gmp_prediction_pct"),
            "subscription_prediction_pct": d.get(
                "subscription_prediction_pct"
            ),
            "v2_shadow_triggered": bool(shadow.get("triggered")),
            "v2_shadow_action": shadow.get("shadow_action"),
            "v2_shadow_strength": shadow.get("strength"),
            "actual_listing_gain_pct": actual,
            "actual_quality": _quality(actual),
            "v1_outcome": _v1_outcome(d.get("action"), actual),
            "v2_outcome": shadow_outcome(shadow, actual),
            "listed": actual is not None,
            "model_policy_version": d.get("policy_version"),
            "capture_reason": d.get("capture_reason"),
        })

    samples.sort(
        key=lambda r: (r.get("closing_date") or "", r.get("captured_at_ist") or ""),
        reverse=True,
    )

    listed = [r for r in samples if r["listed"]]
    pending = [r for r in samples if not r["listed"]]
    v1_selected = [r for r in listed if r.get("v1_action") in SELECTED_ACTIONS]
    v2_triggered = [r for r in listed if r.get("v2_shadow_triggered")]
    combined = [
        r for r in listed
        if r.get("v1_action") in SELECTED_ACTIONS or r.get("v2_shadow_triggered")
    ]
    mb = [r for r in listed if r.get("ipo_type") == "MAINBOARD"]
    sme = [r for r in listed if r.get("ipo_type") == "SME"]

    exact_listed = len(listed)
    progress_pct = round(
        min(100.0, exact_listed / EXACT_LISTED_TARGET * 100.0), 1
    )
    if exact_listed >= EXACT_LISTED_TARGET:
        status = "READY_FOR_MODEL_REVIEW"
        message = (
            "The first clean prospective checkpoint has been reached. "
            "Review V1 vs V2 manually before changing any model; do not auto-retune."
        )
    else:
        status = "COLLECTING_EXACT_1430_EVIDENCE"
        message = (
            f"Collect {EXACT_LISTED_TARGET - exact_listed} more exact 2:30 PM "
            "listed observations before the first model-review checkpoint."
        )

    v2_status = (
        "READY_FOR_SHADOW_REVIEW"
        if len(v2_triggered) >= SME_SHADOW_TRIGGER_TARGET
        else "INSUFFICIENT_EXACT_SHADOW_TRIGGERS"
    )

    return {
        "prospective_version": PROSPECTIVE_VERSION,
        "year": int(year),
        "target_exact_listed_rows": EXACT_LISTED_TARGET,
        "status": status,
        "message": message,
        "progress": {
            "exact_captured_unique_ipos": len(samples),
            "exact_listed_rows": exact_listed,
            "pending_listing_rows": len(pending),
            "remaining_to_checkpoint": max(0, EXACT_LISTED_TARGET - exact_listed),
            "progress_pct": progress_pct,
            "mainboard_listed_rows": len(mb),
            "sme_listed_rows": len(sme),
        },
        "v1_exact_performance": {
            "selected": _metric(v1_selected),
            **_capture_rate(v1_selected, listed),
            "selected_rows": len(v1_selected),
        },
        "v2_exact_performance": {
            "status": v2_status,
            "minimum_shadow_trigger_target": SME_SHADOW_TRIGGER_TARGET,
            "triggered": _metric(v2_triggered),
            **_capture_rate(v2_triggered, listed),
            "triggered_rows": len(v2_triggered),
        },
        "combined_exact_performance": {
            "selected": _metric(combined),
            **_capture_rate(combined, listed),
            "selected_rows": len(combined),
        },
        "by_segment": {
            "MAINBOARD": {
                "listed_rows": len(mb),
                "v1_selected": _metric([
                    r for r in mb if r.get("v1_action") in SELECTED_ACTIONS
                ]),
            },
            "SME": {
                "listed_rows": len(sme),
                "v1_selected": _metric([
                    r for r in sme if r.get("v1_action") in SELECTED_ACTIONS
                ]),
                "v2_triggered": _metric([
                    r for r in sme if r.get("v2_shadow_triggered")
                ]),
            },
        },
        "samples": samples,
        "guardrails": {
            "v1_frozen": True,
            "v2_shadow_only": True,
            "auto_retune": False,
            "checkpoint_rule": (
                "At 20 exact 2:30 PM listed observations, review results manually. "
                "Do not automatically change V1 or promote V2."
            ),
        },
    }

def log_prospective_experiment(report, logger):
    p = report.get("progress") or {}
    logger.info(
        "PROSPECTIVE_EXPERIMENT version=%s status=%s exact_captured=%s "
        "exact_listed=%s pending=%s progress_pct=%s v1=%s v2=%s",
        report.get("prospective_version"),
        report.get("status"),
        p.get("exact_captured_unique_ipos"),
        p.get("exact_listed_rows"),
        p.get("pending_listing_rows"),
        p.get("progress_pct"),
        report.get("v1_exact_performance"),
        report.get("v2_exact_performance"),
    )
    if report.get("status") == "READY_FOR_MODEL_REVIEW":
        logger.warning(
            "PROSPECTIVE_CHECKPOINT_REACHED exact_listed=%s target=%s "
            "ACTION=REVIEW_V1_V2_MANUALLY_NO_AUTO_RETUNE",
            p.get("exact_listed_rows"),
            report.get("target_exact_listed_rows"),
        )
