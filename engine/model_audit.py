import math
from collections import Counter
from statistics import mean, median

AUDIT_VERSION = "audit-v1"
FROZEN_POLICY_VERSION = "research-v1"

SELECTED_ACTIONS = {"STRONG SUBSCRIBE", "SUBSCRIBE"}

def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def _round(v, digits=2):
    return round(v, digits) if v is not None else None

def _pct(num, den):
    if not den:
        return None
    return round(num / den * 100.0, 1)

def _pearson(xs, ys):
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    mx, my = mean(xs), mean(ys)
    num = sum((x-mx)*(y-my) for x, y in zip(xs, ys))
    dx = sum((x-mx)**2 for x in xs)
    dy = sum((y-my)**2 for y in ys)
    if dx <= 0 or dy <= 0:
        return None
    return num / math.sqrt(dx * dy)

def return_quality(gain):
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

def avoid_miss_severity(gain):
    gain = _f(gain)
    if gain is None:
        return "PENDING"
    if gain <= 0:
        return "CORRECT AVOID"
    if gain < 5:
        return "MINOR MISS"
    if gain < 10:
        return "MISS"
    if gain < 20:
        return "SIGNIFICANT MISS"
    return "MAJOR MISS"

def call_outcome_label(action, gain):
    gain = _f(gain)
    if gain is None:
        return "PENDING"

    action = str(action or "").upper()
    quality = return_quality(gain)

    if action in SELECTED_ACTIONS:
        return {
            "EXCELLENT": "EXCELLENT CALL",
            "GOOD": "GOOD CALL",
            "MARGINAL": "MARGINAL WIN",
            "WEAK": "WEAK WIN",
            "LOSS": "CALL LOST",
        }[quality]

    if action == "AVOID":
        return avoid_miss_severity(gain)

    if action == "BORDERLINE":
        return {
            "EXCELLENT": "BORDERLINE — EXCELLENT UPSIDE",
            "GOOD": "BORDERLINE — GOOD UPSIDE",
            "MARGINAL": "BORDERLINE — MARGINAL UPSIDE",
            "WEAK": "BORDERLINE — WEAK/FLAT",
            "LOSS": "BORDERLINE — LOSS",
        }[quality]

    return "NOT EVALUABLE"

def _row_for_json(r):
    keys = (
        "name", "ipo_type", "issue_close", "provider_status",
        "model_action", "model_confidence", "decision_source",
        "primary_prediction_pct", "gmp_prediction_pct",
        "subscription_prediction_pct", "gmp_used_pct",
        "gmp_used_at_ist", "gmp_quality", "total_x",
        "actual_listing_gain_pct", "outcome_vs_call",
        "shadow_v2_triggered", "shadow_v2_action",
        "shadow_v2_strength", "shadow_v2_outcome",
    )
    out = {k: r.get(k) for k in keys}
    out["actual_quality"] = return_quality(r.get("actual_listing_gain_pct"))
    out["audit_outcome"] = call_outcome_label(
        r.get("model_action"), r.get("actual_listing_gain_pct")
    )
    return out

def _stats(rows):
    listed = [
        r for r in rows
        if _f(r.get("actual_listing_gain_pct")) is not None
    ]
    actual = [_f(r.get("actual_listing_gain_pct")) for r in listed]
    pairs = [
        (_f(r.get("primary_prediction_pct")), _f(r.get("actual_listing_gain_pct")))
        for r in listed
        if _f(r.get("primary_prediction_pct")) is not None
    ]
    pred = [p for p, _ in pairs]
    pair_actual = [a for _, a in pairs]
    abs_err = [abs(p-a) for p, a in pairs]

    quality_counts = Counter(return_quality(x) for x in actual)
    source_counts = Counter(str(r.get("decision_source") or "UNKNOWN") for r in listed)

    return {
        "listed_rows": len(listed),
        "positive_rate_pct": _pct(sum(1 for x in actual if x > 0), len(actual)),
        "ge_5_rate_pct": _pct(sum(1 for x in actual if x >= 5), len(actual)),
        "ge_10_rate_pct": _pct(sum(1 for x in actual if x >= 10), len(actual)),
        "ge_20_rate_pct": _pct(sum(1 for x in actual if x >= 20), len(actual)),
        "avg_actual_gain_pct": _round(mean(actual)) if actual else None,
        "median_actual_gain_pct": _round(median(actual)) if actual else None,
        "worst_actual_gain_pct": _round(min(actual)) if actual else None,
        "best_actual_gain_pct": _round(max(actual)) if actual else None,
        "prediction_rows": len(pairs),
        "mae_pp": _round(mean(abs_err)) if abs_err else None,
        "correlation": _round(_pearson(pred, pair_actual), 3) if len(pairs) >= 2 else None,
        "quality_counts": dict(quality_counts),
        "decision_source_counts": dict(source_counts),
    }

