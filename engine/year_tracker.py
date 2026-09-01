import json
import threading
import time
import urllib.error
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from db import (
    historical_market_rows,
    canonical_research_decisions,
    upsert_year_model_tracker,
    year_model_tracker_cache,
    year_model_tracker_rows,
    year_model_tracker_summary,
)
from historical_ipomarkets import (
    BASE, _canon, _fetch, parse_annual_page, fetch_pre1430_gmp,
)
from logging_utils import logger, save_json_report
from recommendation import ResearchDecisionEngine, POLICY_VERSION
from shadow_v2 import shadow_signal_from_v1, shadow_outcome, SHADOW_V2_VERSION
from model_audit import build_model_audit, log_model_audit, call_outcome_label
from prospective_tracker import build_prospective_experiment, log_prospective_experiment

IST = ZoneInfo("Asia/Kolkata")
_SYNC_LOCK = threading.Lock()

def _tracker_key(year, segment, name):
    return f"{int(year)}|{str(segment).upper()}|{_canon(name)}"

def _canon_decision_map():
    out = {}
    for r in canonical_research_decisions():
        key = _canon(r.get("name"))
        if key and key not in out:
            out[key] = r
    return out

def _outcome_vs_call(action, gain):
    return call_outcome_label(action, gain)

def _status_is_listed(row):
    return (
        row.get("listing_gain_pct") is not None
        or "listed" in str(row.get("status_text") or "").lower()
    )

def _usable_gmp(value, quality=None):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return False
    if abs(value) < 1e-12 and str(quality or "") != "VERIFIED_ZERO_GMP":
        return False
    return True

def _load_annual_rows(year):
    all_rows = []
    page_meta = []
    for page in range(1, 10):
        url = (
            f"{BASE}/ipo-calendar/{year}"
            if page == 1
            else f"{BASE}/ipo-calendar/{year}/page/{page}"
        )
        try:
            body, status, headers = _fetch(
                url, f"YEAR_TRACKER_INDEX_{year}_PAGE_{page}", timeout=20
            )
        except urllib.error.HTTPError as exc:
            # A 404 on a later pagination URL means there are no more
            # calendar pages. This is a normal end-of-pagination condition,
            # not a Daily Outcome Sync failure.
            if page > 1 and int(getattr(exc, "code", 0) or 0) == 404:
                logger.info(
                    "YEAR_TRACKER_PAGINATION_END "
                    "year=%s page=%s http=404 url=%s rows_so_far=%s",
                    year, page, url, len(all_rows),
                )
                page_meta.append({
                    "page": page,
                    "http_status": 404,
                    "rows": 0,
                    "url": url,
                    "pagination_end": True,
                })
                break
            raise

        parsed = parse_annual_page(
            body, archive_year=year, listed_only=False
        )
        page_meta.append({
            "page": page,
            "http_status": status,
            "rows": len(parsed),
            "url": url,
        })
        if not parsed:
            break
        all_rows.extend(parsed)
        if len(parsed) < 50:
            break

    dedup = {}
    for r in all_rows:
        key = _tracker_key(year, r.get("ipo_type"), r.get("name"))
        old = dedup.get(key)
        if not old:
            dedup[key] = r
        else:
            fields = (
                "listing_gain_pct", "total_x", "gmp_gain_pct",
                "issue_price", "detail_url"
            )
            if sum(r.get(f) is not None for f in fields) > sum(
                old.get(f) is not None for f in fields
            ):
                dedup[key] = r
    return list(dedup.values()), page_meta

