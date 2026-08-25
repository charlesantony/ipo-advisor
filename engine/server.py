#!/usr/bin/env python3
import json
import mimetypes
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from collector import AutoCollector
from db import (
    save_snapshots, recent_snapshots, dataset_summary, previous_snapshot,
    latest_subscription_snapshot, latest_gmp_snapshot,
    upsert_historical, historical_rows, upsert_historical_web, historical_web_rows,
    upsert_historical_gmp, historical_gmp_rows,
    upsert_historical_market, historical_market_rows, market_integrity_summary,
    save_research_decisions, recent_research_decisions,
    year_model_tracker_rows, year_model_tracker_summary,
    canonical_research_decisions
)
from features import derive_static_features, derive_velocity
from model import benchmark, loocv, chronological_holdout
from normalizer import normalize_ipo, field_readiness
from outcomes import extract_listing_outcome
from historical_web import fetch_table as fetch_historical_web_table
from historical_ipomarkets import fetch_year_archive, parser_self_test
from recommendation import ResearchDecisionEngine, POLICY_VERSION
from year_tracker import sync_year_tracker, DailyYearTracker
from model_audit import build_model_audit, log_model_audit, AUDIT_VERSION
from shadow_v2 import (
    SHADOW_V2_VERSION, audit_tracker_shadow,
    historical_crosscheck_2025, threshold_grid_2025,
)
from prospective_tracker import (
    PROSPECTIVE_VERSION, EXACT_LISTED_TARGET,
    build_prospective_experiment, log_prospective_experiment,
)
from providers.finapi import FinAPIProvider
from gmp_fallback import validate_or_fill_gmp
from subscription_fallback import (
    validate_or_fill_subscription,
    enforce_canonical_subscription_freshness,
)
from logging_utils import logger, LOG_FILE, RAW_DIR, tail_log, clear_log, save_json_report, list_reports

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
IST = ZoneInfo("Asia/Kolkata")
provider = FinAPIProvider()
collector = None
daily_year_tracker = None
logger.info("APP_MODULE_LOADED version=0.4.4")

def now_pair():
    utc = datetime.now(timezone.utc)
    ist = utc.astimezone(IST)
    return utc, ist

def is_decision_window(ist):
    return ist.hour == 14 and 28 <= ist.minute <= 32

def enrich(n, utc, ist):
    n["readiness"] = field_readiness(n)
    n["features"] = derive_static_features(n)
    prev = previous_snapshot(n.get("symbol"))
    current_for_velocity = dict(n)
    current_for_velocity["fetched_at_utc"] = utc.isoformat()
    n["velocity"] = derive_velocity(current_for_velocity, prev)
    n["is_closing_today"] = n.get("end_date") == ist.date().isoformat()
    n["would_be_1430_decision_snapshot"] = (
        n["is_closing_today"] and is_decision_window(ist)
    )
    return n

def fetch_normalized(status="LIVE", ipo_type="ALL"):
    types = ["MAINBOARD", "SME"] if ipo_type.upper() == "ALL" else [ipo_type.upper()]
    records, meta, errors = [], [], []
    utc, ist = now_pair()

    research_engine = ResearchDecisionEngine(
        historical_market_rows(limit=20000)
    )

    for t in types:
        try:
            result = provider.fetch_ipos(status=status, ipo_type=t)
            normalized = []
            for raw in result["data"]:
                n = normalize_ipo(raw)

                previous_gmp = latest_gmp_snapshot(
                    symbol=n.get("symbol"),
                    name=n.get("name"),
                    end_date=n.get("end_date"),
                )
                n = validate_or_fill_gmp(
                    n,
                    previous=previous_gmp,
                    now_ist=ist,
                )

                previous_sub = latest_subscription_snapshot(
                    symbol=n.get("symbol"),
                    name=n.get("name"),
                    end_date=n.get("end_date"),
                )
                n = validate_or_fill_subscription(
                    n,
                    previous=previous_sub,
                    now_ist=ist,
                )

                n = enrich(n, utc, ist)
                recommendation = research_engine.recommend(n, ist)
                recommendation = enforce_canonical_subscription_freshness(
                    n, recommendation, ist
                )
                n["recommendation"] = research_engine.attach_shadow_v2(
                    recommendation
                )
                normalized.append(n)
                rec = n.get("recommendation") or {}
                logger.info(
                    "RESEARCH_RECOMMENDATION symbol=%r name=%r segment=%s status=%s "
                    "closing_today=%s policy=%s action=%r confidence=%s finality=%s "
                    "primary_pred=%s gmp_input=%s total_x=%s gmp_pred=%s sub_pred=%s "
                    "sub_source=%s sub_age_min=%s "
                    "conflict=%s shadow_v2=%s shadow_triggered=%s reasons=%s",
                    n.get("symbol"), n.get("name"), n.get("type"), n.get("status"),
                    n.get("is_closing_today"), rec.get("policy_version"),
                    rec.get("action"), rec.get("research_confidence"),
                    (rec.get("finality") or {}).get("code"),
                    rec.get("primary_prediction_pct"),
                    ((rec.get("predictions") or {}).get("gmp_input_pct")),
                    ((rec.get("predictions") or {}).get("total_subscription_x")),
                    ((rec.get("predictions") or {}).get("gmp_prediction_pct")),
                    ((rec.get("predictions") or {}).get("subscription_prediction_pct")),
                    ((n.get("subscription_validation") or {}).get("source_kind")),
                    ((n.get("subscription_validation") or {}).get("age_minutes")),
                    rec.get("signal_conflict"),
                    ((rec.get("shadow_v2") or {}).get("shadow_action")),
                    ((rec.get("shadow_v2") or {}).get("triggered")),
                    rec.get("reason"),
                )
            records.extend(normalized)
            meta.append({
                "type": t,
                "url": result["url"],
                "http_status": result["http_status"],
                "api_status": result.get("api_status"),
                "api_status_code": result.get("api_status_code"),
                "message": result.get("message"),
                "response_bytes": result.get("response_bytes"),
                "rate_remaining_endpoint": result["rate_remaining_endpoint"],
                "rate_remaining_global": result["rate_remaining_global"],
                "count": len(normalized),
            })
        except Exception as exc:
            errors.append({"type": t, "error": str(exc)})

    return {
        "fetched_at_utc": utc.isoformat(),
        "fetched_at_ist": ist.isoformat(),
        "status_filter": status,
        "type_filter": ipo_type,
        "records": records,
        "source_meta": meta,
        "errors": errors,
    }

