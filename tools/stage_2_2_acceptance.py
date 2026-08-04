from __future__ import annotations

import argparse
import copy
import json
import logging
import math
import os
import sqlite3
import sys
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from deal_radar.bike_identity import normalize_text  # noqa: E402
from deal_radar.config import DealScoringConfig, load_config  # noqa: E402
from deal_radar.deal_scoring import DealEvaluator, detect_condition  # noqa: E402
from deal_radar.models import (  # noqa: E402
    DealCosts,
    DealEvaluation,
    Listing,
    ListingAnalysis,
    MarketValuation,
)
from deal_radar.storage import Storage  # noqa: E402


STATUS_ORDER = ("HOT", "INTERESTING", "MANUAL_REVIEW", "LOW_PRIORITY", "REJECT")
NON_NEGATIVE_FIELDS = (
    "purchase_price_czk",
    "acquisition_costs_czk",
    "logistics_costs_czk",
    "platform_fees_czk",
    "base_investment_czk",
    "risk_reserve_percent",
    "risk_reserve_czk",
    "total_investment_czk",
    "market_median_czk",
    "quick_sale_price_czk",
    "liquidity_score",
    "confidence_score",
    "deal_score",
)
CONDITION_PHRASES = (
    "po servisu",
    "nově servisováno",
    "bez nutnosti servisu",
    "servis není potřeba",
    "bez dalších investic",
    "připraveno k jízdě",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Offline acceptance audit for DealRadar Stage 2.2."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=PROJECT_ROOT / "data" / "deal_radar.sqlite3",
        help="Production SQLite database. It is opened strictly read-only.",
    )
    parser.add_argument(
        "--copy",
        dest="copy_path",
        type=Path,
        help="Destination snapshot. Defaults to a timestamped file beside the source.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "config.example.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="JSON result path. Defaults to the snapshot name with .acceptance.json.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero when an acceptance invariant fails.",
    )
    return parser.parse_args()


def _timestamp() -> str:
    return datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")


def _read_only_uri(path: Path) -> str:
    return f"{path.resolve().as_uri()}?mode=ro"