def sync_year_tracker(year=2026, force_detail_refresh=False):
    if not _SYNC_LOCK.acquire(blocking=False):
        return {
            "year": year,
            "already_running": True,
            "message": "A 2026 tracker update is already running.",
            "summary": year_model_tracker_summary(year),
        }

    try:
        now = datetime.now(IST)
        logger.info(
            "YEAR_TRACKER_SYNC_START year=%s force_detail_refresh=%s at_ist=%s",
            year, force_detail_refresh, now.isoformat()
        )

        annual_rows, page_meta = _load_annual_rows(year)
        cache = year_model_tracker_cache(year)
        canonical = _canon_decision_map()
        engine = ResearchDecisionEngine(
            historical_market_rows(limit=20000)
        )

        output = []
        detail_fetches = 0
        detail_errors = 0
        canonical_used = 0
        proxy_gmp_used = 0
        sub_only_proxy = 0

        for idx, r in enumerate(annual_rows, 1):
            segment = r.get("ipo_type")
            name = r.get("name")
            key = _tracker_key(year, segment, name)
            existing = cache.get(key) or {}
            canonical_decision = canonical.get(_canon(name))

            gmp_used_pct = None
            gmp_used_rupees = None
            gmp_used_at_ist = None
            gmp_quality = None
            decision_source = None

            if canonical_decision:
                canonical_used += 1
                decision_source = "CAPTURED_1430_IST"
                raw_canonical_gmp = canonical_decision.get("gmp_input_pct")
                if _usable_gmp(raw_canonical_gmp, "EXACT_LOCAL_1430_CAPTURE"):
                    gmp_used_pct = raw_canonical_gmp
                    gmp_quality = "EXACT_LOCAL_1430_CAPTURE"
                else:
                    gmp_used_pct = None
                    gmp_quality = (
                        "EXACT_1430_GMP_ZERO_UNVERIFIED"
                        if raw_canonical_gmp is not None
                        else "EXACT_1430_GMP_NOT_AVAILABLE"
                    )
                total_x = canonical_decision.get("total_subscription_x")
                action = canonical_decision.get("action")
                confidence = canonical_decision.get("research_confidence")
                primary_pred = canonical_decision.get("primary_prediction_pct")
                gmp_pred = canonical_decision.get("gmp_prediction_pct")
                sub_pred = canonical_decision.get("subscription_prediction_pct")
                conflict = bool(canonical_decision.get("signal_conflict"))
                gmp_used_at_ist = canonical_decision.get("created_at_ist")
                try:
                    canonical_json = json.loads(
                        canonical_decision.get("recommendation_json") or "{}"
                    )
                except Exception:
                    canonical_json = {}
                v1_for_shadow = canonical_json or {
                    "segment": segment,
                    "action": action,
                    "predictions": {
                        "total_subscription_x": total_x,
                        "gmp_prediction_pct": gmp_pred,
                        "subscription_prediction_pct": sub_pred,
                    },
                }
            else:
                total_x = r.get("total_x")

                if _status_is_listed(r):
                    if (
                        not force_detail_refresh
                        and _usable_gmp(
                            existing.get("gmp_used_pct"),
                            existing.get("gmp_quality"),
                        )
                        and str(existing.get("decision_source") or "").startswith("RETROSPECTIVE")
                    ):
                        gmp_used_pct = existing.get("gmp_used_pct")
                        gmp_used_rupees = existing.get("gmp_used_rupees")
                        gmp_used_at_ist = existing.get("gmp_used_at_ist")
                        gmp_quality = existing.get("gmp_quality")
                    elif r.get("detail_url") and r.get("issue_close"):
                        try:
                            detail = fetch_pre1430_gmp(
                                r.get("detail_url"),
                                r.get("issue_close"),
                                save_raw=False,
                            )
                            detail_fetches += 1
                            selected = (detail or {}).get("selected")
                            if selected:
                                gmp_used_pct = selected.get("gmp_gain_pct")
                                gmp_used_rupees = selected.get("gmp_rupees")
                                gmp_used_at_ist = selected.get("at_ist")
                                gmp_quality = selected.get("quality")
                                proxy_gmp_used += 1
                            time.sleep(0.10)
                        except Exception as exc:
                            detail_errors += 1
                            logger.warning(
                                "YEAR_TRACKER_GMP_DETAIL_FAILED name=%r url=%r error=%r",
                                name, r.get("detail_url"), str(exc)
                            )

                    if gmp_used_pct is None and gmp_quality is None:
                        gmp_quality = "RETROSPECTIVE_GMP_NOT_AVAILABLE"

                    decision_source = (
                        "RETROSPECTIVE_PRE1430_PROXY"
                        if gmp_used_pct is not None
                        else "RETROSPECTIVE_SUBSCRIPTION_ONLY_PROXY"
                    )
                    if gmp_used_pct is None:
                        sub_only_proxy += 1
                else:
                    row_gmp_state = str(r.get("gmp_state") or "")
                    if (
                        row_gmp_state == "OBSERVED"
                        and _usable_gmp(
                            r.get("gmp_gain_pct"),
                            "CURRENT_PROVIDER_STATE",
                        )
                    ):
                        gmp_used_pct = r.get("gmp_gain_pct")
                        gmp_used_rupees = r.get("gmp_rupees")
                        gmp_quality = "CURRENT_PROVIDER_STATE"
                    else:
                        gmp_used_pct = None
                        gmp_used_rupees = None
                        gmp_quality = (
                            "CURRENT_GMP_ZERO_UNVERIFIED"
                            if row_gmp_state == "UNVERIFIED_ZERO"
                            else "CURRENT_GMP_NOT_AVAILABLE"
                        )
                    decision_source = "CURRENT_DAILY_SIGNAL"

                recommendation = engine.classify_proxy(
                    segment,
                    gmp_gain_pct=gmp_used_pct,
                    total_x=total_x,
                )
                action = recommendation.get("action")
                confidence = recommendation.get("research_confidence")
                primary_pred = recommendation.get("primary_prediction_pct")
                preds = recommendation.get("predictions") or {}
                gmp_pred = preds.get("gmp_prediction_pct")
                sub_pred = preds.get("subscription_prediction_pct")
                conflict = bool(recommendation.get("signal_conflict"))
                v1_for_shadow = recommendation

            shadow_v2 = shadow_signal_from_v1(v1_for_shadow)
            actual_gain = r.get("listing_gain_pct")
            outcome = _outcome_vs_call(action, actual_gain)
            shadow_v2_result = shadow_outcome(shadow_v2, actual_gain)

            item = {
                "tracker_key": key,
                "year": int(year),
                "ipo_type": segment,
                "name": name,
                "detail_url": r.get("detail_url"),
                "provider_status": r.get("status_text"),
                "issue_open": r.get("issue_open"),
                "issue_close": r.get("issue_close"),
                "issue_price": r.get("issue_price"),
                "total_x": total_x,
                "gmp_used_pct": gmp_used_pct,
                "gmp_used_rupees": gmp_used_rupees,
                "gmp_used_at_ist": gmp_used_at_ist,
                "gmp_quality": gmp_quality,
                "decision_source": decision_source,
                "model_policy_version": POLICY_VERSION,
                "model_action": action,
                "model_confidence": confidence,
                "primary_prediction_pct": primary_pred,
                "gmp_prediction_pct": gmp_pred,
                "subscription_prediction_pct": sub_pred,
                "signal_conflict": int(conflict),
                "listing_price": r.get("listing_price"),
                "actual_listing_gain_pct": actual_gain,
                "outcome_vs_call": outcome,
                "shadow_v2_version": SHADOW_V2_VERSION,
                "shadow_v2_triggered": int(bool(shadow_v2.get("triggered"))),
                "shadow_v2_action": shadow_v2.get("shadow_action"),
                "shadow_v2_strength": shadow_v2.get("strength"),
                "shadow_v2_outcome": shadow_v2_result,
                "shadow_v2_reason": json.dumps(
                    shadow_v2.get("reason") or [], ensure_ascii=False
                ),
                "last_updated_ist": now.isoformat(),
                "raw_json": json.dumps({
                    "annual_row": r,
                    "decision_source": decision_source,
                    "gmp_quality": gmp_quality,
                    "gmp_state": r.get("gmp_state"),
                }, ensure_ascii=False),
            }
            output.append(item)

            logger.info(
                "YEAR_TRACKER_ROW idx=%s/%s name=%r segment=%s status=%r "
                "source=%s action=%s confidence=%s primary_pred=%s "
                "gmp_used=%s gmp_at=%s total_x=%s actual_gain=%s outcome=%s "
                "shadow_v2=%s shadow_triggered=%s shadow_outcome=%s",
                idx, len(annual_rows), name, segment, r.get("status_text"),
                decision_source, action, confidence, primary_pred,
                gmp_used_pct, gmp_used_at_ist, total_x,
                actual_gain, outcome,
                shadow_v2.get("shadow_action"),
                shadow_v2.get("triggered"),
                shadow_v2_result,
            )

        upsert_year_model_tracker(output)
        summary = year_model_tracker_summary(year)

        tracker_rows = year_model_tracker_rows(year=year, limit=5000)
        audit = build_model_audit(tracker_rows, year=year)
        log_model_audit(audit, logger)
        audit_report = save_json_report(
            f"MODEL_AUDIT_{year}", audit
        )

        prospective = build_prospective_experiment(
            canonical_research_decisions(),
            tracker_rows,
            year=year,
        )
        log_prospective_experiment(prospective, logger)
        prospective_report = save_json_report(
            f"PROSPECTIVE_EXPERIMENT_{year}", prospective
        )

        result = {
            "year": year,
            "synced_at_ist": now.isoformat(),
            "records": len(output),
            "page_meta": page_meta,
            "detail_fetches": detail_fetches,
            "detail_errors": detail_errors,
            "canonical_1430_used": canonical_used,
            "retrospective_gmp_used": proxy_gmp_used,
            "subscription_only_proxy": sub_only_proxy,
            "summary": summary,
            "audit_summary": {
                "policy": audit.get("policy"),
                "prospective_validation": audit.get("prospective_validation"),
                "overall_opportunity_efficiency": (
                    (audit.get("overall") or {}).get("opportunity_efficiency")
                ),
                "major_miss_count": len(
                    (audit.get("misses") or {}).get(
                        "major_avoid_misses_ge_20", []
                    )
                ),
                "selected_loss_count": len(
                    (audit.get("misses") or {}).get(
                        "selected_losses", []
                    )
                ),
            },
            "audit_report_file": str(audit_report),
            "prospective_summary": {
                "status": prospective.get("status"),
                "progress": prospective.get("progress"),
                "v1_exact_performance": prospective.get("v1_exact_performance"),
                "v2_exact_performance": prospective.get("v2_exact_performance"),
            },
            "prospective_report_file": str(prospective_report),
        }
        report = save_json_report(
            f"YEAR_TRACKER_{year}", result
        )
        result["report_file"] = str(report)
        logger.info("YEAR_TRACKER_SYNC_DONE %s", result)
        return result
    finally:
        _SYNC_LOCK.release()

