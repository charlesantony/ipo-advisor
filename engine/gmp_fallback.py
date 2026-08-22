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
        return {
            "gmp_value": value,
            "gmp_gain_pct": _gain_from_gmp(value, price_high, row),
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
        return {
            "gmp_value": value,
            "gmp_gain_pct": _gain_from_gmp(value, price_high, row),
            "gmp_date": row[0],
            "source": url,
            "source_kind": "IPOWATCH_DETAIL_PAGE",
            "row": row,
        }
    return None


def validate_or_fill_gmp(record):
    """Validate current GMP and fill it from IPOWatch when FinAPI omitted it."""
    item = dict(record or {})
    existing = item.get("gmp_gain_pct")

    if existing is not None:
        item["gmp_validation"] = {
            "complete": True,
            "status": "PROVIDER",
            "source": item.get("gmp_source"),
            "provider_gmp_missing": False,
        }
        return item

    validation = {
        "complete": False,
        "status": "INCOMPLETE",
        "provider_gmp_missing": True,
        "attempted_sources": [],
        "errors": [],
    }
    candidate = None
    successful_fetch = False

    try:
        validation["attempted_sources"].append(IPOWATCH_DASHBOARD)
        candidate = _from_dashboard(
            item.get("name"), item.get("price_high")
        )
        successful_fetch = True
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
        except Exception as exc:
            validation["errors"].append(
                f"detail: {type(exc).__name__}: {exc}"
            )

    if candidate is not None:
        item["gmp_value"] = candidate["gmp_value"]
        item["gmp_gain_pct"] = candidate["gmp_gain_pct"]
        item["gmp_date"] = candidate["gmp_date"]
        item["gmp_source"] = candidate["source"]
        item["gmp_fallback_source"] = candidate["source_kind"]

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
        logger.warning(
            "GMP_FALLBACK_INCOMPLETE name=%r errors=%r",
            item.get("name"),
            validation["errors"],
        )

    item["gmp_validation"] = validation
    return item
