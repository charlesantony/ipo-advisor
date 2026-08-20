import html
import re
import urllib.request
from datetime import datetime
from html.parser import HTMLParser

from logging_utils import logger, save_raw_response

SOURCES = {
    "MAINBOARD": "https://ipodhan.com/mainboard-ipo-listings",
    "SME": "https://www.ipodhan.com/sme-ipo-listings",
}

class TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.current_table = None
        self.current_row = None
        self.current_cell = None
        self.in_table = False
        self.in_row = False
        self.in_cell = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "table":
            self.in_table = True
            self.current_table = []
        elif tag == "tr" and self.in_table:
            self.in_row = True
            self.current_row = []
        elif tag in ("th", "td") and self.in_row:
            self.in_cell = True
            self.current_cell = []

    def handle_data(self, data):
        if self.in_cell and self.current_cell is not None:
            self.current_cell.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("th", "td") and self.in_cell:
            text = " ".join(" ".join(self.current_cell).split())
            self.current_row.append(text)
            self.current_cell = None
            self.in_cell = False
        elif tag == "tr" and self.in_row:
            if self.current_row:
                self.current_table.append(self.current_row)
            self.current_row = None
            self.in_row = False
        elif tag == "table" and self.in_table:
            if self.current_table:
                self.tables.append(self.current_table)
            self.current_table = None
            self.in_table = False

def _num(s):
    if s is None:
        return None
    s = str(s).strip()
    if not s or s in {"-", "—", "N/A", "NA"}:
        return None
    s = s.replace(",", "")
    m = re.search(r"-?[0-9]+(?:\.[0-9]+)?", s)
    return float(m.group(0)) if m else None

def _date(s):
    if not s:
        return None
    s = " ".join(str(s).split())
    for fmt in ("%b %d, %Y", "%d %b %Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except Exception:
            pass
    return s

def _clean_company(s, segment):
    s = " ".join((s or "").split())
    # Pages append MAINBOARD/SME to the visible company text.
    suffix = segment.upper()
    if s.upper().endswith(suffix):
        s = s[:-len(suffix)].strip()
    return s

def _canon_header(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def _best_table(tables):
    wanted = {"companyname", "issueopen", "issueclose", "listingdate", "issueprice"}
    best = None
    score = -1
    for table in tables:
        if not table:
            continue
        headers = {_canon_header(x) for x in table[0]}
        s = len(headers & wanted)
        if s > score:
            score = s
            best = table
    return best if score >= 4 else None

def fetch_table(segment):
    segment = segment.upper()
    url = SOURCES[segment]
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 Chrome/150 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-IN,en;q=0.9",
        },
    )
    logger.info("IPODHAN_REQUEST segment=%s url=%s", segment, url)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        status = getattr(resp, "status", 200)
        headers = dict(resp.headers.items())
    logger.info("IPODHAN_RESPONSE segment=%s http=%s bytes=%s", segment, status, len(body))
    save_raw_response(f"IPODHAN_{segment}", url, status, headers, body)

    parser = TableParser()
    parser.feed(body)
    table = _best_table(parser.tables)
    if not table:
        logger.error("IPODHAN_TABLE_NOT_FOUND segment=%s table_count=%s", segment, len(parser.tables))
        return [], {
            "segment": segment,
            "url": url,
            "http_status": status,
            "rows": 0,
            "error": "Could not locate the IPO listing table in returned HTML",
        }

    headers_raw = table[0]
    headers = [_canon_header(x) for x in headers_raw]
    idx = {h: i for i, h in enumerate(headers)}

    def get(row, *names):
        for n in names:
            c = _canon_header(n)
            if c in idx and idx[c] < len(row):
                return row[idx[c]]
        return None

    records = []
    for row in table[1:]:
        name = _clean_company(get(row, "Company Name"), segment)
        if not name:
            continue

        issue_price = _num(get(row, "Issue Price (₹)", "Issue Price"))
        gmp_rupees = _num(get(row, "GMP (₹)", "GMP"))
        listing_gain_pct = _num(get(row, "Listing Gain %"))
        listing_close = _num(get(row, "Listing Close (₹)", "Listing Close"))

        gmp_gain_pct = None
        if gmp_rupees is not None and issue_price:
            gmp_gain_pct = (gmp_rupees / issue_price) * 100.0

        records.append({
            "name": name,
            "ipo_type": segment,
            "issue_open": _date(get(row, "Issue Open")),
            "issue_close": _date(get(row, "Issue Close")),
            "listing_date": _date(get(row, "Listing Date")),
            "issue_price": issue_price,
            "issue_size_cr": _num(get(row, "Issue Size (Cr)", "Issue Size")),
            "lot_size": _num(get(row, "Lot Size")),
            "total_x": _num(get(row, "Overall")),
            "qib_x": _num(get(row, "QIB")),
            "nii_x": _num(get(row, "NII")),
            "retail_x": _num(get(row, "Retail")),
            "gmp_rupees": gmp_rupees,
            "gmp_gain_pct": gmp_gain_pct,
            "listing_close": listing_close,
            "listing_gain_pct": listing_gain_pct,
            "source_url": url,
            "raw_row": dict(zip(headers_raw, row)),
        })

    # Deduplicate page duplicates: keep the row with more populated model fields.
    def completeness(r):
        keys = ("listing_gain_pct", "gmp_gain_pct", "qib_x", "nii_x", "retail_x",
                "total_x", "issue_size_cr", "listing_close")
        return sum(r.get(k) is not None for k in keys)

    dedup = {}
    for r in records:
        key = (
            re.sub(r"[^a-z0-9]", "", r["name"].lower()),
            r.get("listing_date"),
            r.get("issue_price"),
        )
        if key not in dedup or completeness(r) > completeness(dedup[key]):
            dedup[key] = r

    records = list(dedup.values())
    logger.info(
        "IPODHAN_PARSED segment=%s raw_rows=%s dedup_rows=%s targets=%s",
        segment, len(table)-1, len(records),
        sum(1 for r in records if r.get("listing_gain_pct") is not None)
    )

    return records, {
        "segment": segment,
        "url": url,
        "http_status": status,
        "rows": len(records),
        "targets": sum(1 for r in records if r.get("listing_gain_pct") is not None),
        "headers": headers_raw,
    }