def capture_live(reason="manual"):
    payload = fetch_normalized(status="LIVE", ipo_type="ALL")
    if payload["records"]:
        ist = datetime.fromisoformat(payload["fetched_at_ist"])

        # The dedicated scheduled 2:30 workflow is the prospective checkpoint.
        # GitHub/network delay is outside our control, so do not reject the
        # observation just because processing completed after a wall-clock
        # cutoff. Keep the actual capture timestamp for audit.
        workflow_checkpoint = reason == "github_action_1430"
        if workflow_checkpoint:
            for n in payload["records"]:
                if not n.get("is_closing_today"):
                    continue

                rec = n.get("recommendation") or {}
                rec = enforce_canonical_subscription_freshness(
                    n,
                    rec,
                    ist,
                    force_checkpoint=True,
                )

                if rec.get("action") != "NOT READY":
                    rec["finality"] = {
                        "canonical": True,
                        "code": "CANONICAL_1430_WORKFLOW",
                        "label": "2:30 PM WORKFLOW CHECKPOINT RESEARCH DECISION",
                        "captured_at_ist": payload["fetched_at_ist"],
                    }
                    rec["display_decision_source"] = "CAPTURED_1430_WORKFLOW"
                    n["would_be_1430_decision_snapshot"] = True
                else:
                    n["would_be_1430_decision_snapshot"] = False

                n["recommendation"] = rec

        save_snapshots(
            payload["records"],
            payload["fetched_at_utc"],
            payload["fetched_at_ist"],
            source="finapi",
            capture_reason=reason,
            local_date=ist.date().isoformat(),
            decision_window=workflow_checkpoint or is_decision_window(ist),
        )
        payload["decision_saved_count"] = save_research_decisions(
            payload["records"],
            payload["fetched_at_utc"],
            payload["fetched_at_ist"],
            source="finapi",
            capture_reason=reason,
        )
    else:
        payload["decision_saved_count"] = 0
    payload["saved_count"] = len(payload["records"])
    payload["capture_reason"] = reason
    return payload


def _status_counts(records):
    counts = {}
    for x in records or []:
        if not isinstance(x, dict):
            continue
        s = str(x.get("status") or "<missing>")
        counts[s] = counts.get(s, 0) + 1
    return counts

