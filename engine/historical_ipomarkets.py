import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo
from html.parser import HTMLParser

from logging_utils import logger, save_raw_response

BASE = "https://ipomarkets.com"
DEFAULT_YEAR = 2025

def _canon(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def _num(s):
    if s is None:
        return None
    s = str(s).strip().replace(",", "").replace("−", "-").replace("–", "-")
    if not s or s in {"-", "—", "N/A", "NA"}:
        return None
    m = re.search(r"-?[0-9]+(?:\.[0-9]+)?", s)
    return float(m.group(0)) if m else None

def _percent(s):
    if s is None:
        return None
    s = str(s).replace("−", "-").replace("–", "-")
    m = re.search(r"([+\-]?[0-9]+(?:\.[0-9]+)?)\s*%", s)
    return float(m.group(1)) if m else None

def _upper_price(s):
    if not s:
        return None
    nums = re.findall(r"[0-9]+(?:\.[0-9]+)?", str(s).replace(",", ""))
    return float(nums[-1]) if nums else None

def _gmp_state(rupees, gain_pct):
    def zero(v):
        try:
            return abs(float(v)) < 1e-12
        except (TypeError, ValueError):
            return False

    if rupees is None and gain_pct is None:
        return "NOT_AVAILABLE"
    if (
        (zero(rupees) and (gain_pct is None or zero(gain_pct)))
        or (zero(gain_pct) and (rupees is None or zero(rupees)))
    ):
        return "UNVERIFIED_ZERO"
    return "OBSERVED"

def _sanitize_gmp(rupees, gain_pct):
    state = _gmp_state(rupees, gain_pct)
    if state == "UNVERIFIED_ZERO":
        return None, None, state
    return rupees, gain_pct, state

def _date_from_text(s, default_year=DEFAULT_YEAR):
    if not s:
        return None
    s = " ".join(str(s).split())
    for fmt in ("%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except Exception:
            pass
    # Handles "7 Jan" where the annual archive implies the archive year.
    for fmt in ("%d %b", "%d %B"):
        try:
            d = datetime.strptime(s, fmt).date()
            return d.replace(year=default_year).isoformat()
        except Exception:
            pass
    return None

class RichTableParser(HTMLParser):
    """
    Captures HTML tables as rows/cells while preserving links inside cells.
    """
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.table = None
        self.row = None
        self.cell = None
        self.in_table = False
        self.in_row = False
        self.in_cell = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = dict(attrs)
        if tag == "table":
            self.in_table = True
            self.table = []
        elif tag == "tr" and self.in_table:
            self.in_row = True
            self.row = []
        elif tag in ("th", "td") and self.in_row:
            self.in_cell = True
            self.cell = {"text": [], "links": []}
        elif tag == "a" and self.in_cell and self.cell is not None:
            href = attrs.get("href")
            if href:
                self.cell["links"].append(href)

    def handle_data(self, data):
        if self.in_cell and self.cell is not None:
            self.cell["text"].append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("th", "td") and self.in_cell:
            text = " ".join(" ".join(self.cell["text"]).split())
            self.row.append({"text": text, "links": list(self.cell["links"])})
            self.cell = None
            self.in_cell = False
        elif tag == "tr" and self.in_row:
            if self.row:
                self.table.append(self.row)
            self.row = None
            self.in_row = False
        elif tag == "table" and self.in_table:
            if self.table:
                self.tables.append(self.table)
            self.table = None
            self.in_table = False

def _fetch(url, label, timeout=20):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/150 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-IN,en;q=0.9",
        },
    )
    logger.info("IPOMARKETS_REQUEST label=%s url=%s", label, url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = getattr(resp, "status", 200)
            headers = dict(resp.headers.items())
        logger.info(
            "IPOMARKETS_RESPONSE label=%s http=%s bytes=%s",
            label, status, len(body)
        )
        return body, status, headers
    except urllib.error.HTTPError as exc:
        logger.error(
            "IPOMARKETS_HTTP_ERROR label=%s code=%s reason=%r url=%s",
            label, exc.code, exc.reason, url
        )
        raise
    except Exception:
        logger.exception("IPOMARKETS_FETCH_FAILED label=%s url=%s", label, url)
        raise

def _find_annual_table(tables):
    for table in tables:
        if not table:
            continue
        headers = [_canon(c["text"]) for c in table[0]]
        # Current archive: Company | Status | Band / Price | GMP | Sub | Dates | Listing
        if (
            "company" in headers
            and "status" in headers
            and any(x in headers for x in ("bandprice", "priceband"))
            and "sub" in headers
            and "listing" in headers
        ):
            return table
    return None

def _parse_date_range(text, default_year=DEFAULT_YEAR):
    # Examples: "8 Dec 2025 – 10 Dec 2025" and "31 Dec 2025 – 2 Jan 2026"
    if not text:
        return (None, None)
    parts = re.split(r"\s+[–—-]\s+", text)
    if len(parts) < 2:
        return (None, None)
    return _date_from_text(parts[0], default_year), _date_from_text(parts[-1], default_year)

def _segment_from_company_cell(text):
    t = " ".join((text or "").split())
    upper = t.upper()
    if upper.endswith("SME"):
        return "SME", t[:-3].strip()
    if upper.endswith("MAINBOARD"):
        return "MAINBOARD", t[:-9].strip()
    # Some pages may render "InvIT Mainboard"; treat as Mainboard but it can
    # later be excluded if issue price/listing target is unusable.
    if "MAINBOARD" in upper:
        pos = upper.rfind("MAINBOARD")
        return "MAINBOARD", t[:pos].strip()
    return None, t

def _detail_href(cell):
    for href in cell.get("links") or []:
        if "/ipo/" in href:
            return urllib.parse.urljoin(BASE, href)
    return None

def parse_annual_page(body, archive_year=DEFAULT_YEAR, listed_only=True):
    parser = RichTableParser()
    parser.feed(body)
    table = _find_annual_table(parser.tables)
    if not table:
        return []

    headers = [_canon(c["text"]) for c in table[0]]
    idx = {h: i for i, h in enumerate(headers)}

    def cell(row, *names):
        for name in names:
            c = _canon(name)
            if c in idx and idx[c] < len(row):
                return row[idx[c]]
        return {"text": "", "links": []}

    rows = []
    for row in table[1:]:
        company_cell = cell(row, "Company")
        segment, name = _segment_from_company_cell(company_cell["text"])
        if segment not in ("MAINBOARD", "SME") or not name:
            continue

        status_text = cell(row, "Status")["text"]
        if listed_only and "listed" not in status_text.lower():
            continue

        price_text = cell(row, "Band / Price", "Price Band")["text"]
        gmp_text = cell(row, "GMP")["text"]
        sub_text = cell(row, "Sub")["text"]
        dates_text = cell(row, "Dates")["text"]
        listing_text = cell(row, "Listing")["text"]

        issue_open, issue_close = _parse_date_range(dates_text, archive_year)
        issue_price = _upper_price(price_text)
        listing_gain_pct = _percent(listing_text)
        listing_price = _num(listing_text)
        total_x = _num(sub_text)

        gmp_gain_pct = _percent(gmp_text)
        gmp_rupees = _num(gmp_text)
        if gmp_gain_pct is None and gmp_rupees is not None and issue_price:
            gmp_gain_pct = gmp_rupees / issue_price * 100.0

        raw_gmp_rupees = gmp_rupees
        raw_gmp_gain_pct = gmp_gain_pct
        gmp_rupees, gmp_gain_pct, gmp_state = _sanitize_gmp(
            gmp_rupees, gmp_gain_pct
        )

        rows.append({
            "year": int(archive_year),
            "ipo_type": segment,
            "name": name,
            "detail_url": _detail_href(company_cell),
            "status_text": status_text,
            "issue_open": issue_open,
            "issue_close": issue_close,
            "issue_price": issue_price,
            "total_x": total_x,
            "listing_price": listing_price,
            "listing_gain_pct": listing_gain_pct,
            "gmp_rupees": gmp_rupees,
            "gmp_gain_pct": gmp_gain_pct,
            "gmp_state": gmp_state,
            "raw_gmp_rupees": raw_gmp_rupees,
            "raw_gmp_gain_pct": raw_gmp_gain_pct,
            "raw_index": {
                "company": company_cell["text"],
                "status": status_text,
                "price": price_text,
                "gmp": gmp_text,
                "sub": sub_text,
                "dates": dates_text,
                "listing": listing_text,
            },
        })
    return rows

def _find_table_by_headers(tables, required):
    required = {_canon(x) for x in required}
    for table in tables:
        if not table:
            continue
        headers = {_canon(c["text"]) for c in table[0]}
        if required.issubset(headers):
            return table
    return None

def parse_detail_page(body):
    parser = RichTableParser()
    parser.feed(body)
    out = {
        "gmp_rupees": None,
        "gmp_gain_pct": None,
        "qib_x": None,
        "nii_x": None,
        "retail_x": None,
        "total_x": None,
        "listing_open": None,
        "listing_close": None,
    }

    # Daily GMP history table: Date (IST) | GMP | GMP % | Est. listing
    for table in parser.tables:
        if not table:
            continue
        headers = [_canon(c["text"]) for c in table[0]]
        if "gmp" in headers and ("gmp" in headers or "gmppercent" in headers):
            # Prefer an explicit percentage column if present.
            gmp_idx = headers.index("gmp") if "gmp" in headers else None
            pct_idx = None
            for candidate in ("gmp", "gmppercent"):
                positions = [i for i, h in enumerate(headers) if h == candidate]
                if candidate == "gmp" and len(positions) > 1:
                    pct_idx = positions[1]
                elif candidate == "gmppercent" and positions:
                    pct_idx = positions[0]
            if len(table) > 1:
                first = table[1]
                if gmp_idx is not None and gmp_idx < len(first):
                    out["gmp_rupees"] = _num(first[gmp_idx]["text"])
                if pct_idx is not None and pct_idx < len(first):
                    out["gmp_gain_pct"] = _percent(first[pct_idx]["text"])
            if out["gmp_gain_pct"] is not None or out["gmp_rupees"] is not None:
                break

    out["raw_gmp_rupees"] = out["gmp_rupees"]
    out["raw_gmp_gain_pct"] = out["gmp_gain_pct"]
    (
        out["gmp_rupees"],
        out["gmp_gain_pct"],
        out["gmp_state"],
    ) = _sanitize_gmp(out["gmp_rupees"], out["gmp_gain_pct"])

    # Subscription table: Category | Subscription | ...
    for table in parser.tables:
        if not table:
            continue
        headers = [_canon(c["text"]) for c in table[0]]
        if "category" in headers and "subscription" in headers:
            ci = headers.index("category")
            si = headers.index("subscription")
            for row in table[1:]:
                if max(ci, si) >= len(row):
                    continue
                category = _canon(row[ci]["text"])
                value = _num(row[si]["text"])
                if "total" in category:
                    out["total_x"] = value
                elif category in ("qib", "qualifiedinstitutionalbuyers") or "institutional" in category:
                    out["qib_x"] = value
                elif category in ("nii", "hni") or "noninstitutional" in category:
                    out["nii_x"] = value
                elif "retail" in category or "individual" in category:
                    out["retail_x"] = value

    # Listing-day performance table: Exchange | Open | High | Low | Close
    for table in parser.tables:
        if not table:
            continue
        headers = [_canon(c["text"]) for c in table[0]]
        if all(h in headers for h in ("exchange", "open", "close")) and len(table) > 1:
            oi = headers.index("open")
            ci = headers.index("close")
            # Prefer NSE if present; otherwise first exchange row.
            chosen = table[1]
            for row in table[1:]:
                if row and "nse" in _canon(row[0]["text"]):
                    chosen = row
                    break
            if oi < len(chosen):
                out["listing_open"] = _num(chosen[oi]["text"])
            if ci < len(chosen):
                out["listing_close"] = _num(chosen[ci]["text"])
            break

    return out


IST = ZoneInfo("Asia/Kolkata")

def _parse_ist_datetime(text):
    if not text:
        return None
    text = " ".join(str(text).split())
    text = re.sub(r"\s+IST$", "", text, flags=re.IGNORECASE)
    for fmt in (
        "%d %b %Y, %I:%M %p",
        "%d %B %Y, %I:%M %p",
        "%d %b %Y %I:%M %p",
    ):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=IST)
        except Exception:
            pass
    return None

