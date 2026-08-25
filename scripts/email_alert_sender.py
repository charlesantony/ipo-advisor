import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_pct(value):
    value = _f(value)
    return f"{value:.1f}%" if value is not None else "N/A"


def _fmt_x(value):
    value = _f(value)
    return f"{value:.1f}x" if value is not None else "N/A"


def _post_form(url, fields):
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "IPO-Advisor-GitHub-Action/0.5.2",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(raw) if raw else {}
            except Exception:
                body = {"raw": raw[:1000]}
            return resp.status, body
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {"raw": raw[:1000]}
        return exc.code, body


def _batch_kind(alerts):
    kinds = {
        str(a.get("alert_kind") or "").strip().upper()
        for a in alerts
    }
    return "DAY2" if kinds == {"DAY2_EARLY"} else "CLOSING"


def _wire_alert(alert):
    return {
        "name": alert.get("name") or "IPO",
        "symbol": alert.get("symbol") or "",
        "segment": alert.get("segment") or "",
        "signal": alert.get("action") or "",
        "predicted_gain": _fmt_pct(alert.get("predicted_gain_pct")),
        "gmp": _fmt_pct(alert.get("gmp_gain_pct")),
        "total_subscription": _fmt_x(alert.get("total_x")),
        "closing_date": alert.get("end_date") or "",
        "alert_kind": alert.get("alert_kind") or "CLOSING_DAY",
        "previous_signal": alert.get("previous_signal") or "",
    }


def send_research_alerts(alerts, dashboard_url=""):
    """Send one consolidated email payload per scheduled alert run."""
    endpoint = os.environ.get("SUBSCRIBE_ENDPOINT", "").strip()
    alert_key = os.environ.get("EMAIL_ALERT_KEY", "").strip()

    missing = [
        name for name, value in (
            ("SUBSCRIBE_ENDPOINT", endpoint),
            ("EMAIL_ALERT_KEY", alert_key),
        )
        if not value
    ]
    if missing:
        print("EMAIL_ALERT_SKIPPED missing configuration: " + ", ".join(missing))
        return {
            "configured": False,
            "sent_alerts": 0,
            "failed_alerts": 0,
            "missing": missing,
        }

    alerts = list(alerts or [])
    if not alerts:
        return {
            "configured": True,
            "alerts": 0,
            "sent_alerts": 0,
            "failed_alerts": 0,
            "batch_sent": False,
        }

    batch_kind = _batch_kind(alerts)
    fields = {
        "action": "notify_batch",
        "key": alert_key,
        "batch_kind": batch_kind,
        "batch_date": datetime.now(IST).date().isoformat(),
        "alerts_json": json.dumps(
            [_wire_alert(a) for a in alerts],
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "dashboard_url": dashboard_url or "",
    }

    status, response = _post_form(endpoint, fields)
    ok = (
        200 <= int(status) < 300
        and response.get("ok", True) is not False
    )
    print(
        "EMAIL_ALERT_BATCH_SEND "
        f"kind={batch_kind!r} alerts={len(alerts)} "
        f"status={status} ok={ok}"
    )

    return {
        "configured": True,
        "alerts": len(alerts),
        "sent_alerts": len(alerts) if ok else 0,
        "failed_alerts": 0 if ok else len(alerts),
        "batch_sent": bool(ok),
        "batch_kind": batch_kind,
        "status": status,
        "response": response,
    }
