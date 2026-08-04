from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
import sys
from collections import Counter
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from deal_radar.bike_identity import hard_filter_reason, normalize_text  # noqa: E402
from deal_radar.config import load_config  # noqa: E402
from deal_radar.deal_scoring import DealEvaluator  # noqa: E402
from deal_radar.models import (  # noqa: E402
    DealCosts,
    DealEvaluation,
    Listing,
    ListingAnalysis,
    MarketValuation,
)


STATUS_ORDER = ("HOT", "INTERESTING", "MANUAL_REVIEW", "LOW_PRIORITY", "REJECT")
CSV_COLUMNS = (
    "listing_id",
    "url",
    "title",
    "description",
    "purchase_price",
    "market_median",
    "quick_sale_price",
    "total_investment",
    "net_profit",
    "roi",
    "liquidity",
    "confidence",
    "condition",
    "deal_score",
    "status",
    "financial_hot_scenario_a",
    "financial_hot_scenario_b",
    "hard_filter_result",
    "manual_flags",
    "decision_reasons",
    "confidence_causes",
    "condition_diagnostic_group",
    "missing_quick_sale_cause",
    "baseline_status",
)
MANUAL_FLAGS = {
    "ambiguous_identity",
    "condition_contradictory",
    "condition_problem",
    "condition_unknown",
    "critical_market_warning",
    "critical_valuation_status",
    "invalid_additional_costs",
    "low_identity_confidence",
    "low_market_confidence",
    "market_valuation_missing",
    "non_positive_total_investment",
    "purchase_price_missing",
    "purchase_price_outlier",
    "quick_sale_price_missing",
    "service_required",
    "stage_1_2_manual_risk",
}
MISSED_POSITIVE_PHRASES = (
    "100% funkcni",
    "bez poskozeni",
    "bez vad",
    "bezvadny stav",
    "dobry stav",
    "dobrem stavu",
    "idealnim stavu",
    "ihned k pouziti",
    "jako nove",
    "jen nasednout a jet",
    "malo jezdene",
    "minimalne jezdene",
    "pekny stav",
    "peknem stavu",
    "plne funkcni",
    "pripravene k jizde",
    "pripravene na okamzite vyjeti",
    "pravidelne servisovane",
    "pravidelne udrzovane",
    "perfektnim stavu",
    "staci sednout a jet",
    "technicky v poradku",
    "udrzovane",
    "v poradku",
    "vse 100 funkcni",
    "vse funkcni",
    "vse funguje na 100",
    "vse zavcasu servisovano",
    "vybornem stavu",
    "zachovale",
    "zachovalem stavu",
    "zachovaly stav",
)
ADDED_POSITIVE_PHRASES = (
    "pripravene k jizde",
    "pripravene na okamzite vyjeti",
    "ihned k pouziti",
    "staci sednout a jet",
    "jen nasednout a jet",
    "plne funkcni",
    "vse 100 funkcni",
    "vse funguje na 100",
    "pravidelne servisovane",
    "vse zavcasu servisovano",
    "dobrem stavu",
    "peknem stavu",
    "idealnim stavu",
    "perfektnim stavu",
    "vybornem stavu",
    "bezvadny stav",
    "jako nove",
    "zachovale",
    "udrzovane",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Offline final diagnostic calibration for DealRadar Stage 2.2."
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--baseline-json", type=Path, required=True)
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "config.example.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "output" / "stage_2_2_calibration",
    )
    parser.add_argument("--expected-count", type=int, default=74)
    return parser.parse_args()


def read_only_uri(path: Path) -> str:
    return f"{path.resolve().as_uri()}?mode=ro"


