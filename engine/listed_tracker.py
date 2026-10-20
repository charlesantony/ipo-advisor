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

BSE_SCRIP_MASTER_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
)
BSE_BHAVCOPY_URL_TEMPLATE = (
    "https://www.bseindia.com/download/BhavCopy/Equity/"
    "BhavCopy_BSE_CM_0_0_0_{date}_F_0000.CSV"
)

_STOPWORDS = {
    "limited", "ltd", "private", "pvt", "india", "the",
}
_MASTER_CACHE = None
_BSE_MASTER_CACHE = None
_BHAV_CACHE = None


def _now():
    return datetime.now(IST)


def _f(value):
    try:
        if isinstance(value, str):
            value = (
                value.replace(",", "")
                .replace("₹", "")
                .strip()
            )
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


# v0.5.30: identity-quality guard.
# Fuzzy text similarity alone is not enough to bind an IPO to a traded symbol.
_IDENTITY_GENERIC_WORDS = {
    "and", "company", "co", "business", "industry", "industries",
    "engineering", "engineers", "solutions", "solution",
    "technology", "technologies", "systems", "services",
    "enterprise", "enterprises",
}

# No company-specific market mappings are used. Exchange identity must be
# resolved from an exchange security master or another name-validated source.


def _identity_name_key(value):
    text = str(value or "").replace("&", " and ")
    words = re.findall(r"[a-z0-9]+", text.lower())
    words = [
        w for w in words
        if w not in _STOPWORDS and w != "and"
    ]
    return "".join(words)


def _identity_distinctive_words(value):
    text = str(value or "").replace("&", " and ")
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {
        w for w in words
        if (
            w not in _STOPWORDS
            and w not in _IDENTITY_GENERIC_WORDS
            and len(w) >= 3
        )
    }


def _identity_names_credible(left, right):
    a = _identity_name_key(left)
    b = _identity_name_key(right)
    if not a or not b:
        return False
    if a == b:
        return True

    distinctive_overlap = (
        _identity_distinctive_words(left)
        & _identity_distinctive_words(right)
    )
    if not distinctive_overlap:
        return False

    return _name_score(left, right) >= 0.78


def _clear_market_identity_cache(item, reason=None):
    if not isinstance(item, dict):
        return
    if reason:
        item["identity_rejected_reason"] = reason
        item["identity_rejected_at_ist"] = _now().isoformat()

    for key in (
        "identity",
        "current_price",
        "current_price_as_of_ist",
        "current_price_source",
        "market_listing_price",
        "market_listing_price_source",
    ):
        item.pop(key, None)


def _cached_identity_is_credible(name, identity, masters):
    if not isinstance(identity, dict) or not identity:
        return False

    matched_name = identity.get("identity_name")
    market = str(identity.get("market") or "").upper()
    symbol = str(identity.get("symbol") or "").upper()

    if not matched_name and market == "NSE" and symbol:
        for candidate in masters.get("all") or []:
            if str(candidate.get("symbol") or "").upper() == symbol:
                matched_name = candidate.get("name")
                break

    if matched_name:
        return _identity_names_credible(name, matched_name)

    # Older BSE cache rows predate identity_name. Keep them rather than
    # destroying otherwise valid BSE coverage; new BSE resolutions carry name.
    if market == "BSE" and identity.get("bse_code"):
        return True

    return False

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


