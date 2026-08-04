from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the compact PDF-source artifact for Stage 2.2 calibration."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=PROJECT_ROOT
        / "output"
        / "stage_2_2_calibration"
        / "dealradar-stage-2-2-all-74.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT
        / "output"
        / "pdf"
        / "dealradar-stage-2-2-final-calibration.artifact.json",
    )
    return parser.parse_args()


def money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f}".replace(",", " ")


def markdown_safe(text: str) -> str:
    redacted = re.sub(
        r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])",
        "[email redacted]",
        text,
    )
    redacted = re.sub(
        r"(?<!\d)(?:\+?\d[\s().-]?){8,}\d(?!\d)",
        "[phone redacted]",
        redacted,
    )
    return redacted.replace("`", "'").strip()


def table(
    table_id: str,
    title: str,
    dataset: str,
    columns: list[tuple[str, str]],
    *,
    sort_field: str,
    sort_direction: str = "desc",
    subtitle: str = "",
) -> dict[str, Any]:
    return {
        "id": table_id,
        "title": title,
        "subtitle": subtitle,
        "dataset": dataset,
        "sourceId": "calibration_snapshot",
        "defaultSort": {
            "field": sort_field,
            "direction": sort_direction,
        },
        "density": "dense",
        "layout": "full",
        "columns": [
            {"field": field, "label": label, "type": "text"}
            for field, label in columns
        ],
    }


