import re

def _num(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "")
    # "12.3%" -> 12.3, "₹1,200" -> 1200, "35.4x" -> 35.4
    m = re.search(r"-?[0-9]+(?:\.[0-9]+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None

def _parse_price_range(text):
    if not text:
        return (None, None)
    nums = re.findall(r"[0-9]+(?:\.[0-9]+)?", str(text).replace(",", ""))
    if not nums:
        return (None, None)
    vals = [float(x) for x in nums]
    if len(vals) == 1:
        return (vals[0], vals[0])
    return (vals[0], vals[-1])

def _subscription_x(section):
    if not section:
        return None
    return _num(section.get("subscription"))

def normalize_ipo(item):
    schedule = item.get("schedule") or {}
    issue_size = item.get("issueSize") or {}
    gmp = item.get("greyMarketPremium") or {}
    gmp_trends = gmp.get("gmpTrends") or []
    latest_gmp = gmp_trends[0] if gmp_trends else {}
    subs = item.get("subscriptionNumbers") or {}
    low, high = _parse_price_range(item.get("priceRange"))

    return {
        "symbol": item.get("symbol"),
        "type": item.get("type"),
        "name": item.get("name"),
        "status": item.get("status"),
        "exchanges": item.get("exchanges"),
        "details_url": item.get("detailsUrl"),
        "drhp_url": item.get("drhpLink"),
        "rhp_url": item.get("rhpLink"),

        "start_date": schedule.get("startDate"),
        "end_date": schedule.get("endDate"),
        "listing_date": schedule.get("listingDate"),

        "price_low": low,
        "price_high": high,
        "lot_size": _num(item.get("lotSize")),

        "issue_size_cr": _num(issue_size.get("totalIssueSize")),
        "fresh_issue_cr": _num(issue_size.get("freshIssue")),
        "ofs_cr": _num(issue_size.get("offerForSale")),

        "gmp_value": _num(latest_gmp.get("gmp")),
        "gmp_gain_pct": _num(latest_gmp.get("gain")),
        "gmp_date": latest_gmp.get("date"),
        "gmp_source": gmp.get("gmpSource"),
        "gmp_trends": gmp_trends,

        "qib_x": _subscription_x(subs.get("institutional")),
        "nii_x": _subscription_x(subs.get("nii")),
        "retail_x": _subscription_x(subs.get("retail")),
        "total_x": _subscription_x(subs.get("total")),

        "strengths": item.get("strengths") or [],
        "risks": item.get("risks") or [],
        "about_company": item.get("aboutCompany"),
        "raw": item,
    }

def field_readiness(n):
    fields = {
        "identity": bool(n.get("name") and n.get("type")),
        "schedule": bool(n.get("end_date")),
        "price": n.get("price_high") is not None,
        "issue_size": n.get("issue_size_cr") is not None,
        "gmp": n.get("gmp_gain_pct") is not None,
        "gmp_history": len(n.get("gmp_trends") or []) >= 2,
        "qib": n.get("qib_x") is not None,
        "nii": n.get("nii_x") is not None,
        "retail": n.get("retail_x") is not None,
        "total_subscription": n.get("total_x") is not None,
        "prospectus": bool(n.get("rhp_url") or n.get("drhp_url")),
        "risks": bool(n.get("risks")),
    }
    return fields
