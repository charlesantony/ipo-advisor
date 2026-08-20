import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request

from .base import IPOProvider
from logging_utils import logger, save_raw_response

BASE_URL = "https://finapi.upvaly.com/api/ipo"

class FinAPIProvider(IPOProvider):
    def fetch_ipos(self, status="LIVE", ipo_type="MAINBOARD", save_raw=False, diagnostic_label=None):
        params = {}
        if status:
            params["status"] = str(status).upper()
        if ipo_type:
            params["type"] = str(ipo_type).upper()

        query = urllib.parse.urlencode(params)
        url = f"{BASE_URL}?{query}" if query else BASE_URL

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "IPO-Advisor-Local/0.3.1",
                "Accept": "application/json",
            },
            method="GET",
        )

        logger.info(
            "FINAPI_REQUEST status=%s type=%s url=%s raw_capture=%s",
            status, ipo_type, url, save_raw
        )

        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=25) as resp:
                    body_bytes = resp.read()
                    body_text = body_bytes.decode("utf-8", errors="replace")
                    http_status = getattr(resp, "status", 200)
                    headers = dict(resp.headers.items())

                    try:
                        payload = json.loads(body_text)
                    except Exception as exc:
                        logger.exception(
                            "FINAPI_JSON_PARSE_FAILED http=%s bytes=%s url=%s",
                            http_status, len(body_bytes), url
                        )
                        if save_raw:
                            save_raw_response(
                                diagnostic_label or f"{status}_{ipo_type}_parse_error",
                                url, http_status, headers, body_text
                            )
                        raise RuntimeError(f"FinAPI returned invalid JSON: {exc}") from exc

                    data = payload.get("data") or []
                    top_status = payload.get("status")
                    message = payload.get("message")
                    status_code = payload.get("statusCode")

                    logger.info(
                        "FINAPI_RESPONSE http=%s api_status=%r api_statusCode=%r message=%r "
                        "data_type=%s count=%s bytes=%s remaining_endpoint=%r remaining_global=%r "
                        "url=%s",
                        http_status, top_status, status_code, message,
                        type(payload.get("data")).__name__, len(data) if isinstance(data, list) else -1,
                        len(body_bytes),
                        resp.headers.get("X-RateLimit-Remaining-Endpoint"),
                        resp.headers.get("X-RateLimit-Remaining-Global"),
                        url,
                    )

                    if isinstance(data, list) and data:
                        first = data[0]
                        if isinstance(first, dict):
                            logger.info(
                                "FINAPI_FIRST_RECORD_KEYS status=%s type=%s keys=%s",
                                status, ipo_type, sorted(first.keys())
                            )
                            sample_statuses = sorted({
                                str(x.get("status"))
                                for x in data[:100]
                                if isinstance(x, dict) and x.get("status") is not None
                            })
                            logger.info(
                                "FINAPI_RECORD_STATUSES status=%s type=%s values=%s",
                                status, ipo_type, sample_statuses
                            )

                    if save_raw:
                        save_raw_response(
                            diagnostic_label or f"{status or 'NO_STATUS'}_{ipo_type or 'NO_TYPE'}",
                            url, http_status, headers, body_text
                        )

                    if top_status != "success":
                        raise RuntimeError(message or "FinAPI returned a non-success response")

                    return {
                        "url": url,
                        "http_status": http_status,
                        "api_status": top_status,
                        "api_status_code": status_code,
                        "message": message,
                        "response_bytes": len(body_bytes),
                        "rate_remaining_endpoint": resp.headers.get("X-RateLimit-Remaining-Endpoint"),
                        "rate_remaining_global": resp.headers.get("X-RateLimit-Remaining-Global"),
                        "data": data if isinstance(data, list) else [],
                        "top_level_keys": sorted(payload.keys()),
                    }

            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                logger.error(
                    "FINAPI_HTTP_ERROR attempt=%s code=%s reason=%r url=%s body_prefix=%r",
                    attempt + 1, exc.code, exc.reason, url, body[:1500]
                )
                if save_raw and body:
                    save_raw_response(
                        diagnostic_label or f"{status}_{ipo_type}_http_{exc.code}",
                        url, exc.code, dict(exc.headers.items()) if exc.headers else {}, body
                    )
                if exc.code == 429 and attempt < 2:
                    retry_after = exc.headers.get("Retry-After")
                    try:
                        wait = max(1.0, float(retry_after))
                    except Exception:
                        wait = (2 ** attempt) + random.random()
                    logger.warning("FINAPI_RATE_LIMIT retry_in=%.2fs", min(wait, 20))
                    time.sleep(min(wait, 20))
                    continue
                raise RuntimeError(f"FinAPI HTTP {exc.code}: {exc.reason}") from exc

            except Exception as exc:
                logger.exception(
                    "FINAPI_REQUEST_EXCEPTION attempt=%s status=%s type=%s url=%s",
                    attempt + 1, status, ipo_type, url
                )
                if attempt < 2:
                    wait = (2 ** attempt) + random.random()
                    logger.info("FINAPI_RETRY retry_in=%.2fs", wait)
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"FinAPI request failed: {exc}") from exc

        raise RuntimeError("FinAPI request failed after retries")