def _action_metrics(rows):
    actions = ("STRONG SUBSCRIBE", "SUBSCRIBE", "BORDERLINE", "AVOID")
    return {
        action: {
            **_stats([r for r in rows if r.get("model_action") == action]),
            "total_rows": sum(1 for r in rows if r.get("model_action") == action),
        }
        for action in actions
    }

def _opportunity_efficiency(rows):
    listed = [
        r for r in rows
        if _f(r.get("actual_listing_gain_pct")) is not None
    ]
    selected = [
        r for r in listed
        if r.get("model_action") in SELECTED_ACTIONS
    ]

    def winners(threshold):
        return [
            r for r in listed
            if _f(r.get("actual_listing_gain_pct")) >= threshold
        ]

    def selected_winners(threshold):
        return [
            r for r in selected
            if _f(r.get("actual_listing_gain_pct")) >= threshold
        ]

    all10, all20 = winners(10), winners(20)
    sel10, sel20 = selected_winners(10), selected_winners(20)
    apply_rate = len(selected) / len(listed) if listed else 0.0
    capture20 = len(sel20) / len(all20) if all20 else 0.0
    capture10 = len(sel10) / len(all10) if all10 else 0.0
    selected_actual = [
        _f(r.get("actual_listing_gain_pct")) for r in selected
    ]

    positive_gain_pool = sum(
        max(0.0, _f(r.get("actual_listing_gain_pct")) or 0.0)
        for r in listed
    )
    positive_gain_selected = sum(
        max(0.0, _f(r.get("actual_listing_gain_pct")) or 0.0)
        for r in selected
    )

    return {
        "listed_rows": len(listed),
        "selected_rows": len(selected),
        "apply_rate_pct": _pct(len(selected), len(listed)),
        "selected_positive_rate_pct": _pct(
            sum(1 for x in selected_actual if x > 0), len(selected_actual)
        ),
        "selected_ge_10_rate_pct": _pct(len(sel10), len(selected)),
        "selected_ge_20_rate_pct": _pct(len(sel20), len(selected)),
        "selected_avg_gain_pct": (
            _round(mean(selected_actual)) if selected_actual else None
        ),
        "selected_median_gain_pct": (
            _round(median(selected_actual)) if selected_actual else None
        ),
        "selected_worst_gain_pct": (
            _round(min(selected_actual)) if selected_actual else None
        ),
        "all_ge_10_winners": len(all10),
        "captured_ge_10_winners": len(sel10),
        "ge_10_winner_capture_rate_pct": _pct(len(sel10), len(all10)),
        "all_ge_20_winners": len(all20),
        "captured_ge_20_winners": len(sel20),
        "ge_20_winner_capture_rate_pct": _pct(len(sel20), len(all20)),
        "ge_20_opportunity_efficiency_x": (
            _round(capture20 / apply_rate, 2)
            if apply_rate > 0 and all20 else None
        ),
        "positive_gain_capture_pct": (
            _round(positive_gain_selected / positive_gain_pool * 100.0, 1)
            if positive_gain_pool > 0 else None
        ),
    }

def _basis_metrics(rows):
    listed = [
        r for r in rows
        if _f(r.get("actual_listing_gain_pct")) is not None
    ]
    groups = {}
    for r in listed:
        src = str(r.get("decision_source") or "UNKNOWN")
        if src == "CAPTURED_1430_IST":
            group = "EXACT_1430"
        elif src.startswith("RETROSPECTIVE"):
            group = "RETROSPECTIVE_PROXY"
        else:
            group = src
        groups.setdefault(group, []).append(r)

    out = {}
    for name, group_rows in groups.items():
        selected = [
            r for r in group_rows
            if r.get("model_action") in SELECTED_ACTIONS
        ]
        out[name] = {
            **_stats(group_rows),
            "selected_rows": len(selected),
            "selected_positive_rate_pct": _pct(
                sum(
                    1 for r in selected
                    if _f(r.get("actual_listing_gain_pct")) > 0
                ),
                len(selected)
            ),
            "selected_ge_20_rate_pct": _pct(
                sum(
                    1 for r in selected
                    if _f(r.get("actual_listing_gain_pct")) >= 20
                ),
                len(selected)
            ),
        }
    return out