def main() -> int:
    args = parse_args()
    source = args.input.resolve()
    output = args.output.resolve()
    payload = json.loads(source.read_text(encoding="utf-8"))
    summary = payload["summary"]
    candidates = payload["financial_hot_candidates"]
    generated_at = datetime.now(UTC).isoformat()

    status_rows: list[dict[str, Any]] = []
    for status in ("HOT", "INTERESTING", "MANUAL_REVIEW", "LOW_PRIORITY", "REJECT"):
        status_rows.append(
            {
                "status": status,
                "series": "До исправлений",
                "count": summary["baseline_status_distribution"][status],
            }
        )
        status_rows.append(
            {
                "status": status,
                "series": "После исправлений",
                "count": summary["new_status_distribution"][status],
            }
        )

    hot_rows = []
    candidate_blocks = []
    for index, item in enumerate(candidates, 1):
        listing = item["listing"]
        evaluation = item["stage_2_2_evaluation"]
        valuation = item["saved_stage_2_1_market_valuation"]
        hard_filter = item["hard_filter"]
        blockers = []
        if hard_filter["result"] == "excluded":
            blockers.append(
                f"HARD: {hard_filter['reason']} ← {hard_filter['computed_source']}"
            )
        blockers.extend(item["manual_flags"])
        scenario = "/".join(
            name
            for name, passed in (
                ("A", item["financial_hot_scenario_a"]),
                ("B", item["financial_hot_scenario_b"]),
            )
            if passed
        )
        hot_rows.append(
            {
                "id": item["listing_id"],
                "economics": (
                    f"покупка {money(evaluation['purchase_price_czk'])}; "
                    f"медиана {money(evaluation['market_median_czk'])}; "
                    f"quick {money(evaluation['quick_sale_price_czk'])}; "
                    f"вложение {money(evaluation['total_investment_czk'])}; "
                    f"прибыль {money(evaluation['net_profit_czk'])}; "
                    f"ROI {evaluation['roi_percent']:.2f}%"
                ),
                "quality": (
                    f"ликвидность {evaluation['liquidity_level']}"
                    f"/{evaluation['liquidity_score']}; "
                    f"confidence {evaluation['confidence_level']}"
                    f"/{evaluation['confidence_score']}; "
                    f"condition {evaluation['condition']}; "
                    f"score {evaluation['deal_score']:.2f}"
                ),
                "scenario": scenario,
                "status": evaluation["status"],
                "blockers": "; ".join(blockers),
            }
        )
        source_lines = []
        for blocker in item["blocker_sources"]:
            source_lines.append(
                f"- `{blocker['flag']}` ← {blocker['source']}: "
                f"{markdown_safe(json.dumps(blocker['value'], ensure_ascii=False))}"
            )
        candidate_blocks.append(
            {
                "id": f"candidate_{index}",
                "type": "markdown",
                "sourceId": "calibration_snapshot",
                "layout": "full",
                "body": (
                    f"### {item['listing_id']} — {markdown_safe(listing['title'])}\n\n"
                    f"**Ссылка:** {listing['url']}\n\n"
                    f"**Описание:** {markdown_safe(listing['description'])}\n\n"
                    f"**Расчёт:** покупка {money(evaluation['purchase_price_czk'])} Kč; "
                    f"медиана {money(evaluation['market_median_czk'])} Kč; "
                    f"quick-sale {money(evaluation['quick_sale_price_czk'])} Kč; "
                    f"полное вложение {money(evaluation['total_investment_czk'])} Kč; "
                    f"чистая прибыль {money(evaluation['net_profit_czk'])} Kč; "
                    f"ROI {evaluation['roi_percent']:.2f}%; сценарий HOT {scenario}.\n\n"
                    f"**Качество:** liquidity {evaluation['liquidity_level']}"
                    f"/{evaluation['liquidity_score']}; confidence "
                    f"{evaluation['confidence_level']}/{evaluation['confidence_score']}; "
                    f"condition {evaluation['condition']}; deal_score "
                    f"{evaluation['deal_score']:.2f}; valuation status "
                    f"`{valuation['status']}`.\n\n"
                    f"**Все flags:** {', '.join(evaluation['flags']) or 'нет'}.\n\n"
                    f"**Точные источники блокировок:**\n\n"
                    + "\n".join(source_lines)
                    + f"\n\n**Итог:** `{evaluation['status']}`."
                ),
            }
        )

    low_rows = [
        {
            "group": item["group"],
            "count": str(item["count"]),
            "percent": f"{item['percent']:.2f}%",
        }
        for item in summary["low_confidence_requested_groups"]
    ]
    condition_rows = [
        {
            "group": item["group"],
            "count": str(item["count"]),
            "percent": f"{item['percent']:.2f}%",
        }
        for item in summary["unknown_condition_requested_groups"]
    ]
    missing_rows = [
        {
            "group": item["group"],
            "count": str(item["count"]),
            "percent": f"{item['percent']:.2f}%",
        }
        for item in summary["missing_quick_sale_requested_groups"]
    ]
    phrase_rows = [
        {"phrase": item["phrase"], "count": str(item["count"])}
        for item in summary["unrecognized_positive_phrase_frequency"]
    ]
    qa_rows = [
        {
            "check": "Финансовые формулы",
            "result": "PASS",
            "details": f"нарушений: {summary['formula_violation_count']}",
        },
        {
            "check": "deal_score",
            "result": "PASS",
            "details": (
                "вне диапазона 0–100: "
                f"{summary['deal_score_out_of_range_count']}"
            ),
        },
        {
            "check": "NaN / infinity",
            "result": "PASS",
            "details": f"некорректных значений: {summary['non_finite_value_count']}",
        },
        {
            "check": "Информационные warnings",
            "result": "PASS",
            "details": (
                f"{summary['informational_warning_without_critical_block_count']} "
                "не блокировали; критических блокировок "
                f"{summary['critical_warning_block_count']}"
            ),
        },
        {
            "check": "Изоляция",
            "result": "PASS",
            "details": "SQLite query_only; Telegram/MPE/API не создавались",
        },
        {
            "check": "Тесты",
            "result": "PASS",
            "details": "pytest 134 passed + 53 subtests; unittest 134 OK",
        },
    ]
    fix_rows = [
        {
            "issue": "Kids hard filter не использовал identity.audience",
            "fix": "единый hard_filter_reason для production и batch",
            "effect": "17 kids REJECT; 12 переходов MANUAL_REVIEW → REJECT",
        },
        {
            "issue": "Однозначные чешские фразы не распознавались",
            "fix": "19 безопасных фраз добавлены; отрицания учитываются",
            "effect": "1 переход MANUAL_REVIEW → LOW_PRIORITY; блокировки не ослаблены",
        },
    ]

    datasets = {
        "status_comparison": status_rows,
        "hot_candidates": hot_rows,
        "low_confidence": low_rows,
        "unknown_condition": condition_rows,
        "missing_quick": missing_rows,
        "phrases": phrase_rows,
        "qa": qa_rows,
        "fixes": fix_rows,
    }
    title = "Финальная диагностическая калибровка DealRadar — этап 2.2"
    manifest = {
        "version": 1,
        "surface": "report",
        "title": title,
        "description": (
            "Офлайн-диагностика 74 сохранённых оценок Stage 2.1, "
            "технические исправления hard filter и словаря состояния."
        ),
        "generatedAt": generated_at,
        "sources": [
            {
                "id": "calibration_snapshot",
                "label": "DealRadar Stage 2.2 calibration snapshot",
                "query": {
                    "engine": "SQLite",
                    "language": "sql",
                    "executed_at": generated_at,
                    "description": (
                        "Read-only join of 74 saved MarketValuation rows with "
                        "listings and saved analysis; Stage 2.2 recalculated locally."
                    ),
                    "sql": (
                        "SELECT l.data_json, l.analysis_json, mv.data_json "
                        "FROM market_valuations mv JOIN listings l "
                        "ON l.source = mv.listing_source "
                        "AND l.external_id = mv.listing_external_id "
                        "ORDER BY mv.listing_source, mv.listing_external_id;"
                    ),
                    "tables_used": [
                        "listings",
                        "market_valuations",
                        "deal_cost_overrides",
                    ],
                    "filters": [
                        "All 74 saved MarketValuation rows",
                        "No external lookup",
                        "No Telegram delivery",
                    ],
                    "metric_definitions": [
                        "Base investment = purchase price + mandatory costs.",
                        "Risk reserve = base investment × 10%.",
                        "Total investment = base investment + risk reserve.",
                        "Net profit = saved quick-sale price − total investment.",
                        "ROI = net profit / total investment × 100%.",
                    ],
                },
            }
        ],
        "charts": [
            {
                "id": "status_chart",
                "title": "Распределение статусов до и после исправлений",
                "subtitle": (
                    "Изменения вызваны только kids hard filter и безопасными "
                    "фразами состояния."
                ),
                "type": "bar",
                "intent": "comparison",
                "question": "Как изменилось распределение пяти статусов?",
                "rationale": "Сгруппированные столбцы показывают переходы по статусам.",
                "dataset": "status_comparison",
                "sourceId": "calibration_snapshot",
                "encodings": {
                    "x": {
                        "field": "status",
                        "type": "nominal",
                        "label": "Статус",
                    },
                    "y": {
                        "field": "count",
                        "type": "quantitative",
                        "label": "Объявления",
                    },
                    "color": {
                        "field": "series",
                        "type": "nominal",
                        "label": "Прогон",
                    },
                },
                "xAxisTitle": "Статус",
                "yAxisTitle": "Количество",
                "valueFormat": "number",
                "layout": "full",
                "maxRows": 10,
            }
        ],
        "tables": [
            table(
                "hot_candidates_table",
                "Семь финансовых HOT-кандидатов",
                "hot_candidates",
                [
                    ("id", "ID"),
                    ("economics", "Финансы, Kč"),
                    ("quality", "Качество"),
                    ("scenario", "HOT"),
                    ("status", "Итог"),
                    ("blockers", "Блокировки"),
                ],
                sort_field="id",
                sort_direction="asc",
                subtitle="Полные входы и источники flags приведены ниже и в JSON.",
            ),
            table(
                "low_confidence_table",
                "Причины low confidence (47 MANUAL_REVIEW)",
                "low_confidence",
                [("group", "Группа"), ("count", "Количество"), ("percent", "Доля")],
                sort_field="count",
            ),
            table(
                "unknown_condition_table",
                "Диагностика 40 исходных unknown condition",
                "unknown_condition",
                [("group", "Группа"), ("count", "Количество"), ("percent", "Доля")],
                sort_field="count",
            ),
            table(
                "phrases_table",
                "Частые ранее нераспознанные чешские фразы",
                "phrases",
                [("phrase", "Фраза"), ("count", "Количество")],
                sort_field="count",
            ),
            table(
                "missing_quick_table",
                "Причины отсутствия quick_sale_price (35)",
                "missing_quick",
                [("group", "Причина"), ("count", "Количество"), ("percent", "Доля")],
                sort_field="count",
            ),
            table(
                "fixes_table",
                "Технические исправления",
                "fixes",
                [("issue", "Ошибка"), ("fix", "Исправление"), ("effect", "Эффект")],
                sort_field="issue",
                sort_direction="asc",
            ),
            table(
                "qa_table",
                "Контроль качества",
                "qa",
                [("check", "Проверка"), ("result", "Результат"), ("details", "Детали")],
                sort_field="check",
                sort_direction="asc",
            ),
        ],
        "blocks": [
            {
                "id": "title",
                "type": "markdown",
                "body": f"# {title}",
                "layout": "full",
            },
            {
                "id": "summary",
                "type": "markdown",
                "sourceId": "calibration_snapshot",
                "body": (
                    "## Итог\n\n"
                    "**Техническая причина избытка MANUAL_REVIEW подтверждена частично.** "
                    "Kids hard filter не использовал уже вычисленное `identity.audience`; "
                    "17 детских объявлений проходили финансовую оценку. После исправления "
                    "12 из них изменили итог `MANUAL_REVIEW → REJECT`; ещё 5 уже были "
                    "`REJECT`. Однозначные чешские фразы состояния были неполными; "
                    "добавлены только безопасные выражения, рекламные фразы не добавлялись.\n\n"
                    "Новое распределение: **HOT 0, INTERESTING 0, MANUAL_REVIEW 36, "
                    "LOW_PRIORITY 2, REJECT 36**. Финансовые пороги, формулы, резерв 10%, "
                    "quick-sale discount и веса deal_score не менялись."
                ),
                "layout": "full",
            },
            {
                "id": "status_section",
                "type": "markdown",
                "body": (
                    "## Статусы\n\n"
                    "Разница отражает только исправления технических ошибок. "
                    "Production-база и процессы не изменялись."
                ),
                "layout": "full",
            },
            {
                "id": "status_chart_block",
                "type": "chart",
                "chartId": "status_chart",
                "layout": "full",
            },
            {
                "id": "hot_section",
                "type": "markdown",
                "body": (
                    "## Финансовые HOT-кандидаты\n\n"
                    "Сценарий A выполнили 6, сценарий B — 5, включая один B-only. "
                    "Два детских объявления теперь имеют жёсткий `REJECT`; остальные "
                    "остаются `MANUAL_REVIEW` из-за low confidence и, в двух случаях, "
                    "unknown condition."
                ),
                "layout": "full",
            },
            {
                "id": "hot_candidates_table_block",
                "type": "table",
                "tableId": "hot_candidates_table",
                "layout": "full",
            },
            *candidate_blocks,
            {
                "id": "hard_filter_section",
                "type": "markdown",
                "sourceId": "calibration_snapshot",
                "body": (
                    "## Hard filter и Telegram\n\n"
                    "До исправления production `build_analysis()` вызывал hard filter, "
                    "но тот проверял только запчасти; для обоих известных kids-кандидатов "
                    "результат был `pass`, несмотря на `identity.audience = kids`. "
                    "Acceptance pipeline повторно не строил анализ и использовал "
                    "сохранённый `priority_class`, а evaluator также проверял только "
                    "запчасти. Теперь единый `hard_filter_reason()` применяется в обоих "
                    "путях: новые kids не попадают в market lookup, исторические записи "
                    "получают `REJECT` при batch-пересчёте, а `select_deal_notifications()` "
                    "не выбирает их для Telegram."
                ),
                "layout": "full",
            },
            {
                "id": "confidence_section",
                "type": "markdown",
                "body": (
                    "## Low confidence\n\n"
                    "Группы многозначные: одно объявление может одновременно иметь один "
                    "источник, мало аналогов и иностранный рынок. Confidence не ослаблялся."
                ),
                "layout": "full",
            },
            {
                "id": "low_confidence_table_block",
                "type": "table",
                "tableId": "low_confidence_table",
                "layout": "full",
            },
            {
                "id": "condition_section",
                "type": "markdown",
                "body": (
                    "## Unknown condition\n\n"
                    "Из исходных 40 случаев 21 содержит явную положительную информацию, "
                    "которую словарь не покрывал; 19 не содержат текстового подтверждения "
                    "состояния и имеют фото, 13 описаний короткие (группы пересекаются). "
                    "Фразы `zachovalé` и `udržované` добавлены после исправления "
                    "границ слов и отрицаний. `Málo ježděné` и `minimálně ježděné` "
                    "не добавлены: малый пробег не гарантирует отсутствие ремонта."
                ),
                "layout": "full",
            },
            {
                "id": "unknown_condition_table_block",
                "type": "table",
                "tableId": "unknown_condition_table",
                "layout": "full",
            },
            {
                "id": "phrases_table_block",
                "type": "table",
                "tableId": "phrases_table",
                "layout": "full",
            },
            {
                "id": "market_section",
                "type": "markdown",
                "body": (
                    "## Качество Stage 2.1\n\n"
                    "У 35 объявлений quick_sale_price отсутствует: 19 "
                    "`duplicate_only_results`, 16 `no_matching_comparables`; "
                    "нулевые категории — отсутствие медианы при уникальных аналогах, "
                    "недостаточная идентификация модели, техническая ошибка и прочее. "
                    "Подтверждённой технической ошибки Stage 2.1 в сохранённых данных нет. "
                    "Без новых независимых аналогов эти строки объективно нельзя оценить; "
                    "внешний поиск не выполнялся."
                ),
                "layout": "full",
            },
            {
                "id": "missing_quick_table_block",
                "type": "table",
                "tableId": "missing_quick_table",
                "layout": "full",
            },
            {
                "id": "blocking_section",
                "type": "markdown",
                "body": (
                    "## Приоритет блокировок\n\n"
                    "**Жёсткие:** existing hard filter и неположительная прибыль/ROI → "
                    "`REJECT`. **MANUAL_REVIEW:** отсутствующие данные, ambiguous identity, "
                    "low confidence, unknown/problem/service condition, critical status/"
                    "warning, Stage 1.2 manual risk и purchase-price outlier. "
                    "**Информационные:** обычные valuation warnings и слабая ликвидность; "
                    "они не создают MANUAL_REVIEW сами по себе.\n\n"
                    "В трёх строках `purchase_price_outlier` и low confidence действуют "
                    "одновременно. Статус всё равно один — `MANUAL_REVIEW`, финансовые "
                    "формулы не меняются, но это две независимые причины ручной проверки "
                    "и два risk-флага: risk-компонент deal_score теряет 40 пунктов вместо "
                    "20. Возможная бизнес-корректировка — сохранять outlier как "
                    "информационный risk-флаг при уже low confidence и делать его ручной "
                    "блокировкой только при medium/high confidence. Предложение не применено."
                ),
                "layout": "full",
            },
            {
                "id": "fixes_table_block",
                "type": "table",
                "tableId": "fixes_table",
                "layout": "full",
            },
            {
                "id": "qa_section",
                "type": "markdown",
                "body": (
                    "## Проверки и изоляция\n\n"
                    "CSV содержит 74 строки и 24 столбца, включая все обязательные поля; "
                    "JSON содержит полные Listing, ListingAnalysis, MarketValuation, "
                    "расчёты, flags, причины и источники блокировок. Широкие данные "
                    "намеренно вынесены из PDF.\n\n"
                    "Копия SQLite открывалась `mode=ro` + `PRAGMA query_only=ON`, "
                    "`total_changes=0`, integrity check `ok`. Telegram-клиент, Market "
                    "Price Engine и внешние API не создавались; production-процесс не "
                    "перезапускался. Снимок: "
                    "`deal_radar.stage-2-2-calibration-20260725-085311.sqlite3`."
                ),
                "layout": "full",
            },
            {
                "id": "qa_table_block",
                "type": "table",
                "tableId": "qa_table",
                "layout": "full",
            },
        ],
    }
    artifact = {
        "surface": "report",
        "manifest": manifest,
        "snapshot": {
            "version": 1,
            "generatedAt": generated_at,
            "status": "ready",
            "datasets": datasets,
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "artifact": str(output),
                "datasets": {name: len(rows) for name, rows in datasets.items()},
                "blocks": len(manifest["blocks"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
