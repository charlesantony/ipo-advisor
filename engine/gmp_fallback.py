import re
import urllib.parse
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from logging_utils import logger

IST = ZoneInfo("Asia/Kolkata")
IPOWATCH_DASHBOARD = (
    "https://ipowatch.in/ipo-grey-market-premium-latest-ipo-gmp/"
)

_DASHBOARD_ROWS = None
_DASHBOARD_ERROR = None


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("td", "th") and self._cell is not None:
            text = " ".join("".join(self._cell).split())
            self._row.append(text)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
            self._cell = None


def _canon(value):
    text = str(value or "").lower()
    text = re.sub(r"\b(india|limited|ltd|ipo|mainboard|sme)\b", " ", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def _same_company(a, b):
    ca, cb = _canon(a), _canon(b)
    if not ca or not cb:
        return False
    if ca == cb:
        return True
    shorter, longer = sorted((ca, cb), key=len)
    return len(shorter) >= 8 and shorter in longer


def _is_zero(value):
    try:
        return abs(float(value)) < 1e-12
    except (TypeError, ValueError):
        return False


def _gmp_state(value, gain_pct):
    if value is None and gain_pct is None:
        return "NOT_AVAILABLE"
    if (
        (_is_zero(value) and (gain_pct is None or _is_zero(gain_pct)))
        or (_is_zero(gain_pct) and (value is None or _is_zero(value)))
    ):
        return "UNVERIFIED_ZERO"
    return "OBSERVED"


def _money(value):
    text = str(value or "").replace(",", "")
    m = re.search(
        r"(?:₹|rs\.?|inr)\s*([+-]?[0-9]+(?:\.[0-9]+)?)",
        text,
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _percent_from_row(row):
    for cell in row:
        m = re.search(r"([+-]?[0-9]+(?:\.[0-9]+)?)\s*%", str(cell))
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return None


def _gain_from_gmp(gmp_value, price_high, row):
    try:
        price = float(price_high)
    except (TypeError, ValueError):
        price = None
    if price and price > 0:
        return round(float(gmp_value) * 100.0 / price, 2)
    pct = _percent_from_row(row)
    return round(pct, 2) if pct is not None else None


def _allowed_source(url):
    try:
        parsed = urllib.parse.urlparse(str(url or ""))
    except Exception:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname in {"ipowatch.in", "www.ipowatch.in"}
    )


def _fetch_rows(url, timeout=8):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 Chrome/124 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    parser = _TableParser()
    parser.feed(body)
    return parser.rows


def _dashboard_rows():
    global _DASHBOARD_ROWS, _DASHBOARD_ERROR
    if _DASHBOARD_ROWS is not None:
        return _DASHBOARD_ROWS
    if _DASHBOARD_ERROR is not None:
        raise RuntimeError(_DASHBOARD_ERROR)
    try:
        _DASHBOARD_ROWS = _fetch_rows(IPOWATCH_DASHBOARD)
        return _DASHBOARD_ROWS
    except Exception as exc:
        _DASHBOARD_ERROR = str(exc)
        raise


def _from_dashboard(name, price_high):
    rows = _dashboard_rows()
    for row in rows:
        if len(row) < 2 or not _same_company(row[0], name):
            continue
        value = _money(row[1])
        if value is None:
            return None
        gain_pct = _gain_from_gmp(value, price_high, row)
        return {
            "gmp_value": value,
            "gmp_gain_pct": gain_pct,
            "gmp_state": _gmp_state(value, gain_pct),
            "gmp_date": datetime.now(IST).strftime("%d %B"),
            "source": IPOWATCH_DASHBOARD,
            "source_kind": "IPOWATCH_LATEST_DASHBOARD",
            "row": row,
        }
    return None


def _looks_like_date(value):
    text = str(value or "").lower()
    return bool(
        re.search(
            r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
            r"(?:uary|ruary|ch|il|e|y|ust|tember|ober|ember)?\b",
            text,
        )
        or re.match(r"^\s*\d{1,2}\b", text)
    )


def _from_detail_page(url, price_high):
    rows = _fetch_rows(url)
    for row in rows:
        if len(row) < 2 or not _looks_like_date(row[0]):
            continue
        value = _money(row[1])
        if value is None:
            continue
        gain_pct = _gain_from_gmp(value, price_high, row)
        return {
            "gmp_value": value,
            "gmp_gain_pct": gain_pct,
            "gmp_state": _gmp_state(value, gain_pct),
            "gmp_date": row[0],
            "source": url,
            "source_kind": "IPOWATCH_DETAIL_PAGE",
            "row": row,
        }
    return None


def _parse_ist(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def _age_minutes(observed_at_ist, now_ist):
    observed = _parse_ist(observed_at_ist)
    current = _parse_ist(now_ist) or datetime.now(IST)
    if observed is None:
        return None
    return max(
        0.0,
        round((current - observed).total_seconds() / 60.0, 1),
    )


def _previous_gmp_candidate(previous, now_ist, max_age_minutes):
    if not previous:
        return None

    gain_pct = previous.get("gmp_gain_pct")
    value = previous.get("gmp_value")
    if (
        gain_pct is None
        or _gmp_state(value, gain_pct) != "OBSERVED"
    ):
        return None

    observed = (
        previous.get("fetched_at_ist")
        or previous.get("created_at_ist")
    )
    age = _age_minutes(observed, now_ist)
    if age is None or age > float(max_age_minutes):
        return None

    return {
        "gmp_value": value,
        "gmp_gain_pct": gain_pct,
        "gmp_date": previous.get("gmp_date"),
        "observed_at_ist": observed,
        "age_minutes": age,
        "source_kind": "LOCAL_LAST_KNOWN_GMP",
    }


def validate_or_fill_gmp(
    record,
    previous=None,
    now_ist=None,
    max_carry_age_minutes=120,
):
    """Validate current GMP, using a recent prior GMP only on fetch failure."""
    item = dict(record or {})
    existing = item.get("gmp_gain_pct")
    existing_value = item.get("gmp_value")
    provider_zero_unverified = (
        existing is not None
        and _gmp_state(existing_value, existing) == "UNVERIFIED_ZERO"
    )

    if existing is not None and not provider_zero_unverified:
        item["gmp_status"] = "VERIFIED"
        item["gmp_validation"] = {
            "complete": True,
            "status": "PROVIDER",
            "source": item.get("gmp_source"),
            "provider_gmp_missing": False,
        }
        return item

    if provider_zero_unverified:
        item["gmp_raw_value"] = existing_value
        item["gmp_raw_gain_pct"] = existing
        item["gmp_value"] = None
        item["gmp_gain_pct"] = None

    validation = {
        "complete": False,
        "status": "INCOMPLETE",
        "provider_gmp_missing": True,
        "attempted_sources": [],
        "errors": [],
    }
    candidate = None
    zero_candidate = None
    successful_fetch = False

    try:
        validation["attempted_sources"].append(IPOWATCH_DASHBOARD)
        candidate = _from_dashboard(
            item.get("name"), item.get("price_high")
        )
        successful_fetch = True
        if candidate and candidate.get("gmp_state") == "UNVERIFIED_ZERO":
            zero_candidate = candidate
            candidate = None
    except Exception as exc:
        validation["errors"].append(
            f"dashboard: {type(exc).__name__}: {exc}"
        )

    source = item.get("gmp_source")
    if candidate is None and _allowed_source(source):
        try:
            validation["attempted_sources"].append(source)
            candidate = _from_detail_page(
                source, item.get("price_high")
            )
            successful_fetch = True
            if candidate and candidate.get("gmp_state") == "UNVERIFIED_ZERO":
                zero_candidate = candidate
                candidate = None
        except Exception as exc:
            validation["errors"].append(
                f"detail: {type(exc).__name__}: {exc}"
            )

    if candidate is None and zero_candidate is not None:
        candidate = zero_candidate

    if candidate is not None and candidate.get("gmp_state") == "UNVERIFIED_ZERO":
        item["gmp_raw_value"] = candidate["gmp_value"]
        item["gmp_raw_gain_pct"] = candidate["gmp_gain_pct"]
        item["gmp_value"] = None
        item["gmp_gain_pct"] = None
        item["gmp_status"] = "NOT_AVAILABLE"
        validation.update(
            {
                "complete": True,
                "status": "ZERO_UNVERIFIED",
                "source": candidate["source"],
                "source_kind": candidate["source_kind"],
                "raw_gmp_value": candidate["gmp_value"],
                "raw_gmp_gain_pct": candidate["gmp_gain_pct"],
            }
        )
        logger.warning(
            "GMP_ZERO_TREATED_AS_UNAVAILABLE "
            "name=%r raw_value=%s raw_gain_pct=%s source=%s",
            item.get("name"),
            candidate["gmp_value"],
            candidate["gmp_gain_pct"],
            candidate["source_kind"],
        )
    elif candidate is not None:
        item["gmp_value"] = candidate["gmp_value"]
        item["gmp_gain_pct"] = candidate["gmp_gain_pct"]
        item["gmp_date"] = candidate["gmp_date"]
        item["gmp_source"] = candidate["source"]
        item["gmp_fallback_source"] = candidate["source_kind"]
        item["gmp_status"] = "VERIFIED"

        trends = list(item.get("gmp_trends") or [])
        trends.insert(
            0,
            {
                "date": candidate["gmp_date"],
                "gmp": f"₹{candidate['gmp_value']:g}",
                "gain": (
                    f"{candidate['gmp_gain_pct']:.2f}%"
                    if candidate["gmp_gain_pct"] is not None
                    else None
                ),
                "source": candidate["source_kind"],
            },
        )
        item["gmp_trends"] = trends
        validation.update(
            {
                "complete": True,
                "status": "FALLBACK",
                "source": candidate["source"],
                "source_kind": candidate["source_kind"],
                "gmp_value": candidate["gmp_value"],
                "gmp_gain_pct": candidate["gmp_gain_pct"],
            }
        )
        logger.info(
            "GMP_FALLBACK_SUCCESS name=%r value=%s gain_pct=%s source=%s",
            item.get("name"),
            candidate["gmp_value"],
            candidate["gmp_gain_pct"],
            candidate["source_kind"],
        )
    elif successful_fetch:
        item["gmp_value"] = None
        item["gmp_gain_pct"] = None
        item["gmp_status"] = "NOT_AVAILABLE"
        validation.update(
            {
                "complete": True,
                "status": "VERIFIED_ABSENT",
                "source": IPOWATCH_DASHBOARD,
            }
        )
        logger.info(
            "GMP_FALLBACK_VERIFIED_ABSENT name=%r",
            item.get("name"),
        )
    else:
        carried = _previous_gmp_candidate(
            previous,
            now_ist,
            max_carry_age_minutes,
        )

        if carried is not None:
            item["gmp_value"] = carried["gmp_value"]
            item["gmp_gain_pct"] = carried["gmp_gain_pct"]
            item["gmp_date"] = carried.get("gmp_date")
            item["gmp_source"] = carried["source_kind"]
            item["gmp_fallback_source"] = carried["source_kind"]
            item["gmp_status"] = "CARRIED_FORWARD"

            trends = list(item.get("gmp_trends") or [])
            trends.insert(
                0,
                {
                    "date": carried.get("gmp_date"),
                    "gmp": (
                        f"₹{carried['gmp_value']:g}"
                        if carried.get("gmp_value") is not None
                        else None
                    ),
                    "gain": f"{float(carried['gmp_gain_pct']):.2f}%",
                    "source": carried["source_kind"],
                },
            )
            item["gmp_trends"] = trends

            validation.update(
                {
                    "complete": True,
                    "status": "CARRIED_FORWARD",
                    "source": carried["source_kind"],
                    "source_kind": carried["source_kind"],
                    "observed_at_ist": carried["observed_at_ist"],
                    "age_minutes": carried["age_minutes"],
                    "gmp_value": carried["gmp_value"],
                    "gmp_gain_pct": carried["gmp_gain_pct"],
                    "fallback_errors": list(validation["errors"]),
                    "max_carry_age_minutes": max_carry_age_minutes,
                }
            )
            logger.warning(
                "GMP_CARRY_FORWARD name=%r value=%s gain_pct=%s "
                "observed_at=%s age_minutes=%s fallback_errors=%r",
                item.get("name"),
                carried["gmp_value"],
                carried["gmp_gain_pct"],
                carried["observed_at_ist"],
                carried["age_minutes"],
                validation["errors"],
            )
        else:
            item["gmp_value"] = None
            item["gmp_gain_pct"] = None
            item["gmp_status"] = "FETCH_INCOMPLETE"
            logger.warning(
                "GMP_FALLBACK_INCOMPLETE name=%r errors=%r",
                item.get("name"),
                validation["errors"],
            )

    item["gmp_validation"] = validation
    return item
