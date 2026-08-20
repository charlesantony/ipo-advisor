
from statistics import mean, median

SHADOW_V2_VERSION = "sme-demand-override-shadow-v2.1"

DEMAND_TOTAL_X_THRESHOLD = 40.0
DEMAND_SUB_PRED_THRESHOLD = 25.0
STRONG_TOTAL_X_THRESHOLD = 60.0
STRONG_SUB_PRED_THRESHOLD = 30.0

ELIGIBLE_V1_ACTIONS = {"AVOID", "BORDERLINE"}

def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def shadow_signal_from_v1(v1_recommendation):
    v1 = v1_recommendation or {}
    segment = str(v1.get("segment") or "").upper()
    action = str(v1.get("action") or "").upper()
    preds = v1.get("predictions") or {}

    total_x = _f(preds.get("total_subscription_x"))
    sub_pred = _f(preds.get("subscription_prediction_pct"))
    gmp_pred = _f(preds.get("gmp_prediction_pct"))

    base = {
        "version": SHADOW_V2_VERSION,
        "segment": segment,
        "v1_action": action,
        "triggered": False,
        "shadow_action": "NO OVERRIDE",
        "strength": "NONE",
        "total_subscription_x": total_x,
        "subscription_prediction_pct": sub_pred,
        "gmp_prediction_pct": gmp_pred,
        "thresholds": {
            "candidate_total_x": DEMAND_TOTAL_X_THRESHOLD,
            "candidate_subscription_prediction_pct": DEMAND_SUB_PRED_THRESHOLD,
            "strong_total_x": STRONG_TOTAL_X_THRESHOLD,
            "strong_subscription_prediction_pct": STRONG_SUB_PRED_THRESHOLD,
        },
        "reason": [],
        "disclaimer": "Shadow V2 only. It does not replace or alter Research Model V1.",
    }

    if segment != "SME":
        base["shadow_action"] = "NOT APPLICABLE"
        base["reason"] = ["Shadow V2 is intentionally SME-only."]
        return base

    if action not in ELIGIBLE_V1_ACTIONS:
        base["reason"] = [
            "V1 is already SUBSCRIBE/STRONG SUBSCRIBE, so no demand override is needed."
        ]
        return base

    if total_x is None or sub_pred is None:
        base["reason"] = [
            "Insufficient subscription data for the SME demand-override hypothesis."
        ]
        return base

    if total_x >= STRONG_TOTAL_X_THRESHOLD and sub_pred >= STRONG_SUB_PRED_THRESHOLD:
        base["triggered"] = True
        base["shadow_action"] = "STRONG DEMAND OVERRIDE CANDIDATE"
        base["strength"] = "STRONG"
        base["reason"] = [
            f"V1={action}, but total subscription is {total_x:.2f}x.",
            f"Subscription model predicts {sub_pred:.2f}%, above the strong shadow threshold.",
            "Shadow only; V1 remains unchanged.",
        ]
        return base

    if total_x >= DEMAND_TOTAL_X_THRESHOLD and sub_pred >= DEMAND_SUB_PRED_THRESHOLD:
        base["triggered"] = True
        base["shadow_action"] = "DEMAND OVERRIDE CANDIDATE"
        base["strength"] = "STANDARD"
        base["reason"] = [
            f"V1={action}, but total subscription is {total_x:.2f}x.",
            f"Subscription model predicts {sub_pred:.2f}%, above the shadow threshold.",
            "Shadow only; V1 remains unchanged.",
        ]
        return base

    base["reason"] = ["Strong-demand override thresholds are not both satisfied."]
    return base

def shadow_outcome(signal, actual_gain):
    signal = signal or {}
    if not signal.get("triggered"):
        return "NO SHADOW TRIGGER"
    gain = _f(actual_gain)
    if gain is None:
        return "PENDING"
    if gain >= 20:
        return "SHADOW MAJOR HIT"
    if gain >= 10:
        return "SHADOW GOOD HIT"
    if gain >= 5:
        return "SHADOW MARGINAL HIT"
    if gain >= 0:
        return "SHADOW WEAK HIT"
    return "SHADOW LOSS"

def _metric(rows, gain_field="actual_listing_gain_pct"):
    gains = [_f(r.get(gain_field)) for r in rows]
    gains = [g for g in gains if g is not None]
    if not gains:
        return {
            "count": 0, "positive_rate_pct": None, "ge_10_rate_pct": None,
            "ge_20_rate_pct": None, "avg_gain_pct": None,
            "median_gain_pct": None, "worst_gain_pct": None,
            "best_gain_pct": None,
        }
    def pct(n): return round(n / len(gains) * 100.0, 1)
    return {
        "count": len(gains),
        "positive_rate_pct": pct(sum(g > 0 for g in gains)),
        "ge_10_rate_pct": pct(sum(g >= 10 for g in gains)),
        "ge_20_rate_pct": pct(sum(g >= 20 for g in gains)),
        "avg_gain_pct": round(mean(gains), 2),
        "median_gain_pct": round(median(gains), 2),
        "worst_gain_pct": round(min(gains), 2),
        "best_gain_pct": round(max(gains), 2),
    }