def load_offline_config(path: Path) -> tuple[Any, Any]:
    os.environ["TELEGRAM_SEND_HOT"] = "false"
    os.environ["TELEGRAM_SEND_INTERESTING"] = "false"
    os.environ["TELEGRAM_SEND_MANUAL_REVIEW"] = "false"
    os.environ["TELEGRAM_SEND_LOW_PRIORITY"] = "false"
    os.environ["TELEGRAM_SEND_REJECT"] = "false"
    app = load_config(path)
    scoring = replace(
        app.deal_scoring,
        enabled=True,
        telegram_send_hot=False,
        telegram_send_interesting=False,
        telegram_send_manual_review=False,
        telegram_send_low_priority=False,
        telegram_send_reject=False,
    )
    scoring.validate()
    return scoring, app.priority


def percentage(count: int, denominator: int) -> float:
    return round(count / denominator * 100, 2) if denominator else 0.0


def counts_with_percent(
    counts: Counter[str],
    denominator: int,
) -> list[dict[str, Any]]:
    return [
        {
            "group": name,
            "count": count,
            "percent": percentage(count, denominator),
        }
        for name, count in counts.most_common()
    ]


def ordered_counts_with_percent(
    counts: Counter[str],
    denominator: int,
    order: tuple[str, ...],
) -> list[dict[str, Any]]:
    return [
        {
            "group": name,
            "count": counts.get(name, 0),
            "percent": percentage(counts.get(name, 0), denominator),
        }
        for name in order
    ]


def valuation_spread(valuation: MarketValuation) -> float | None:
    prices = [
        float(item.price_czk)
        for item in valuation.comparables
        if item.price_czk is not None and item.price_czk > 0
    ]
    if valuation.market_price_czk and prices:
        return round((max(prices) - min(prices)) / valuation.market_price_czk, 4)
    return None


def confidence_causes(valuation: MarketValuation) -> list[str]:
    if valuation.confidence != "low":
        return []
    causes: list[str] = []
    if valuation.status == "duplicate_only_results":
        causes.append("duplicate_only_results")
    if valuation.comparables_unique <= 0:
        causes.append("no_unique_comparables")
    if len(set(valuation.sources_used)) == 1:
        causes.append("one_source")
    if 0 < valuation.comparables_unique < 3:
        causes.append("few_comparables")
    if (
        valuation.comparables_unique > 0
        and valuation.exact_comparables + valuation.close_comparables == 0
    ):
        causes.append("weak_model_match")
    spread = valuation_spread(valuation)
    if spread is not None and spread > 0.55:
        causes.append("large_price_spread")
    if (
        valuation.status == "foreign_only_estimate"
        or (
            valuation.foreign_comparables > 0
            and valuation.cz_comparables == 0
        )
    ):
        causes.append("foreign_market")
    if not causes:
        causes.append("other")
    return causes


def primary_confidence_cause(causes: list[str]) -> str:
    priority = (
        "duplicate_only_results",
        "no_unique_comparables",
        "foreign_market",
        "weak_model_match",
        "few_comparables",
        "large_price_spread",
        "one_source",
        "other",
    )
    return next((item for item in priority if item in causes), "other")


def missing_quick_sale_cause(
    analysis: ListingAnalysis,
    valuation: MarketValuation,
) -> str:
    if valuation.quick_sale_price_czk is not None:
        return ""
    if valuation.status == "no_matching_comparables":
        return "no_matching_comparables"
    if valuation.status == "duplicate_only_results":
        return "duplicate_only_results"
    if valuation.market_price_czk is None and valuation.comparables_unique > 0:
        return "no_market_median"
    identity = analysis.identity
    if (
        valuation.status == "ambiguous_model"
        or identity is None
        or not identity.brand
        or not identity.model
    ):
        return "insufficient_model_identification"
    if valuation.status in {"currency_error", "source_unavailable"}:
        return "technical_error"
    return "other"


def matched_phrases(text: str) -> list[str]:
    normalized = normalize_text(text)
    return sorted(
        phrase for phrase in MISSED_POSITIVE_PHRASES if phrase in normalized
    )