def import_recent_closed_history():
    utc, ist = now_pair()
    logger.info("RECENT_CLOSED_IMPORT_START at_ist=%s", ist.isoformat())

    all_records = []
    meta = []
    errors = []
    diagnostics = []

    for t in ("MAINBOARD", "SME"):
        try:
            result = provider.fetch_ipos(
                status="CLOSED",
                ipo_type=t,
                save_raw=True,
                diagnostic_label=f"IMPORT_CLOSED_{t}"
            )
            logger.info(
                "RECENT_CLOSED_RESULT type=%s count=%s message=%r api_statusCode=%r",
                t, len(result["data"]), result.get("message"), result.get("api_status_code")
            )

            normalized = []
            for raw in result["data"]:
                n = normalize_ipo(raw)
                n["features"] = derive_static_features(n)
                n["outcome"] = extract_listing_outcome(raw, n)
                normalized.append(n)

            all_records.extend(normalized)
            item_meta = {
                "type": t,
                "http_status": result["http_status"],
                "api_status": result.get("api_status"),
                "api_status_code": result.get("api_status_code"),
                "message": result.get("message"),
                "response_bytes": result.get("response_bytes"),
                "count": len(normalized),
            }

            # If LISTED returns zero, perform one low-cost unfiltered probe.
            # This helps reveal the provider's actual status values without guessing.
            if len(result["data"]) == 0:
                logger.warning(
                    "RECENT_CLOSED_ZERO type=%s; running unfiltered status diagnostic",
                    t
                )
                try:
                    probe = provider.fetch_ipos(
                        status=None,
                        ipo_type=t,
                        save_raw=True,
                        diagnostic_label=f"DIAGNOSTIC_NO_STATUS_{t}"
                    )
                    counts = _status_counts(probe["data"])
                    sample = []
                    for x in probe["data"][:10]:
                        if isinstance(x, dict):
                            sample.append({
                                "symbol": x.get("symbol"),
                                "name": x.get("name"),
                                "status": x.get("status"),
                                "listingDate": (x.get("schedule") or {}).get("listingDate"),
                            })
                    diag = {
                        "type": t,
                        "listed_count": 0,
                        "unfiltered_probe_count": len(probe["data"]),
                        "unfiltered_api_message": probe.get("message"),
                        "status_counts_seen": counts,
                        "sample_records": sample,
                    }
                    diagnostics.append(diag)
                    item_meta["zero_result_probe"] = diag
                    logger.warning(
                        "HISTORICAL_ZERO_DIAGNOSTIC type=%s probe_count=%s status_counts=%s sample=%s",
                        t, len(probe["data"]), counts, sample
                    )
                except Exception as probe_exc:
                    diagnostics.append({
                        "type": t,
                        "listed_count": 0,
                        "probe_error": str(probe_exc),
                    })
                    logger.exception(
                        "HISTORICAL_ZERO_DIAGNOSTIC_FAILED type=%s", t
                    )

            meta.append(item_meta)

        except Exception as exc:
            errors.append({"type": t, "error": str(exc)})
            logger.exception("HISTORICAL_IMPORT_TYPE_FAILED type=%s", t)

    changed = 0
    if all_records:
        changed = upsert_historical(
            all_records,
            imported_at_ist=ist.isoformat(),
            source="finapi_listed",
        )

    target_count = sum(
        1 for r in all_records
        if (r.get("outcome") or {}).get("listing_gain_pct") is not None
    )

    result_payload = {
        "imported_at_ist": ist.isoformat(),
        "records_received": len(all_records),
        "rows_written_or_updated": changed,
        "records_with_listing_target": target_count,
        "source_meta": meta,
        "errors": errors,
        "diagnostics": diagnostics,
        "log_file": str(LOG_FILE.relative_to(ROOT)),
        "raw_response_folder": str(RAW_DIR.relative_to(ROOT)),
        "sample_outcome_diagnostics": [
            {
                "symbol": r.get("symbol"),
                "name": r.get("name"),
                "type": r.get("type"),
                "listing_date": r.get("listing_date"),
                "outcome": r.get("outcome"),
            }
            for r in all_records[:5]
        ],
    }

    logger.info(
        "RECENT_CLOSED_IMPORT_DONE records=%s targets=%s db_changes=%s errors=%s diagnostics=%s",
        len(all_records), target_count, changed, errors, diagnostics
    )
    return result_payload

def run_listed_diagnostic():
    _, ist = now_pair()
    logger.info("MANUAL_LISTED_DIAGNOSTIC_START at_ist=%s", ist.isoformat())
    results = []

    for t in ("MAINBOARD", "SME"):
        entry = {"type": t}
        try:
            listed = provider.fetch_ipos(
                status="CLOSED",
                ipo_type=t,
                save_raw=True,
                diagnostic_label=f"MANUAL_LISTED_{t}"
            )
            entry["listed"] = {
                "http_status": listed.get("http_status"),
                "api_status": listed.get("api_status"),
                "api_status_code": listed.get("api_status_code"),
                "message": listed.get("message"),
                "count": len(listed.get("data") or []),
                "status_counts": _status_counts(listed.get("data") or []),
            }
        except Exception as exc:
            entry["listed_error"] = str(exc)
            logger.exception("MANUAL_LISTED_DIAGNOSTIC_LISTED_FAILED type=%s", t)

        try:
            probe = provider.fetch_ipos(
                status=None,
                ipo_type=t,
                save_raw=True,
                diagnostic_label=f"MANUAL_NO_STATUS_{t}"
            )
            entry["no_status_probe"] = {
                "http_status": probe.get("http_status"),
                "api_status": probe.get("api_status"),
                "api_status_code": probe.get("api_status_code"),
                "message": probe.get("message"),
                "count": len(probe.get("data") or []),
                "status_counts": _status_counts(probe.get("data") or []),
                "samples": [
                    {
                        "symbol": x.get("symbol"),
                        "name": x.get("name"),
                        "status": x.get("status"),
                        "listingDate": (x.get("schedule") or {}).get("listingDate"),
                    }
                    for x in (probe.get("data") or [])[:10]
                    if isinstance(x, dict)
                ],
            }
        except Exception as exc:
            entry["no_status_probe_error"] = str(exc)
            logger.exception("MANUAL_LISTED_DIAGNOSTIC_PROBE_FAILED type=%s", t)

        results.append(entry)

    logger.info("MANUAL_LISTED_DIAGNOSTIC_DONE results=%s", results)
    return {
        "run_at_ist": ist.isoformat(),
        "results": results,
        "log_file": str(LOG_FILE.relative_to(ROOT)),
        "raw_response_folder": str(RAW_DIR.relative_to(ROOT)),
    }

