import json
import re
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from zoneinfo import ZoneInfo

from logging_utils import logger

IST = ZoneInfo("Asia/Kolkata")

GROWW_SUBSCRIPTION_URL = "https://groww.in/ipo/subscription"
NSE_CURRENT_ISSUE_URL = "https://www.nseindia.com/api/ipo-current-issue"
NSE_PRIME_URL = "https://www.nseindia.com/option-chain"

MAX_CANONICAL_SUBSCRIPTION_AGE_MINUTES = 45

_GROWW_ROWS = None
_GROWW_ERROR = None
_NSE_ROWS = None
_NSE_ERROR = None


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
    text = re.sub(
        r"\b(india|limited|ltd|ipo|mainboard|main board|sme)\b",
        " ",
        text,
    )
    return re.sub(r"[^a-z0-9]+", "", text)


def _same_company(a, b):
    ca, cb = _canon(a), _canon(b)
    if not ca or not cb:
        return False
    if ca == cb:
        return True
    shorter, longer = sorted((ca, cb), key=len)
    return len(shorter) >= 8 and shorter in longer


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if text in {"", "-", "--", "—", "NA", "N/A", "null", "None"}:
        return None
    m = re.search(
        r"[-+]?[0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?",
        text,
    )
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _parse_iso(value):
    try:
        return datetime.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        return None


def _age_minutes(observed_at_ist, now_ist):
    observed = _parse_iso(observed_at_ist)
    if observed is None:
        return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=IST)
    now = now_ist if now_ist.tzinfo else now_ist.replace(tzinfo=IST)
    return round(max(0.0, (now - observed).total_seconds() / 60.0), 1)


def _http_text(url, timeout=8):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 Chrome/124 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.8",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _groww_rows():
    global _GROWW_ROWS, _GROWW_ERROR
    if _GROWW_ROWS is not None:
        return _GROWW_ROWS
    if _GROWW_ERROR is not None:
        raise RuntimeError(_GROWW_ERROR)

    try:
        body = _http_text(GROWW_SUBSCRIPTION_URL)
        parser = _TableParser()
        parser.feed(body)
        _GROWW_ROWS = parser.rows
        if not _GROWW_ROWS:
            raise RuntimeError("Groww subscription page contained no table rows")
        return _GROWW_ROWS
    except Exception as exc:
        _GROWW_ERROR = str(exc)
        raise


def _header_map(rows):
    for row in rows:
        cells = [str(x or "").strip().lower() for x in row]
        if not cells:
            continue
        if (
            any("company" in x and "name" in x for x in cells)
            and any(x == "total" or "total" in x for x in cells)
        ):
            out = {}
            for idx, cell in enumerate(cells):
                if "company" in cell and "name" in cell:
                    out["name"] = idx
                elif cell == "qib" or "qib" in cell:
                    out["qib_x"] = idx
                elif cell == "nii" or "nii" in cell:
                    out["nii_x"] = idx
                elif cell in {"retail", "rii"} or "retail" in cell:
                    out["retail_x"] = idx
                elif cell == "total" or "total" in cell:
                    out["total_x"] = idx
            if "name" in out and "total_x" in out:
                return out
    return None


def _groww_candidate(name):
    rows = _groww_rows()
    mapping = _header_map(rows)
    mapping = mapping or {
        "name": 0,
        "qib_x": 5,
        "nii_x": 6,
        "retail_x": 7,
        "total_x": 9,
    }

    for row in rows:
        name_idx = mapping.get("name", 0)
        if name_idx >= len(row) or not _same_company(row[name_idx], name):
            continue

        values = {}
        for field in ("qib_x", "nii_x", "retail_x", "total_x"):
            idx = mapping.get(field)
            values[field] = (
                _num(row[idx])
                if idx is not None and idx < len(row)
                else None
            )

        if values["total_x"] is None:
            continue

        return {
            **values,
            "source": GROWW_SUBSCRIPTION_URL,
            "source_kind": "GROWW_CONSOLIDATED_SUBSCRIPTION",
            "row": row,
        }
    return None


def _nse_json_rows():
    global _NSE_ROWS, _NSE_ERROR
    if _NSE_ROWS is not None:
        return _NSE_ROWS
    if _NSE_ERROR is not None:
        raise RuntimeError(_NSE_ERROR)

    jar = CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar)
    )
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) "
            "Gecko/20100101 Firefox/118.0"
        ),
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://www.nseindia.com/market-data/all-upcoming-issues-ipo",
    }

    try:
        try:
            opener.open(
                urllib.request.Request(
                    NSE_PRIME_URL,
                    headers={**headers, "Accept": "text/html,*/*"},
                ),
                timeout=6,
            ).read(1)
        except Exception:
            pass

        req = urllib.request.Request(
            NSE_CURRENT_ISSUE_URL,
            headers=headers,
            method="GET",
        )
        with opener.open(req, timeout=8) as resp:
            payload = json.loads(
                resp.read().decode("utf-8", errors="replace")
            )

        if not isinstance(payload, list):
            raise RuntimeError(
                f"unexpected NSE IPO payload type: {type(payload).__name__}"
            )
        _NSE_ROWS = payload
        return _NSE_ROWS
    except Exception as exc:
        _NSE_ERROR = str(exc)
        raise