def condition_group(
    listing: Listing,
    priority_description_min_chars: int,
) -> tuple[str, list[str], list[str]]:
    normalized = normalize_text(f"{listing.title} {listing.description}")
    positives = matched_phrases(normalized)
    negative_markers = (
        "nutny servis",
        "potrebuje servis",
        "pred servisem",
        "na opravu",
        "nepojizdne",
        "poskozene",
        "nefunkcni",
    )
    negatives = [item for item in negative_markers if item in normalized]
    labels: list[str] = []
    if positives and negatives:
        labels.append("positive_negative_conflict")
    elif positives:
        labels.append("unrecognized_positive_condition_phrase")
    description_length = len(normalize_text(listing.description))
    if description_length < priority_description_min_chars:
        labels.append("short_description")
    if not positives and not negatives:
        labels.append("no_condition_information_in_text")
        if listing.image_url:
            labels.append("photos_but_no_textual_condition")
    if negatives and not positives:
        labels.append("other_unrecognized_negative_condition")
    if "positive_negative_conflict" in labels:
        primary = "positive_negative_conflict"
    elif "unrecognized_positive_condition_phrase" in labels:
        primary = "unrecognized_positive_condition_phrase"
    elif "short_description" in labels:
        primary = "short_description"
    elif "photos_but_no_textual_condition" in labels:
        primary = "photos_but_no_textual_condition"
    elif "no_condition_information_in_text" in labels:
        primary = "no_condition_information_in_text"
    else:
        primary = "other"
    return primary, positives, labels


def hard_filter_diagnostic(
    listing: Listing,
    analysis: ListingAnalysis,
) -> dict[str, Any]:
    computed = hard_filter_reason(listing, analysis.identity)
    stored_excluded = (
        analysis.priority_class == "excluded"
        or analysis.notification_status == "excluded"
    )
    stored_reason = analysis.notification_reason if stored_excluded else ""
    effective = computed or stored_reason
    return {
        "result": "excluded" if effective or stored_excluded else "pass",
        "reason": effective or ("stored_excluded" if stored_excluded else ""),
        "computed_reason": computed,
        "computed_source": (
            "ListingAnalysis.identity.audience"
            if computed == "hard_filter_kids_bike"
            else "listing.title accessory terms"
            if computed == "hard_filter_accessory_or_part"
            else ""
        ),
        "stored_priority_class": analysis.priority_class,
        "stored_notification_status": analysis.notification_status,
        "stored_notification_reason": analysis.notification_reason,
        "historical_hard_filter_result": (
            "excluded" if stored_excluded else "pass"
        ),
        "historical_implementation_checked_kids_audience": False,
        "current_implementation_checks_kids_audience": True,
    }