def import_public_historical_web():
    _, ist = now_pair()
    logger.info("PUBLIC_HISTORICAL_IMPORT_START at_ist=%s", ist.isoformat())
    all_records = []
    meta = []
    errors = []

    for segment in ("MAINBOARD", "SME"):
        try:
            records, source_meta = fetch_historical_web_table(segment)
            all_records.extend(records)
            meta.append(source_meta)
        except Exception as exc:
            errors.append({"type": segment, "error": str(exc)})
            logger.exception("PUBLIC_HISTORICAL_IMPORT_FAILED segment=%s", segment)

    changed = 0
    if all_records:
        changed = upsert_historical_web(
            all_records,
            imported_at_ist=ist.isoformat(),
            source="ipodhan_web",
        )

    targets = sum(1 for r in all_records if r.get("listing_gain_pct") is not None)
    gmp_rows = sum(1 for r in all_records if r.get("gmp_gain_pct") is not None)
    complete_core = sum(
        1 for r in all_records
        if all(r.get(k) is not None for k in (
            "listing_gain_pct", "gmp_gain_pct", "qib_x", "nii_x",
            "retail_x", "total_x", "issue_size_cr"
        ))
    )

    logger.info(
        "PUBLIC_HISTORICAL_IMPORT_DONE records=%s targets=%s gmp_rows=%s complete_core=%s changes=%s errors=%s",
        len(all_records), targets, gmp_rows, complete_core, changed, errors
    )
    return {
        "imported_at_ist": ist.isoformat(),
        "records_received": len(all_records),
        "rows_written_or_updated": changed,
        "records_with_listing_target": targets,
        "records_with_gmp": gmp_rows,
        "complete_combined_rows": complete_core,
        "source_meta": meta,
        "errors": errors,
        "warning": (
            "Historical web data is a provisional training source. "
            "Live 2:30 PM decisions will use timestamped local data, and final outcome validation "
            "should be cross-checked against exchange data."
        ),
    }



def _historical_cache_map():
    rows = historical_market_rows(limit=20000)
    cache = {}
    for r in rows:
        key = (
            int(r.get("year") or 0),
            str(r.get("ipo_type") or "").upper(),
            _canon_record_name(r.get("name")),
        )
        cache[key] = r
    return cache

def import_market_year_history(year, target_gmp_per_segment=25):
    _, ist = now_pair()
    year = int(year)
    logger.info(
        "MARKET_YEAR_IMPORT_START year=%s target_gmp_per_segment=%s at_ist=%s",
        year, target_gmp_per_segment, ist.isoformat()
    )
    try:
        cache = _historical_cache_map()
        records, meta = fetch_year_archive(
            year=year,
            target_gmp_per_segment=target_gmp_per_segment,
            max_pages=10,
            cached_records=cache,
        )
        changed = upsert_historical_market(
            records,
            imported_at_ist=ist.isoformat(),
            source=f"ipomarkets_{year}",
        )
        payload = {
            "year": year,
            "records_received": len(records),
            "listing_targets": sum(
                1 for r in records if r.get("listing_gain_pct") is not None
            ),
            "total_subscription_rows": sum(
                1 for r in records
                if r.get("listing_gain_pct") is not None
                and r.get("total_x") is not None
            ),
            "gmp_complete_rows": sum(
                1 for r in records
                if r.get("listing_gain_pct") is not None
                and r.get("gmp_gain_pct") is not None
            ),
            "gmp_mainboard_rows": sum(
                1 for r in records
                if r.get("ipo_type") == "MAINBOARD"
                and r.get("listing_gain_pct") is not None
                and r.get("gmp_gain_pct") is not None
            ),
            "gmp_sme_rows": sum(
                1 for r in records
                if r.get("ipo_type") == "SME"
                and r.get("listing_gain_pct") is not None
                and r.get("gmp_gain_pct") is not None
            ),
            "rows_written_or_updated": changed,
            "meta": meta,
            "errors": [],
        }
        logger.info(
            "MARKET_YEAR_IMPORT_DONE year=%s payload=%s",
            year, payload
        )
        return payload
    except Exception as exc:
        logger.exception("MARKET_YEAR_IMPORT_FAILED year=%s", year)
        return {
            "year": year,
            "records_received": 0,
            "listing_targets": 0,
            "total_subscription_rows": 0,
            "gmp_complete_rows": 0,
            "gmp_mainboard_rows": 0,
            "gmp_sme_rows": 0,
            "rows_written_or_updated": 0,
            "meta": {},
            "errors": [{
                "type": f"IPOMARKETS_{year}",
                "error": str(exc)
            }],
        }

def import_all_backtest_history():
    integrity_before = market_integrity_summary()
    logger.info("DATA_INTEGRITY_BEFORE_IMPORT %s", integrity_before)

    # 25 GMP observations per segment per year => target ~50 unique GMP
    # observations per segment across 2024+2025.
    market_2024 = import_market_year_history(
        2024, target_gmp_per_segment=25
    )
    market_2025 = import_market_year_history(
        2025, target_gmp_per_segment=25
    )

    # Keep the current category-level source for future QIB/NII/Retail research.
    public_2026 = import_public_historical_web()

    integrity_after = market_integrity_summary()
    logger.info("DATA_INTEGRITY_AFTER_IMPORT %s", integrity_after)

    payload = {
        "market_2024": market_2024,
        "market_2025": market_2025,
        "public_2026": public_2026,
        "data_integrity_before": integrity_before,
        "data_integrity_after": integrity_after,
        "errors": (
            (market_2024.get("errors") or [])
            + (market_2025.get("errors") or [])
            + (public_2026.get("errors") or [])
        ),
    }

    market_rows = historical_market_rows(limit=20000)
    category_rows = historical_web_rows(limit=5000)

    payload["post_import_dataset_summary"] = {
        "market_all_years": {
            "total_rows": len(market_rows),
            "years": {
                "2024": sum(1 for r in market_rows if int(r.get("year") or 0) == 2024),
                "2025": sum(1 for r in market_rows if int(r.get("year") or 0) == 2025),
            },
            "mainboard": _segment_summary(market_rows, "MAINBOARD"),
            "sme": _segment_summary(market_rows, "SME"),
        },
        "category_history": {
            "total_rows": len(category_rows),
            "mainboard": _segment_summary(category_rows, "MAINBOARD"),
            "sme": _segment_summary(category_rows, "SME"),
        },
    }

    logger.info(
        "IMPORT_ALL_BACKTEST_DONE market_2024=%s market_2025=%s "
        "public_2026=%s errors=%s",
        market_2024, market_2025, public_2026, payload["errors"]
    )
    logger.info(
        "IMPORT_DATASET_COVERAGE %s",
        payload["post_import_dataset_summary"]
    )
    report = save_json_report("IMPORT_FULL", payload)
    payload["report_file"] = str(report.relative_to(ROOT))
    return payload