def parse_gmp_history_page(body):
    """Parse all rows from an IPOMarkets dedicated /gmp history page."""
    parser = RichTableParser()
    parser.feed(body)
    readings = []

    for table in parser.tables:
        if not table:
            continue
        headers = [_canon(c["text"]) for c in table[0]]
        if "dateist" not in headers and "date" not in headers:
            continue

        gmp_positions = [i for i, h in enumerate(headers) if h == "gmp"]
        if not gmp_positions and "gmppercent" not in headers:
            continue

        date_idx = headers.index("dateist") if "dateist" in headers else headers.index("date")
        gmp_idx = gmp_positions[0] if gmp_positions else None
        pct_idx = headers.index("gmppercent") if "gmppercent" in headers else (
            gmp_positions[1] if len(gmp_positions) > 1 else None
        )

        for row in table[1:]:
            if date_idx >= len(row):
                continue
            dt = _parse_ist_datetime(row[date_idx]["text"])
            if not dt:
                continue
            raw_rupees = (
                _num(row[gmp_idx]["text"])
                if gmp_idx is not None and gmp_idx < len(row) else None
            )
            raw_gain_pct = (
                _percent(row[pct_idx]["text"])
                if pct_idx is not None and pct_idx < len(row) else None
            )
            gmp_rupees, gmp_gain_pct, gmp_state = _sanitize_gmp(
                raw_rupees, raw_gain_pct
            )
            readings.append({
                "at_ist": dt.isoformat(),
                "gmp_rupees": gmp_rupees,
                "gmp_gain_pct": gmp_gain_pct,
                "gmp_state": gmp_state,
                "raw_gmp_rupees": raw_rupees,
                "raw_gmp_gain_pct": raw_gain_pct,
            })

        if readings:
            break

    readings.sort(key=lambda x: x["at_ist"])
    return readings

