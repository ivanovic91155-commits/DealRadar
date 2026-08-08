from __future__ import annotations

import unittest

from deal_radar.bike_identity import (
    configure_identity,
    hard_filter_reason,
    has_accessory_terms,
    identify_bike,
)
from deal_radar.config import IdentityConfig
from deal_radar.models import BikeIdentity, Listing

RETAIL_IDENTITY_GATE = 0.7  # RetailConfig.identity_min_confidence
DEAL_IDENTITY_GATE = 0.55  # DealScoringConfig.minimum_identity_confidence


def listing(title: str, description: str = "") -> Listing:
    return Listing(
        source="bazos",
        external_id="1",
        profile="test",
        title=title,
        url="https://example.test/1",
        description=description,
    )


class DirtyTitleTest(unittest.TestCase):
    """Заголовки продавцов из раздела 20 аудита."""

    def assert_no_invented_model(self, title: str, expected_brand: str) -> None:
        identity = identify_bike(title)
        self.assertEqual(identity.brand, expected_brand, title)
        self.assertEqual(identity.model, "", title)
        self.assertFalse(identity.model_confirmed, title)
        self.assertEqual(identity.model_source, "", title)
        self.assertLess(identity.confidence, DEAL_IDENTITY_GATE, title)

    def test_condition_phrase_does_not_become_a_model(self) -> None:
        self.assert_no_invented_model("Trek horské kolo ve skvělém stavu", "Trek")

    def test_service_phrase_does_not_become_a_model(self) -> None:
        self.assert_no_invented_model("Cube kolo po servisu", "Cube")

    def test_sale_phrase_does_not_become_a_model(self) -> None:
        self.assert_no_invented_model("Specialized prodám levně", "Specialized")

    def test_urgency_phrase_does_not_become_a_model(self) -> None:
        self.assert_no_invented_model("Scott kolo rychle", "Scott")

    def test_wheelset_listing_is_hard_filtered(self) -> None:
        title = "Author Vision sada kol 29"
        self.assertEqual(
            hard_filter_reason(listing(title)), "hard_filter_accessory_or_part"
        )

    def test_standalone_frame_listing_is_hard_filtered(self) -> None:
        self.assertTrue(has_accessory_terms("Prodám rám Trek"))
        self.assertEqual(
            hard_filter_reason(listing("Prodám rám Trek")), "hard_filter_accessory_or_part"
        )

    def test_battery_listing_is_hard_filtered(self) -> None:
        identity = identify_bike("Kolo bez baterie")
        self.assertEqual(identity.brand, "")
        self.assertEqual(identity.model, "")
        self.assertEqual(
            hard_filter_reason(listing("Kolo bez baterie")), "hard_filter_accessory_or_part"
        )

    def test_unknown_bike_without_brand_gets_no_identity(self) -> None:
        identity = identify_bike("Nevím model, po synovi")
        self.assertEqual(identity.brand, "")
        self.assertEqual(identity.model, "")
        self.assertEqual(identity.confidence, 0.0)

    def test_kids_model_is_recognized_from_catalog(self) -> None:
        identity = identify_bike("Woom 4")
        self.assertEqual(identity.model, "4")
        self.assertTrue(identity.model_confirmed)
        self.assertEqual(identity.audience, "kids")
        self.assertEqual(hard_filter_reason(listing("Woom 4"), identity), "hard_filter_kids_bike")

    def test_ebike_is_recognized_without_the_word_ebike(self) -> None:
        identity = identify_bike("Haibike AllMtn 7 Bosch CX")
        self.assertEqual(identity.model, "AllMtn 7")
        self.assertTrue(identity.model_confirmed)
        self.assertIs(identity.electric, True)