def _nse_candidate(record):
    symbol = str(record.get("symbol") or "").strip().upper()
    name = record.get("name")
    rows = _nse_json_rows()

    for row in rows:
        if not isinstance(row, dict):
            continue
        row_symbol = str(row.get("symbol") or "").strip().upper()
        company = row.get("companyName")
        matched = bool(symbol and row_symbol == symbol)
        if not matched:
            matched = _same_company(company, name)
        if not matched:
            continue

        total = _num(row.get("noOfTime"))
        if total is None:
            continue

        # NSE can expose noOfTime=0 before usable bid data has populated.
        # Do not treat that placeholder as verified zero demand.
        if total <= 0:
            return {
                "qib_x": None,
                "nii_x": None,
                "retail_x": None,
                "total_x": None,
                "source": NSE_CURRENT_ISSUE_URL,
                "source_kind": "NSE_CURRENT_ISSUE_TOTAL",
                "zero_not_ready": True,
                "raw_total_x": total,
                "row": row,
            }

        return {
            "qib_x": None,
            "nii_x": None,
            "retail_x": None,
            "total_x": total,
            "source": NSE_CURRENT_ISSUE_URL,
            "source_kind": "NSE_CURRENT_ISSUE_TOTAL",
            "zero_not_ready": False,
            "row": row,
        }
    return None


def _copy_missing_subscription_fields(item, candidate):
    changed = []
    for field in ("qib_x", "nii_x", "retail_x", "total_x"):
        if item.get(field) is None and candidate.get(field) is not None:
            item[field] = candidate[field]
            changed.append(field)
    return changed


def _previous_candidate(previous):
    if not previous or previous.get("total_x") is None:
        return None
    return {
        "qib_x": _num(previous.get("qib_x")),
        "nii_x": _num(previous.get("nii_x")),
        "retail_x": _num(previous.get("retail_x")),
        "total_x": _num(previous.get("total_x")),
        "source": "LOCAL_SNAPSHOT_DB",
        "source_kind": "LAST_KNOWN_VALID_SNAPSHOT",
        "observed_at_ist": previous.get("fetched_at_ist"),
    }


def validate_or_fill_subscription(record, previous=None, now_ist=None):
    """Repair missing subscription data and preserve the newest valid evidence."""
    item = dict(record or {})
    now = now_ist or datetime.now(IST)
    validation = {
        "complete": False,
        "status": "INCOMPLETE",
        "source": None,
        "source_kind": None,
        "observed_at_ist": None,
        "age_minutes": None,
        "provider_subscription_missing": item.get("total_x") is None,
        "attempted_sources": [],
        "errors": [],
        "field_sources": {},
    }

    if item.get("total_x") is not None:
        observed = now.isoformat()
        validation.update(
            {
                "complete": True,
                "status": "PROVIDER",
                "source": "FINAPI",
                "source_kind": "FINAPI_CURRENT_SUBSCRIPTION",
                "observed_at_ist": observed,
                "age_minutes": 0.0,
            }
        )
        for field in ("qib_x", "nii_x", "retail_x", "total_x"):
            if item.get(field) is not None:
                validation["field_sources"][field] = "FINAPI"
        item["subscription_source"] = "FINAPI"
        item["subscription_observed_at_ist"] = observed
        item["subscription_age_minutes"] = 0.0
        item["subscription_validation"] = validation
        return item

    direct_candidate = None

    try:
        validation["attempted_sources"].append(GROWW_SUBSCRIPTION_URL)
        direct_candidate = _groww_candidate(item.get("name"))
    except Exception as exc:
        validation["errors"].append(
            f"groww: {type(exc).__name__}: {exc}"
        )

    if direct_candidate is None:
        try:
            validation["attempted_sources"].append(NSE_CURRENT_ISSUE_URL)
            direct_candidate = _nse_candidate(item)
        except Exception as exc:
            validation["errors"].append(
                f"nse: {type(exc).__name__}: {exc}"
            )

    if direct_candidate is not None and direct_candidate.get("zero_not_ready"):
        validation.update(
            {
                "complete": False,
                "status": "ZERO_NOT_READY",
                "source": direct_candidate["source"],
                "source_kind": direct_candidate["source_kind"],
                "observed_at_ist": now.isoformat(),
                "age_minutes": 0.0,
                "raw_total_x": direct_candidate.get("raw_total_x"),
            }
        )
        logger.info(
            "SUBSCRIPTION_NSE_ZERO_NOT_READY "
            "name=%r raw_total_x=%s",
            item.get("name"),
            direct_candidate.get("raw_total_x"),
        )
        direct_candidate = None

    if direct_candidate is not None:
        changed = _copy_missing_subscription_fields(item, direct_candidate)
        observed = now.isoformat()
        validation.update(
            {
                "complete": item.get("total_x") is not None,
                "status": "FALLBACK",
                "source": direct_candidate["source"],
                "source_kind": direct_candidate["source_kind"],
                "observed_at_ist": observed,
                "age_minutes": 0.0,
            }
        )
        for field in changed:
            validation["field_sources"][field] = direct_candidate["source_kind"]

        item["subscription_source"] = direct_candidate["source_kind"]
        item["subscription_observed_at_ist"] = observed
        item["subscription_age_minutes"] = 0.0
        item["subscription_validation"] = validation

        logger.info(
            "SUBSCRIPTION_FALLBACK_SUCCESS "
            "name=%r source=%s qib_x=%s nii_x=%s retail_x=%s total_x=%s",
            item.get("name"),
            direct_candidate["source_kind"],
            item.get("qib_x"),
            item.get("nii_x"),
            item.get("retail_x"),
            item.get("total_x"),
        )
        return item

    carried = _previous_candidate(previous)
    if carried is not None:
        changed = _copy_missing_subscription_fields(item, carried)
        observed = carried.get("observed_at_ist")
        age = _age_minutes(observed, now)
        validation.update(
            {
                "complete": item.get("total_x") is not None,
                "status": "CARRIED_FORWARD",
                "source": carried["source"],
                "source_kind": carried["source_kind"],
                "observed_at_ist": observed,
                "age_minutes": age,
            }
        )
        for field in changed:
            validation["field_sources"][field] = carried["source_kind"]

        item["subscription_source"] = carried["source_kind"]
        item["subscription_observed_at_ist"] = observed
        item["subscription_age_minutes"] = age
        item["subscription_validation"] = validation

        logger.warning(
            "SUBSCRIPTION_CARRY_FORWARD "
            "name=%r total_x=%s observed_at=%s age_minutes=%s "
            "fallback_errors=%r",
            item.get("name"),
            item.get("total_x"),
            observed,
            age,
            validation["errors"],
        )
        return item

    item["subscription_validation"] = validation
    if validation.get("status") == "ZERO_NOT_READY":
        logger.warning(
            "SUBSCRIPTION_FALLBACK_WAITING_FOR_BIDS "
            "name=%r source=%s raw_total_x=%s",
            item.get("name"),
            validation.get("source_kind"),
            validation.get("raw_total_x"),
        )
    else:
        logger.warning(
            "SUBSCRIPTION_FALLBACK_INCOMPLETE name=%r errors=%r",
            item.get("name"),
            validation["errors"],
        )
    return item


