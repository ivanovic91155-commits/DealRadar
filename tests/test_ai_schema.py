from __future__ import annotations

import json
import unittest
from typing import Any

from deal_radar.ai.client import AIInvalidResponse
from deal_radar.ai.prompt_loader import PromptNotFound, clear_cache, load_prompt
from deal_radar.ai.schema_validate import parse_listing_analysis

BUNDLE = load_prompt("listing-analysis", "v1.0.0")
SCHEMA = BUNDLE.schema


def payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "classification": {
            "is_bicycle": True,
            "listing_type": "COMPLETE_BICYCLE",
            "relevance_confidence": 0.98,
        },
        "identity": {
            "brand": "Trek",
            "model": "Marlin 7",
            "generation": "Gen 3",
            "model_year": 2024,
            "bike_type": "MTB_HARDTAIL",
            "is_electric": False,
            "identity_confidence": 0.91,
            "manual_identification_needed": False,
        },
        "specifications": {
            "frame_size_raw": "M/L",
            "frame_size_normalized": "L",
            "wheel_size_inches": 29,
            "frame_material": "ALUMINIUM",
            "fork": "RockShox Judy",
            "groupset": "Shimano Deore",
            "brakes": "HYDRAULIC_DISC",
        },
        "condition": {
            "claimed_condition": "GOOD",
            "service_needed": False,
            "defects": [],
            "missing_parts": [],
            "condition_confidence": 0.73,
        },
        "opportunity": {
            "seller_urgency": "MEDIUM",
            "listing_quality": "MEDIUM",
            "hidden_opportunity": False,
        },
        "risk": {
            "risk_flags": [],
            "suspicious_price": False,
            "possible_stolen_bike": False,
            "possible_scam": False,
        },
        "evidence": [
            {"field": "brand", "value": "Trek", "source": "TITLE", "excerpt": "Trek Marlin 7"}
        ],
        "warnings": [],
    }
    base.update(overrides)
    return base


class PromptLoaderTest(unittest.TestCase):
    def tearDown(self) -> None:
        clear_cache()

    def test_bundle_exposes_versioned_identifiers(self) -> None:
        bundle = load_prompt("listing-analysis", "v1.0.0")
        self.assertEqual(bundle.label, "listing-analysis-v1.0.0")
        self.assertEqual(bundle.schema_version, "dealradar.ai-analysis.v1")
        self.assertEqual(bundle.schema_name, "listing_analysis")
        self.assertIn("untrusted", bundle.system.casefold())

    def test_user_message_appends_data_after_the_task(self) -> None:
        bundle = load_prompt("listing-analysis", "v1.0.0")
        message = bundle.build_user_message({"title": "Trek Marlin 7"})
        self.assertTrue(message.rstrip().endswith('{"title": "Trek Marlin 7"}'))
        self.assertIn("DATA:", message)

    def test_missing_version_raises_a_clear_error(self) -> None:
        with self.assertRaises(PromptNotFound):
            load_prompt("listing-analysis", "v9.9.9")


