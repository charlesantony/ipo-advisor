import csv
import io
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo

from historical_ipomarkets import (
    RichTableParser,
    _date_from_text,
    _fetch,
)
from logging_utils import logger

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "state" / "listed_tracker.json"

NSE_EQUITY_MASTER_URL = (
    "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
)
NSE_SME_MASTER_URL = (
    "https://nsearchives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv"
)

YAHOO_SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
YAHOO_SPARK_URL = "https://query1.finance.yahoo.com/v7/finance/spark"

_STOPWORDS = {
    "limited", "ltd", "private", "pvt", "india", "the",
}
_MASTER_CACHE = None
_BHAV_CACHE = None


def _now():
    return datetime.now(IST)


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _http_text(url, timeout=10):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/150 Safari/537.36"
            ),
            "Accept": "text/csv,application/json,text/plain,*/*",
            "Accept-Language": "en-IN,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8-sig", errors="replace")


def _load_state():
    if not STATE_PATH.exists():
        return {
            "version": 1,
            "updated_at_ist": None,
            "records": {},
        }
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("state is not an object")
        value.setdefault("version", 1)
        value.setdefault("records", {})
        return value
    except Exception as exc:
        logger.warning("LISTED_STATE_READ_FAILED error=%r", str(exc))
        return {
            "version": 1,
            "updated_at_ist": None,
            "records": {},
        }


def _save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at_ist"] = _now().isoformat()
    temp = STATE_PATH.with_suffix(".json.tmp")
    temp.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temp.replace(STATE_PATH)


def _words(value):
    words = re.findall(r"[a-z0-9]+", str(value or "").lower())
    return [w for w in words if w not in _STOPWORDS]


def _name_key(value):
    return "".join(_words(value))