def select_pre1430_gmp(readings, issue_close):
    """
    Latest GMP observation known at or before 2:30 PM IST on IPO closing day.
    This avoids using an evening reading that was not available at decision time.
    """
    if not issue_close:
        return None
    try:
        close_date = datetime.strptime(issue_close, "%Y-%m-%d").date()
    except Exception:
        return None

    cutoff = datetime(
        close_date.year, close_date.month, close_date.day,
        14, 30, tzinfo=IST
    )
    candidates = []
    for r in readings or []:
        try:
            dt = datetime.fromisoformat(r["at_ist"])
        except Exception:
            continue
        if dt <= cutoff and r.get("gmp_gain_pct") is not None:
            candidates.append((dt, r))

    if not candidates:
        return None

    dt, chosen = max(candidates, key=lambda x: x[0])
    result = dict(chosen)
    result["decision_cutoff_ist"] = cutoff.isoformat()
    result["age_hours_at_decision"] = round(
        (cutoff - dt).total_seconds() / 3600.0, 2
    )
    result["quality"] = (
        "CLOSING_DAY_PRE1430"
        if dt.date() == close_date
        else "LATEST_BEFORE_CLOSING_DAY_1430"
    )
    return result

def fetch_pre1430_gmp(detail_url, issue_close, save_raw=False):
    if not detail_url:
        return None
    url = detail_url.rstrip("/") + "/gmp"
    body, status, headers = _fetch(
        url,
        f"IPOMARKETS_GMP_TRACK_{_canon(detail_url)[-45:]}",
        timeout=15,
    )
    if save_raw:
        save_raw_response(
            f"IPOMARKETS_GMP_TRACK_{_canon(detail_url)[-45:]}",
            url, status, headers, body
        )
    readings = parse_gmp_history_page(body)
    return {
        "url": url,
        "reading_count": len(readings),
        "observed_count": sum(
            1 for r in readings if r.get("gmp_state") == "OBSERVED"
        ),
        "zero_unverified_count": sum(
            1 for r in readings if r.get("gmp_state") == "UNVERIFIED_ZERO"
        ),
        "selected": select_pre1430_gmp(readings, issue_close),
    }