def _median_field(rows, field):
    vals = [_f(r.get(field)) for r in rows]
    vals = [x for x in vals if x is not None]
    return _round(median(vals)) if vals else None

def _cohort_diag(rows):
    if not rows:
        return {
            "count": 0,
            "median_gmp_used_pct": None,
            "median_total_subscription_x": None,
            "median_primary_prediction_pct": None,
            "median_gmp_prediction_pct": None,
            "median_subscription_prediction_pct": None,
            "missing_gmp_pct": None,
            "decision_source_counts": {},
        }
    return {
        "count": len(rows),
        "median_gmp_used_pct": _median_field(rows, "gmp_used_pct"),
        "median_total_subscription_x": _median_field(rows, "total_x"),
        "median_primary_prediction_pct": _median_field(
            rows, "primary_prediction_pct"
        ),
        "median_gmp_prediction_pct": _median_field(
            rows, "gmp_prediction_pct"
        ),
        "median_subscription_prediction_pct": _median_field(
            rows, "subscription_prediction_pct"
        ),
        "missing_gmp_pct": _pct(
            sum(1 for r in rows if _f(r.get("gmp_used_pct")) is None),
            len(rows)
        ),
        "decision_source_counts": dict(
            Counter(str(r.get("decision_source") or "UNKNOWN") for r in rows)
        ),
    }

def build_model_audit(rows, year=2026):
    rows = [r for r in rows if int(r.get("year") or year) == int(year)]
    listed = [
        r for r in rows
        if _f(r.get("actual_listing_gain_pct")) is not None
    ]

    by_segment = {}
    for segment in ("MAINBOARD", "SME"):
        seg = [r for r in listed if r.get("ipo_type") == segment]
        by_segment[segment] = {
            "overall": _stats(seg),
            "by_action": _action_metrics(seg),
            "opportunity_efficiency": _opportunity_efficiency(seg),
        }

    overall = {
        "overall": _stats(listed),
        "by_action": _action_metrics(listed),
        "opportunity_efficiency": _opportunity_efficiency(listed),
    }

    major_missed = sorted(
        [
            _row_for_json(r) for r in listed
            if r.get("model_action") == "AVOID"
            and _f(r.get("actual_listing_gain_pct")) >= 20
        ],
        key=lambda r: r.get("actual_listing_gain_pct") or -999,
        reverse=True,
    )
    significant_missed = sorted(
        [
            _row_for_json(r) for r in listed
            if r.get("model_action") == "AVOID"
            and 10 <= _f(r.get("actual_listing_gain_pct")) < 20
        ],
        key=lambda r: r.get("actual_listing_gain_pct") or -999,
        reverse=True,
    )
    selected_losses = sorted(
        [
            _row_for_json(r) for r in listed
            if r.get("model_action") in SELECTED_ACTIONS
            and _f(r.get("actual_listing_gain_pct")) < 0
        ],
        key=lambda r: r.get("actual_listing_gain_pct") or 0,
    )
    borderline_major = sorted(
        [
            _row_for_json(r) for r in listed
            if r.get("model_action") == "BORDERLINE"
            and _f(r.get("actual_listing_gain_pct")) >= 20
        ],
        key=lambda r: r.get("actual_listing_gain_pct") or -999,
        reverse=True,
    )

    sme_avoid = [
        r for r in listed
        if r.get("ipo_type") == "SME"
        and r.get("model_action") == "AVOID"
    ]
    sme_major_miss_rows = [
        r for r in sme_avoid
        if _f(r.get("actual_listing_gain_pct")) >= 20
    ]
    sme_correct_avoid_rows = [
        r for r in sme_avoid
        if _f(r.get("actual_listing_gain_pct")) <= 0
    ]

    exact_count = sum(
        1 for r in listed if r.get("decision_source") == "CAPTURED_1430_IST"
    )
    prospective_status = (
        "READY_FOR_PROSPECTIVE_COMPARISON"
        if exact_count >= 20
        else "INSUFFICIENT_EXACT_1430_SAMPLE"
    )

    return {
        "audit_version": AUDIT_VERSION,
        "year": int(year),
        "policy": {
            "version": FROZEN_POLICY_VERSION,
            "frozen": True,
            "status": "FROZEN_FOR_PROSPECTIVE_VALIDATION",
            "reason": (
                "Do not tune V1 thresholds on the same 2026 retrospective outcomes "
                "used to evaluate them. New ideas must become a separate shadow V2."
            ),
        },
        "return_quality_definition": {
            "EXCELLENT": "actual listing gain >= 20%",
            "GOOD": "10% <= actual listing gain < 20%",
            "MARGINAL": "5% <= actual listing gain < 10%",
            "WEAK": "0% <= actual listing gain < 5%",
            "LOSS": "actual listing gain < 0%",
        },
        "prospective_validation": {
            "exact_1430_listed_rows": exact_count,
            "status": prospective_status,
            "minimum_target_rows": 20,
        },
        "overall": overall,
        "by_segment": by_segment,
        "decision_basis": _basis_metrics(listed),
        "misses": {
            "major_avoid_misses_ge_20": major_missed,
            "significant_avoid_misses_10_to_20": significant_missed,
            "selected_losses": selected_losses,
            "borderline_major_winners_ge_20": borderline_major,
        },
        "sme_avoid_diagnostic": {
            "major_missed_winners_ge_20": _cohort_diag(sme_major_miss_rows),
            "correct_avoids_le_0": _cohort_diag(sme_correct_avoid_rows),
            "interpretation_hint": (
                "Compare missing GMP rate, GMP level and total subscription between "
                "major SME misses and correct SME avoids. This is feature-discovery only; "
                "V1 thresholds remain frozen."
            ),
        },
    }

