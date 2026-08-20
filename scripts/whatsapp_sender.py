import json
import os
import re
import urllib.error
import urllib.request

def parse_recipients(raw):
    raw = (raw or "").strip()
    if not raw:
        return []

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            values = parsed
        else:
            values = [str(parsed)]
    except Exception:
        values = re.split(r"[\s,;]+", raw)

    out = []
    seen = set()
    for value in values:
        digits = re.sub(r"\D", "", str(value))
        if digits and digits not in seen:
            seen.add(digits)
            out.append(digits)
    return out

def _request(url, token, payload):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(text) if text else {}
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(text)
        except Exception:
            payload = {"raw": text}
        return exc.code, payload

def template_payload(to, template_name, lang, params):
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": lang},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": str(x)}
                        for x in params
                    ],
                }
            ],
        },
    }

def send_research_alerts(alerts, dashboard_url=""):
    token = os.environ.get("WHATSAPP_TOKEN", "").strip()
    phone_number_id = os.environ.get(
        "WHATSAPP_PHONE_NUMBER_ID", ""
    ).strip()
    template_name = os.environ.get(
        "WHATSAPP_TEMPLATE_NAME", ""
    ).strip()
    recipients = parse_recipients(
        os.environ.get("WHATSAPP_RECIPIENTS", "")
    )
    graph_version = os.environ.get(
        "WHATSAPP_GRAPH_VERSION", ""
    ).strip()
    lang = (
        os.environ.get("WHATSAPP_TEMPLATE_LANG", "en_US").strip()
        or "en_US"
    )
    dry_run = os.environ.get(
        "WHATSAPP_DRY_RUN", ""
    ).strip().lower() in {"1", "true", "yes"}

    missing = [
        name
        for name, value in (
            ("WHATSAPP_TOKEN", token),
            ("WHATSAPP_PHONE_NUMBER_ID", phone_number_id),
            ("WHATSAPP_TEMPLATE_NAME", template_name),
            ("WHATSAPP_RECIPIENTS", recipients),
            ("WHATSAPP_GRAPH_VERSION", graph_version),
        )
        if not value
    ]
    if missing:
        print(
            "WHATSAPP_SKIPPED missing configuration: "
            + ", ".join(missing)
        )
        return {
            "configured": False,
            "sent": 0,
            "failed": 0,
            "skipped_reason": "missing_configuration",
            "missing": missing,
        }

    url = (
        f"https://graph.facebook.com/{graph_version}/"
        f"{phone_number_id}/messages"
    )
    sent = 0
    failed = 0
    details = []

    for alert in alerts:
        pred = alert.get("predicted_gain_pct")
        pred_text = (
            f"{float(pred):.1f}%"
            if pred is not None else "N/A"
        )
        gmp = alert.get("gmp_gain_pct")
        total = alert.get("total_x")
        market = (
            f"GMP {float(gmp):.1f}%"
            if gmp is not None else "GMP N/A"
        )
        market += (
            f", Total {float(total):.1f}x"
            if total is not None else ", Total N/A"
        )
        if dashboard_url:
            market += f" | {dashboard_url}"

        params = [
            alert.get("name") or "IPO",
            alert.get("segment") or "",
            alert.get("action") or "",
            pred_text,
            market,
        ]

        for recipient in recipients:
            payload = template_payload(
                recipient, template_name, lang, params
            )
            if dry_run:
                status, response = 200, {
                    "dry_run": True,
                    "recipient": recipient,
                    "payload": payload,
                }
            else:
                status, response = _request(
                    url, token, payload
                )

            ok = 200 <= int(status) < 300
            sent += 1 if ok else 0
            failed += 0 if ok else 1
            details.append({
                "recipient_last4": recipient[-4:],
                "ipo": alert.get("name"),
                "status": status,
                "ok": ok,
                "response": response,
            })
            print(
                "WHATSAPP_SEND "
                f"ipo={alert.get('name')!r} "
                f"recipient=***{recipient[-4:]} "
                f"status={status} ok={ok}"
            )

    return {
        "configured": True,
        "dry_run": dry_run,
        "recipients": len(recipients),
        "alerts": len(alerts),
        "sent": sent,
        "failed": failed,
        "details": details,
    }