def tracking_parser_self_test():
    annual = """
    <table>
      <tr><th>Company</th><th>Status</th><th>Band / Price</th><th>GMP</th><th>Sub</th><th>Dates</th><th>Listing</th></tr>
      <tr>
        <td><a href="/ipo/sample-ipo">Sample IPO Mainboard</a></td>
        <td>Closes today</td><td>₹90–₹100</td><td>₹20 +20.00%</td><td>12.5×</td>
        <td>1 Jan 2026 – 3 Jan 2026</td><td>—</td>
      </tr>
    </table>
    """
    rows = parse_annual_page(
        annual, archive_year=2026, listed_only=False
    )
    if len(rows) != 1 or rows[0]["status_text"] != "Closes today":
        raise RuntimeError(f"Tracking annual parser self-test failed: {rows}")

    gmp = """
    <table>
      <tr><th>Date (IST)</th><th>GMP</th><th>GMP %</th><th>Est. listing</th></tr>
      <tr><td>3 Jan 2026, 08:00 pm IST</td><td>₹25</td><td>+25.00%</td><td>₹125</td></tr>
      <tr><td>2 Jan 2026, 08:00 pm IST</td><td>₹20</td><td>+20.00%</td><td>₹120</td></tr>
    </table>
    """
    readings = parse_gmp_history_page(gmp)
    chosen = select_pre1430_gmp(readings, "2026-01-03")
    if not chosen or round(chosen["gmp_gain_pct"], 2) != 20.0:
        raise RuntimeError(
            f"Tracking GMP cutoff self-test failed: {chosen}"
        )

    return {
        "annual_rows": len(rows),
        "gmp_readings": len(readings),
        "selected_pct": chosen["gmp_gain_pct"],
        "selected_quality": chosen["quality"],
    }