class StrictSchemaShapeTest(unittest.TestCase):
    """Structured Outputs отвергнет схему, нарушающую правила strict mode."""

    def walk(self, node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                self.walk(item)
            return
        if not isinstance(node, dict):
            return
        types = node.get("type")
        types = types if isinstance(types, list) else [types]
        if "object" in types:
            self.assertIs(node.get("additionalProperties"), False, node)
            declared = sorted((node.get("properties") or {}).keys())
            self.assertEqual(sorted(node.get("required", [])), declared, node)
        for value in node.values():
            self.walk(value)

    def test_every_object_declares_all_properties_as_required(self) -> None:
        self.walk(SCHEMA)

    def test_schema_uses_no_unsupported_keywords(self) -> None:
        forbidden = {"allOf", "not", "if", "then", "else", "dependentRequired", "dependentSchemas"}
        self.assertFalse(forbidden & set(json.dumps(SCHEMA).split('"')))

    def test_optional_values_are_expressed_as_null_unions(self) -> None:
        model = SCHEMA["properties"]["identity"]["properties"]["model"]
        self.assertEqual(model["type"], ["string", "null"])


class ValidationTest(unittest.TestCase):
    def test_valid_payload_maps_onto_typed_blocks(self) -> None:
        result = parse_listing_analysis(payload(), SCHEMA)
        self.assertTrue(result.classification.is_bicycle)
        self.assertEqual(result.identity.brand, "Trek")
        self.assertEqual(result.identity.model, "Marlin 7")
        self.assertEqual(result.identity.model_year, 2024)
        self.assertEqual(result.specifications.wheel_size_inches, 29.0)
        self.assertEqual(result.condition.claimed_condition, "GOOD")
        self.assertEqual(len(result.evidence), 1)

    def test_missing_required_block_is_rejected(self) -> None:
        broken = payload()
        del broken["identity"]
        with self.assertRaises(AIInvalidResponse):
            parse_listing_analysis(broken, SCHEMA)

    def test_empty_payload_is_rejected(self) -> None:
        with self.assertRaises(AIInvalidResponse):
            parse_listing_analysis({}, SCHEMA)

    def test_confidence_outside_the_unit_range_is_clamped(self) -> None:
        result = parse_listing_analysis(
            payload(identity={"brand": "Trek", "model": "Marlin", "identity_confidence": 7.5}),
            SCHEMA,
        )
        self.assertEqual(result.identity.identity_confidence, 1.0)

    def test_non_numeric_confidence_becomes_zero(self) -> None:
        result = parse_listing_analysis(
            payload(condition={"claimed_condition": "GOOD", "condition_confidence": "very sure"}),
            SCHEMA,
        )
        self.assertEqual(result.condition.condition_confidence, 0.0)

    def test_value_outside_the_schema_enum_falls_back_to_the_default(self) -> None:
        result = parse_listing_analysis(
            payload(
                classification={"is_bicycle": True, "listing_type": "SPACESHIP"},
                condition={"claimed_condition": "PRISTINE"},
            ),
            SCHEMA,
        )
        self.assertEqual(result.classification.listing_type, "OTHER")
        self.assertEqual(result.condition.claimed_condition, "UNKNOWN")

    def test_unknown_risk_flags_are_dropped_and_duplicates_collapse(self) -> None:
        result = parse_listing_analysis(
            payload(
                risk={"risk_flags": ["PRICE_TOO_LOW", "PRICE_TOO_LOW", "ALIENS", "NO_PHOTOS"]}
            ),
            SCHEMA,
        )
        self.assertEqual(result.risk.risk_flags, ["PRICE_TOO_LOW", "NO_PHOTOS"])

    def test_model_without_brand_is_discarded(self) -> None:
        result = parse_listing_analysis(
            payload(identity={"brand": None, "model": "Marlin 7", "identity_confidence": 0.9}),
            SCHEMA,
        )
        self.assertIsNone(result.identity.model)
        self.assertIn("ai_model_without_brand_discarded", result.warnings)
        self.assertTrue(result.identity.manual_identification_needed)

    def test_model_that_only_repeats_the_brand_is_discarded(self) -> None:
        result = parse_listing_analysis(
            payload(identity={"brand": "Scott", "model": "scott", "identity_confidence": 0.8}),
            SCHEMA,
        )
        self.assertIsNone(result.identity.model)
        self.assertIn("ai_model_repeated_brand_discarded", result.warnings)

    def test_absent_model_always_requests_manual_identification(self) -> None:
        result = parse_listing_analysis(
            payload(
                identity={
                    "brand": "Cube",
                    "model": None,
                    "identity_confidence": 0.4,
                    "manual_identification_needed": False,
                }
            ),
            SCHEMA,
        )
        self.assertTrue(result.identity.manual_identification_needed)

    def test_implausible_year_and_wheel_size_become_null(self) -> None:
        result = parse_listing_analysis(
            payload(
                identity={"brand": "Trek", "model": "Marlin", "model_year": 1492},
                specifications={"wheel_size_inches": 900},
            ),
            SCHEMA,
        )
        self.assertIsNone(result.identity.model_year)
        self.assertIsNone(result.specifications.wheel_size_inches)

    def test_evidence_without_an_excerpt_is_dropped(self) -> None:
        result = parse_listing_analysis(
            payload(
                evidence=[
                    {"field": "brand", "value": "Trek", "source": "TITLE", "excerpt": ""},
                    {"field": "invented", "value": "x", "source": "TITLE", "excerpt": "x"},
                    {"field": "model", "value": "Marlin 7", "source": "TITLE", "excerpt": "Marlin"},
                ]
            ),
            SCHEMA,
        )
        self.assertEqual([item["field"] for item in result.evidence], ["model"])

    def test_wrongly_typed_blocks_degrade_to_defaults_instead_of_crashing(self) -> None:
        result = parse_listing_analysis(
            payload(specifications="unknown", opportunity=[1, 2], risk=None), SCHEMA
        )
        self.assertIsNone(result.specifications.fork)
        self.assertEqual(result.opportunity.seller_urgency, "UNKNOWN")
        self.assertEqual(result.risk.risk_flags, [])


if __name__ == "__main__":
    unittest.main()
