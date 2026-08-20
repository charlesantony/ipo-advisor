import math
import random
from statistics import mean, median

FEATURE_SETS = {
    "GMP only": ["gmp_gain_pct"],
    "Demand only": ["log_qib", "log_nii", "log_retail", "log_total"],
    "Combined": [
        "gmp_gain_pct", "log_qib", "log_nii", "log_retail",
        "log_total", "log_issue_size"
    ],
}

def _dot(a, b):
    return sum(x*y for x, y in zip(a, b))

def _solve_linear(A, b):
    # Gauss-Jordan with partial pivoting.
    n = len(b)
    aug = [list(A[i]) + [b[i]] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        div = aug[col][col]
        aug[col] = [v/div for v in aug[col]]
        for r in range(n):
            if r == col:
                continue
            f = aug[r][col]
            if f:
                aug[r] = [aug[r][c] - f*aug[col][c] for c in range(n+1)]
    return [aug[i][-1] for i in range(n)]

def ridge_fit(X, y, lam=1.0):
    # Adds intercept as first column, standardizes non-intercept features.
    p = len(X[0])
    means = [mean(row[j] for row in X) for j in range(p)]
    stds = []
    for j in range(p):
        m = means[j]
        var = mean((row[j]-m)**2 for row in X)
        stds.append(math.sqrt(var) if var > 1e-12 else 1.0)

    Z = [[1.0] + [(row[j]-means[j])/stds[j] for j in range(p)] for row in X]
    k = p + 1
    A = [[0.0]*k for _ in range(k)]
    b = [0.0]*k
    for z, target in zip(Z, y):
        for i in range(k):
            b[i] += z[i]*target
            for j in range(k):
                A[i][j] += z[i]*z[j]
    for i in range(1, k):
        A[i][i] += lam
    beta = _solve_linear(A, b)
    if beta is None:
        return None
    return {"beta": beta, "means": means, "stds": stds}

def ridge_predict(model, row):
    z = [1.0] + [
        (row[j]-model["means"][j])/model["stds"][j]
        for j in range(len(row))
    ]
    return _dot(model["beta"], z)

def pearson(a, b):
    if len(a) < 2:
        return None
    ma, mb = mean(a), mean(b)
    da = [x-ma for x in a]
    db = [x-mb for x in b]
    den = math.sqrt(sum(x*x for x in da) * sum(x*x for x in db))
    if den <= 1e-12:
        return None
    return sum(x*y for x,y in zip(da,db)) / den

def _row_for_features(r, features):
    vals = []
    for f in features:
        v = r.get(f)
        if v is None:
            return None
        try:
            vals.append(float(v))
        except Exception:
            return None
    return vals


def _safe_name(r):
    return r.get("name") or r.get("symbol") or "Unknown IPO"

def _safe_date(r):
    return (
        r.get("issue_close")
        or r.get("close_date")
        or r.get("listing_date")
        or r.get("end_date")
        or ""
    )

def _stats(vals):
    if not vals:
        return {"n": 0, "min": None, "mean": None, "max": None}
    return {
        "n": len(vals),
        "min": round(min(vals), 3),
        "mean": round(mean(vals), 3),
        "max": round(max(vals), 3),
    }

def _clip_prediction(value, prediction_cap=None):
    if prediction_cap is not None:
        value = min(float(prediction_cap), value)
    return value

def _threshold_summary(predictions, threshold, baseline_positive_rate):
    chosen = [
        row for row in predictions
        if row["predicted_gain_pct"] >= threshold
    ]
    actual = [row["actual_gain_pct"] for row in chosen]
    losses = [row for row in chosen if row["actual_gain_pct"] <= 0]

    if not actual:
        return {
            "threshold_pct": threshold,
            "count": 0,
            "win_rate_pct": None,
            "avg_actual_gain_pct": None,
            "median_actual_gain_pct": None,
            "worst_actual_gain_pct": None,
            "best_actual_gain_pct": None,
            "improvement_vs_baseline_pp": None,
            "loss_count": 0,
            "losses": [],
        }

    win_rate = mean(1.0 if x > 0 else 0.0 for x in actual) * 100.0
    return {
        "threshold_pct": threshold,
        "count": len(chosen),
        "win_rate_pct": round(win_rate, 1),
        "avg_actual_gain_pct": round(mean(actual), 2),
        "median_actual_gain_pct": round(median(actual), 2),
        "worst_actual_gain_pct": round(min(actual), 2),
        "best_actual_gain_pct": round(max(actual), 2),
        "improvement_vs_baseline_pp": round(
            win_rate - baseline_positive_rate * 100.0, 1
        ),
        "loss_count": len(losses),
        "losses": losses,
    }

def _evaluate_predictions(predictions, features, thresholds=(10.0, 20.0)):
    if not predictions:
        return None

    actual = [p["actual_gain_pct"] for p in predictions]
    pred = [p["predicted_gain_pct"] for p in predictions]
    errors = [p-y for p, y in zip(pred, actual)]

    baseline_positive_rate = mean(1.0 if y > 0 else 0.0 for y in actual)
    baseline_avg_gain = mean(actual)

    false_positive = [
        row for row in predictions
        if row["predicted_positive"] and not row["actual_positive"]
    ]
    false_negative = [
        row for row in predictions
        if not row["predicted_positive"] and row["actual_positive"]
    ]

    threshold_analysis = {
        str(int(t) if float(t).is_integer() else t):
            _threshold_summary(predictions, float(t), baseline_positive_rate)
        for t in thresholds
    }

    return {
        "n": len(predictions),
        "ready": True,
        "features": list(features),
        "mae": round(mean(abs(e) for e in errors), 2),
        "rmse": round(math.sqrt(mean(e*e for e in errors)), 2),
        "sign_hit_rate_pct": round(
            mean(
                1.0 if (p > 0) == (y > 0) else 0.0
                for p, y in zip(pred, actual)
            ) * 100.0,
            1,
        ),
        "correlation": (
            round(pearson(pred, actual), 3)
            if pearson(pred, actual) is not None else None
        ),
        "baseline_positive_rate_pct": round(
            baseline_positive_rate * 100.0, 1
        ),
        "baseline_avg_gain_pct": round(baseline_avg_gain, 2),
        "false_positive_count": len(false_positive),
        "false_negative_count": len(false_negative),
        "actual_gain_distribution": _stats(actual),
        "predicted_gain_distribution": _stats(pred),
        "threshold_analysis": threshold_analysis,
        "worst_prediction_misses": sorted(
            predictions,
            key=lambda x: x["abs_error_pct_points"],
            reverse=True,
        )[:10],
        "predictions": predictions,
    }

def chronological_holdout(
    train_rows,
    test_rows,
    features,
    lam=1.0,
    prediction_cap=None,
    thresholds=(10.0, 20.0),
):
    """
    Fit only on train_rows (e.g. 2024) and evaluate only on later test_rows
    (e.g. 2025). This is the primary no-look-ahead validation in V0.3.7.
    """
    train_clean = []
    test_clean = []

    train_missing = {f: 0 for f in features}
    test_missing = {f: 0 for f in features}
    train_target_missing = 0
    test_target_missing = 0

    for r in train_rows:
        y = r.get("listing_gain_pct")
        if y is None:
            train_target_missing += 1
            continue
        x = _row_for_features(r, features)
        if x is None:
            for f in features:
                if r.get(f) is None:
                    train_missing[f] += 1
            continue
        train_clean.append((r, x, float(y)))

    for r in test_rows:
        y = r.get("listing_gain_pct")
        if y is None:
            test_target_missing += 1
            continue
        x = _row_for_features(r, features)
        if x is None:
            for f in features:
                if r.get(f) is None:
                    test_missing[f] += 1
            continue
        test_clean.append((r, x, float(y)))

    min_train = max(15, len(features) + 8)
    min_test = 8

    if len(train_clean) < min_train or len(test_clean) < min_test:
        return {
            "ready": False,
            "n": len(test_clean),
            "reason": (
                f"Need at least {min_train} train rows and {min_test} test rows"
            ),
            "train_complete_rows": len(train_clean),
            "test_complete_rows": len(test_clean),
            "train_input_rows": len(train_rows),
            "test_input_rows": len(test_rows),
            "train_target_missing": train_target_missing,
            "test_target_missing": test_target_missing,
            "train_missing_by_feature": train_missing,
            "test_missing_by_feature": test_missing,
            "prediction_cap": prediction_cap,
        }

    fitted = ridge_fit(
        [x for _, x, _ in train_clean],
        [y for _, _, y in train_clean],
        lam=lam,
    )
    if not fitted:
        return {
            "ready": False,
            "n": len(test_clean),
            "reason": "Could not fit holdout model",
            "train_complete_rows": len(train_clean),
            "test_complete_rows": len(test_clean),
        }

    predictions = []
    for r, x, actual in test_clean:
        raw_p = ridge_predict(fitted, x)
        p = _clip_prediction(raw_p, prediction_cap)
        predictions.append({
            "name": _safe_name(r),
            "date": _safe_date(r),
            "actual_gain_pct": round(actual, 4),
            "predicted_gain_pct": round(p, 4),
            "raw_predicted_gain_pct": round(raw_p, 4),
            "prediction_was_capped": prediction_cap is not None and raw_p > prediction_cap,
            "error_pct_points": round(p - actual, 4),
            "abs_error_pct_points": round(abs(p - actual), 4),
            "actual_positive": actual > 0,
            "predicted_positive": p > 0,
        })

    metrics = _evaluate_predictions(
        predictions, features, thresholds=thresholds
    )
    metrics.update({
        "validation": "chronological_holdout",
        "train_complete_rows": len(train_clean),
        "test_complete_rows": len(test_clean),
        "train_input_rows": len(train_rows),
        "test_input_rows": len(test_rows),
        "train_target_missing": train_target_missing,
        "test_target_missing": test_target_missing,
        "train_missing_by_feature": train_missing,
        "test_missing_by_feature": test_missing,
        "prediction_cap": prediction_cap,
        "capped_prediction_count": sum(
            1 for p in predictions if p["prediction_was_capped"]
        ),
    })
    return metrics

def loocv(rows, features, lam=1.0, prediction_cap=None):
    clean = []
    missing_by_feature = {f: 0 for f in features}
    target_missing = 0

    for r in rows:
        y = r.get("listing_gain_pct")
        if y is None:
            target_missing += 1
            continue

        x = []
        bad = False
        for f in features:
            v = r.get(f)
            if v is None:
                missing_by_feature[f] += 1
                bad = True
                break
            try:
                x.append(float(v))
            except Exception:
                missing_by_feature[f] += 1
                bad = True
                break
        if not bad:
            clean.append((r, x, float(y)))

    minimum = max(8, len(features) + 3)
    if len(clean) < minimum:
        return {
            "n": len(clean),
            "ready": False,
            "reason": f"Need at least {minimum} complete rows",
            "input_rows": len(rows),
            "target_missing": target_missing,
            "missing_by_feature": missing_by_feature,
        }

    predictions = []
    for i in range(len(clean)):
        train = clean[:i] + clean[i+1:]
        X = [x for _, x, _ in train]
        y = [yy for _, _, yy in train]
        fitted = ridge_fit(X, y, lam=lam)
        if not fitted:
            continue
        raw_p = ridge_predict(fitted, clean[i][1])
        p = _clip_prediction(raw_p, prediction_cap)
        r = clean[i][0]
        actual = clean[i][2]
        predictions.append({
            "name": _safe_name(r),
            "date": _safe_date(r),
            "actual_gain_pct": round(actual, 4),
            "predicted_gain_pct": round(p, 4),
            "raw_predicted_gain_pct": round(raw_p, 4),
            "prediction_was_capped": prediction_cap is not None and raw_p > prediction_cap,
            "error_pct_points": round(p - actual, 4),
            "abs_error_pct_points": round(abs(p - actual), 4),
            "actual_positive": actual > 0,
            "predicted_positive": p > 0,
            "selected_pred_ge_10": p >= 10.0,
        })

    if not predictions:
        return {
            "n": len(clean),
            "ready": False,
            "reason": "Could not fit model",
            "input_rows": len(rows),
            "target_missing": target_missing,
            "missing_by_feature": missing_by_feature,
        }

    actual = [p["actual_gain_pct"] for p in predictions]
    pred = [p["predicted_gain_pct"] for p in predictions]
    errors = [p-y for p,y in zip(pred,actual)]

    mae = mean(abs(e) for e in errors)
    rmse = math.sqrt(mean(e*e for e in errors))
    hit = mean(1.0 if (p > 0) == (y > 0) else 0.0 for p,y in zip(pred,actual))
    corr = pearson(pred, actual)

    baseline_positive_rate = mean(1.0 if y > 0 else 0.0 for y in actual)
    baseline_avg_gain = mean(actual)

    chosen = [row for row in predictions if row["selected_pred_ge_10"]]
    chosen_actual = [row["actual_gain_pct"] for row in chosen]
    win_rate = mean(1.0 if y > 0 else 0.0 for y in chosen_actual) if chosen_actual else None
    avg_chosen = mean(chosen_actual) if chosen_actual else None

    false_positive = [
        row for row in predictions
        if row["predicted_positive"] and not row["actual_positive"]
    ]
    false_negative = [
        row for row in predictions
        if not row["predicted_positive"] and row["actual_positive"]
    ]

    selected_losses = [
        row for row in chosen if row["actual_gain_pct"] <= 0
    ]

    worst_misses = sorted(
        predictions,
        key=lambda x: x["abs_error_pct_points"],
        reverse=True
    )[:10]

    strongest_selected = sorted(
        chosen,
        key=lambda x: x["predicted_gain_pct"],
        reverse=True
    )[:10]

    return {
        "n": len(predictions),
        "ready": True,
        "features": list(features),
        "input_rows": len(rows),
        "target_missing": target_missing,
        "missing_by_feature": missing_by_feature,

        "mae": round(mae, 2),
        "rmse": round(rmse, 2),
        "sign_hit_rate_pct": round(hit*100, 1),
        "correlation": round(corr, 3) if corr is not None else None,

        "baseline_positive_rate_pct": round(baseline_positive_rate*100, 1),
        "baseline_avg_gain_pct": round(baseline_avg_gain, 2),

        "apply_if_pred_10_count": len(chosen),
        "apply_if_pred_10_win_rate_pct": round(win_rate*100, 1) if win_rate is not None else None,
        "apply_if_pred_10_avg_actual_gain_pct": round(avg_chosen, 2) if avg_chosen is not None else None,
        "apply_if_pred_10_improvement_vs_baseline_pp": (
            round(win_rate*100 - baseline_positive_rate*100, 1)
            if win_rate is not None else None
        ),
        "apply_if_pred_10_losses": selected_losses,

        "false_positive_count": len(false_positive),
        "false_negative_count": len(false_negative),

        "actual_gain_distribution": _stats(actual),
        "predicted_gain_distribution": _stats(pred),

        "worst_prediction_misses": worst_misses,
        "strongest_selected_cases": strongest_selected,
        "threshold_analysis": {
            "10": _threshold_summary(predictions, 10.0, baseline_positive_rate),
            "20": _threshold_summary(predictions, 20.0, baseline_positive_rate),
        },
        "prediction_cap": prediction_cap,
        "capped_prediction_count": sum(
            1 for row in predictions if row.get("prediction_was_capped")
        ),
        "predictions": predictions,
    }

def benchmark(rows):
    out = {}
    for segment in ("MAINBOARD", "SME"):
        seg = [r for r in rows if str(r.get("ipo_type","")).upper() == segment]
        out[segment] = {
            "rows_with_target": sum(1 for r in seg if r.get("listing_gain_pct") is not None),
            "models": {
                label: loocv(seg, feats)
                for label, feats in FEATURE_SETS.items()
            }
        }
    return out