def _even_sample(items, count):
    if len(items) <= count:
        return list(items)
    if count <= 1:
        return [items[len(items)//2]]
    idxs = []
    for i in range(count):
        idx = round(i * (len(items)-1) / (count-1))
        if idx not in idxs:
            idxs.append(idx)
    return [items[i] for i in idxs]

def _cache_key(year, segment, name):
    return (int(year), str(segment or "").upper(), _canon(name))

def fetch_year_archive(
    year,
    target_gmp_per_segment=25,
    max_pages=10,
    cached_records=None,
):
    """
    Import all annual index rows for one year, then enrich an evenly distributed
    subset of Mainboard and SME detail pages until approximately
    target_gmp_per_segment historical GMP observations exist per segment.

    cached_records is a map keyed by (year, segment, canonical_name). Cached GMP
    and detail-page fields are applied before any network detail request, so
    repeated imports do not re-fetch IPO detail pages unnecessarily.
    """
    year = int(year)
    cached_records = cached_records or {}
    all_rows = []
    page_meta = []

    for page in range(1, max_pages + 1):
        url = (
            f"{BASE}/ipo-calendar/{year}"
            if page == 1
            else f"{BASE}/ipo-calendar/{year}/page/{page}"
        )
        body, status, headers = _fetch(
            url, f"IPOMARKETS_YEAR_{year}_PAGE_{page}", timeout=20
        )
        if page == 1:
            save_raw_response(
                f"IPOMARKETS_{year}_INDEX_PAGE1", url, status, headers, body
            )

        parsed = parse_annual_page(body, archive_year=year)
        for r in parsed:
            r["year"] = year

        logger.info(
            "IPOMARKETS_INDEX_PARSED year=%s page=%s rows=%s",
            year, page, len(parsed)
        )
        page_meta.append({
            "year": year,
            "page": page,
            "url": url,
            "http_status": status,
            "rows": len(parsed),
        })

        if not parsed:
            break
        all_rows.extend(parsed)

        # Current archive page size is 50; a shorter page is the last page.
        if len(parsed) < 50:
            break

    # Stable annual de-duplication.
    dedup = {}
    for r in all_rows:
        key = (
            _canon(r.get("name")),
            r.get("ipo_type"),
        )
        existing = dedup.get(key)
        if not existing:
            dedup[key] = r
        else:
            # Prefer whichever row contains more useful predictor/outcome fields.
            fields = (
                "listing_gain_pct", "total_x", "issue_price",
                "gmp_gain_pct", "detail_url"
            )
            score_new = sum(r.get(f) is not None for f in fields)
            score_old = sum(existing.get(f) is not None for f in fields)
            if score_new > score_old:
                dedup[key] = r
    all_rows = list(dedup.values())

    # Apply cached detail enrichment before deciding what must be fetched.
    cache_hits = 0
    cache_gmp_hits = 0
    detail_fields = (
        "gmp_rupees", "gmp_gain_pct", "qib_x", "nii_x", "retail_x",
        "listing_open", "listing_close"
    )
    for r in all_rows:
        cached = cached_records.get(
            _cache_key(year, r.get("ipo_type"), r.get("name"))
        )
        if not cached:
            continue
        cache_hits += 1
        if cached.get("gmp_gain_pct") is not None:
            cache_gmp_hits += 1
        for field in detail_fields:
            if cached.get(field) is not None:
                r[field] = cached.get(field)
        # Preserve richer total subscription if detail page provided it.
        if cached.get("total_x") is not None:
            r["total_x"] = cached.get("total_x")

    detail_meta = []

    for segment in ("MAINBOARD", "SME"):
        candidates = [
            r for r in all_rows
            if r.get("ipo_type") == segment
            and r.get("listing_gain_pct") is not None
            and r.get("issue_price") is not None
            and r.get("detail_url")
        ]

        already_complete = sum(
            1 for r in candidates if r.get("gmp_gain_pct") is not None
        )
        need = max(0, target_gmp_per_segment - already_complete)

        # Spread selected detail pages across the full year. Oversample the
        # candidate list so missing historical GMP on individual pages does not
        # prevent us from reaching the target.
        missing_candidates = [
            r for r in candidates if r.get("gmp_gain_pct") is None
        ]
        sample_count = min(
            len(missing_candidates),
            max(target_gmp_per_segment * 3, need * 4)
        )
        sample = _even_sample(missing_candidates, sample_count)

        fetched = 0
        gmp_added = 0
        errors = 0

        for r in sample:
            if gmp_added >= need:
                break
            try:
                body, status, headers = _fetch(
                    r["detail_url"],
                    f"IPOMARKETS_DETAIL_{year}_{segment}_{_canon(r['name'])[:40]}",
                    timeout=15,
                )
                d = parse_detail_page(body)
                fetched += 1

                for field in detail_fields:
                    if d.get(field) is not None:
                        r[field] = d[field]
                if d.get("total_x") is not None:
                    r["total_x"] = d["total_x"]

                if (
                    r.get("gmp_gain_pct") is None
                    and r.get("gmp_rupees") is not None
                    and r.get("issue_price")
                ):
                    r["gmp_gain_pct"] = (
                        r["gmp_rupees"] / r["issue_price"] * 100.0
                    )

                if r.get("gmp_gain_pct") is not None:
                    gmp_added += 1

                # Conservative crawl cadence.
                time.sleep(0.18)
            except Exception as exc:
                errors += 1
                logger.warning(
                    "IPOMARKETS_DETAIL_SKIPPED year=%s segment=%s name=%r error=%r",
                    year, segment, r.get("name"), str(exc)
                )

        total_gmp = sum(
            1 for r in candidates if r.get("gmp_gain_pct") is not None
        )
        detail_meta.append({
            "year": year,
            "segment": segment,
            "candidate_rows": len(candidates),
            "target_gmp_rows": target_gmp_per_segment,
            "gmp_rows_before_network": already_complete,
            "detail_pages_fetched": fetched,
            "detail_errors": errors,
            "gmp_rows_after_enrichment": total_gmp,
        })

    logger.info(
        "IPOMARKETS_ARCHIVE_DONE year=%s rows=%s mainboard=%s sme=%s "
        "gmp_mainboard=%s gmp_sme=%s cache_hits=%s cache_gmp_hits=%s",
        year,
        len(all_rows),
        sum(1 for r in all_rows if r.get("ipo_type") == "MAINBOARD"),
        sum(1 for r in all_rows if r.get("ipo_type") == "SME"),
        sum(1 for r in all_rows if r.get("ipo_type") == "MAINBOARD" and r.get("gmp_gain_pct") is not None),
        sum(1 for r in all_rows if r.get("ipo_type") == "SME" and r.get("gmp_gain_pct") is not None),
        cache_hits,
        cache_gmp_hits,
    )

    return all_rows, {
        "year": year,
        "pages": page_meta,
        "details": detail_meta,
        "rows": len(all_rows),
        "listing_targets": sum(
            1 for r in all_rows if r.get("listing_gain_pct") is not None
        ),
        "with_total_subscription": sum(
            1 for r in all_rows if r.get("total_x") is not None
        ),
        "with_gmp": sum(
            1 for r in all_rows if r.get("gmp_gain_pct") is not None
        ),
        "cache_hits": cache_hits,
        "cache_gmp_hits": cache_gmp_hits,
    }



def parser_self_test():
    """
    Offline regression test for annual archive parsing.
    No network access is used.
    """
    sample = """
    <html><body>
    <table>
      <tr>
        <th>Company</th><th>Status</th><th>Band / Price</th>
        <th>GMP</th><th>Sub</th><th>Dates</th><th>Listing</th>
      </tr>
      <tr>
        <td><a href="/ipo/sample-mainboard">Sample Industries Mainboard</a></td>
        <td>Listed</td><td>₹95 - ₹100</td><td>₹20 (20%)</td>
        <td>12.5x</td><td>1 Jan 2024 – 3 Jan 2024</td>
        <td>₹125 (+25%)</td>
      </tr>
    </table>
    </body></html>
    """
    rows_2024 = parse_annual_page(sample, archive_year=2024)
    rows_2025 = parse_annual_page(
        sample.replace("2024", "2025"),
        archive_year=2025,
    )

    checks = {
        "rows_2024": len(rows_2024),
        "rows_2025": len(rows_2025),
        "year_2024": rows_2024[0]["year"] if rows_2024 else None,
        "year_2025": rows_2025[0]["year"] if rows_2025 else None,
        "name": rows_2024[0]["name"] if rows_2024 else None,
        "segment": rows_2024[0]["ipo_type"] if rows_2024 else None,
        "listing_gain_pct": rows_2024[0]["listing_gain_pct"] if rows_2024 else None,
    }
    ok = (
        checks["rows_2024"] == 1
        and checks["rows_2025"] == 1
        and checks["year_2024"] == 2024
        and checks["year_2025"] == 2025
        and checks["segment"] == "MAINBOARD"
    )
    if not ok:
        raise RuntimeError(f"IPOMarkets parser self-test failed: {checks}")
    return checks
