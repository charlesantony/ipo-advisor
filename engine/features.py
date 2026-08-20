import math
from datetime import datetime

def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def log1p(v):
    v = _f(v)
    if v is None or v < 0:
        return None
    return math.log1p(v)

def _gain_from_trend(x):
    if not isinstance(x, dict):
        return None
    return _f(x.get("gain"))

def derive_static_features(n):
    trends = n.get("gmp_trends") or []
    gains = [g for g in (_gain_from_trend(x) for x in trends) if g is not None]

    latest = gains[0] if gains else n.get("gmp_gain_pct")
    prev = gains[1] if len(gains) >= 2 else None
    third = gains[2] if len(gains) >= 3 else None

    issue = _f(n.get("issue_size_cr"))
    ofs = _f(n.get("ofs_cr"))

    return {
        "gmp_latest_pct": latest,
        "gmp_change_1obs": round(latest - prev, 3) if latest is not None and prev is not None else None,
        "gmp_change_3obs": round(latest - third, 3) if latest is not None and third is not None else None,
        "log_qib": log1p(n.get("qib_x")),
        "log_nii": log1p(n.get("nii_x")),
        "log_retail": log1p(n.get("retail_x")),
        "log_total": log1p(n.get("total_x")),
        "log_issue_size": log1p(issue),
        "ofs_ratio": (ofs / issue) if issue and ofs is not None else None,
    }

def derive_velocity(current, previous):
    if not previous:
        return {
            "minutes_since_previous": None,
            "qib_x_per_hour": None,
            "nii_x_per_hour": None,
            "retail_x_per_hour": None,
            "total_x_per_hour": None,
        }

    try:
        t1 = datetime.fromisoformat(current["fetched_at_utc"])
        t0 = datetime.fromisoformat(previous["fetched_at_utc"])
        minutes = (t1 - t0).total_seconds() / 60.0
    except Exception:
        minutes = None

    out = {"minutes_since_previous": round(minutes, 1) if minutes else None}
    for field, label in [
        ("qib_x", "qib_x_per_hour"),
        ("nii_x", "nii_x_per_hour"),
        ("retail_x", "retail_x_per_hour"),
        ("total_x", "total_x_per_hour"),
    ]:
        now = _f(current.get(field))
        before = _f(previous.get(field))
        if minutes and minutes > 0 and now is not None and before is not None:
            out[label] = round((now - before) * 60.0 / minutes, 3)
        else:
            out[label] = None
    return out