def enforce_canonical_subscription_freshness(
    record,
    recommendation,
    ist,
    max_age_minutes=MAX_CANONICAL_SUBSCRIPTION_AGE_MINUTES,
    force_checkpoint=False,
):
    """Block a closing-day checkpoint when subscription evidence is too stale."""
    rec = dict(recommendation or {})
    if not record.get("is_closing_today"):
        return rec

    minute = ist.hour * 60 + ist.minute
    if (
        not force_checkpoint
        and not (14 * 60 + 28 <= minute <= 14 * 60 + 32)
    ):
        return rec

    validation = record.get("subscription_validation") or {}
    preds = rec.get("predictions") or {}
    total_x = preds.get("total_subscription_x")
    age = validation.get("age_minutes")
    complete = bool(validation.get("complete"))

    fresh = (
        complete
        and total_x is not None
        and age is not None
        and float(age) <= float(max_age_minutes)
    )

    if fresh:
        quality = dict(rec.get("data_quality") or {})
        quality.update(
            {
                "subscription_fresh_for_canonical": True,
                "subscription_age_minutes": age,
                "subscription_source": validation.get("source_kind"),
                "canonical_max_age_minutes": max_age_minutes,
            }
        )
        rec["data_quality"] = quality
        return rec

    raw_action = rec.get("action")
    raw_confidence = rec.get("research_confidence")
    raw_primary = rec.get("primary_prediction_pct")

    rec["action"] = "NOT READY"
    rec["action_priority"] = 0
    rec["research_confidence"] = "LOW"
    rec["primary_prediction_pct"] = None
    rec["ranking_score"] = -999.0
    rec["signal_conflict"] = False
    rec["reason"] = [
        "Fresh subscription data was not available for the exact 2:30 PM IST checkpoint.",
        (
            f"Canonical capture requires subscription evidence no older than "
            f"{max_age_minutes} minutes."
        ),
    ]
    rec["finality"] = {
        "canonical": False,
        "code": "CANONICAL_1430_DATA_NOT_READY",
        "label": (
            "2:30 PM checkpoint not captured — fresh subscription "
            "data unavailable"
        ),
    }
    rec["data_quality"] = {
        "subscription_fresh_for_canonical": False,
        "subscription_age_minutes": age,
        "subscription_source": validation.get("source_kind"),
        "canonical_max_age_minutes": max_age_minutes,
        "raw_model_action": raw_action,
        "raw_model_confidence": raw_confidence,
        "raw_primary_prediction_pct": raw_primary,
    }

    logger.warning(
        "CANONICAL_1430_BLOCKED_STALE_SUBSCRIPTION "
        "name=%r source=%s age_minutes=%s max_age=%s raw_action=%s",
        record.get("name"),
        validation.get("source_kind"),
        age,
        max_age_minutes,
        raw_action,
    )
    return rec
