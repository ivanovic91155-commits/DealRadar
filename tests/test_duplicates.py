from __future__ import annotations

import unittest

from deal_radar.duplicates import compare_listings, description_fingerprint, normalize_duplicate_text
from deal_radar.models import Listing


BASE_DESCRIPTION = (
    "Prodám celoodpružené trailové kolo Merida One-Twenty 400 modelový rok 2023, "
    "velikost rámu L a kola 29. Kolo má zdvih 130 mm vpředu a 120 mm vzadu, "
    "pravidelný servis, teleskopickou sedlovku a pohon Shimano Deore."
)


def item(
    source: str,
    external_id: str,
    *,
    title: str = "Merida One-Twenty 400 velikost L kola 29",
    description: str = BASE_DESCRIPTION,
    price: int = 29000,
) -> Listing:
    return Listing(
        source=source,
        external_id=external_id,
        title=title,
        description=description,
        url=f"https://{source}.example/{external_id}",
        profile="test",
        price_czk=price,
        price_amount=price,
        price_status="numeric",
    )


class DuplicateDetectionTest(unittest.TestCase):
    def test_same_description_price_and_model_across_sources_is_confirmed(self) -> None:
        match = compare_listings(item("bazos", "1"), item("cyklobazar", "2"))
        self.assertEqual(match.level, "confirmed")
        self.assertGreaterEqual(match.similarity, 0.95)

    def test_nearly_same_description_is_possible(self) -> None:
        words = BASE_DESCRIPTION.split()
        changed = " ".join(word for index, word in enumerate(words) if index not in set(range(4, 9)))
        changed += " odlišný stav komponentů"
        match = compare_listings(
            item("bazos", "1"),
            item("cyklobazar", "2", description=changed),
        )
        self.assertEqual(match.level, "possible")

    def test_same_model_and_price_with_different_descriptions_is_not_confirmed(self) -> None:
        unrelated = (
            "Kolo po závodech, poškozený rám a vidlice, prodává se bez kol, brzd a řazení. "
            "Pouze osobní převzetí, žádný servis ani další příslušenství není součástí nabídky."
        )
        match = compare_listings(item("bazos", "1"), item("cyklobazar", "2", description=unrelated))
        self.assertEqual(match.level, "none")

    def test_same_description_with_different_model_is_not_merged(self) -> None:
        match = compare_listings(
            item("bazos", "1"),
            item("cyklobazar", "2", title="Merida Big Nine 400 velikost L kola 29"),
        )
        self.assertEqual(match.level, "none")
        self.assertEqual(match.reason, "model_mismatch")

    def test_different_trim_is_not_merged(self) -> None:
        first = item("bazos", "1", title="Trek Marlin 7 Comp 29 2025")
        second = item("cyklobazar", "2", title="Trek Marlin 7 Elite 29 2025")
        first.description = BASE_DESCRIPTION.replace("Merida One-Twenty 400", "Trek Marlin 7")
        second.description = first.description
        match = compare_listings(first, second)
        self.assertEqual(match.level, "none")
        self.assertEqual(match.reason, "trim_mismatch")

    def test_normalization_and_fingerprint_ignore_spacing_case_and_punctuation(self) -> None:
        first = "MERIDA  One-Twenty 400\n\nvelikost L!!!"
        second = "merida one twenty 400 velikost l"
        self.assertEqual(normalize_duplicate_text(first), normalize_duplicate_text(second))
        self.assertEqual(description_fingerprint(first), description_fingerprint(second))


if __name__ == "__main__":
    unittest.main()