def _segment_year(rows, segment, year):
    return [
        r for r in rows
        if str(r.get("ipo_type") or "").upper() == segment
        and int(r.get("year") or 0) == int(year)
    ]

def run_layered_backtest():
    market_rows_raw = historical_market_rows(limit=20000)
    market_rows = _dedupe_market_rows(market_rows_raw)
    category_rows = historical_web_rows(limit=5000)

    integrity = market_integrity_summary()
    logger.info(
        "BACKTEST_DATA_INTEGRITY database=%s raw_market_rows=%s "
        "deduped_market_rows=%s",
        integrity, len(market_rows_raw), len(market_rows)
    )

    primary_holdout = {}
    secondary_loocv = {}

    for segment in ("MAINBOARD", "SME"):
        train_2024 = _segment_year(market_rows, segment, 2024)
        test_2025 = _segment_year(market_rows, segment, 2025)
        m2025 = test_2025
        cseg = [
            r for r in category_rows
            if str(r.get("ipo_type", "")).upper() == segment
        ]

        cap = 90.0 if segment == "SME" else None

        primary_holdout[segment] = {
            "train_year": 2024,
            "test_year": 2025,
            "train_source_rows": len(train_2024),
            "test_source_rows": len(test_2025),
            "prediction_cap": cap,
            "models": {
                "GMP only": chronological_holdout(
                    train_2024, test_2025,
                    ["gmp_gain_pct"],
                    prediction_cap=cap,
                    thresholds=(10.0, 20.0),
                ),
                "Total subscription only": chronological_holdout(
                    train_2024, test_2025,
                    ["log_total"],
                    prediction_cap=cap,
                    thresholds=(10.0, 20.0),
                ),
                "GMP + total subscription": chronological_holdout(
                    train_2024, test_2025,
                    ["gmp_gain_pct", "log_total"],
                    prediction_cap=cap,
                    thresholds=(10.0, 20.0),
                ),
            },
        }

        secondary_loocv[segment] = {
            "market_source_rows": len(m2025),
            "category_source_rows": len(cseg),
            "models": {
                "GMP only — 2025 LOOCV": loocv(
                    m2025, ["gmp_gain_pct"], prediction_cap=cap
                ),
                "Total subscription only — 2025 LOOCV": loocv(
                    m2025, ["log_total"], prediction_cap=cap
                ),
                "GMP + total subscription — 2025 LOOCV": loocv(
                    m2025, ["gmp_gain_pct", "log_total"],
                    prediction_cap=cap
                ),
                "Category demand only — current sparse history": loocv(
                    cseg, ["log_qib", "log_nii", "log_retail", "log_total"]
                ),
                "Full combined — current sparse history": loocv(
                    cseg, [
                        "gmp_gain_pct", "log_qib", "log_nii",
                        "log_retail", "log_total", "log_issue_size"
                    ]
                ),
            },
        }

    payload = {
        "warning": (
            "PRIMARY validation trains only on 2024 and predicts 2025. "
            "Historical GMP/subscription observations are still proxy/final values, "
            "not exact 2:30 PM snapshots. SME predictions are capped at 90%. "
            "2025 LOOCV remains only a secondary diagnostic."
        ),
        "data_integrity": {
            "database": integrity,
            "raw_market_rows": len(market_rows_raw),
            "deduped_market_rows": len(market_rows),
            "in_memory_duplicates_removed": (
                len(market_rows_raw) - len(market_rows)
            ),
        },
        "dataset_summary": {
            "market_all_years": {
                "total_rows": len(market_rows),
                "year_2024_rows": sum(
                    1 for r in market_rows
                    if int(r.get("year") or 0) == 2024
                ),
                "year_2025_rows": sum(
                    1 for r in market_rows
                    if int(r.get("year") or 0) == 2025
                ),
                "mainboard": _segment_summary(
                    market_rows, "MAINBOARD"
                ),
                "sme": _segment_summary(market_rows, "SME"),
            },
            "category_history": {
                "total_rows": len(category_rows),
                "mainboard": _segment_summary(
                    category_rows, "MAINBOARD"
                ),
                "sme": _segment_summary(
                    category_rows, "SME"
                ),
            },
        },
        "primary_holdout": primary_holdout,
        "secondary_loocv": secondary_loocv,
        # compatibility alias for older report readers
        "benchmark": secondary_loocv,
    }

    log_backtest_interpretation_payload(payload)
    report = save_json_report("BACKTEST_FULL", payload)
    payload["report_file"] = str(report.relative_to(ROOT))
    return payload


def _canon_record_name(value):
    import re
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())