def _bse_api_json(url, params=None, timeout=15):
    query = urllib.parse.urlencode(params or {})
    target = f"{url}?{query}" if query else url
    req = urllib.request.Request(
        target,
        headers={
            "Host": "api.bseindia.com",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/150 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-IN,en;q=0.9",
            "Origin": "https://www.bseindia.com",
            "Referer": "https://www.bseindia.com/",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(
            resp.read().decode("utf-8-sig", errors="replace")
        )


def _bse_bhavcopy_text(day, timeout=18):
    url = BSE_BHAVCOPY_URL_TEMPLATE.format(
        date=day.strftime("%Y%m%d")
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/150 Safari/537.36"
            ),
            "Accept": "text/csv,text/plain,*/*",
            "Accept-Language": "en-IN,en;q=0.9",
            "Referer": "https://www.bseindia.com/markets/marketinfo/bhavcopy",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return (
            resp.read().decode("utf-8-sig", errors="replace"),
            url,
        )


def _parse_bse_bhavcopy(body, fallback_day):
    rows = []
    reader = csv.DictReader(io.StringIO(body))

    for raw in reader:
        code = _csv_value(
            raw,
            "FinInstrmId",
            "FININSTRMID",
        )
        symbol = _csv_value(
            raw,
            "TckrSymb",
            "TICKER SYMBOL",
        )
        name = _csv_value(
            raw,
            "FinInstrmNm",
            "INSTRUMENT NAME",
            "SECURITY NAME",
        )
        isin = _csv_value(raw, "ISIN")
        series = _csv_value(raw, "SctySrs", "SERIES")
        trade_date = _csv_value(raw, "TradDt", "BizDt")

        if not code or not name:
            continue

        price = _f(_csv_value(
            raw,
            "LastPric",
            "ClsPric",
            "PrvsClsgPric",
        ))
        volume = _f(_csv_value(raw, "TtlTradgVol"))

        as_of_day = fallback_day
        if trade_date:
            try:
                as_of_day = datetime.strptime(
                    trade_date[:10], "%Y-%m-%d"
                ).date()
            except ValueError:
                pass

        rows.append({
            "bse_code": str(code).strip(),
            "symbol": (
                str(symbol).strip().upper()
                if symbol not in (None, "")
                else str(code).strip()
            ),
            "name": str(name).strip(),
            "isin": (
                str(isin).strip()
                if isin not in (None, "")
                else None
            ),
            "group": (
                str(series).strip()
                if series not in (None, "")
                else None
            ),
            "price": (
                round(price, 2)
                if price is not None and price > 0
                else None
            ),
            "volume": volume,
            "as_of_ist": (
                f"{as_of_day.isoformat()}T16:00:00+05:30"
            ),
            "source_kind": "BSE_UDIFF_BHAVCOPY",
        })

    return rows


def _load_bse_master():
    """Build BSE identity + EOD price universe from public UDiFF bhavcopy.

    The BSE ListofScripData API returns HTTP 403 from GitHub-hosted runners.
    The public BSE UDiFF bhavcopy is a static exchange report and carries the
    fields needed here: BSE code, ISIN, ticker, security name and EOD price.
    """
    global _BSE_MASTER_CACHE
    if _BSE_MASTER_CACHE is not None:
        return _BSE_MASTER_CACHE

    errors = []
    by_code = {}
    successful_days = []
    latest_as_of = None
    source_urls = []

    # Collect several recent sessions. This helps with very illiquid SME
    # securities while keeping the total download volume modest.
    day = _now().date()
    attempts = 0
    while attempts < 14 and len(successful_days) < 5:
        candidate = day - timedelta(days=attempts)
        attempts += 1

        if candidate.weekday() >= 5:
            continue

        try:
            body, url = _bse_bhavcopy_text(candidate)
            parsed = _parse_bse_bhavcopy(body, candidate)
            if not parsed:
                errors.append(
                    f"{candidate.isoformat()}: empty BSE bhavcopy"
                )
                continue

            successful_days.append(candidate.isoformat())
            source_urls.append(url)

            # Latest session wins for price; older sessions only fill
            # identities/prices absent in the newer report.
            for row in parsed:
                code = str(row.get("bse_code") or "")
                if not code:
                    continue
                current = by_code.get(code)
                if current is None:
                    by_code[code] = row
                elif (
                    current.get("price") is None
                    and row.get("price") is not None
                ):
                    current["price"] = row.get("price")
                    current["as_of_ist"] = row.get("as_of_ist")
                    current["volume"] = row.get("volume")

            if latest_as_of is None and parsed:
                latest_as_of = parsed[0].get("as_of_ist")

            logger.info(
                "LISTED_BSE_BHAVCOPY_SUCCESS day=%s rows=%s",
                candidate.isoformat(),
                len(parsed),
            )
        except urllib.error.HTTPError as exc:
            if int(getattr(exc, "code", 0) or 0) == 404:
                continue
            errors.append(
                f"{candidate.isoformat()}: HTTPError {exc.code}"
            )
            logger.warning(
                "LISTED_BSE_BHAVCOPY_FAILED day=%s error=%r",
                candidate.isoformat(),
                str(exc),
            )
        except Exception as exc:
            errors.append(
                f"{candidate.isoformat()}: "
                f"{type(exc).__name__}: {exc}"
            )
            logger.warning(
                "LISTED_BSE_BHAVCOPY_FAILED day=%s error=%r",
                candidate.isoformat(),
                str(exc),
            )

    rows = list(by_code.values())

    # Last-resort compatibility fallback only if the public static report could
    # not be downloaded at all. Do not call the API when bhavcopy succeeded,
    # avoiding the GitHub-runner 403 seen in production.
    if not rows:
        try:
            payload = _bse_api_json(
                BSE_SCRIP_MASTER_URL,
                {
                    "scripcode": "",
                    "Group": "",
                    "industry": "",
                    "segment": "Equity",
                    "status": "Active",
                },
                timeout=18,
            )
            candidates = (
                payload.get("Table")
                or payload.get("data")
                or payload.get("Data")
                or []
            ) if isinstance(payload, dict) else (
                payload if isinstance(payload, list) else []
            )

            for raw in candidates:
                if not isinstance(raw, dict):
                    continue
                code = (
                    raw.get("SCRIP_CD")
                    or raw.get("Scrip_Code")
                    or raw.get("scripcode")
                    or raw.get("scrip_code")
                )
                name = (
                    raw.get("Scrip_Name")
                    or raw.get("SCRIP_NAME")
                    or raw.get("Long_Name")
                    or raw.get("LONG_NAME")
                    or raw.get("Security_Name")
                )
                if code in (None, "") or not name:
                    continue
                symbol = (
                    raw.get("scrip_id")
                    or raw.get("SCRIP_ID")
                    or raw.get("Scrip_Id")
                    or raw.get("SYMBOL")
                )
                isin = (
                    raw.get("ISIN_NUMBER")
                    or raw.get("ISIN")
                    or raw.get("ISIN_CODE")
                )
                rows.append({
                    "bse_code": str(code).strip(),
                    "symbol": (
                        str(symbol).strip().upper()
                        if symbol not in (None, "")
                        else str(code).strip()
                    ),
                    "name": str(name).strip(),
                    "isin": (
                        str(isin).strip()
                        if isin not in (None, "")
                        else None
                    ),
                    "group": (
                        raw.get("GROUP")
                        or raw.get("Group")
                        or raw.get("Group_Name")
                    ),
                    "price": None,
                    "as_of_ist": None,
                    "source_kind": "BSE_ACTIVE_SECURITY_MASTER",
                })
            logger.info(
                "LISTED_BSE_MASTER_API_FALLBACK rows=%s",
                len(rows),
            )
        except Exception as exc:
            errors.append(
                f"BSE master API fallback: "
                f"{type(exc).__name__}: {exc}"
            )
            logger.warning(
                "LISTED_BSE_MASTER_API_FALLBACK_FAILED error=%r",
                str(exc),
            )

    _BSE_MASTER_CACHE = {
        "rows": rows,
        "errors": errors,
        "successful_days": successful_days,
        "latest_as_of_ist": latest_as_of,
        "source_urls": source_urls,
    }

    logger.info(
        "LISTED_BSE_UNIVERSE rows=%s sessions=%s",
        len(rows),
        successful_days,
    )
    return _BSE_MASTER_CACHE

def _resolve_from_bse_master(name, segment, bse_master):
    """Resolve a BSE security using strict company-name identity."""
    best = None
    best_score = 0.0

    for candidate in bse_master.get("rows") or []:
        candidate_name = candidate.get("name")
        if not _identity_names_credible(name, candidate_name):
            continue

        score = _name_score(name, candidate_name)
        if score > best_score:
            best = candidate
            best_score = score

    if not best:
        return None

    code = str(best.get("bse_code") or "").strip()
    if not code:
        return None

    return {
        "market": "BSE",
        "symbol": best.get("symbol") or code,
        "market_symbol": f"{code}.BO",
        "bse_code": code,
        "series": best.get("group"),
        "isin": best.get("isin"),
        "listing_date": None,
        "identity_source": best.get("source_kind")
            or "BSE_UDIFF_BHAVCOPY",
        "identity_score": round(best_score, 3),
        "identity_name": best.get("name"),
        "current_price": best.get("price"),
        "current_price_as_of_ist": best.get("as_of_ist"),
        "current_price_source": (
            "BSE_UDIFF_BHAVCOPY"
            if best.get("price") is not None
            else None
        ),
    }

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
    # Never use character similarity alone. A traded symbol is accepted only
    # when the exchange master name itself is credible for the IPO name.
    pool = (
        masters.get(str(segment or "").upper())
        or masters.get("all")
        or []
    )
    best = None
    best_score = 0.0

    for candidate in pool:
        candidate_name = candidate.get("name")
        if not _identity_names_credible(name, candidate_name):
            continue
        score = _name_score(name, candidate_name)
        if score > best_score:
            best = candidate
            best_score = score

    if not best:
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
        "identity_name": best.get("name"),
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
        if not _identity_names_credible(name, candidate_name):
            continue
        score = _name_score(name, candidate_name)
        if score > best_score:
            best_score = score
            best = q

    if not best:
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
        "identity_name": (
            best.get("longname")
            or best.get("shortname")
            or best.get("displayName")
        ),
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
    bse_master = _load_bse_master()

    unresolved = []
    for row in listed_rows:
        key = str(row.get("tracker_key") or "")
        if not key:
            continue
        item = cache.setdefault(key, {})
        item["name"] = row.get("name")
        item["ipo_type"] = row.get("ipo_type")
        item["detail_url"] = row.get("detail_url")

        cached_identity = item.get("identity") or {}
        if (
            cached_identity
            and not _cached_identity_is_credible(
                row.get("name"),
                cached_identity,
                masters,
            )
        ):
            logger.warning(
                "LISTED_IDENTITY_REJECTED name=%r market_symbol=%r source=%r",
                row.get("name"),
                cached_identity.get("market_symbol"),
                cached_identity.get("identity_source"),
            )
            _clear_market_identity_cache(
                item,
                reason="NAME_IDENTITY_MISMATCH",
            )
            cached_identity = {}

        identity = cached_identity or _resolve_from_nse_master(
            row.get("name"),
            row.get("ipo_type"),
            masters,
        )
        if not identity:
            identity = _resolve_from_bse_master(
                row.get("name"),
                row.get("ipo_type"),
                bse_master,
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

        quote = None

        # BSE's own UDiFF bhavcopy is the authoritative EOD source for BSE
        # securities and avoids Yahoo throttling.
        if (
            identity.get("market") == "BSE"
            and _f(identity.get("current_price")) is not None
        ):
            quote = {
                "price": _f(identity.get("current_price")),
                "as_of_ist": identity.get(
                    "current_price_as_of_ist"
                ),
                "source": identity.get(
                    "current_price_source"
                ) or "BSE_UDIFF_BHAVCOPY",
            }
        else:
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
            "bse_code": identity.get("bse_code"),
            "isin": identity.get("isin"),
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
                BSE_BHAVCOPY_URL_TEMPLATE,
            ],
            "current_price_primary": "YAHOO_FINANCE_MARKET",
            "current_price_fallback": "NSE_BHAVCOPY",
            "unlock_events": "IPOMARKETS_LOCKIN_TABLE",
        },
        "errors": {
            "security_master": (
                (masters.get("errors") or [])
                + (bse_master.get("errors") or [])
            ),
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

    candidates = [
        item for item in candidates
        if _identity_names_credible(name, item[2])
    ]
    if not candidates:
        return None

    candidates.sort(reverse=True, key=lambda item: item[0])
    score, code, label = candidates[0]

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
        "identity_name": label,
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
    header = payload.get("Header") or {}

    if isinstance(curr, list):
        curr = curr[0] if curr else {}
    if isinstance(header, list):
        header = header[0] if header else {}
    if not isinstance(curr, dict):
        curr = {}
    if not isinstance(header, dict):
        header = {}

    price = _f(
        curr.get("LTP")
        or curr.get("ltp")
        or header.get("LTP")
        or header.get("LastTradedPrice")
        or payload.get("CurrVal")
        or payload.get("LTP")
    )
    if price is None or price <= 0:
        logger.warning(
            "LISTED_BSE_QUOTE_EMPTY code=%r keys=%s",
            code,
            sorted(payload.keys()) if isinstance(payload, dict) else [],
        )
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


def _v0526_listed_identity(record):
    return (
        str(record.get("ipo_type") or "").strip().upper(),
        _name_key(record.get("name")),
    )


def _v0526_record_score(record):
    """Prefer the permanent annual tracker row over a temporary HANDOFF row."""
    score = 0
    tracker_key = str(record.get("tracker_key") or "")
    if not tracker_key.startswith("HANDOFF|"):
        score += 100
    if record.get("detail_url"):
        score += 20
    if record.get("symbol"):
        score += 8
    if record.get("market_symbol"):
        score += 8
    if _f(record.get("listing_price")) is not None:
        score += 10
    if _f(record.get("current_price")) is not None:
        score += 10
    score += min(len(record.get("unlock_events") or []), 10)
    return score


def _v0526_merge_listed_record(primary, other):
    """Merge useful data from a duplicate without changing IPO identity."""
    merged = dict(primary)

    scalar_fields = (
        "symbol",
        "market",
        "market_symbol",
        "bse_code",
        "isin",
        "listing_date",
        "issue_price",
        "listing_price",
        "listing_price_source",
        "listing_return_pct",
        "current_price",
        "current_return_pct",
        "current_price_as_of_ist",
        "current_price_source",
        "detail_url",
        "unlock_checked_at_ist",
    )
    for field in scalar_fields:
        if merged.get(field) in (None, "") and other.get(field) not in (None, ""):
            merged[field] = other.get(field)

    # Prefer the richer static unlock schedule.
    current_events = merged.get("unlock_events") or []
    other_events = other.get("unlock_events") or []
    if len(other_events) > len(current_events):
        merged["unlock_events"] = other_events
        merged["next_unlock"] = (
            other.get("next_unlock")
            or (other_events[0] if other_events else None)
        )
        merged["unlock_status"] = other.get("unlock_status") or "AVAILABLE"
        if other.get("unlock_checked_at_ist"):
            merged["unlock_checked_at_ist"] = other.get(
                "unlock_checked_at_ist"
            )

    # If both rows have current prices, keep the one with the newer timestamp.
    left_ts = str(merged.get("current_price_as_of_ist") or "")
    right_ts = str(other.get("current_price_as_of_ist") or "")
    if (
        _f(other.get("current_price")) is not None
        and right_ts
        and (not left_ts or right_ts > left_ts)
    ):
        merged["current_price"] = other.get("current_price")
        merged["current_return_pct"] = other.get("current_return_pct")
        merged["current_price_as_of_ist"] = other.get(
            "current_price_as_of_ist"
        )
        merged["current_price_source"] = other.get(
            "current_price_source"
        )

    return merged


def _v0530_alias_words(value):
    text = str(value or "").replace("&", " and ")
    words = [
        w for w in re.findall(r"[a-z0-9]+", text.lower())
        if (
            w not in _STOPWORDS
            and w not in {"and", "of"}
        )
    ]
    return words


def _v0530_names_alias(left, right):
    a = _v0530_alias_words(left)
    b = _v0530_alias_words(right)
    if not a or not b:
        return False
    if a == b:
        return True

    # Provider prefixes such as TI Tonbo / TS TNA should not create a second IPO.
    if len(a) > 1 and len(a[0]) <= 2 and a[1:] == b:
        return True
    if len(b) > 1 and len(b[0]) <= 2 and b[1:] == a:
        return True

    # General acronym alias: NSE <-> National Stock Exchange, etc.
    if len(a) == 1 and 2 <= len(a[0]) <= 8 and len(b) >= 2:
        if a[0] == "".join(word[0] for word in b):
            return True
    if len(b) == 1 and 2 <= len(b[0]) <= 8 and len(a) >= 2:
        if b[0] == "".join(word[0] for word in a):
            return True

    ka = "".join(a)
    kb = "".join(b)
    return SequenceMatcher(None, ka, kb).ratio() >= 0.84


def _v0530_same_listed_ipo(left, right):
    if (
        str(left.get("ipo_type") or "").upper()
        != str(right.get("ipo_type") or "").upper()
    ):
        return False

    lsym = str(left.get("market_symbol") or "").upper()
    rsym = str(right.get("market_symbol") or "").upper()
    if lsym and rsym and lsym == rsym:
        return True

    ls = str(left.get("symbol") or "").upper()
    rs = str(right.get("symbol") or "").upper()
    if ls and rs and ls == rs:
        return True

    left_listing = str(left.get("listing_date") or "")
    right_listing = str(right.get("listing_date") or "")
    if (
        left_listing
        and right_listing
        and left_listing != right_listing
    ):
        return False

    lp = _f(left.get("issue_price"))
    rp = _f(right.get("issue_price"))
    if lp is not None and rp is not None and abs(lp - rp) > 0.01:
        return False

    if _v0530_names_alias(left.get("name"), right.get("name")):
        return True

    # Temporary HANDOFF aliases may be shortened by a different upstream feed
    # (e.g. Zetwerk vs Zetwerk Manufacturing Business).
    lhandoff = str(left.get("tracker_key") or "").startswith("HANDOFF|")
    rhandoff = str(right.get("tracker_key") or "").startswith("HANDOFF|")
    if lhandoff or rhandoff:
        aw = _v0530_alias_words(left.get("name"))
        bw = _v0530_alias_words(right.get("name"))
        if (
            aw and bw
            and aw[0] == bw[0]
            and len(aw[0]) >= 5
        ):
            return True

    return False


def _v0526_dedupe_listed_records(records):
    groups = []

    for record in records or []:
        placed = False
        for group in groups:
            if _v0530_same_listed_ipo(record, group[0]):
                group.append(record)
                placed = True
                break
        if not placed:
            groups.append([record])

    result = []
    removed = 0
    duplicate_names = []

    for group in groups:
        if len(group) == 1:
            result.append(group[0])
            continue

        group = sorted(
            group,
            key=_v0526_record_score,
            reverse=True,
        )
        merged = dict(group[0])
        for duplicate in group[1:]:
            merged = _v0526_merge_listed_record(
                merged, duplicate
            )

        result.append(merged)
        removed += len(group) - 1
        duplicate_names.append(merged.get("name"))

    if removed:
        logger.warning(
            "LISTED_DUPLICATES_MERGED_V0530 removed=%s names=%s",
            removed,
            duplicate_names,
        )

    return result, removed


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
        masters = _load_nse_masters()

        if (
            identity
            and not _cached_identity_is_credible(
                record.get("name"),
                identity,
                masters,
            )
        ):
            logger.warning(
                "LISTED_IDENTITY_REJECTED_POST_BUILD name=%r market_symbol=%r",
                record.get("name"),
                identity.get("market_symbol"),
            )
            _clear_market_identity_cache(
                item,
                reason="NAME_IDENTITY_MISMATCH_POST_BUILD",
            )
            identity = {}
            record["symbol"] = None
            record["market"] = None
            record["market_symbol"] = None
            record["current_price"] = None
            record["current_return_pct"] = None
            record["current_price_as_of_ist"] = None
            record["current_price_source"] = None

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
            record["bse_code"] = (
                identity.get("bse_code")
                or record.get("bse_code")
            )
            record["isin"] = (
                identity.get("isin")
                or record.get("isin")
            )

        # During deep refresh, prefer a direct BSE quote for BSE securities.
        if (
            deep_refresh
            and record.get("current_price") is None
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

    records, duplicate_rows_removed = _v0526_dedupe_listed_records(
        payload.get("records") or []
    )
    payload["records"] = records
    summary = payload.setdefault("summary", {})
    summary["listed_records"] = len(records)
    summary["duplicate_rows_removed"] = duplicate_rows_removed
    summary["market_identity_resolved"] = sum(
        1 for r in records
        if r.get("market") and r.get("symbol")
    )
    summary["current_price_available"] = sum(
        1 for r in records
        if _f(r.get("current_price")) is not None
    )
    summary["bse_identities_added"] = bse_resolved
    summary["bse_identity_resolved_total"] = sum(
        1 for r in records
        if r.get("market") == "BSE"
    )
    summary["bse_direct_quotes"] = bse_quotes
    summary["listing_price_market_fallbacks"] = listing_fallbacks
    summary["current_price_missing"] = sum(
        1 for r in records
        if _f(r.get("current_price")) is None
    )
    summary["current_price_missing_due_identity"] = sum(
        1 for r in records
        if (
            _f(r.get("current_price")) is None
            and not (r.get("market") and r.get("symbol"))
        )
    )
    summary["current_price_missing_after_identity"] = sum(
        1 for r in records
        if (
            _f(r.get("current_price")) is None
            and r.get("market")
            and r.get("symbol")
        )
    )

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

# ---------------------------------------------------------------------------
# v0.5.32: static lot-size enrichment and per-lot gain calculations.
# ---------------------------------------------------------------------------
_build_listed_payload_v0531 = build_listed_payload


def _v0532_parse_application(detail_url, issue_price=None):
    """Return the minimum retail/individual IPO application.

    Mainboard pages normally label this row "Retail · min".
    SME pages normally label it "Individual · min".

    `application_shares` is therefore the actual minimum application quantity,
    not the exchange lot size. For example, if an SME minimum is 2 lots of
    1,600 shares, application_shares is 3,200.
    """
    body, _, _ = _fetch(
        detail_url,
        "LISTED_APPLICATION_DETAIL",
        timeout=15,
    )
    parser = RichTableParser()
    parser.feed(body)

    candidates = []

    for table in parser.tables:
        if not table:
            continue

        headers = [
            re.sub(
                r"[^a-z0-9]",
                "",
                str(c.get("text") or "").lower(),
            )
            for c in table[0]
        ]
        if "lots" not in headers or "shares" not in headers:
            continue

        lots_i = headers.index("lots")
        shares_i = headers.index("shares")
        amount_i = headers.index("amount") if "amount" in headers else None

        # Usually "Who" or "Application"; if not, use the first non-numeric
        # descriptive column.
        label_i = None
        for candidate_header in ("who", "application", "applicant"):
            if candidate_header in headers:
                label_i = headers.index(candidate_header)
                break
        if label_i is None:
            for idx in range(len(headers)):
                if idx not in {lots_i, shares_i, amount_i}:
                    label_i = idx
                    break

        for row in table[1:]:
            if max(lots_i, shares_i) >= len(row):
                continue

            label = (
                " ".join(str(row[label_i].get("text") or "").split())
                if label_i is not None and label_i < len(row)
                else ""
            )
            label_key = re.sub(r"[^a-z0-9]", "", label.lower())

            lots = _f(row[lots_i].get("text"))
            shares = _f(row[shares_i].get("text"))
            amount = (
                _f(row[amount_i].get("text"))
                if amount_i is not None and amount_i < len(row)
                else None
            )

            if (
                lots is None
                or shares is None
                or lots <= 0
                or shares <= 0
            ):
                continue

            is_min = "min" in label_key
            is_retail = "retail" in label_key
            is_individual = "individual" in label_key

            candidates.append({
                "label": label or None,
                "lots": int(round(lots)),
                "shares": int(round(shares)),
                "amount": amount,
                "preferred": bool(
                    is_min and (is_retail or is_individual)
                ),
            })

        if candidates:
            break

    if not candidates:
        return None

    preferred = [c for c in candidates if c["preferred"]]
    pool = preferred or candidates

    # The minimum public application has the smallest positive share count.
    chosen = min(
        pool,
        key=lambda c: (
            c.get("shares") or 10**18,
            c.get("amount") or 10**18,
        ),
    )

    shares = int(chosen["shares"])
    amount = _f(chosen.get("amount"))
    price = _f(issue_price)

    # For listed IPOs issue_price is final. Prefer a deterministic recompute
    # so the displayed application amount exactly matches issue price × shares.
    if price is not None and price > 0:
        amount = round(price * shares, 2)

    return {
        "application_shares": shares,
        "application_amount": amount,
        "application_lots": chosen.get("lots"),
        "application_tier": chosen.get("label"),
        "application_source": "IPOMARKETS_MIN_APPLICATION_TABLE",
    }


def build_listed_payload(*args, **kwargs):
    payload = _build_listed_payload_v0531(*args, **kwargs)
    deep_refresh = bool(kwargs.get("deep_refresh", False))
    persist = bool(kwargs.get("persist", False))

    state = _load_state()
    cache = state.setdefault("records", {})
    records = payload.get("records") or []

    fetched = 0
    max_application_fetches = 40

    # Newest listings are already sorted first by the base builder.
    if deep_refresh:
        for record in records:
            if fetched >= max_application_fetches:
                break

            key = str(record.get("tracker_key") or "")
            item = cache.setdefault(key, {})
            if item.get("application_shares") is not None:
                continue

            detail_url = record.get("detail_url")
            if not detail_url:
                continue

            try:
                application = _v0532_parse_application(
                    detail_url,
                    issue_price=record.get("issue_price"),
                )
                if application:
                    item.update(application)
                    logger.info(
                        "LISTED_APPLICATION_REFRESH "
                        "name=%r shares=%s amount=%s lots=%s tier=%r",
                        record.get("name"),
                        application.get("application_shares"),
                        application.get("application_amount"),
                        application.get("application_lots"),
                        application.get("application_tier"),
                    )
                else:
                    logger.warning(
                        "LISTED_APPLICATION_NOT_FOUND name=%r url=%r",
                        record.get("name"),
                        detail_url,
                    )
            except Exception as exc:
                logger.warning(
                    "LISTED_APPLICATION_FAILED name=%r url=%r error=%r",
                    record.get("name"),
                    detail_url,
                    str(exc),
                )

            fetched += 1
            time.sleep(0.05)

    application_available = 0
    for record in records:
        key = str(record.get("tracker_key") or "")
        item = cache.get(key) or {}

        application_shares = _f(item.get("application_shares"))
        if application_shares is not None and application_shares > 0:
            application_shares = int(round(application_shares))
            application_available += 1
        else:
            application_shares = None

        issue = _f(record.get("issue_price"))
        listing = _f(record.get("listing_price"))
        current = _f(record.get("current_price"))

        application_amount = _f(item.get("application_amount"))
        if application_shares is not None and issue is not None:
            application_amount = round(
                application_shares * issue,
                2,
            )

        record["application_shares"] = application_shares
        record["application_amount"] = application_amount
        record["application_lots"] = item.get("application_lots")
        record["application_tier"] = item.get("application_tier")
        record["application_source"] = item.get("application_source")

        record["listing_gain_loss"] = (
            round(
                (listing - issue) * application_shares,
                2,
            )
            if (
                application_shares is not None
                and issue is not None
                and listing is not None
            )
            else None
        )
        record["current_gain_loss"] = (
            round(
                (current - issue) * application_shares,
                2,
            )
            if (
                application_shares is not None
                and issue is not None
                and current is not None
            )
            else None
        )

        # Remove obsolete public per-lot fields from the output. Existing
        # cached lot_size may remain internally but is no longer user-facing.
        record.pop("lot_size", None)
        record.pop("lot_size_source", None)
        record.pop("listing_gain_per_lot", None)
        record.pop("current_gain_per_lot", None)

    summary = payload.setdefault("summary", {})
    summary.pop("lot_size_available", None)
    summary["application_size_available"] = application_available

    sources = payload.setdefault("sources", {})
    sources.pop("lot_size", None)
    sources["application_size"] = "IPOMARKETS_MIN_APPLICATION_TABLE"

    if persist:
        state["records"] = cache
        _save_state(state)

    logger.info(
        "LISTED_APPLICATION_ENRICH available=%s/%s fetched=%s",
        application_available,
        len(records),
        fetched,
    )
    return payload