def audit_tracker_shadow(rows):
    listed = [
        r for r in rows
        if str(r.get("ipo_type") or "").upper() == "SME"
        and _f(r.get("actual_listing_gain_pct")) is not None
    ]
    triggered = [r for r in listed if bool(r.get("shadow_v2_triggered"))]
    v1_missed_major = [
        r for r in listed
        if str(r.get("model_action") or "").upper() in ELIGIBLE_V1_ACTIONS
        and _f(r.get("actual_listing_gain_pct")) >= 20
    ]
    recovered_major = [r for r in v1_missed_major if bool(r.get("shadow_v2_triggered"))]
    losses = [r for r in triggered if _f(r.get("actual_listing_gain_pct")) < 0]
    exact_triggered = [
        r for r in triggered if r.get("decision_source") == "CAPTURED_1430_IST"
    ]
    return {
        "version": SHADOW_V2_VERSION,
        "status": "SHADOW_ONLY_DISCOVERY_COHORT",
        "rule": {
            "eligible_v1_actions": sorted(ELIGIBLE_V1_ACTIONS),
            "candidate_total_x": DEMAND_TOTAL_X_THRESHOLD,
            "candidate_subscription_prediction_pct": DEMAND_SUB_PRED_THRESHOLD,
            "strong_total_x": STRONG_TOTAL_X_THRESHOLD,
            "strong_subscription_prediction_pct": STRONG_SUB_PRED_THRESHOLD,
        },
        "listed_sme_rows": len(listed),
        "triggered_performance": _metric(triggered),
        "v1_missed_major_winners": len(v1_missed_major),
        "recovered_major_winners": len(recovered_major),
        "major_recovery_rate_pct": (
            round(len(recovered_major) / len(v1_missed_major) * 100.0, 1)
            if v1_missed_major else None
        ),
        "shadow_losses": len(losses),
        "exact_1430_shadow_triggers": len(exact_triggered),
        "triggered_cases": [
            {
                "name": r.get("name"),
                "v1_action": r.get("model_action"),
                "shadow_action": r.get("shadow_v2_action"),
                "total_x": r.get("total_x"),
                "subscription_prediction_pct": r.get("subscription_prediction_pct"),
                "gmp_prediction_pct": r.get("gmp_prediction_pct"),
                "actual_listing_gain_pct": r.get("actual_listing_gain_pct"),
                "shadow_outcome": r.get("shadow_v2_outcome"),
                "decision_source": r.get("decision_source"),
            }
            for r in triggered
        ],
    }

def historical_crosscheck_2025(historical_rows, engine):
    rows = [
        r for r in historical_rows
        if int(r.get("year") or 0) == 2025
        and str(r.get("ipo_type") or "").upper() == "SME"
        and _f(r.get("listing_gain_pct")) is not None
    ]
    evaluated = []
    for r in rows:
        v1 = engine.classify_proxy(
            "SME", gmp_gain_pct=r.get("gmp_gain_pct"), total_x=r.get("total_x")
        )
        sig = shadow_signal_from_v1(v1)
        if sig.get("triggered"):
            evaluated.append({
                "name": r.get("name"),
                "actual_listing_gain_pct": _f(r.get("listing_gain_pct")),
                "v1_action": v1.get("action"),
                "v1_primary_prediction_pct": v1.get("primary_prediction_pct"),
                "gmp_gain_pct": r.get("gmp_gain_pct"),
                "total_x": r.get("total_x"),
                "subscription_prediction_pct": (
                    (v1.get("predictions") or {}).get("subscription_prediction_pct")
                ),
                "shadow_action": sig.get("shadow_action"),
            })
    return {
        "version": SHADOW_V2_VERSION,
        "status": "INDEPENDENT_2025_HISTORICAL_CROSSCHECK",
        "available_2025_sme_rows": len(rows),
        "triggered_performance": _metric(evaluated),
        "triggered_cases": evaluated,
        "important_note": (
            "2025 historical GMP coverage is incomplete, so sample size may be small. "
            "The rule remains shadow-only until prospective exact 2:30 PM evidence accumulates."
        ),
    }

def threshold_grid_2025(historical_rows, engine):
    total_thresholds = (20.0, 40.0, 60.0)
    sub_thresholds = (20.0, 25.0, 30.0)
    rows = [
        r for r in historical_rows
        if int(r.get("year") or 0) == 2025
        and str(r.get("ipo_type") or "").upper() == "SME"
        and _f(r.get("listing_gain_pct")) is not None
    ]
    base = []
    for r in rows:
        v1 = engine.classify_proxy(
            "SME", gmp_gain_pct=r.get("gmp_gain_pct"), total_x=r.get("total_x")
        )
        if str(v1.get("action") or "").upper() not in ELIGIBLE_V1_ACTIONS:
            continue
        p = v1.get("predictions") or {}
        base.append({
            "name": r.get("name"),
            "actual_listing_gain_pct": r.get("listing_gain_pct"),
            "total_x": _f(p.get("total_subscription_x")),
            "sub_pred": _f(p.get("subscription_prediction_pct")),
        })
    grid = []
    for total_t in total_thresholds:
        for sub_t in sub_thresholds:
            selected = [
                r for r in base
                if r.get("total_x") is not None and r.get("sub_pred") is not None
                and r["total_x"] >= total_t and r["sub_pred"] >= sub_t
            ]
            grid.append({
                "total_x_threshold": total_t,
                "subscription_prediction_threshold": sub_t,
                **_metric(selected),
                "is_prespecified_rule": (
                    total_t == DEMAND_TOTAL_X_THRESHOLD
                    and sub_t == DEMAND_SUB_PRED_THRESHOLD
                ),
            })
    return grid