def _dedupe_market_rows(rows):
    """
    Backtest safety net. Even if an older/malformed database somehow contains
    duplicates, a company appears only once per source/year/segment.
    """
    groups = {}
    for r in rows:
        key = (
            r.get("source"),
            r.get("year"),
            str(r.get("ipo_type") or "").upper(),
            _canon_record_name(r.get("name")),
        )
        groups.setdefault(key, []).append(r)

    def score(r):
        return sum(
            r.get(f) is not None
            for f in (
                "listing_gain_pct", "gmp_gain_pct", "total_x",
                "issue_price", "listing_price", "issue_close",
                "listing_open", "listing_close", "qib_x", "nii_x", "retail_x",
            )
        )

    result = []
    for group in groups.values():
        result.append(max(group, key=score))
    return result

def _field_coverage(rows, fields):
    out = {}
    total = len(rows)
    for f in fields:
        present = sum(1 for r in rows if r.get(f) is not None)
        out[f] = {
            "present": present,
            "missing": total - present,
            "coverage_pct": round((present / total * 100.0), 1) if total else 0.0,
        }
    return out

def _segment_summary(rows, segment):
    seg = [r for r in rows if str(r.get("ipo_type", "")).upper() == segment]
    return {
        "segment": segment,
        "rows": len(seg),
        "field_coverage": _field_coverage(
            seg,
            [
                "listing_gain_pct", "gmp_gain_pct", "total_x",
                "qib_x", "nii_x", "retail_x", "issue_price",
                "listing_price", "listing_open", "listing_close",
            ],
        ),
        "positive_listing_count": sum(
            1 for r in seg if r.get("listing_gain_pct") is not None and float(r["listing_gain_pct"]) > 0
        ),
        "negative_or_flat_listing_count": sum(
            1 for r in seg if r.get("listing_gain_pct") is not None and float(r["listing_gain_pct"]) <= 0
        ),
    }

def _log_model_metrics(prefix, segment, model_name, metrics):
    if not metrics.get("ready"):
        logger.warning(
            "%s_MODEL_NOT_READY segment=%s model=%r n=%s reason=%r details=%s",
            prefix, segment, model_name, metrics.get("n"),
            metrics.get("reason"), metrics
        )
        return

    logger.info(
        "%s_MODEL_RESULT segment=%s model=%r n=%s features=%s "
        "mae=%s rmse=%s direction_hit=%s correlation=%s "
        "baseline_positive=%s baseline_avg_gain=%s "
        "false_positive=%s false_negative=%s prediction_cap=%s "
        "capped_predictions=%s threshold_analysis=%s "
        "actual_distribution=%s predicted_distribution=%s",
        prefix, segment, model_name, metrics.get("n"),
        metrics.get("features"), metrics.get("mae"),
        metrics.get("rmse"), metrics.get("sign_hit_rate_pct"),
        metrics.get("correlation"),
        metrics.get("baseline_positive_rate_pct"),
        metrics.get("baseline_avg_gain_pct"),
        metrics.get("false_positive_count"),
        metrics.get("false_negative_count"),
        metrics.get("prediction_cap"),
        metrics.get("capped_prediction_count"),
        metrics.get("threshold_analysis"),
        metrics.get("actual_gain_distribution"),
        metrics.get("predicted_gain_distribution"),
    )

    for threshold_key, threshold in (
        metrics.get("threshold_analysis") or {}
    ).items():
        for row in threshold.get("losses") or []:
            logger.warning(
                "%s_SELECTED_LOSS segment=%s model=%r threshold=%s "
                "name=%r date=%r predicted=%s actual=%s error=%s",
                prefix, segment, model_name, threshold_key,
                row.get("name"), row.get("date"),
                row.get("predicted_gain_pct"),
                row.get("actual_gain_pct"),
                row.get("error_pct_points"),
            )

    for row in (metrics.get("worst_prediction_misses") or [])[:5]:
        logger.info(
            "%s_WORST_MISS segment=%s model=%r name=%r date=%r "
            "predicted=%s actual=%s abs_error=%s",
            prefix, segment, model_name,
            row.get("name"), row.get("date"),
            row.get("predicted_gain_pct"),
            row.get("actual_gain_pct"),
            row.get("abs_error_pct_points"),
        )

