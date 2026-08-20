import re
from normalizer import _num

def _canon(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())

def flatten(obj, prefix=""):
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            out.extend(flatten(v, p))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(flatten(v, f"{prefix}[{i}]"))
    else:
        out.append((prefix, obj))
    return out

def _lookup(flat, aliases):
    aliases = {_canon(a) for a in aliases}
    exact = []
    fuzzy = []
    for path, value in flat:
        last = path.split(".")[-1]
        last = re.sub(r"\[\d+\]$", "", last)
        c = _canon(last)
        if c in aliases:
            exact.append((path, value))
        elif any(a and a in c for a in aliases):
            fuzzy.append((path, value))
    for path, value in exact + fuzzy:
        n = _num(value)
        if n is not None:
            return n, path
    return None, None

def extract_listing_outcome(raw, normalized=None):
    """
    Best-effort extractor because free IPO feeds can alter their nested schema.

    We store both the extracted values and the exact source paths so the UI can
    show what was actually used rather than silently guessing.
    """
    flat = flatten(raw or {})
    normalized = normalized or {}

    issue_price, issue_path = _lookup(flat, [
        "issuePrice", "finalIssuePrice", "offerPrice", "ipoPrice"
    ])
    if issue_price is None:
        issue_price = normalized.get("price_high")
        issue_path = "normalized.price_high" if issue_price is not None else None

    listing_open, open_path = _lookup(flat, [
        "listingPrice", "listingOpen", "listingOpenPrice", "openListingPrice",
        "listingPriceOpen", "listingDayOpen"
    ])

    listing_close, close_path = _lookup(flat, [
        "listingDayClose", "listingClose", "listingClosePrice",
        "listingDayClosingPrice", "closeListingPrice"
    ])

    gain_pct, gain_path = _lookup(flat, [
        "listingGainPct", "listingGainPercent", "listingGains",
        "listingGain", "listingDayGainPct", "listingDayGainPercent",
        "listingDayGainLossPercent", "listingDayReturn"
    ])

    # Prefer listing-day close as the target if available.
    target_price = listing_close if listing_close is not None else listing_open
    target_kind = "listing_day_close" if listing_close is not None else (
        "listing_open" if listing_open is not None else None
    )

    derived_gain = None
    if issue_price and target_price is not None:
        derived_gain = (target_price / issue_price - 1.0) * 100.0

    # If an explicit percentage exists and appears plausible, use it.
    # Otherwise derive from price. Values > 500 are more likely rupee amounts
    # or a malformed field, so we reject them here.
    if gain_pct is not None and -100 <= gain_pct <= 500:
        target_gain_pct = gain_pct
        target_source = gain_path
        target_kind = target_kind or "explicit_gain_pct"
    else:
        target_gain_pct = derived_gain
        target_source = f"derived:{issue_path}->{close_path or open_path}" if derived_gain is not None else None

    interesting_paths = []
    for path, value in flat:
        c = _canon(path)
        if any(token in c for token in ("listing", "issueprice", "gainloss")):
            interesting_paths.append({"path": path, "value": value})
    interesting_paths = interesting_paths[:30]

    return {
        "issue_price": issue_price,
        "listing_open": listing_open,
        "listing_close": listing_close,
        "listing_gain_pct": round(target_gain_pct, 4) if target_gain_pct is not None else None,
        "target_kind": target_kind,
        "target_source": target_source,
        "issue_price_source": issue_path,
        "listing_open_source": open_path,
        "listing_close_source": close_path,
        "diagnostic_paths": interesting_paths,
    }
