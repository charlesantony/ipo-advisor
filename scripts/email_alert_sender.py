import json
import os
import urllib.error
import urllib.parse
import urllib.request


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


def send_research_alerts(alerts, dashboard_url=""):
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

    sent = 0
    failed = 0
    details = []

    for alert in alerts:
        fields = {
            "action": "notify",
            "key": alert_key,
            "ipo_name": alert.get("name") or "IPO",
            "segment": alert.get("segment") or "",
            "signal": alert.get("action") or "",
            "predicted_gain": _fmt_pct(alert.get("predicted_gain_pct")),
            "gmp": _fmt_pct(alert.get("gmp_gain_pct")),
            "total_subscription": _fmt_x(alert.get("total_x")),
            "dashboard_url": dashboard_url or "",
            "alert_kind": alert.get("alert_kind") or "CLOSING_DAY",
            "previous_signal": alert.get("previous_signal") or "",
        }
        status, response = _post_form(endpoint, fields)
        ok = 200 <= int(status) < 300 and response.get("ok", True) is not False
        if ok:
            sent += 1
        else:
            failed += 1
        details.append({
            "ipo": alert.get("name"),
            "alert_kind": alert.get("alert_kind"),
            "status": status,
            "ok": ok,
            "response": response,
        })
        print(
            "EMAIL_ALERT_SEND "
            f"kind={alert.get('alert_kind')!r} "
            f"ipo={alert.get('name')!r} status={status} ok={ok}"
        )

    return {
        "configured": True,
        "alerts": len(alerts),
        "sent_alerts": sent,
        "failed_alerts": failed,
        "details": details,
    }