class DailyYearTracker:
    def __init__(self, callback, year=2026):
        self.callback = callback
        self.year = year
        self.enabled = True
        self.running = False
        self.last_run = None
        self.next_run = None
        self.last_error = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, daemon=True
        )

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def state(self):
        return {
            "enabled": self.enabled,
            "running": self.running,
            "last_run_ist": (
                self.last_run.isoformat() if self.last_run else None
            ),
            "next_run_ist": (
                self.next_run.isoformat() if self.next_run else None
            ),
            "last_error": self.last_error,
            "schedule": (
                "Daily at 6:00 PM IST + catch-up on startup if stale"
            ),
        }

    def _run(self, reason):
        if self.running or not self.enabled:
            return
        self.running = True
        logger.info(
            "YEAR_TRACKER_SCHEDULE_RUN reason=%s", reason
        )
        try:
            result = self.callback()
            if not result.get("already_running"):
                self.last_run = datetime.now(IST)
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)
            logger.exception(
                "YEAR_TRACKER_SCHEDULE_FAILED reason=%s", reason
            )
        finally:
            self.running = False

    def _next_1800(self, now):
        target = now.replace(
            hour=18, minute=0, second=0, microsecond=0
        )
        if target <= now:
            target += timedelta(days=1)
        return target

    def _loop(self):
        try:
            summary = year_model_tracker_summary(self.year)
            last = summary.get("last_updated_ist")
            stale = True
            if last:
                try:
                    stale = (
                        datetime.fromisoformat(last)
                        .astimezone(IST).date()
                        != datetime.now(IST).date()
                    )
                except Exception:
                    stale = True
            if stale:
                if self._stop.wait(5):
                    return
                self._run("startup-catchup")
        except Exception:
            logger.exception(
                "YEAR_TRACKER_STALE_CHECK_FAILED"
            )

        while not self._stop.is_set():
            now = datetime.now(IST)
            target = self._next_1800(now)
            self.next_run = target
            wait = max(
                1, (target - now).total_seconds()
            )
            if self._stop.wait(min(wait, 60)):
                return
            if wait > 60:
                continue
            self._run("daily-1800")
            self._stop.wait(70)