class CatalogConfirmationTest(unittest.TestCase):
    def test_catalog_model_keeps_the_existing_cache_key(self) -> None:
        identity = identify_bike('Trek Marlin 7 Gen 3 29" 2025')
        self.assertEqual(identity.normalized_key, "trek|marlin-7|gen-3|2025|29")
        self.assertTrue(identity.model_confirmed)
        self.assertEqual(identity.model_source, "catalog")
        self.assertGreaterEqual(identity.confidence, 0.8)

    def test_trim_is_still_extracted_for_catalog_models(self) -> None:
        identity = identify_bike("Trek Fuel EX 8 GX Gen 3 2023 29")
        self.assertEqual(identity.model, "Fuel EX 8")
        self.assertEqual(identity.trim, "GX")

    def test_multi_word_model_wins_over_shorter_alias(self) -> None:
        self.assertEqual(identify_bike("Trek Fuel EX 8 2023").model, "Fuel EX 8")

    def test_unknown_model_stays_a_candidate_below_the_retail_gate(self) -> None:
        identity = identify_bike("Trek Zzyzx 9 2022")
        self.assertEqual(identity.model, "Zzyzx 9")
        self.assertFalse(identity.model_confirmed)
        self.assertEqual(identity.model_source, "tail")
        self.assertGreater(identity.confidence, 0.0)
        self.assertLess(identity.confidence, RETAIL_IDENTITY_GATE)

    def test_typo_in_model_is_confirmed_by_fuzzy_match(self) -> None:
        identity = identify_bike("Specialized Rockhoper 29 2021")
        self.assertEqual(identity.model, "Rockhopper")
        self.assertTrue(identity.model_confirmed)
        self.assertEqual(identity.model_source, "catalog_fuzzy")

    def test_numeric_model_is_not_taken_from_arbitrary_numbers(self) -> None:
        # "5" здесь — это размер рамы, а не модель Woom.
        identity = identify_bike("Cube Aim velikost 5")
        self.assertEqual(identity.model, "Aim")

    def test_confirmed_model_clears_both_downstream_gates(self) -> None:
        identity = identify_bike("Cube Stereo 140 2021 29")
        self.assertGreaterEqual(identity.confidence, RETAIL_IDENTITY_GATE)
        self.assertGreaterEqual(identity.confidence, DEAL_IDENTITY_GATE)


class IdentityConfigTest(unittest.TestCase):
    def tearDown(self) -> None:
        configure_identity(IdentityConfig())

    def test_missing_catalog_degrades_to_candidates(self) -> None:
        configure_identity(IdentityConfig(catalog_path="does/not/exist.json"))
        identity = identify_bike("Trek Marlin 7 2025")
        self.assertEqual(identity.model, "Marlin 7")
        self.assertFalse(identity.model_confirmed)
        self.assertLess(identity.confidence, RETAIL_IDENTITY_GATE)

    def test_disabled_catalog_degrades_to_candidates(self) -> None:
        configure_identity(IdentityConfig(catalog_enabled=False))
        identity = identify_bike("Trek Marlin 7 2025")
        self.assertEqual(identity.model, "Marlin 7")
        self.assertEqual(identity.model_source, "tail")

    def test_validate_rejects_inconsistent_scores(self) -> None:
        with self.assertRaises(ValueError):
            IdentityConfig(candidate_model_score=0.5, confirmed_model_score=0.4).validate()
        with self.assertRaises(ValueError):
            IdentityConfig(unconfirmed_confidence_cap=0.5).validate()
        with self.assertRaises(ValueError):
            IdentityConfig(fuzzy_match_cutoff=0.1).validate()
        IdentityConfig().validate()


class IdentityPersistenceTest(unittest.TestCase):
    def test_legacy_payload_without_new_fields_loads(self) -> None:
        legacy = {"brand": "Trek", "model": "Marlin 7", "confidence": 0.8}
        identity = BikeIdentity.from_dict(legacy)
        self.assertEqual(identity.model, "Marlin 7")
        self.assertFalse(identity.model_confirmed)
        self.assertEqual(identity.model_source, "")

    def test_round_trip_keeps_confirmation_fields(self) -> None:
        identity = identify_bike("Trek Marlin 7 2025")
        restored = BikeIdentity.from_dict(identity.to_dict())
        self.assertEqual(restored.model_confirmed, identity.model_confirmed)
        self.assertEqual(restored.model_source, identity.model_source)


if __name__ == "__main__":
    unittest.main()