def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    names = (
        "listings",
        "feedback",
        "valuation_cache",
        "market_valuations",
        "pricing_comparables",
        "deal_cost_overrides",
        "deal_evaluations",
    )
    return {
        name: int(connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
        if name in tables
        else 0
        for name in names
    }


def _backup_read_only(source: Path, destination: Path) -> dict[str, Any]:
    if not source.is_file():
        raise FileNotFoundError(source)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    before = source.stat()
    source_connection = sqlite3.connect(
        _read_only_uri(source),
        uri=True,
        timeout=30,
    )
    source_connection.execute("PRAGMA query_only = ON")
    source_connection.row_factory = sqlite3.Row
    try:
        source_query_only = int(source_connection.execute("PRAGMA query_only").fetchone()[0])
        source_counts = _table_counts(source_connection)
        destination_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(destination_connection)
            integrity = str(
                destination_connection.execute("PRAGMA integrity_check").fetchone()[0]
            )
            destination_counts = _table_counts(destination_connection)
        finally:
            destination_connection.close()
        source_total_changes = int(source_connection.total_changes)
    finally:
        source_connection.close()
    after = source.stat()
    return {
        "source_path": str(source.resolve()),
        "copy_path": str(destination.resolve()),
        "source_open_mode": "mode=ro + PRAGMA query_only=ON",
        "source_query_only": source_query_only,
        "source_total_changes": source_total_changes,
        "source_size_before": before.st_size,
        "source_size_after_backup": after.st_size,
        "source_mtime_ns_before": before.st_mtime_ns,
        "source_mtime_ns_after_backup": after.st_mtime_ns,
        "source_counts": source_counts,
        "copy_counts_before_migration": destination_counts,
        "copy_integrity_check": integrity,
    }


def _load_offline_config(path: Path) -> tuple[DealScoringConfig, Any]:
    os.environ["TELEGRAM_SEND_HOT"] = "false"
    os.environ["TELEGRAM_SEND_INTERESTING"] = "false"
    os.environ["TELEGRAM_SEND_MANUAL_REVIEW"] = "false"
    os.environ["TELEGRAM_SEND_LOW_PRIORITY"] = "false"
    os.environ["TELEGRAM_SEND_REJECT"] = "false"
    app_config = load_config(path)
    deal_config = replace(
        app_config.deal_scoring,
        enabled=True,
        telegram_send_hot=False,
        telegram_send_interesting=False,
        telegram_send_manual_review=False,
        telegram_send_low_priority=False,
        telegram_send_reject=False,
    )
    deal_config.validate()
    return deal_config, app_config.market_pricing


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _round_stage_2_1_price(value: float, market_config: Any) -> int:
    step = (
        market_config.expensive_rounding_step_czk
        if value >= market_config.expensive_rounding_threshold_czk
        else market_config.cheap_rounding_step_czk
    )
    return int(round(float(value) / step) * step)


def _prepared_rows(storage: Storage) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = storage.connection.execute(
        """
        SELECT
            l.data_json AS listing_json,
            l.analysis_json AS analysis_json,
            l.notification_status AS stored_notification_status,
            l.notification_reason AS stored_notification_reason,
            mv.data_json AS valuation_json
        FROM market_valuations AS mv
        JOIN listings AS l
          ON l.source = mv.listing_source
         AND l.external_id = mv.listing_external_id
        ORDER BY mv.listing_source, mv.listing_external_id
        """
    ).fetchall()
    prepared: list[dict[str, Any]] = []
    financially_unusable: list[dict[str, Any]] = []
    for row in rows:
        listing = Listing.from_dict(json.loads(row["listing_json"]))
        valuation = MarketValuation.from_dict(json.loads(row["valuation_json"]))
        reasons: list[str] = []
        if valuation.market_price_czk is None or valuation.market_price_czk <= 0:
            reasons.append("market_price_czk_missing_or_non_positive")
        if valuation.quick_sale_price_czk is None or valuation.quick_sale_price_czk <= 0:
            reasons.append("quick_sale_price_czk_missing_or_non_positive")
        if valuation.comparables_unique <= 0:
            reasons.append("no_unique_comparables")
        if reasons:
            financially_unusable.append(
                {
                    "id": listing.key,
                    "url": listing.url,
                    "reasons": reasons,
                    "valuation_status": valuation.status,
                }
            )
        raw_analysis = str(row["analysis_json"] or "")
        if raw_analysis and raw_analysis != "{}":
            analysis = ListingAnalysis.from_dict(json.loads(raw_analysis))
        else:
            analysis = ListingAnalysis(
                preliminary_priority_score=0,
                priority_class="manual_review",
                risks=["Сохранённый предварительный анализ отсутствует."],
                analysis_confidence="low",
                notification_status=str(row["stored_notification_status"]),
                notification_reason=str(row["stored_notification_reason"]),
            )
        analysis.market_valuation = valuation
        if analysis.used_comparables is None:
            analysis.used_comparables = valuation.as_used_comparables()
        prepared.append(
            {
                "listing": listing,
                "analysis": analysis,
                "valuation": valuation,
                "valuation_issues": reasons,
            }
        )
    return prepared, financially_unusable


def _expected_score(
    evaluation: DealEvaluation,
    config: DealScoringConfig,
) -> float:
    active = {
        name: config.score_weights.get(name, 0.0)
        for name in evaluation.score_components
        if config.score_weights.get(name, 0.0) > 0
    }
    weight = sum(active.values())
    if weight <= 0:
        return 0.0
    return round(
        max(
            0.0,
            min(
                100.0,
                sum(evaluation.score_components[name] * value for name, value in active.items())
                / weight,
            ),
        ),
        2,
    )


def _matched_terms(text: str, terms: list[str]) -> list[str]:
    normalized = normalize_text(text)
    return sorted(
        {
            term
            for term in terms
            if normalize_text(term) and normalize_text(term) in normalized
        }
    )


def _evaluate_and_validate(
    storage: Storage,
    rows: list[dict[str, Any]],
    config: DealScoringConfig,
    market_config: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    evaluator = DealEvaluator(config)
    results: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    inserted = 0
    quick_sale_mismatches = 0
    scenario_a_count = 0
    scenario_b_count = 0
    scenario_b_only_count = 0
    hot_candidate_low_confidence = 0
    hot_candidate_condition_blocked = 0
    hot_candidate_weak_liquidity = 0

    for item in rows:
        listing: Listing = item["listing"]
        analysis: ListingAnalysis = item["analysis"]
        valuation: MarketValuation = item["valuation"]
        costs = storage.get_deal_costs(listing)
        evaluation = evaluator.evaluate(listing, analysis, costs)
        inserted += int(storage.save_deal_evaluation(evaluation))

        row_violations: list[str] = []
        values = evaluation.to_dict()
        for field, value in values.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if not math.isfinite(float(value)):
                    row_violations.append(f"{field}:non_finite")
        for field in NON_NEGATIVE_FIELDS:
            value = values.get(field)
            if value is not None and float(value) < 0:
                row_violations.append(f"{field}:negative")

        formula_checks: dict[str, tuple[float | None, float]] = {}
        if evaluation.purchase_price_czk is not None:
            purchase = Decimal(str(evaluation.purchase_price_czk))
            extras = sum(
                Decimal(str(value))
                for value in (
                    evaluation.acquisition_costs_czk,
                    evaluation.logistics_costs_czk,
                    evaluation.platform_fees_czk,
                )
            )
            expected_base = _money(purchase + extras)
            expected_reserve = _money(
                (purchase + extras)
                * Decimal(str(config.risk_reserve_percent))
                / Decimal("100")
            )
            expected_total = _money(
                Decimal(str(expected_base)) + Decimal(str(expected_reserve))
            )
            formula_checks.update(
                {
                    "base_investment_czk": (
                        evaluation.base_investment_czk,
                        expected_base,
                    ),
                    "risk_reserve_czk": (
                        evaluation.risk_reserve_czk,
                        expected_reserve,
                    ),
                    "total_investment_czk": (
                        evaluation.total_investment_czk,
                        expected_total,
                    ),
                }
            )
            if evaluation.quick_sale_price_czk is not None and expected_total > 0:
                expected_profit = _money(
                    Decimal(str(evaluation.quick_sale_price_czk))
                    - Decimal(str(expected_total))
                )
                expected_roi = float(
                    (
                        Decimal(str(expected_profit))
                        / Decimal(str(expected_total))
                        * Decimal("100")
                    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                )
                formula_checks.update(
                    {
                        "net_profit_czk": (
                            evaluation.net_profit_czk,
                            expected_profit,
                        ),
                        "roi_percent": (evaluation.roi_percent, expected_roi),
                    }
                )
        for field, (actual, expected) in formula_checks.items():
            if actual is None or abs(float(actual) - float(expected)) > 0.011:
                row_violations.append(f"{field}:expected={expected}:actual={actual}")

        score_expected = _expected_score(evaluation, config)
        if abs(evaluation.deal_score - score_expected) > 0.011:
            row_violations.append(
                f"deal_score:expected={score_expected}:actual={evaluation.deal_score}"
            )

        if valuation.market_price_czk is not None and valuation.market_price_czk > 0:
            expected_quick = _round_stage_2_1_price(
                float(valuation.market_price_czk)
                * (1 - market_config.quick_sale_discount),
                market_config,
            )
            if valuation.quick_sale_price_czk != expected_quick:
                quick_sale_mismatches += 1
                row_violations.append(
                    "quick_sale_price_czk:"
                    f"expected_stage_2_1={expected_quick}:actual={valuation.quick_sale_price_czk}"
                )

        scenario_a = (
            evaluation.net_profit_czk is not None
            and evaluation.net_profit_czk >= config.hot_min_profit_czk
            and evaluation.roi_percent is not None
            and evaluation.roi_percent >= config.hot_min_roi_percent
        )
        scenario_b = (
            evaluation.net_profit_czk is not None
            and evaluation.net_profit_czk > 0
            and evaluation.roi_percent is not None
            and evaluation.roi_percent >= config.high_roi_percent
        )
        scenario_a_count += int(scenario_a)
        scenario_b_count += int(scenario_b)
        scenario_b_only_count += int(scenario_b and not scenario_a)
        financial_hot = scenario_a or scenario_b

        manual_flags = {
            "ambiguous_identity",
            "low_identity_confidence",
            "condition_unknown",
            "service_required",
            "condition_problem",
            "condition_contradictory",
            "low_market_confidence",
            "critical_valuation_status",
            "critical_market_warning",
            "stage_1_2_manual_risk",
            "purchase_price_outlier",
            "invalid_additional_costs",
            "purchase_price_missing",
            "market_valuation_missing",
            "quick_sale_price_missing",
        }
        if evaluation.status == "HOT":
            if not financial_hot:
                row_violations.append("HOT_without_financial_scenario")
            if set(evaluation.flags) & manual_flags:
                row_violations.append("HOT_with_manual_review_flag")
            if evaluation.confidence_level == "low":
                row_violations.append("HOT_with_low_confidence")
            if evaluation.condition in {
                "unknown",
                "service_required",
                "problematic",
                "contradictory",
            }:
                row_violations.append("HOT_with_blocking_condition")
        if financial_hot and evaluation.confidence_level == "low":
            hot_candidate_low_confidence += 1
            if evaluation.status not in {"MANUAL_REVIEW", "REJECT"}:
                row_violations.append("low_confidence_failed_to_block_HOT")
        if financial_hot and evaluation.condition in {
            "unknown",
            "service_required",
            "problematic",
            "contradictory",
        }:
            hot_candidate_condition_blocked += 1
            if evaluation.status not in {"MANUAL_REVIEW", "REJECT"}:
                row_violations.append("condition_failed_to_block_HOT")
        if (
            financial_hot
            and evaluation.liquidity_level == "low"
            and not (set(evaluation.flags) & manual_flags)
        ):
            hot_candidate_weak_liquidity += 1
            if evaluation.status != "INTERESTING":
                row_violations.append("weak_liquidity_not_downgraded_to_INTERESTING")
        if (
            evaluation.net_profit_czk is not None
            and evaluation.net_profit_czk < 0
            and evaluation.status != "REJECT"
        ):
            row_violations.append("negative_profit_not_REJECT")
        if (
            evaluation.roi_percent is not None
            and evaluation.roi_percent < 0
            and evaluation.status != "REJECT"
        ):
            row_violations.append("negative_roi_not_REJECT")

        text = f"{listing.title} {listing.description}"
        condition_matches = {
            "positive": _matched_terms(text, config.positive_condition_terms),
            "completed_service": _matched_terms(
                text,
                config.completed_service_terms,
            ),
            "service_required_raw": _matched_terms(
                text,
                config.service_required_terms,
            ),
            "problem": _matched_terms(text, config.problem_condition_terms),
        }
        result = {
            "id": listing.key,
            "source": listing.source,
            "external_id": listing.external_id,
            "url": listing.url,
            "title": listing.title,
            "description": listing.description,
            "purchase_price_czk": evaluation.purchase_price_czk,
            "market_median_czk": evaluation.market_median_czk,
            "quick_sale_price_czk": evaluation.quick_sale_price_czk,
            "base_investment_czk": evaluation.base_investment_czk,
            "risk_reserve_czk": evaluation.risk_reserve_czk,
            "total_investment_czk": evaluation.total_investment_czk,
            "net_profit_czk": evaluation.net_profit_czk,
            "roi_percent": evaluation.roi_percent,
            "liquidity_score": evaluation.liquidity_score,
            "liquidity_level": evaluation.liquidity_level,
            "confidence_score": evaluation.confidence_score,
            "confidence_level": evaluation.confidence_level,
            "deal_score": evaluation.deal_score,
            "status": evaluation.status,
            "condition": evaluation.condition,
            "reasons": evaluation.reasons,
            "flags": evaluation.flags,
            "valuation_warnings": valuation.warnings,
            "valuation_status": valuation.status,
            "valuation_issues": item["valuation_issues"],
            "comparable_count": evaluation.comparable_count,
            "source_count": evaluation.source_count,
            "condition_matches": condition_matches,
            "scenario_a": scenario_a,
            "scenario_b": scenario_b,
            "input_fingerprint": evaluation.input_fingerprint,
            "violations": row_violations,
        }
        results.append(result)
        if row_violations:
            violations.append({"id": listing.key, "violations": row_violations})

    return results, {
        "baseline_history_inserts": inserted,
        "formula_and_invariant_violations": violations,
        "quick_sale_rounding_mismatches": quick_sale_mismatches,
        "financial_hot_scenario_a_candidates": scenario_a_count,
        "financial_hot_scenario_b_candidates": scenario_b_count,
        "financial_hot_scenario_b_only_candidates": scenario_b_only_count,
        "financial_hot_low_confidence_candidates": hot_candidate_low_confidence,
        "financial_hot_condition_blocked_candidates": hot_candidate_condition_blocked,
        "financial_hot_weak_liquidity_candidates": hot_candidate_weak_liquidity,
    }


def _idempotence_checks(
    storage: Storage,
    rows: list[dict[str, Any]],
    results: list[dict[str, Any]],
    config: DealScoringConfig,
) -> dict[str, Any]:
    evaluator = DealEvaluator(config)
    count_after_baseline = int(
        storage.connection.execute("SELECT COUNT(*) FROM deal_evaluations").fetchone()[0]
    )
    second_inserts = 0
    cache_counter_inserts = 0
    cache_fingerprint_mismatches = 0
    result_by_id = {result["id"]: result for result in results}

    for item in rows:
        listing: Listing = item["listing"]
        analysis: ListingAnalysis = item["analysis"]
        costs = storage.get_deal_costs(listing)
        identical = evaluator.evaluate(listing, analysis, costs)
        second_inserts += int(storage.save_deal_evaluation(identical))

        changed_analysis = copy.deepcopy(analysis)
        changed_valuation = MarketValuation.from_dict(item["valuation"].to_dict())
        changed_valuation.cache_used = not changed_valuation.cache_used
        changed_valuation.cache_hits += 17
        changed_valuation.cache_misses += 11
        changed_valuation.http_requests = {
            **changed_valuation.http_requests,
            "acceptance_probe": 999,
        }
        changed_analysis.market_valuation = changed_valuation
        cache_changed = evaluator.evaluate(listing, changed_analysis, costs)
        baseline_fingerprint = result_by_id[listing.key]["input_fingerprint"]
        cache_fingerprint_mismatches += int(
            cache_changed.input_fingerprint != baseline_fingerprint
        )
        cache_counter_inserts += int(storage.save_deal_evaluation(cache_changed))

    count_after_identical = int(
        storage.connection.execute("SELECT COUNT(*) FROM deal_evaluations").fetchone()[0]
    )

    variation: dict[str, Any] = {
        "tested": False,
        "listing_id": "",
        "cost_change_inserted": False,
        "config_change_inserted": False,
        "history_before_variations": count_after_identical,
        "history_after_variations": count_after_identical,
    }
    financially_calculable_rows = [
        item
        for item in rows
        if result_by_id[item["listing"].key]["net_profit_czk"] is not None
    ]
    if financially_calculable_rows:
        target = max(
            financially_calculable_rows,
            key=lambda item: result_by_id[item["listing"].key]["deal_score"],
        )
        listing = target["listing"]
        analysis = target["analysis"]
        original_costs = storage.get_deal_costs(listing)
        changed_costs = replace(
            original_costs,
            logistics_costs_czk=original_costs.logistics_costs_czk + 137.0,
        )
        storage.set_deal_costs(listing, changed_costs)
        cost_evaluation = evaluator.evaluate(listing, analysis, changed_costs)
        cost_inserted = storage.save_deal_evaluation(cost_evaluation)
        storage.set_deal_costs(listing, original_costs)

        changed_config = replace(
            config,
            risk_reserve_percent=config.risk_reserve_percent + 1.0,
        )
        changed_config.validate()
        config_evaluation = DealEvaluator(changed_config).evaluate(
            listing,
            analysis,
            original_costs,
        )
        config_inserted = storage.save_deal_evaluation(config_evaluation)
        variation = {
            "tested": True,
            "listing_id": listing.key,
            "cost_change_inserted": bool(cost_inserted),
            "cost_change_czk": 137.0,
            "config_change_inserted": bool(config_inserted),
            "config_change": {
                "risk_reserve_percent_before": config.risk_reserve_percent,
                "risk_reserve_percent_after": changed_config.risk_reserve_percent,
            },
            "history_before_variations": count_after_identical,
            "history_after_variations": int(
                storage.connection.execute(
                    "SELECT COUNT(*) FROM deal_evaluations"
                ).fetchone()[0]
            ),
        }

    duplicate_fingerprints = int(
        storage.connection.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT listing_source, listing_external_id, input_fingerprint, COUNT(*) AS n
                FROM deal_evaluations
                GROUP BY listing_source, listing_external_id, input_fingerprint
                HAVING n > 1
            )
            """
        ).fetchone()[0]
    )
    return {
        "history_after_baseline": count_after_baseline,
        "identical_rerun_inserts": second_inserts,
        "cache_counter_change_inserts": cache_counter_inserts,
        "cache_counter_fingerprint_mismatches": cache_fingerprint_mismatches,
        "history_after_identical_and_cache_checks": count_after_identical,
        "duplicate_fingerprint_groups": duplicate_fingerprints,
        "variation_checks": variation,
    }


def _condition_phrase_checks(config: DealScoringConfig) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for index, phrase in enumerate(CONDITION_PHRASES, start=1):
        listing = Listing(
            source="acceptance",
            external_id=str(index),
            title="Test bike",
            description=phrase,
            url="https://example.invalid",
            profile="acceptance",
            price_czk=10_000,
        )
        condition, flags = detect_condition(listing, config)
        checks.append(
            {
                "phrase": phrase,
                "condition": condition,
                "flags": flags,
                "expected_positive": condition in {"good", "excellent"} and not flags,
            }
        )
    return checks


def _representative_rows(
    results: list[dict[str, Any]],
    status: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    candidates = sorted(
        [result for result in results if result["status"] == status],
        key=lambda result: (-result["deal_score"], result["id"]),
    )
    if len(candidates) <= limit:
        return candidates
    high_count = (limit + 1) // 2
    low_count = limit - high_count
    selected = candidates[:high_count] + list(reversed(candidates[-low_count:]))
    return selected


def _summarize(
    results: list[dict[str, Any]],
    financially_unusable: list[dict[str, Any]],
    checks: dict[str, Any],
    history: dict[str, Any],
    condition_phrases: list[dict[str, Any]],
) -> dict[str, Any]:
    distribution = Counter(result["status"] for result in results)
    manual_reasons = Counter(
        reason
        for result in results
        if result["status"] == "MANUAL_REVIEW"
        for reason in result["reasons"]
    )
    condition_distribution = Counter(result["condition"] for result in results)
    condition_triggered = [
        result
        for result in results
        if result["condition"] != "unknown"
        or any(result["condition_matches"].values())
    ]
    raw_service_matches = [
        result
        for result in results
        if result["condition_matches"]["service_required_raw"]
    ]
    service_triggered = [
        result
        for result in raw_service_matches
        if result["condition"] in {"service_required", "contradictory"}
    ]
    suppressed_completed_service = [
        result
        for result in raw_service_matches
        if result["condition"] in {"good", "excellent"}
        and result["condition_matches"]["completed_service"]
    ]
    contradictory = [
        result for result in results if result["condition"] == "contradictory"
    ]
    critical_warnings = [
        result for result in results if "critical_market_warning" in result["flags"]
    ]
    informational_warnings = [
        result
        for result in results
        if result["valuation_warnings"]
        and "critical_market_warning" not in result["flags"]
    ]
    suspicious: list[dict[str, Any]] = []
    suspicious.extend(checks["formula_and_invariant_violations"])
    for result in results:
        if result["status"] == "MANUAL_REVIEW" and not result["flags"]:
            suspicious.append(
                {
                    "id": result["id"],
                    "issue": "MANUAL_REVIEW has no machine-readable flags",
                }
            )
        if result["status"] == "REJECT" and result["net_profit_czk"] is not None:
            if result["net_profit_czk"] > 0 and "existing_hard_filter" not in result["flags"]:
                suspicious.append(
                    {
                        "id": result["id"],
                        "issue": "Positive-profit REJECT without existing hard filter",
                    }
                )

    acceptance_failures: list[str] = []
    if checks["formula_and_invariant_violations"]:
        acceptance_failures.append("formula_or_numeric_invariants_failed")
    if history["identical_rerun_inserts"] != 0:
        acceptance_failures.append("identical_rerun_created_history")
    if history["cache_counter_change_inserts"] != 0:
        acceptance_failures.append("cache_counter_change_created_history")
    if history["cache_counter_fingerprint_mismatches"] != 0:
        acceptance_failures.append("cache_counter_changed_fingerprint")
    if history["duplicate_fingerprint_groups"] != 0:
        acceptance_failures.append("duplicate_history_fingerprints_found")
    variation = history["variation_checks"]
    if variation["tested"] and not variation["cost_change_inserted"]:
        acceptance_failures.append("cost_change_did_not_create_history")
    if variation["tested"] and not variation["config_change_inserted"]:
        acceptance_failures.append("config_change_did_not_create_history")
    if any(not check["expected_positive"] for check in condition_phrases):
        acceptance_failures.append("positive_condition_phrase_not_recognized")

    return {
        "processed": len(results),
        "financially_usable_market_valuations": (
            len(results) - len(financially_unusable)
        ),
        "financially_unusable_market_valuations": financially_unusable,
        "status_distribution": {
            status: distribution.get(status, 0) for status in STATUS_ORDER
        },
        "all_hot": sorted(
            [result for result in results if result["status"] == "HOT"],
            key=lambda result: (-result["deal_score"], result["id"]),
        ),
        "representative_examples": {
            status: _representative_rows(results, status)
            for status in STATUS_ORDER
            if status != "HOT"
        },
        "manual_review_reason_frequency": [
            {"reason": reason, "count": count}
            for reason, count in manual_reasons.most_common()
        ],
        "outlier_flag_count": sum(
            "purchase_price_outlier" in result["flags"] for result in results
        ),
        "condition_distribution": dict(condition_distribution),
        "condition_trigger_count": len(condition_triggered),
        "service_required_raw_match_count": len(raw_service_matches),
        "service_required_trigger_count": len(service_triggered),
        "service_required_triggers": service_triggered,
        "service_false_positive_suppressed_count": len(
            suppressed_completed_service
        ),
        "service_false_positives_suppressed": suppressed_completed_service,
        "contradictory_condition_count": len(contradictory),
        "condition_phrase_checks": condition_phrases,
        "critical_warning_trigger_count": len(critical_warnings),
        "critical_warning_triggers": critical_warnings,
        "informational_warning_without_block_count": len(informational_warnings),
        "suspicious_decisions": suspicious,
        "acceptance_failures": acceptance_failures,
    }


def main() -> int:
    args = _parse_args()
    source = args.source.resolve()
    copy_path = (
        args.copy_path.resolve()
        if args.copy_path
        else source.with_name(f"{source.stem}.stage-2-2-acceptance-{_timestamp()}{source.suffix}")
    )
    output = (
        args.output.resolve()
        if args.output
        else copy_path.with_suffix(".acceptance.json")
    )

    logging.basicConfig(level=logging.WARNING)
    provenance = _backup_read_only(source, copy_path)
    config, market_config = _load_offline_config(args.config.resolve())
    storage = Storage(str(copy_path))
    try:
        eligible, financially_unusable = _prepared_rows(storage)
        history_before = int(
            storage.connection.execute("SELECT COUNT(*) FROM deal_evaluations").fetchone()[0]
        )
        results, checks = _evaluate_and_validate(
            storage,
            eligible,
            config,
            market_config,
        )
        history = _idempotence_checks(storage, eligible, results, config)
        history["history_before_acceptance"] = history_before
        phrase_checks = _condition_phrase_checks(config)
        summary = _summarize(
            results,
            financially_unusable,
            checks,
            history,
            phrase_checks,
        )
        copy_counts_after = _table_counts(storage.connection)
        copy_integrity_after = str(
            storage.connection.execute("PRAGMA integrity_check").fetchone()[0]
        )
    finally:
        storage.close()

    source_after = source.stat()
    payload = {
        "run": {
            "started_and_completed_at": datetime.now(UTC).isoformat(),
            "project_root": str(PROJECT_ROOT),
            "config_path": str(args.config.resolve()),
            "telegram_delivery_enabled": False,
            "telegram_client_instantiated": False,
            "market_price_engine_instantiated": False,
            "market_search_calls": 0,
            "production_process_restarted": False,
        },
        "database": {
            **provenance,
            "source_size_after_run": source_after.st_size,
            "source_mtime_ns_after_run": source_after.st_mtime_ns,
            "copy_counts_after_acceptance": copy_counts_after,
            "copy_integrity_check_after_acceptance": copy_integrity_after,
        },
        "configuration": {
            "algorithm_version": config.algorithm_version,
            "risk_reserve_percent": config.risk_reserve_percent,
            "hot_min_profit_czk": config.hot_min_profit_czk,
            "hot_min_roi_percent": config.hot_min_roi_percent,
            "interesting_min_roi_percent": config.interesting_min_roi_percent,
            "high_roi_percent": config.high_roi_percent,
            "hot_min_liquidity_score": config.hot_min_liquidity_score,
            "hot_min_confidence_score": config.hot_min_confidence_score,
            "telegram_send_flags": {
                "HOT": config.telegram_send_hot,
                "INTERESTING": config.telegram_send_interesting,
                "MANUAL_REVIEW": config.telegram_send_manual_review,
                "LOW_PRIORITY": config.telegram_send_low_priority,
                "REJECT": config.telegram_send_reject,
            },
        },
        "checks": checks,
        "history_checks": history,
        "summary": summary,
        "results": results,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "copy_path": str(copy_path),
                "output_path": str(output),
                "processed": summary["processed"],
                "status_distribution": summary["status_distribution"],
                "acceptance_failures": summary["acceptance_failures"],
            },
            ensure_ascii=False,
        )
    )
    return 2 if args.strict and summary["acceptance_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