def log_backtest_interpretation_payload(payload):
    logger.info("BACKTEST_BEGIN")
    logger.info("BACKTEST_WARNING %s", payload.get("warning"))
    logger.info("BACKTEST_DATASET_SUMMARY %s", payload.get("dataset_summary"))
    logger.info("BACKTEST_DATA_INTEGRITY_REPORT %s", payload.get("data_integrity"))

    for segment, segdata in (payload.get("primary_holdout") or {}).items():
        logger.info(
            "HOLDOUT_SEGMENT segment=%s train_year=%s test_year=%s "
            "train_source_rows=%s test_source_rows=%s cap=%s",
            segment, segdata.get("train_year"), segdata.get("test_year"),
            segdata.get("train_source_rows"),
            segdata.get("test_source_rows"),
            segdata.get("prediction_cap"),
        )
        for model_name, metrics in (segdata.get("models") or {}).items():
            _log_model_metrics(
                "HOLDOUT", segment, model_name, metrics
            )

    for segment, segdata in (payload.get("secondary_loocv") or {}).items():
        logger.info(
            "LOOCV_SEGMENT segment=%s market_source_rows=%s "
            "category_source_rows=%s",
            segment, segdata.get("market_source_rows"),
            segdata.get("category_source_rows"),
        )
        for model_name, metrics in (segdata.get("models") or {}).items():
            _log_model_metrics(
                "LOOCV", segment, model_name, metrics
            )

    logger.info("BACKTEST_END")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        msg = fmt % args
        print(f"[web] {self.address_string()} - {msg}")
        logger.info("HTTP_CLIENT %s %s", self.address_string(), msg)

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path):
        if not path.exists() or not path.is_file():
            return self.send_error(404)
        data = path.read_bytes()
        ctype, _ = mimetypes.guess_type(str(path))
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)

        if parsed.path == "/api/health":
            return self._json({
                "ok": True,
                "provider": "FinAPI",
                "auth_required": False,
                "version": "0.4.4",
                "research_policy_version": POLICY_VERSION,
                "recommendation_engine": "ENABLED_RESEARCH_ONLY",
                "collector": collector.state() if collector else None,
                "daily_year_tracker": (
                    daily_year_tracker.state()
                    if daily_year_tracker else None
                ),
                "year_tracker_summary": year_model_tracker_summary(2026),
                "model_audit_version": AUDIT_VERSION,
                "shadow_v2_version": SHADOW_V2_VERSION,
                "shadow_v2_status": "SME_SHADOW_ONLY_V1_UNCHANGED",
                "prospective_version": PROSPECTIVE_VERSION,
                "prospective_exact_listed_target": EXACT_LISTED_TARGET,
                "policy_frozen": True,
                "dataset": dataset_summary(),
                "log_file": str(LOG_FILE.relative_to(ROOT)),
                "raw_response_folder": str(RAW_DIR.relative_to(ROOT)),
            })

        if parsed.path == "/api/ipos":
            status = q.get("status", ["LIVE"])[0]
            ipo_type = q.get("type", ["ALL"])[0]
            payload = fetch_normalized(status=status, ipo_type=ipo_type)
            return self._json(payload, 200 if payload["records"] or not payload["errors"] else 502)

        if parsed.path == "/api/snapshots":
            symbol = q.get("symbol", [None])[0]
            try:
                limit = max(1, min(1000, int(q.get("limit", ["100"])[0])))
            except Exception:
                limit = 100
            return self._json({"rows": recent_snapshots(limit=limit, symbol=symbol)})

        if parsed.path == "/api/decisions":
            symbol = q.get("symbol", [None])[0]
            try:
                limit = max(1, min(1000, int(q.get("limit", ["100"])[0])))
            except Exception:
                limit = 100
            return self._json({
                "policy_version": POLICY_VERSION,
                "rows": recent_research_decisions(limit=limit, symbol=symbol),
            })

        if parsed.path == "/api/historical":
            try:
                limit = max(1, min(5000, int(q.get("limit", ["1000"])[0])))
            except Exception:
                limit = 1000
            return self._json({"rows": historical_web_rows(limit=limit)})

        if parsed.path == "/api/backtest":
            return self._json(run_layered_backtest())

        if parsed.path == "/api/log/tail":
            try:
                lines = max(20, min(1000, int(q.get("lines", ["300"])[0])))
            except Exception:
                lines = 300
            return self._json({
                "log_file": str(LOG_FILE.relative_to(ROOT)),
                "text": tail_log(lines),
            })

        if parsed.path == "/api/log/download":
            if not LOG_FILE.exists():
                LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
                LOG_FILE.write_text("", encoding="utf-8")
            data = LOG_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="ipo_advisor.log"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path == "/api/integrity":
            return self._json({
                "historical_market": market_integrity_summary(),
            })

        if parsed.path == "/api/year-tracker":
            try:
                year = int(q.get("year", ["2026"])[0])
            except Exception:
                year = 2026
            return self._json({
                "year": year,
                "summary": year_model_tracker_summary(year),
                "scheduler": (
                    daily_year_tracker.state()
                    if daily_year_tracker else None
                ),
                "rows": year_model_tracker_rows(
                    year=year, limit=1000
                ),
            })

        if parsed.path == "/api/model-audit":
            try:
                year = int(q.get("year", ["2026"])[0])
            except Exception:
                year = 2026
            rows = year_model_tracker_rows(year=year, limit=5000)
            audit = build_model_audit(rows, year=year)
            historical = historical_market_rows(limit=20000)
            engine = ResearchDecisionEngine(historical)
            audit["shadow_v2"] = {
                "discovery_2026": audit_tracker_shadow(rows),
                "historical_crosscheck_2025": historical_crosscheck_2025(
                    historical, engine
                ),
                "threshold_sensitivity_2025": threshold_grid_2025(
                    historical, engine
                ),
            }
            logger.info(
                "SHADOW_V2_AUDIT version=%s discovery=%s crosscheck=%s",
                SHADOW_V2_VERSION,
                audit["shadow_v2"]["discovery_2026"],
                audit["shadow_v2"]["historical_crosscheck_2025"],
            )
            log_model_audit(audit, logger)
            report = save_json_report(f"MODEL_AUDIT_{year}", audit)
            audit["report_file"] = str(report.relative_to(ROOT))
            return self._json(audit)

        if parsed.path == "/api/prospective-experiment":
            try:
                year = int(q.get("year", ["2026"])[0])
            except Exception:
                year = 2026
            report = build_prospective_experiment(
                canonical_research_decisions(),
                year_model_tracker_rows(year=year, limit=5000),
                year=year,
            )
            log_prospective_experiment(report, logger)
            saved = save_json_report(
                f"PROSPECTIVE_EXPERIMENT_{year}", report
            )
            report["report_file"] = str(saved.relative_to(ROOT))
            return self._json(report)

        if parsed.path == "/api/reports":
            return self._json({"reports": list_reports(limit=50)})

        if parsed.path == "/api/report/latest-backtest":
            reports = [r for r in list_reports(limit=50) if "BACKTEST_FULL" in r["name"]]
            if not reports:
                return self._json({"error": "No backtest report available"}, 404)
            path = ROOT / reports[0]["path"]
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path == "/api/report/latest-import":
            reports = [r for r in list_reports(limit=50) if "IMPORT_FULL" in r["name"]]
            if not reports:
                return self._json({"error": "No import report available"}, 404)
            path = ROOT / reports[0]["path"]
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path == "/api/report/latest-model-audit":
            reports = [
                r for r in list_reports(limit=100)
                if "MODEL_AUDIT_2026" in r["name"]
            ]
            if not reports:
                return self._json(
                    {"error": "No model audit report available"}, 404
                )
            path = ROOT / reports[0]["path"]
            data = path.read_bytes()
            self.send_response(200)
            self.send_header(
                "Content-Type", "application/json; charset=utf-8"
            )
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{path.name}"'
            )
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path == "/api/report/latest-prospective":
            reports = [
                r for r in list_reports(limit=100)
                if "PROSPECTIVE_EXPERIMENT_2026" in r["name"]
            ]
            if not reports:
                return self._json(
                    {"error": "No prospective experiment report available"}, 404
                )
            path = ROOT / reports[0]["path"]
            data = path.read_bytes()
            self.send_response(200)
            self.send_header(
                "Content-Type", "application/json; charset=utf-8"
            )
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{path.name}"'
            )
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path == "/api/dataset":
            return self._json(dataset_summary())

        if parsed.path in ("/", "/index.html"):
            return self._serve_file(STATIC / "index.html")

        if parsed.path.startswith("/static/"):
            rel = parsed.path[len("/static/"):]
            safe = (STATIC / rel).resolve()
            if STATIC.resolve() not in safe.parents and safe != STATIC.resolve():
                return self.send_error(403)
            return self._serve_file(safe)

        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/capture":
            payload = capture_live("manual")
            return self._json(payload, 200 if payload["records"] or not payload["errors"] else 502)

        if parsed.path == "/api/historical/import":
            payload = import_all_backtest_history()
            ok = bool(
                (payload.get("market_2024") or {}).get("records_received")
                or (payload.get("market_2025") or {}).get("records_received")
                or (payload.get("public_2026") or {}).get("records_received")
            ) or not payload.get("errors")
            return self._json(payload, 200 if ok else 502)

        if parsed.path == "/api/historical/import-recent-closed":
            payload = import_recent_closed_history()
            ok = bool(payload["records_received"]) or not payload["errors"]
            return self._json(payload, 200 if ok else 502)

        if parsed.path == "/api/year-tracker/sync":
            payload = sync_year_tracker(
                year=2026, force_detail_refresh=False
            )
            return self._json(payload)

        if parsed.path == "/api/diagnostic/listed":
            return self._json(run_listed_diagnostic())

        if parsed.path == "/api/log/clear":
            clear_log()
            return self._json({"ok": True, "log_file": str(LOG_FILE.relative_to(ROOT))})

        self.send_error(404)