def log_model_audit(audit, logger):
    logger.info(
        "MODEL_AUDIT_SUMMARY year=%s policy=%s prospective=%s overall=%s",
        audit.get("year"),
        (audit.get("policy") or {}).get("version"),
        audit.get("prospective_validation"),
        (audit.get("overall") or {}).get("opportunity_efficiency"),
    )

    for segment, data in (audit.get("by_segment") or {}).items():
        logger.info(
            "MODEL_AUDIT_SEGMENT segment=%s overall=%s opportunity=%s",
            segment, data.get("overall"), data.get("opportunity_efficiency")
        )
        for action, metrics in (data.get("by_action") or {}).items():
            logger.info(
                "MODEL_AUDIT_ACTION segment=%s action=%r metrics=%s",
                segment, action, metrics
            )

    for basis, metrics in (audit.get("decision_basis") or {}).items():
        logger.info(
            "MODEL_AUDIT_BASIS basis=%s metrics=%s", basis, metrics
        )

    for row in (audit.get("misses") or {}).get("major_avoid_misses_ge_20", []):
        logger.warning(
            "MODEL_AUDIT_MAJOR_MISS name=%r segment=%s predicted=%s actual=%s "
            "gmp_used=%s total_x=%s source=%s",
            row.get("name"), row.get("ipo_type"),
            row.get("primary_prediction_pct"),
            row.get("actual_listing_gain_pct"),
            row.get("gmp_used_pct"), row.get("total_x"),
            row.get("decision_source"),
        )

    for row in (audit.get("misses") or {}).get("selected_losses", []):
        logger.warning(
            "MODEL_AUDIT_SELECTED_LOSS name=%r segment=%s action=%s predicted=%s "
            "actual=%s gmp_used=%s total_x=%s source=%s",
            row.get("name"), row.get("ipo_type"), row.get("model_action"),
            row.get("primary_prediction_pct"),
            row.get("actual_listing_gain_pct"),
            row.get("gmp_used_pct"), row.get("total_x"),
            row.get("decision_source"),
        )

    logger.info(
        "MODEL_AUDIT_SME_MISS_DIAGNOSTIC %s",
        audit.get("sme_avoid_diagnostic"),
    )