def _name_score(left, right):
    a = _name_key(left)
    b = _name_key(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    seq = SequenceMatcher(None, a, b).ratio()
    ta, tb = set(_words(left)), set(_words(right))
    jaccard = (
        len(ta & tb) / len(ta | tb)
        if ta and tb else 0.0
    )
    contains = (
        min(len(a), len(b)) / max(len(a), len(b))
        if a in b or b in a else 0.0
    )
    return max(seq, jaccard, contains)


def _csv_value(row, *names):
    normalized = {
        re.sub(r"[^a-z0-9]", "", str(k or "").lower()): v
        for k, v in row.items()
    }
    for name in names:
        key = re.sub(r"[^a-z0-9]", "", name.lower())
        value = normalized.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return None


def _parse_master_csv(body, source_kind):
    rows = []
    reader = csv.DictReader(io.StringIO(body))
    for raw in reader:
        symbol = _csv_value(raw, "SYMBOL", "SYMB")
        name = _csv_value(
            raw,
            "NAME OF COMPANY",
            "NAMEOFCOMPANY",
            "SECURITY NAME",
            "COMPANY NAME",
        )
        if not symbol or not name:
            continue
        rows.append({
            "symbol": symbol.strip().upper(),
            "name": name.strip(),
            "series": _csv_value(raw, "SERIES"),
            "isin": _csv_value(raw, "ISIN NUMBER", "ISIN"),
            "listing_date_raw": _csv_value(
                raw, "DATE OF LISTING", "LISTING DATE"
            ),
            "source_kind": source_kind,
        })
    return rows


def _load_nse_masters():
    global _MASTER_CACHE
    if _MASTER_CACHE is not None:
        return _MASTER_CACHE

    equity = []
    sme = []
    errors = []

    for url, label, target in (
        (NSE_EQUITY_MASTER_URL, "NSE_EQUITY_MASTER", equity),
        (NSE_SME_MASTER_URL, "NSE_SME_MASTER", sme),
    ):
        try:
            body = _http_text(url, timeout=12)
            target.extend(_parse_master_csv(body, label))
            logger.info(
                "LISTED_SECURITY_MASTER_SUCCESS source=%s rows=%s",
                label, len(target),
            )
        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
            logger.warning(
                "LISTED_SECURITY_MASTER_FAILED source=%s error=%r",
                label, str(exc),
            )

    _MASTER_CACHE = {
        "MAINBOARD": equity,
        "SME": sme,
        "all": equity + sme,
        "errors": errors,
    }
    return _MASTER_CACHE


def _parse_master_listing_date(value):
    if not value:
        return None
    text = " ".join(str(value).split())
    for fmt in (
        "%d-%b-%Y",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%Y-%m-%d",
        "%d %b %Y",
        "%d %B %Y",
    ):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _resolve_from_nse_master(name, segment, masters):
    pool = masters.get(str(segment or "").upper()) or masters.get("all") or []
    best = None
    best_score = 0.0
    for candidate in pool:
        score = _name_score(name, candidate.get("name"))
        if score > best_score:
            best = candidate
            best_score = score

    if not best or best_score < 0.76:
        return None

    return {
        "market": "NSE",
        "symbol": best["symbol"],
        "market_symbol": f'{best["symbol"]}.NS',
        "series": best.get("series"),
        "isin": best.get("isin"),
        "listing_date": _parse_master_listing_date(
            best.get("listing_date_raw")
        ),
        "identity_source": best.get("source_kind"),
        "identity_score": round(best_score, 3),
    }


def _resolve_from_yahoo(name):
    params = urllib.parse.urlencode({
        "q": name,
        "quotesCount": 10,
        "newsCount": 0,
    })
    url = f"{YAHOO_SEARCH_URL}?{params}"
    try:
        payload = json.loads(_http_text(url, timeout=8))
    except Exception as exc:
        logger.warning(
            "LISTED_YAHOO_SEARCH_FAILED name=%r error=%r",
            name, str(exc),
        )
        return None

    best = None
    best_score = 0.0
    for q in payload.get("quotes") or []:
        symbol = str(q.get("symbol") or "").upper()
        if not (
            symbol.endswith(".NS")
            or symbol.endswith(".BO")
        ):
            continue
        quote_type = str(q.get("quoteType") or "").upper()
        if quote_type and quote_type != "EQUITY":
            continue
        candidate_name = (
            q.get("longname")
            or q.get("shortname")
            or q.get("displayName")
            or ""
        )
        score = _name_score(name, candidate_name)
        if score > best_score:
            best_score = score
            best = q

    if not best or best_score < 0.74:
        return None

    market_symbol = str(best.get("symbol") or "").upper()
    market = "NSE" if market_symbol.endswith(".NS") else "BSE"
    symbol = market_symbol.rsplit(".", 1)[0]
    return {
        "market": market,
        "symbol": symbol,
        "market_symbol": market_symbol,
        "series": None,
        "isin": None,
        "listing_date": None,
        "identity_source": "YAHOO_SEARCH",
        "identity_score": round(best_score, 3),
    }


def _status_listing_date(row):
    text = str(row.get("provider_status") or "")
    match = re.search(
        r"\blisted\s+(\d{1,2})\s+([A-Za-z]{3,9})",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    year = int(row.get("year") or _now().year)
    raw = f"{match.group(1)} {match.group(2)} {year}"
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _is_listed(row):
    direct_listing_date = row.get("listing_date")
    if direct_listing_date:
        try:
            listing_day = datetime.strptime(
                str(direct_listing_date), "%Y-%m-%d"
            ).date()
            if listing_day <= _now().date():
                return True
        except (TypeError, ValueError):
            pass

    return (
        row.get("listing_price") is not None
        or row.get("actual_listing_gain_pct") is not None
        or "listed" in str(row.get("provider_status") or "").lower()
    )


def _bhav_price_map(body, as_of_date):
    result = {}
    reader = csv.DictReader(io.StringIO(body))
    for raw in reader:
        symbol = _csv_value(raw, "SYMBOL", "SYMB")
        if not symbol:
            continue
        series = (_csv_value(raw, "SERIES") or "").upper()
        if series and series not in {
            "EQ", "BE", "BZ", "SM", "ST", "SZ",
        }:
            continue
        last = _f(_csv_value(
            raw,
            "LAST_PRICE", "LAST", "LTP", "LASTTRADEDPRICE",
        ))
        close = _f(_csv_value(
            raw,
            "CLOSE_PRICE", "CLOSE", "CLOSINGPRICE",
        ))
        price = last if last is not None and last > 0 else close
        if price is None or price <= 0:
            continue
        result[symbol.strip().upper()] = {
            "price": price,
            "as_of_ist": (
                f"{as_of_date.isoformat()}T16:00:00+05:30"
            ),
            "source": "NSE_BHAVCOPY",
        }
    return result


def _download_bhav_for_day(day):
    stamp = day.strftime("%d%m%Y")
    maps = {}
    errors = []

    candidates = [
        (
            f"https://nsearchives.nseindia.com/products/content/"
            f"sec_bhavdata_full_{stamp}.csv"
        ),
        (
            f"https://archives.nseindia.com/products/content/"
            f"sec_bhavdata_full_{stamp}.csv"
        ),
    ]
    for url in candidates:
        try:
            body = _http_text(url, timeout=8)
            parsed = _bhav_price_map(body, day)
            if parsed:
                maps.update(parsed)
                break
        except Exception as exc:
            errors.append(f"{url}: {type(exc).__name__}: {exc}")

    sme_candidates = [
        (
            f"https://nsearchives.nseindia.com/emerge/corporates/content/"
            f"sme{stamp}.csv"
        ),
        (
            f"https://archives.nseindia.com/emerge/corporates/content/"
            f"sme{stamp}.csv"
        ),
    ]
    for url in sme_candidates:
        try:
            body = _http_text(url, timeout=8)
            parsed = _bhav_price_map(body, day)
            if parsed:
                maps.update(parsed)
                break
        except Exception as exc:
            errors.append(f"{url}: {type(exc).__name__}: {exc}")

    return maps, errors


def _latest_nse_bhav():
    global _BHAV_CACHE
    if _BHAV_CACHE is not None:
        return _BHAV_CACHE

    today = _now().date()
    all_errors = []
    for offset in range(0, 10):
        day = today - timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        prices, errors = _download_bhav_for_day(day)
        all_errors.extend(errors)
        if prices:
            logger.info(
                "LISTED_NSE_BHAV_SUCCESS day=%s symbols=%s",
                day.isoformat(), len(prices),
            )
            _BHAV_CACHE = {
                "prices": prices,
                "day": day.isoformat(),
                "errors": all_errors,
            }
            return _BHAV_CACHE

    logger.warning(
        "LISTED_NSE_BHAV_UNAVAILABLE errors=%s",
        all_errors[-5:],
    )
    _BHAV_CACHE = {
        "prices": {},
        "day": None,
        "errors": all_errors,
    }
    return _BHAV_CACHE


def _chunks(values, size):
    for i in range(0, len(values), size):
        yield values[i:i + size]


def _yahoo_spark_prices(symbols):
    symbols = sorted({str(s).upper() for s in symbols if s})
    out = {}
    errors = []

    for batch in _chunks(symbols, 35):
        params = urllib.parse.urlencode({
            "symbols": ",".join(batch),
            "range": "1d",
            "interval": "5m",
        })
        url = f"{YAHOO_SPARK_URL}?{params}"
        try:
            payload = json.loads(_http_text(url, timeout=8))
        except Exception as exc:
            errors.append(
                f"{type(exc).__name__}: {exc}"
            )
            continue

        for item in (payload.get("spark") or {}).get("result") or []:
            symbol = str(item.get("symbol") or "").upper()
            responses = item.get("response") or []
            if not symbol or not responses:
                continue
            response = responses[0] or {}
            timestamps = response.get("timestamp") or []
            quotes = (
                ((response.get("indicators") or {}).get("quote") or [{}])[0]
            )
            closes = quotes.get("close") or []

            price = None
            price_ts = None
            for idx in range(min(len(timestamps), len(closes)) - 1, -1, -1):
                candidate = _f(closes[idx])
                if candidate is not None and candidate > 0:
                    price = candidate
                    price_ts = timestamps[idx]
                    break

            meta = response.get("meta") or {}
            if price is None:
                price = _f(meta.get("regularMarketPrice"))
                price_ts = meta.get("regularMarketTime")

            if price is None or price <= 0:
                continue

            as_of = None
            try:
                as_of = datetime.fromtimestamp(
                    int(price_ts), tz=IST
                ).isoformat()
            except (TypeError, ValueError, OSError):
                as_of = _now().isoformat()

            out[symbol] = {
                "price": round(price, 2),
                "as_of_ist": as_of,
                "source": "YAHOO_FINANCE_MARKET",
            }

    if errors:
        logger.warning(
            "LISTED_YAHOO_SPARK_PARTIAL errors=%s",
            errors[-5:],
        )
    else:
        logger.info(
            "LISTED_YAHOO_SPARK_SUCCESS requested=%s received=%s",
            len(symbols), len(out),
        )
    return out, errors


def _parse_unlock_events(detail_url):
    body, _, _ = _fetch(
        detail_url,
        "LISTED_UNLOCK_DETAIL",
        timeout=15,
    )
    parser = RichTableParser()
    parser.feed(body)

    events = []
    for table in parser.tables:
        if not table:
            continue
        headers = [
            re.sub(r"[^a-z0-9]", "", str(c.get("text") or "").lower())
            for c in table[0]
        ]
        if not (
            "shareslockedin" in headers
            and "duration" in headers
            and "unlockson" in headers
        ):
            continue

        li = headers.index("shareslockedin")
        di = headers.index("duration")
        ui = headers.index("unlockson")

        for row in table[1:]:
            if max(li, di, ui) >= len(row):
                continue
            label = " ".join(str(row[li].get("text") or "").split())
            duration = " ".join(str(row[di].get("text") or "").split())
            unlock_text = " ".join(str(row[ui].get("text") or "").split())
            unlock_date = _date_from_text(
                unlock_text,
                default_year=_now().year,
            )
            if not label or not unlock_date:
                continue

            lower = label.lower()
            if "anchor" in lower:
                category = "ANCHOR"
            elif "non-promoter" in lower or "pre-issue" in lower:
                category = "PRE_IPO"
            elif "promoter" in lower:
                category = "PROMOTER"
            else:
                category = "OTHER"

            events.append({
                "category": category,
                "label": label,
                "duration": duration or None,
                "unlock_date": unlock_date,
                "source_url": detail_url,
                "source_kind": "IPOMARKETS_LOCKIN_TABLE",
            })
        break

    events.sort(key=lambda x: x.get("unlock_date") or "9999-12-31")
    return events


def _cache_needs_unlock_refresh(item):
    checked = item.get("unlock_checked_at_ist")
    events = item.get("unlock_events")
    if not checked:
        return True
    try:
        dt = datetime.fromisoformat(str(checked))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        age_days = (_now() - dt.astimezone(IST)).total_seconds() / 86400.0
    except Exception:
        return True

    # A successful lock-in table is essentially static. A missing table is
    # rechecked sooner because the upstream page may fill it after listing.
    return age_days >= (60 if events else 7)


def _decorate_unlock_events(events):
    today = _now().date()
    future = []
    for event in events or []:
        try:
            day = datetime.strptime(
                event.get("unlock_date"), "%Y-%m-%d"
            ).date()
        except Exception:
            continue
        if day < today:
            continue
        item = dict(event)
        item["days_remaining"] = (day - today).days
        future.append(item)
    future.sort(key=lambda x: x.get("unlock_date") or "9999-12-31")
    return future


def _return_pct(price, issue_price):
    p = _f(price)
    issue = _f(issue_price)
    if p is None or issue is None or issue == 0:
        return None
    return round((p - issue) / issue * 100.0, 2)


def _latest_timestamp(values):
    best = None
    best_dt = None
    for value in values:
        if not value:
            continue
        try:
            dt = datetime.fromisoformat(str(value))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST)
            dt = dt.astimezone(IST)
        except Exception:
            continue
        if best_dt is None or dt > best_dt:
            best = value
            best_dt = dt
    return best


def build_listed_payload(
    tracker_rows,
    deep_refresh=False,
    persist=False,
    max_unlock_fetches=30,
    max_yahoo_identity_searches=20,
):
    now = _now()
    state = _load_state()
    cache = state.setdefault("records", {})
    listed_rows = [r for r in (tracker_rows or []) if _is_listed(r)]

    # Newest issues are the most useful to populate first.
    listed_rows.sort(
        key=lambda r: (
            str(r.get("issue_close") or ""),
            str(r.get("name") or ""),
        ),
        reverse=True,
    )

    masters = _load_nse_masters()

    unresolved = []
    for row in listed_rows:
        key = str(row.get("tracker_key") or "")
        if not key:
            continue
        item = cache.setdefault(key, {})
        item["name"] = row.get("name")
        item["ipo_type"] = row.get("ipo_type")
        item["detail_url"] = row.get("detail_url")

        identity = item.get("identity") or _resolve_from_nse_master(
            row.get("name"),
            row.get("ipo_type"),
            masters,
        )
        if identity:
            item["identity"] = identity
        elif deep_refresh:
            unresolved.append((row, item))

    if deep_refresh and unresolved:
        for row, item in unresolved[:max_yahoo_identity_searches]:
            identity = _resolve_from_yahoo(row.get("name"))
            if identity:
                item["identity"] = identity
            time.sleep(0.08)

    # Fetch lock-in tables incrementally. Recent listings are processed first,
    # then the cache fills older 2026 records over subsequent daily runs.
    if deep_refresh:
        unlock_fetches = 0
        for row in listed_rows:
            if unlock_fetches >= max_unlock_fetches:
                break
            key = str(row.get("tracker_key") or "")
            item = cache.get(key) or {}
            detail_url = row.get("detail_url")
            if not detail_url or not _cache_needs_unlock_refresh(item):
                continue
            try:
                events = _parse_unlock_events(detail_url)
                item["unlock_events"] = events
                item["unlock_status"] = (
                    "AVAILABLE" if events else "NOT_AVAILABLE"
                )
                logger.info(
                    "LISTED_UNLOCK_REFRESH name=%r events=%s source=%s",
                    row.get("name"), len(events), detail_url,
                )
            except Exception as exc:
                item.setdefault("unlock_events", [])
                item["unlock_status"] = "FETCH_FAILED"
                item["unlock_error"] = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "LISTED_UNLOCK_FAILED name=%r url=%r error=%r",
                    row.get("name"), detail_url, str(exc),
                )
            item["unlock_checked_at_ist"] = now.isoformat()
            unlock_fetches += 1
            time.sleep(0.08)

    market_symbols = []
    for row in listed_rows:
        item = cache.get(str(row.get("tracker_key") or "")) or {}
        identity = item.get("identity") or {}
        market_symbol = identity.get("market_symbol")
        if market_symbol:
            market_symbols.append(market_symbol)

    yahoo_prices, yahoo_errors = _yahoo_spark_prices(market_symbols)
    bhav = _latest_nse_bhav()
    bhav_prices = bhav.get("prices") or {}

    records = []
    price_timestamps = []
    resolved_count = 0
    current_price_count = 0
    unlock_count = 0

    for row in listed_rows:
        key = str(row.get("tracker_key") or "")
        item = cache.setdefault(key, {})
        identity = item.get("identity") or {}
        if identity:
            resolved_count += 1

        market_symbol = str(identity.get("market_symbol") or "").upper()
        symbol = (
            identity.get("symbol")
            or (market_symbol.rsplit(".", 1)[0] if market_symbol else None)
        )

        quote = yahoo_prices.get(market_symbol)
        if quote is None and symbol and identity.get("market") == "NSE":
            quote = bhav_prices.get(str(symbol).upper())

        if quote:
            item["current_price"] = quote.get("price")
            item["current_price_as_of_ist"] = quote.get("as_of_ist")
            item["current_price_source"] = quote.get("source")
        current_price = item.get("current_price")
        current_price_as_of = item.get("current_price_as_of_ist")
        current_price_source = item.get("current_price_source")
        if current_price is not None:
            current_price_count += 1
            if current_price_as_of:
                price_timestamps.append(current_price_as_of)

        issue_price = _f(row.get("issue_price"))
        listing_price = _f(row.get("listing_price"))
        listing_pct = _f(row.get("actual_listing_gain_pct"))
        if listing_pct is None:
            listing_pct = _return_pct(listing_price, issue_price)

        events = _decorate_unlock_events(item.get("unlock_events") or [])
        if events:
            unlock_count += 1

        listing_date = (
            row.get("listing_date")
            or identity.get("listing_date")
            or _status_listing_date(row)
        )

        records.append({
            "tracker_key": key,
            "name": row.get("name"),
            "symbol": symbol,
            "market": identity.get("market"),
            "market_symbol": market_symbol or None,
            "ipo_type": row.get("ipo_type"),
            "listing_date": listing_date,
            "issue_price": issue_price,
            "listing_price": listing_price,
            "listing_return_pct": (
                round(listing_pct, 2)
                if listing_pct is not None else None
            ),
            "current_price": _f(current_price),
            "current_return_pct": _return_pct(
                current_price, issue_price
            ),
            "current_price_as_of_ist": current_price_as_of,
            "current_price_source": current_price_source,
            "unlock_events": events,
            "next_unlock": events[0] if events else None,
            "unlock_status": item.get("unlock_status") or (
                "PENDING_DISCOVERY"
                if row.get("detail_url")
                else "NOT_AVAILABLE"
            ),
            "unlock_checked_at_ist": item.get("unlock_checked_at_ist"),
            "detail_url": row.get("detail_url"),
        })

    records.sort(
        key=lambda r: (
            str(r.get("listing_date") or ""),
            str(r.get("name") or ""),
        ),
        reverse=True,
    )

    state["records"] = cache
    if persist:
        _save_state(state)

    payload = {
        "version": "listed-tracker-v1",
        "generated_at_ist": now.isoformat(),
        "current_prices_as_of_ist": _latest_timestamp(price_timestamps),
        "summary": {
            "listed_records": len(records),
            "market_identity_resolved": resolved_count,
            "current_price_available": current_price_count,
            "unlock_schedule_available": unlock_count,
        },
        "records": records,
        "sources": {
            "security_master": [
                NSE_EQUITY_MASTER_URL,
                NSE_SME_MASTER_URL,
            ],
            "current_price_primary": "YAHOO_FINANCE_MARKET",
            "current_price_fallback": "NSE_BHAVCOPY",
            "unlock_events": "IPOMARKETS_LOCKIN_TABLE",
        },
        "errors": {
            "security_master": masters.get("errors") or [],
            "current_price": yahoo_errors,
            "nse_bhav": bhav.get("errors") or [],
        },
    }
    logger.info(
        "LISTED_PAYLOAD records=%s identities=%s prices=%s unlocks=%s "
        "deep_refresh=%s persist=%s",
        len(records), resolved_count, current_price_count, unlock_count,
        deep_refresh, persist,
    )
    return payload