def main():
    global collector, daily_year_tracker
    parser_checks = parser_self_test()
    from historical_ipomarkets import tracking_parser_self_test
    tracking_checks = tracking_parser_self_test()
    logger.info(
        "IPOMARKETS_PARSER_SELFTEST_OK %s", parser_checks
    )
    logger.info(
        "IPOMARKETS_TRACKING_SELFTEST_OK %s", tracking_checks
    )

    host = os.environ.get("IPO_ADVISOR_HOST", "127.0.0.1")
    port = int(os.environ.get("IPO_ADVISOR_PORT", "8000"))
    auto_enabled = os.environ.get("IPO_AUTO_CAPTURE", "1") not in ("0", "false", "False")

    collector = AutoCollector(capture_live)
    collector.enabled = auto_enabled
    collector.start()

    daily_year_tracker = DailyYearTracker(
        lambda: sync_year_tracker(
            year=2026, force_detail_refresh=False
        ),
        year=2026,
    )
    daily_year_tracker.start()

    server = ThreadingHTTPServer((host, port), Handler)
    print()
    logger.info("APP_START version=0.4.4 host=%s port=%s auto_capture=%s", host, port, auto_enabled)
    print("IPO Advisor v0.4.4 — prospective experiment tracking")
    print(f"Open: http://{host}:{port}")
    print("Time zone: IST (Asia/Kolkata)")
    print("Auto-capture:", "ON" if auto_enabled else "OFF")
    print("Cadence: every 15 min, weekdays 09:30–15:30 IST")
    print("Canonical decision snapshot: 14:30 IST on IPO closing day")
    print(f"Research recommendation policy: {POLICY_VERSION}")
    print("2026 model-vs-actual tracker: daily 6:00 PM IST + startup catch-up")
    logger.info("RESEARCH_POLICY_ENABLED version=%s canonical_time=14:30_IST", POLICY_VERSION)
    print("Press Ctrl+C to stop.")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        collector.stop()
        if daily_year_tracker:
            daily_year_tracker.stop()
        logger.info("APP_STOP")

if __name__ == "__main__":
    main()
