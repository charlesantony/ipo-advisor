import math
from datetime import datetime

from model import ridge_fit, ridge_predict
from shadow_v2 import shadow_signal_from_v1

POLICY_VERSION = "research-v1"

POLICY_EVIDENCE = {
    "validation": "2024_to_2025_chronological_holdout",
    "mainboard": {
        "gmp_ge_10": {"cases": 6, "win_rate_pct": 100.0, "avg_gain_pct": 31.2, "worst_gain_pct": 12.9},
        "gmp_ge_20": {"cases": 4, "win_rate_pct": 100.0, "avg_gain_pct": 37.8, "worst_gain_pct": 20.09},
        "subscription_ge_20": {"cases": 58, "win_rate_pct": 84.5, "avg_gain_pct": 17.01, "worst_gain_pct": -3.42},
    },
    "sme": {
        "gmp_ge_10": {"cases": 7, "win_rate_pct": 85.7, "avg_gain_pct": 55.18, "worst_gain_pct": -20.0},
        "gmp_ge_20": {"cases": 4, "win_rate_pct": 100.0, "avg_gain_pct": 90.0, "worst_gain_pct": 90.0},
        "subscription_ge_20": {"cases": 86, "win_rate_pct": 88.4, "avg_gain_pct": 37.22, "worst_gain_pct": -20.0},
    },
}

ACTION_PRIORITY = {
    "STRONG SUBSCRIBE": 5,
    "SUBSCRIBE": 4,
    "BORDERLINE": 3,
    "AVOID": 2,
    "WATCH": 1,
    "NOT READY": 0,
}

def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def _log1p(v):
    v = _f(v)
    if v is None or v < 0:
        return None
    return math.log1p(v)

def _clip(v, cap=None):
    if v is None:
        return None
    if cap is not None:
        return min(float(cap), float(v))
    return float(v)

def _fit(rows, features, minimum):
    clean_x, clean_y = [], []
    for r in rows:
        target = _f(r.get("listing_gain_pct"))
        if target is None:
            continue
        vals = []
        bad = False
        for f in features:
            value = _f(r.get(f))
            if value is None:
                bad = True
                break
            vals.append(value)
        if not bad:
            clean_x.append(vals)
            clean_y.append(target)
    if len(clean_x) < minimum:
        return None, len(clean_x)
    return ridge_fit(clean_x, clean_y, lam=1.0), len(clean_x)

def _time_finality(n, ist):
    if str(n.get("status") or "").upper() != "LIVE":
        return {
            "code": "NOT_ACTIONABLE",
            "label": "NOT ACTIONABLE — IPO is not LIVE",
            "canonical": False,
        }

    if not n.get("is_closing_today"):
        return {
            "code": "EARLY",
            "label": "EARLY SIGNAL — NOT FINAL; use closing-day 2:30 PM IST decision",
            "canonical": False,
        }

    minute = ist.hour * 60 + ist.minute
    canonical_start = 14 * 60 + 28
    canonical_end = 14 * 60 + 32
    if canonical_start <= minute <= canonical_end:
        return {
            "code": "CANONICAL_1430",
            "label": "CANONICAL 2:30 PM IST RESEARCH DECISION",
            "canonical": True,
        }
    if minute < canonical_start:
        return {
            "code": "CLOSING_DAY_PRE_1430",
            "label": "PRELIMINARY — closes today; wait for 2:30 PM IST snapshot",
            "canonical": False,
        }
    return {
        "code": "CLOSING_DAY_POST_1430",
        "label": "POST-2:30 PM — latest research signal; canonical snapshot should be retained",
        "canonical": False,
    }