# ---------------------------------------------------------------------------
# v0.5.22: improve listed-market coverage without changing frozen IPO model.
# ---------------------------------------------------------------------------

_V0522_BSE_SEARCH_URL = (
    "https://api.bseindia.com/Msource/1D/getQouteSearch.aspx"
)
_V0522_BSE_HEADER_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/getScripHeaderData/w"
)
_V0522_YAHOO_CHART_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart"
)


def _v0522_strip_html(value):
    import html
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return " ".join(html.unescape(text).split())


def _v0522_bse_text(url, timeout=8):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/150 Safari/537.36"
            ),
            "Accept": "application/json,text/html,text/plain,*/*",
            "Accept-Language": "en-IN,en;q=0.9",
            "Origin": "https://www.bseindia.com",
            "Referer": "https://www.bseindia.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _v0522_resolve_bse(name):
    params = urllib.parse.urlencode({
        "Type": "EQ",
        "text": name,
        "flag": "site",
    })
    url = f"{_V0522_BSE_SEARCH_URL}?{params}"

    try:
        body = _v0522_bse_text(url)
    except Exception as exc:
        logger.warning(
            "LISTED_BSE_SEARCH_FAILED name=%r error=%r",
            name, str(exc),
        )
        return None

    candidates = []
    for match in re.finditer(
        r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
        body,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        href = match.group(1)
        label = _v0522_strip_html(match.group(2))
        code_match = re.search(r"/(\d{6})(?:/|$|\?)", href)
        if not code_match:
            code_match = re.search(r"\b(\d{6})\b", href)
        if not code_match:
            continue
        candidates.append((
            _name_score(name, label),
            code_match.group(1),
            label,
        ))

    if not candidates:
        for match in re.finditer(r"/(\d{6})(?:/|\?)", body):
            start = max(0, match.start() - 220)
            end = min(len(body), match.end() + 220)
            label = _v0522_strip_html(body[start:end])
            candidates.append((
                _name_score(name, label),
                match.group(1),
                label,
            ))

    if not candidates:
        return None

    candidates.sort(reverse=True, key=lambda item: item[0])
    score, code, label = candidates[0]
    if score < 0.62:
        return None

    logger.info(
        "LISTED_BSE_IDENTITY_SUCCESS name=%r code=%s score=%.3f",
        name, code, score,
    )
    return {
        "market": "BSE",
        "symbol": code,
        "market_symbol": f"{code}.BO",
        "bse_code": code,
        "series": None,
        "isin": None,
        "listing_date": None,
        "identity_source": "BSE_QUOTE_SEARCH",
        "identity_score": round(score, 3),
    }


def _v0522_bse_quote(code):
    params = urllib.parse.urlencode({
        "Debtflag": "",
        "scripcode": str(code),
        "seriesid": "",
    })
    url = f"{_V0522_BSE_HEADER_URL}?{params}"

    try:
        payload = json.loads(_v0522_bse_text(url))
    except Exception as exc:
        logger.warning(
            "LISTED_BSE_QUOTE_FAILED code=%r error=%r",
            code, str(exc),
        )
        return None

    curr = payload.get("CurrRate") or {}
    if isinstance(curr, list):
        curr = curr[0] if curr else {}
    if not isinstance(curr, dict):
        curr = {}

    price = _f(
        curr.get("LTP")
        or curr.get("ltp")
        or payload.get("CurrVal")
        or payload.get("LTP")
    )
    if price is None or price <= 0:
        return None

    return {
        "price": round(price, 2),
        "as_of_ist": _now().isoformat(),
        "source": "BSE_DIRECT_QUOTE",
    }


def _v0522_nse_listing_open(symbol, listing_date):
    try:
        day = datetime.strptime(
            str(listing_date), "%Y-%m-%d"
        ).date()
    except (TypeError, ValueError):
        return None

    stamp = day.strftime("%d%m%Y")
    urls = [
        (
            "https://nsearchives.nseindia.com/products/content/"
            f"sec_bhavdata_full_{stamp}.csv"
        ),
        (
            "https://archives.nseindia.com/products/content/"
            f"sec_bhavdata_full_{stamp}.csv"
        ),
        (
            "https://nsearchives.nseindia.com/emerge/corporates/content/"
            f"sme{stamp}.csv"
        ),
    ]

    for url in urls:
        try:
            body = _http_text(url, timeout=8)
            reader = csv.DictReader(io.StringIO(body))
            for raw in reader:
                row_symbol = _csv_value(raw, "SYMBOL", "SYMB")
                if str(row_symbol or "").strip().upper() != str(symbol).upper():
                    continue
                open_price = _f(_csv_value(
                    raw,
                    "OPEN_PRICE", "OPEN", "OPENINGPRICE",
                ))
                if open_price is not None and open_price > 0:
                    return {
                        "price": round(open_price, 2),
                        "source": "NSE_LISTING_DAY_OPEN",
                    }
        except Exception:
            continue
    return None


def _v0522_yahoo_listing_open(market_symbol, listing_date):
    if not market_symbol or not listing_date:
        return None
    try:
        listing_day = datetime.strptime(
            str(listing_date), "%Y-%m-%d"
        ).date()
    except ValueError:
        return None

    start = datetime(
        listing_day.year,
        listing_day.month,
        listing_day.day,
        tzinfo=IST,
    ) - timedelta(days=1)
    end = start + timedelta(days=5)

    encoded = urllib.parse.quote(str(market_symbol), safe="")
    params = urllib.parse.urlencode({
        "period1": int(start.timestamp()),
        "period2": int(end.timestamp()),
        "interval": "1d",
        "events": "history",
    })
    url = f"{_V0522_YAHOO_CHART_URL}/{encoded}?{params}"

    try:
        payload = json.loads(_http_text(url, timeout=8))
        result = (
            ((payload.get("chart") or {}).get("result") or [None])[0]
        ) or {}
        timestamps = result.get("timestamp") or []
        quote = (
            ((result.get("indicators") or {}).get("quote") or [{}])[0]
        )
        opens = quote.get("open") or []

        for idx, timestamp in enumerate(timestamps):
            if idx >= len(opens):
                break
            price = _f(opens[idx])
            if price is None or price <= 0:
                continue
            observed_day = datetime.fromtimestamp(
                int(timestamp), tz=IST
            ).date()
            if observed_day == listing_day:
                return {
                    "price": round(price, 2),
                    "source": "YAHOO_LISTING_DAY_OPEN",
                }
    except Exception as exc:
        logger.warning(
            "LISTED_YAHOO_LISTING_OPEN_FAILED symbol=%r date=%r error=%r",
            market_symbol, listing_date, str(exc),
        )
    return None


_build_listed_payload_v0517 = build_listed_payload


def build_listed_payload(*args, **kwargs):
    """v0.5.22 wrapper adding BSE identities and market-price fallbacks."""
    payload = _build_listed_payload_v0517(*args, **kwargs)
    deep_refresh = bool(kwargs.get("deep_refresh", False))
    persist = bool(kwargs.get("persist", False))

    state = _load_state()
    cache = state.setdefault("records", {})
    bse_resolved = 0
    bse_quotes = 0
    listing_fallbacks = 0

    for record in payload.get("records") or []:
        key = str(record.get("tracker_key") or "")
        item = cache.setdefault(key, {})
        identity = item.get("identity") or {}

        # The original implementation resolves NSE well but misses many BSE-SME
        # securities. Discover BSE identity during the deeper daily sync.
        if not identity and deep_refresh:
            resolved_identity = _v0522_resolve_bse(
                record.get("name")
            )
            if resolved_identity:
                identity = resolved_identity
                item["identity"] = identity
                bse_resolved += 1

        # Resolution failure is a normal data-availability condition.
        # Never let an optional market-identity lookup turn the whole
        # Daily Outcome Sync into a failure.
        if not isinstance(identity, dict):
            identity = {}

        if identity:
            record["market"] = identity.get("market")
            record["symbol"] = (
                identity.get("symbol")
                or record.get("symbol")
            )
            record["market_symbol"] = (
                identity.get("market_symbol")
                or record.get("market_symbol")
            )

        # During deep refresh, prefer a direct BSE quote for BSE securities.
        if (
            deep_refresh
            and identity.get("market") == "BSE"
            and identity.get("bse_code")
        ):
            quote = _v0522_bse_quote(identity.get("bse_code"))
            if quote:
                item["current_price"] = quote["price"]
                item["current_price_as_of_ist"] = quote["as_of_ist"]
                item["current_price_source"] = quote["source"]
                record["current_price"] = quote["price"]
                record["current_price_as_of_ist"] = quote["as_of_ist"]
                record["current_price_source"] = quote["source"]
                record["current_return_pct"] = _return_pct(
                    quote["price"],
                    record.get("issue_price"),
                )
                bse_quotes += 1

        # Reuse the cached BSE quote on lightweight refreshes if Yahoo does not
        # expose the security yet.
        if record.get("current_price") is None:
            cached_price = _f(item.get("current_price"))
            if cached_price is not None:
                record["current_price"] = cached_price
                record["current_price_as_of_ist"] = item.get(
                    "current_price_as_of_ist"
                )
                record["current_price_source"] = item.get(
                    "current_price_source"
                )
                record["current_return_pct"] = _return_pct(
                    cached_price,
                    record.get("issue_price"),
                )

        # If the IPO source has not populated listing price yet, take the
        # actual first-day market open. For NSE use bhavcopy first.
        if record.get("listing_price") is None:
            cached_listing = _f(item.get("market_listing_price"))
            listing = None
            if cached_listing is not None:
                listing = {
                    "price": cached_listing,
                    "source": item.get(
                        "market_listing_price_source"
                    ) or "MARKET_LISTING_FALLBACK",
                }

            market = identity.get("market") or record.get("market")
            symbol = identity.get("symbol") or record.get("symbol")
            market_symbol = (
                identity.get("market_symbol")
                or record.get("market_symbol")
            )
            listing_date = record.get("listing_date")

            if listing is None and market == "NSE" and symbol:
                listing = _v0522_nse_listing_open(
                    symbol, listing_date
                )
            if listing is None and market_symbol:
                listing = _v0522_yahoo_listing_open(
                    market_symbol, listing_date
                )

            if listing:
                price = _f(listing.get("price"))
                if price is not None:
                    record["listing_price"] = price
                    record["listing_price_source"] = listing.get("source")
                    record["listing_return_pct"] = _return_pct(
                        price,
                        record.get("issue_price"),
                    )
                    item["market_listing_price"] = price
                    item["market_listing_price_source"] = listing.get(
                        "source"
                    )
                    listing_fallbacks += 1

    records = payload.get("records") or []
    summary = payload.setdefault("summary", {})
    summary["market_identity_resolved"] = sum(
        1 for r in records
        if r.get("market") and r.get("symbol")
    )
    summary["current_price_available"] = sum(
        1 for r in records
        if _f(r.get("current_price")) is not None
    )
    summary["bse_identities_added"] = bse_resolved
    summary["bse_direct_quotes"] = bse_quotes
    summary["listing_price_market_fallbacks"] = listing_fallbacks

    payload["current_prices_as_of_ist"] = _latest_timestamp([
        r.get("current_price_as_of_ist")
        for r in records
    ])

    sources = payload.setdefault("sources", {})
    sources["bse_identity"] = "BSE_QUOTE_SEARCH"
    sources["bse_current_price"] = "BSE_DIRECT_QUOTE"
    sources["listing_price_fallback"] = [
        "NSE_LISTING_DAY_OPEN",
        "YAHOO_LISTING_DAY_OPEN",
    ]

    state["records"] = cache
    if persist:
        _save_state(state)

    logger.info(
        "LISTED_V0522_ENRICH bse_resolved=%s bse_quotes=%s "
        "listing_fallbacks=%s prices=%s/%s",
        bse_resolved,
        bse_quotes,
        listing_fallbacks,
        summary.get("current_price_available"),
        summary.get("listed_records"),
    )
    return payload
