import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
RAW_DIR = LOG_DIR / "raw"
LOG_FILE = LOG_DIR / "ipo_advisor.log"
IST = ZoneInfo("Asia/Kolkata")

LOG_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)

class ISTFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, IST)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %I:%M:%S %p IST")

def get_logger():
    logger = logging.getLogger("ipo_advisor")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(ISTFormatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    ))
    logger.addHandler(handler)
    logger.propagate = False
    return logger

logger = get_logger()

def save_raw_response(label, url, http_status, headers, body_text):
    stamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S_%f")
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in label)
    path = RAW_DIR / f"{stamp}_{safe}.json"
    payload = {
        "captured_at_ist": datetime.now(IST).isoformat(),
        "request_url": url,
        "http_status": http_status,
        "response_headers": dict(headers or {}),
        "body": body_text,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("RAW_RESPONSE_SAVED label=%s path=%s", label, path.relative_to(ROOT))
    return path

def tail_log(max_lines=300):
    if not LOG_FILE.exists():
        return ""
    lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:])

def clear_log():
    # Do not remove rotated archives automatically; only reset current file.
    LOG_FILE.write_text("", encoding="utf-8")
    logger.info("LOG_CLEARED")


def save_json_report(label, payload):
    REPORT_DIR = LOG_DIR / "reports"
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S_%f")
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in label)
    path = REPORT_DIR / f"{stamp}_{safe}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    logger.info("JSON_REPORT_SAVED label=%s path=%s", label, path.relative_to(ROOT))
    return path

def list_reports(limit=30):
    report_dir = LOG_DIR / "reports"
    if not report_dir.exists():
        return []
    rows = []
    for p in sorted(report_dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
        rows.append({
            "name": p.name,
            "size": p.stat().st_size,
            "path": str(p.relative_to(ROOT)),
        })
    return rows