class ResearchDecisionEngine:
    def __init__(self, historical_rows):
        self.models = {}
        self.training = {}

        for segment in ("MAINBOARD", "SME"):
            train = [
                r for r in historical_rows
                if int(r.get("year") or 0) == 2024
                and str(r.get("ipo_type") or "").upper() == segment
            ]
            gmp_model, gmp_n = _fit(train, ["gmp_gain_pct"], minimum=15)
            sub_model, sub_n = _fit(train, ["log_total"], minimum=20)
            self.models[segment] = {
                "gmp": gmp_model,
                "subscription": sub_model,
            }
            self.training[segment] = {
                "source_year": 2024,
                "source_rows": len(train),
                "gmp_complete_rows": gmp_n,
                "subscription_complete_rows": sub_n,
            }

    def ready(self, segment):
        m = self.models.get(segment) or {}
        return bool(m.get("gmp") or m.get("subscription"))

    def _predict(self, segment, n):
        m = self.models.get(segment) or {}
        cap = 90.0 if segment == "SME" else None

        gmp_input = _f(n.get("gmp_gain_pct"))
        total_input = _f(n.get("total_x"))
        log_total = _log1p(total_input)

        gmp_pred = None
        if m.get("gmp") and gmp_input is not None:
            gmp_pred = _clip(ridge_predict(m["gmp"], [gmp_input]), cap)

        sub_pred = None
        if m.get("subscription") and log_total is not None:
            sub_pred = _clip(ridge_predict(m["subscription"], [log_total]), cap)

        return {
            "gmp_input_pct": gmp_input,
            "total_subscription_x": total_input,
            "log_total": log_total,
            "gmp_prediction_pct": round(gmp_pred, 2) if gmp_pred is not None else None,
            "subscription_prediction_pct": round(sub_pred, 2) if sub_pred is not None else None,
            "prediction_cap_pct": cap,
        }

    def _mainboard_policy(self, p):
        gp = p["gmp_prediction_pct"]
        sp = p["subscription_prediction_pct"]
        reasons = []
        conflict = False

        if gp is not None:
            if gp >= 20:
                action = "STRONG SUBSCRIBE"
                confidence = "HIGHER"
                reasons.append("GMP model is at or above the +20% research threshold.")
            elif gp >= 10:
                action = "SUBSCRIBE"
                confidence = "MEDIUM"
                reasons.append("GMP model is between +10% and +20%.")
            elif gp >= 5:
                action = "BORDERLINE"
                confidence = "LOW"
                reasons.append("GMP model is positive but below the +10% subscribe threshold.")
            else:
                action = "AVOID"
                confidence = "MEDIUM"
                reasons.append("GMP model is below the +5% research threshold.")

            if sp is not None:
                if sp >= 20:
                    reasons.append("Total-subscription model is strongly supportive.")
                    if action in ("STRONG SUBSCRIBE", "SUBSCRIBE"):
                        confidence = "HIGHER"
                elif sp >= 10:
                    reasons.append("Total-subscription model is supportive.")
                elif sp < 0 and action in ("STRONG SUBSCRIBE", "SUBSCRIBE"):
                    conflict = True
                    confidence = "MEDIUM"
                    reasons.append("Demand model conflicts with GMP; action is retained but confidence is reduced.")
                else:
                    reasons.append("Total-subscription model is neutral/weak.")
        elif sp is not None:
            if sp >= 20:
                action = "SUBSCRIBE"
                confidence = "MEDIUM"
                reasons.append("GMP is unavailable; broad subscription model is at or above +20%.")
            elif sp >= 10:
                action = "BORDERLINE"
                confidence = "LOW"
                reasons.append("GMP is unavailable; subscription model is only moderately positive.")
            else:
                action = "AVOID"
                confidence = "LOW"
                reasons.append("GMP is unavailable and subscription model is below +10%.")
        else:
            action = "NOT READY"
            confidence = "LOW"
            reasons.append("Neither GMP nor total-subscription input is available.")

        primary = gp if gp is not None else sp
        return action, confidence, conflict, reasons, primary

    def _sme_policy(self, p):
        gp = p["gmp_prediction_pct"]
        sp = p["subscription_prediction_pct"]
        reasons = []
        conflict = False

        if gp is not None:
            if gp >= 20:
                if sp is None or sp >= 10:
                    action = "STRONG SUBSCRIBE"
                    confidence = "HIGHER" if sp is not None else "MEDIUM"
                    reasons.append("SME GMP model is at or above +20%.")
                    if sp is not None:
                        reasons.append("Demand confirmation is positive.")
                elif sp >= 0:
                    action = "SUBSCRIBE"
                    confidence = "MEDIUM"
                    conflict = True
                    reasons.append("GMP is strong, but demand confirmation is weak.")
                else:
                    action = "BORDERLINE"
                    confidence = "LOW"
                    conflict = True
                    reasons.append("Strong GMP conflicts with a negative demand model.")
            elif gp >= 10:
                if sp is not None and sp >= 20:
                    action = "SUBSCRIBE"
                    confidence = "MEDIUM"
                    reasons.append("GMP is +10% to +20% and subscription confirmation is very strong.")
                else:
                    action = "BORDERLINE"
                    confidence = "LOW"
                    reasons.append("SME GMP is +10% to +20%; the holdout contained a -20% false positive in this zone.")
                    if sp is not None and sp < 0:
                        conflict = True
                        reasons.append("Demand model is negative.")
            elif gp >= 5:
                if sp is not None and sp >= 20:
                    action = "BORDERLINE"
                    confidence = "LOW"
                    reasons.append("GMP is modest, while demand is strong; keep as borderline.")
                else:
                    action = "AVOID"
                    confidence = "LOW"
                    reasons.append("SME GMP model is below +10% without strong demand confirmation.")
            else:
                action = "AVOID"
                confidence = "MEDIUM"
                reasons.append("SME GMP model is below +5%.")
        elif sp is not None:
            if sp >= 20:
                action = "SUBSCRIBE"
                confidence = "MEDIUM"
                reasons.append("GMP is unavailable; SME subscription model is at or above +20%.")
            elif sp >= 10:
                action = "BORDERLINE"
                confidence = "LOW"
                reasons.append("GMP is unavailable; subscription model is moderately positive.")
            else:
                action = "AVOID"
                confidence = "LOW"
                reasons.append("GMP is unavailable and subscription model is below +10%.")
        else:
            action = "NOT READY"
            confidence = "LOW"
            reasons.append("Neither GMP nor total-subscription input is available.")

        primary = gp if gp is not None else sp
        return action, confidence, conflict, reasons, primary

    def classify_proxy(self, segment, gmp_gain_pct=None, total_x=None):
        """
        Apply Research Model V1 without LIVE/finality gating.

        Used by the 2026 retrospective/daily tracker. The caller must label
        whether the supplied values are an exact local 2:30 capture or proxies.
        """
        segment = str(segment or "").upper()
        if segment not in ("MAINBOARD", "SME"):
            return {
                "policy_version": POLICY_VERSION,
                "segment": segment,
                "action": "NOT READY",
                "research_confidence": "LOW",
                "reason": ["Unsupported IPO segment."],
            }

        p = self._predict(
            segment,
            {"gmp_gain_pct": gmp_gain_pct, "total_x": total_x},
        )

        if segment == "MAINBOARD":
            action, confidence, conflict, reasons, primary = self._mainboard_policy(p)
        else:
            action, confidence, conflict, reasons, primary = self._sme_policy(p)

        return {
            "policy_version": POLICY_VERSION,
            "segment": segment,
            "action": action,
            "action_priority": ACTION_PRIORITY.get(action, 0),
            "research_confidence": confidence,
            "predictions": p,
            "primary_prediction_pct": (
                round(primary, 2) if primary is not None else None
            ),
            "ranking_score": (
                round(primary, 2) if primary is not None else -999.0
            ),
            "signal_conflict": conflict,
            "reason": reasons,
            "training": self.training.get(segment),
            "evidence": POLICY_EVIDENCE.get(segment.lower()),
        }

    def attach_shadow_v2(self, recommendation):
        recommendation = dict(recommendation or {})
        recommendation["shadow_v2"] = shadow_signal_from_v1(recommendation)
        return recommendation

    def recommend(self, n, ist):
        segment = str(n.get("type") or n.get("ipo_type") or "").upper()
        finality = _time_finality(n, ist)

        if segment not in ("MAINBOARD", "SME"):
            return {
                "policy_version": POLICY_VERSION,
                "action": "NOT READY",
                "research_confidence": "LOW",
                "finality": finality,
                "reason": ["Unsupported IPO segment."],
            }

        p = self._predict(segment, n)

        if str(n.get("status") or "").upper() != "LIVE":
            return {
                "policy_version": POLICY_VERSION,
                "segment": segment,
                "action": "WATCH",
                "research_confidence": "LOW",
                "finality": finality,
                "predictions": p,
                "primary_prediction_pct": (
                    p["gmp_prediction_pct"]
                    if p["gmp_prediction_pct"] is not None
                    else p["subscription_prediction_pct"]
                ),
                "signal_conflict": False,
                "reason": ["Recommendation is not actionable until the IPO is LIVE."],
                "training": self.training.get(segment),
                "evidence": POLICY_EVIDENCE.get(segment.lower()),
            }

        if segment == "MAINBOARD":
            action, confidence, conflict, reasons, primary = self._mainboard_policy(p)
        else:
            action, confidence, conflict, reasons, primary = self._sme_policy(p)

        if finality["code"] != "CANONICAL_1430":
            reasons.append(finality["label"])

        ranking = primary
        if ranking is None:
            ranking = -999.0

        return {
            "policy_version": POLICY_VERSION,
            "segment": segment,
            "action": action,
            "action_priority": ACTION_PRIORITY.get(action, 0),
            "research_confidence": confidence,
            "finality": finality,
            "predictions": p,
            "primary_prediction_pct": round(primary, 2) if primary is not None else None,
            "ranking_score": round(ranking, 2),
            "signal_conflict": conflict,
            "reason": reasons,
            "training": self.training.get(segment),
            "evidence": POLICY_EVIDENCE.get(segment.lower()),
            "disclaimer": (
                "Research model only. It estimates listing-gain potential from historical GMP and "
                "subscription patterns; it does not consider your financial situation or guarantee profit."
            ),
        }