def blocker_sources(
    evaluation: DealEvaluation,
    listing: Listing,
    analysis: ListingAnalysis,
    valuation: MarketValuation,
    hard_filter: dict[str, Any],
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for flag in evaluation.flags:
        if flag == "condition_unknown":
            source = {
                "flag": flag,
                "source": "listing.title + listing.description",
                "value": "no configured condition term matched",
            }
        elif flag == "low_market_confidence":
            source = {
                "flag": flag,
                "source": "saved Stage 2.1 MarketValuation",
                "value": {
                    "confidence": valuation.confidence,
                    "status": valuation.status,
                    "comparables_unique": valuation.comparables_unique,
                    "sources_used": valuation.sources_used,
                    "causes": confidence_causes(valuation),
                },
            }
        elif flag == "purchase_price_outlier":
            source = {
                "flag": flag,
                "source": "Stage 2.2 price-outlier rule",
                "value": {
                    "purchase_price_czk": evaluation.purchase_price_czk,
                    "market_median_czk": evaluation.market_median_czk,
                    "threshold_percent": evaluation.config_snapshot.get(
                        "price_outlier_discount_percent"
                    ),
                },
            }
        elif flag == "stage_1_2_manual_risk":
            source = {
                "flag": flag,
                "source": "saved ListingAnalysis.risks",
                "value": analysis.risks,
            }
        elif flag in {"existing_hard_filter", "hard_filter_kids_bike", "hard_filter_accessory_or_part"}:
            source = {
                "flag": flag,
                "source": hard_filter.get("computed_source") or "saved ListingAnalysis",
                "value": hard_filter,
            }
        elif flag in MANUAL_FLAGS:
            source = {
                "flag": flag,
                "source": "Stage 2.2 evaluator",
                "value": evaluation.reasons,
            }
        else:
            continue
        sources.append(source)
    return sources


def load_rows(
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT
            l.data_json AS listing_json,
            l.analysis_json AS analysis_json,
            l.notification_status AS stored_notification_status,
            l.notification_reason AS stored_notification_reason,
            mv.data_json AS valuation_json,
            COALESCE(dc.acquisition_costs_czk, 0) AS acquisition_costs_czk,
            COALESCE(dc.logistics_costs_czk, 0) AS logistics_costs_czk,
            COALESCE(dc.platform_fees_czk, 0) AS platform_fees_czk
        FROM market_valuations AS mv
        JOIN listings AS l
          ON l.source = mv.listing_source
         AND l.external_id = mv.listing_external_id
        LEFT JOIN deal_cost_overrides AS dc
          ON dc.listing_source = mv.listing_source
         AND dc.listing_external_id = mv.listing_external_id
        ORDER BY mv.listing_source, mv.listing_external_id
        """
    ).fetchall()
    prepared: list[dict[str, Any]] = []
    for row in rows:
        listing = Listing.from_dict(json.loads(row["listing_json"]))
        valuation = MarketValuation.from_dict(json.loads(row["valuation_json"]))
        raw_analysis = str(row["analysis_json"] or "")
        if raw_analysis and raw_analysis != "{}":
            analysis = ListingAnalysis.from_dict(json.loads(raw_analysis))
        else:
            analysis = ListingAnalysis(
                preliminary_priority_score=0,
                priority_class="manual_review",
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
                "costs": DealCosts(
                    float(row["acquisition_costs_czk"]),
                    float(row["logistics_costs_czk"]),
                    float(row["platform_fees_czk"]),
                ),
            }
        )
    return prepared


def main() -> int:
    args = parse_args()
    database = args.database.resolve()
    baseline_path = args.baseline_json.resolve()
    output_dir = args.output_dir.resolve()
    if not database.is_file():
        raise FileNotFoundError(database)
    if not baseline_path.is_file():
        raise FileNotFoundError(baseline_path)

    baseline_payload = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline_by_id = {
        item["id"]: item for item in baseline_payload.get("results", [])
    }
    scoring_config, priority_config = load_offline_config(args.config.resolve())
    evaluator = DealEvaluator(scoring_config)

    before = database.stat()
    connection = sqlite3.connect(read_only_uri(database), uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    try:
        rows = load_rows(connection)
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        total_changes = int(connection.total_changes)
    finally:
        connection.close()
    after = database.stat()
    if len(rows) != args.expected_count:
        raise RuntimeError(
            f"Expected {args.expected_count} valuations, found {len(rows)}"
        )

    results: list[dict[str, Any]] = []
    csv_rows: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    low_confidence_primary: Counter[str] = Counter()
    low_confidence_multi: Counter[str] = Counter()
    condition_primary: Counter[str] = Counter()
    condition_multilabel: Counter[str] = Counter()
    missing_quick_counts: Counter[str] = Counter()
    phrase_counts: Counter[str] = Counter()
    financial_hot: list[dict[str, Any]] = []
    formula_violations: list[dict[str, Any]] = []

    for row in rows:
        listing: Listing = row["listing"]
        analysis: ListingAnalysis = row["analysis"]
        valuation: MarketValuation = row["valuation"]
        costs: DealCosts = row["costs"]
        evaluation = evaluator.evaluate(listing, analysis, costs)
        baseline = baseline_by_id.get(listing.key, {})
        scenario_a = bool(
            evaluation.net_profit_czk is not None
            and evaluation.roi_percent is not None
            and evaluation.net_profit_czk >= scoring_config.hot_min_profit_czk
            and evaluation.roi_percent >= scoring_config.hot_min_roi_percent
        )
        scenario_b = bool(
            evaluation.net_profit_czk is not None
            and evaluation.roi_percent is not None
            and evaluation.net_profit_czk > 0
            and evaluation.roi_percent >= scoring_config.high_roi_percent
        )
        hard_filter = hard_filter_diagnostic(listing, analysis)
        causes = confidence_causes(valuation)
        missing_cause = missing_quick_sale_cause(analysis, valuation)
        baseline_unknown_manual = (
            baseline.get("status") == "MANUAL_REVIEW"
            and baseline.get("condition") == "unknown"
        )
        condition_diagnostic = ""
        phrases: list[str] = []
        condition_labels: list[str] = []
        if baseline_unknown_manual:
            condition_diagnostic, phrases, condition_labels = condition_group(
                listing,
                priority_config.description_min_chars,
            )
            condition_primary[condition_diagnostic] += 1
            condition_multilabel.update(condition_labels)
            phrase_counts.update(phrases)
        if (
            baseline.get("status") == "MANUAL_REVIEW"
            and baseline.get("confidence_level") == "low"
        ):
            low_confidence_primary[primary_confidence_cause(causes)] += 1
            low_confidence_multi.update(causes)
        if missing_cause:
            missing_quick_counts[missing_cause] += 1

        manual_flags = sorted(set(evaluation.flags) & MANUAL_FLAGS)
        blocker_details = blocker_sources(
            evaluation,
            listing,
            analysis,
            valuation,
            hard_filter,
        )
        detail = {
            "listing_id": listing.key,
            "listing": listing.to_dict(),
            "saved_stage_1_2_analysis": analysis.to_dict(),
            "saved_stage_2_1_market_valuation": valuation.to_dict(),
            "deal_costs": costs.to_dict(),
            "baseline_acceptance_result": baseline,
            "stage_2_2_evaluation": evaluation.to_dict(),
            "financial_hot_scenario_a": scenario_a,
            "financial_hot_scenario_b": scenario_b,
            "hard_filter": hard_filter,
            "manual_flags": manual_flags,
            "blocker_sources": blocker_details,
            "confidence_diagnostic": {
                "causes": causes,
                "primary_cause": primary_confidence_cause(causes) if causes else "",
                "spread_ratio": valuation_spread(valuation),
            },
            "condition_diagnostic": {
                "baseline_unknown_manual": baseline_unknown_manual,
                "group": condition_diagnostic,
                "labels": condition_labels,
                "unrecognized_positive_phrases": phrases,
                "description_normalized_length": len(
                    normalize_text(listing.description)
                ),
                "has_image": bool(listing.image_url),
            },
            "missing_quick_sale_cause": missing_cause,
        }
        results.append(detail)
        status_counts[evaluation.status] += 1
        if evaluation.purchase_price_czk is not None:
            expected_base = (
                evaluation.purchase_price_czk
                + evaluation.acquisition_costs_czk
                + evaluation.logistics_costs_czk
                + evaluation.platform_fees_czk
            )
            expected_reserve = (
                expected_base * scoring_config.risk_reserve_percent / 100
            )
            expected_total = expected_base + expected_reserve
            checks = {
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
            if evaluation.quick_sale_price_czk is not None and expected_total > 0:
                expected_profit = evaluation.quick_sale_price_czk - expected_total
                expected_roi = expected_profit / expected_total * 100
                checks["net_profit_czk"] = (
                    evaluation.net_profit_czk,
                    expected_profit,
                )
                checks["roi_percent"] = (
                    evaluation.roi_percent,
                    expected_roi,
                )
            for field, (actual, expected) in checks.items():
                if actual is None or not math.isclose(
                    float(actual),
                    float(expected),
                    abs_tol=0.011,
                ):
                    formula_violations.append(
                        {
                            "listing_id": listing.key,
                            "field": field,
                            "actual": actual,
                            "expected": round(expected, 4),
                        }
                    )
        if scenario_a or scenario_b:
            financial_hot.append(detail)
        csv_rows.append(
            {
                "listing_id": listing.key,
                "url": listing.url,
                "title": listing.title,
                "description": listing.description,
                "purchase_price": evaluation.purchase_price_czk,
                "market_median": evaluation.market_median_czk,
                "quick_sale_price": evaluation.quick_sale_price_czk,
                "total_investment": evaluation.total_investment_czk,
                "net_profit": evaluation.net_profit_czk,
                "roi": evaluation.roi_percent,
                "liquidity": (
                    f"{evaluation.liquidity_level}:{evaluation.liquidity_score}"
                ),
                "confidence": (
                    f"{evaluation.confidence_level}:{evaluation.confidence_score}"
                ),
                "condition": evaluation.condition,
                "deal_score": evaluation.deal_score,
                "status": evaluation.status,
                "financial_hot_scenario_a": scenario_a,
                "financial_hot_scenario_b": scenario_b,
                "hard_filter_result": (
                    f"{hard_filter['result']}:{hard_filter['reason']}"
                    if hard_filter["reason"]
                    else hard_filter["result"]
                ),
                "manual_flags": "|".join(manual_flags),
                "decision_reasons": " | ".join(evaluation.reasons),
                "confidence_causes": "|".join(causes),
                "condition_diagnostic_group": condition_diagnostic,
                "missing_quick_sale_cause": missing_cause,
                "baseline_status": baseline.get("status", ""),
            }
        )

    duplicate_outlier_overlap = [
        item["listing_id"]
        for item in results
        if "purchase_price_outlier" in item["stage_2_2_evaluation"]["flags"]
        and "low_market_confidence" in item["stage_2_2_evaluation"]["flags"]
    ]
    kids = [
        item
        for item in results
        if item["hard_filter"]["reason"] == "hard_filter_kids_bike"
    ]
    informational_warning_rows = [
        item
        for item in results
        if item["saved_stage_2_1_market_valuation"]["warnings"]
        and "critical_market_warning"
        not in item["stage_2_2_evaluation"]["flags"]
    ]
    non_finite = []
    for item in results:
        evaluation = item["stage_2_2_evaluation"]
        for name in (
            "purchase_price_czk",
            "base_investment_czk",
            "risk_reserve_czk",
            "total_investment_czk",
            "market_median_czk",
            "quick_sale_price_czk",
            "net_profit_czk",
            "roi_percent",
            "deal_score",
        ):
            value = evaluation.get(name)
            if isinstance(value, (int, float)) and not math.isfinite(float(value)):
                non_finite.append({"listing_id": item["listing_id"], "field": name})

    baseline_status = Counter(
        item.get("status", "missing") for item in baseline_by_id.values()
    )
    status_transitions = Counter(
        (
            item["baseline_acceptance_result"].get("status", "missing"),
            item["stage_2_2_evaluation"]["status"],
        )
        for item in results
    )
    summary = {
        "processed": len(results),
        "baseline_status_distribution": {
            name: baseline_status.get(name, 0) for name in STATUS_ORDER
        },
        "new_status_distribution": {
            name: status_counts.get(name, 0) for name in STATUS_ORDER
        },
        "status_transitions": [
            {"from": old, "to": new, "count": count}
            for (old, new), count in sorted(status_transitions.items())
        ],
        "financial_hot_candidate_count": len(financial_hot),
        "financial_hot_candidate_ids": [
            item["listing_id"] for item in financial_hot
        ],
        "kids_hard_filter_count": len(kids),
        "kids_hard_filter_ids": [item["listing_id"] for item in kids],
        "low_confidence_manual_count": sum(low_confidence_primary.values()),
        "low_confidence_primary_distribution": counts_with_percent(
            low_confidence_primary,
            sum(low_confidence_primary.values()),
        ),
        "low_confidence_multilabel_distribution": counts_with_percent(
            low_confidence_multi,
            sum(low_confidence_primary.values()),
        ),
        "low_confidence_requested_groups": ordered_counts_with_percent(
            low_confidence_multi,
            sum(low_confidence_primary.values()),
            (
                "one_source",
                "few_comparables",
                "weak_model_match",
                "duplicate_only_results",
                "no_unique_comparables",
                "large_price_spread",
                "foreign_market",
                "other",
            ),
        ),
        "baseline_unknown_condition_manual_count": sum(condition_primary.values()),
        "unknown_condition_primary_distribution": counts_with_percent(
            condition_primary,
            sum(condition_primary.values()),
        ),
        "unknown_condition_multilabel_distribution": counts_with_percent(
            condition_multilabel,
            sum(condition_primary.values()),
        ),
        "unknown_condition_requested_groups": ordered_counts_with_percent(
            condition_multilabel,
            sum(condition_primary.values()),
            (
                "no_condition_information_in_text",
                "unrecognized_positive_condition_phrase",
                "photos_but_no_textual_condition",
                "short_description",
                "positive_negative_conflict",
                "other",
            ),
        ),
        "unrecognized_positive_phrase_frequency": [
            {"phrase": phrase, "count": count}
            for phrase, count in phrase_counts.most_common()
        ],
        "positive_condition_phrases_added": list(ADDED_POSITIVE_PHRASES),
        "positive_condition_phrases_not_added": {
            "malo jezdene": "Low usage does not by itself prove that no repair is needed.",
            "minimalne jezdene": "Low usage does not by itself prove that no repair is needed.",
        },
        "missing_quick_sale_count": sum(missing_quick_counts.values()),
        "missing_quick_sale_distribution": counts_with_percent(
            missing_quick_counts,
            sum(missing_quick_counts.values()),
        ),
        "missing_quick_sale_requested_groups": ordered_counts_with_percent(
            missing_quick_counts,
            sum(missing_quick_counts.values()),
            (
                "no_matching_comparables",
                "duplicate_only_results",
                "no_market_median",
                "insufficient_model_identification",
                "technical_error",
                "other",
            ),
        ),
        "purchase_outlier_and_low_confidence_overlap_count": len(
            duplicate_outlier_overlap
        ),
        "purchase_outlier_and_low_confidence_overlap_ids": duplicate_outlier_overlap,
        "informational_warning_without_critical_block_count": len(
            informational_warning_rows
        ),
        "critical_warning_block_count": sum(
            "critical_market_warning" in item["stage_2_2_evaluation"]["flags"]
            for item in results
        ),
        "non_finite_value_count": len(non_finite),
        "non_finite_values": non_finite,
        "formula_violation_count": len(formula_violations),
        "formula_violations": formula_violations,
        "deal_score_out_of_range_count": sum(
            not 0 <= item["stage_2_2_evaluation"]["deal_score"] <= 100
            for item in results
        ),
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "dealradar-stage-2-2-all-74.csv"
    json_path = output_dir / "dealradar-stage-2-2-all-74.json"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(csv_rows)

    payload = {
        "run": {
            "completed_at": datetime.now(UTC).isoformat(),
            "database_path": str(database),
            "database_open_mode": "mode=ro + PRAGMA query_only=ON",
            "database_integrity_check": integrity,
            "database_total_changes": total_changes,
            "database_size_before": before.st_size,
            "database_size_after": after.st_size,
            "database_mtime_ns_before": before.st_mtime_ns,
            "database_mtime_ns_after": after.st_mtime_ns,
            "config_path": str(args.config.resolve()),
            "baseline_acceptance_json": str(baseline_path),
            "telegram_delivery_enabled": False,
            "telegram_client_instantiated": False,
            "market_price_engine_instantiated": False,
            "market_search_calls": 0,
            "external_api_calls": 0,
            "production_database_used": False,
            "production_process_restarted": False,
        },
        "configuration": {
            "deal_scoring": asdict(scoring_config),
        },
        "summary": summary,
        "financial_hot_candidates": financial_hot,
        "kids_hard_filter_rows": kids,
        "results": results,
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "database_path": str(database),
                "csv_path": str(csv_path),
                "json_path": str(json_path),
                "processed": len(results),
                "summary": summary,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
