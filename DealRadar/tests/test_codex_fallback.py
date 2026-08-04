from pathlib import Path
import unittest

from deal_radar.bike_identity import identify_bike
from deal_radar.codex_fallback import CodexMatchResolver, CodexUnavailable
from deal_radar.models import RetailOffer


class CodexFallbackTest(unittest.TestCase):
    def test_missing_codex_does_not_expose_or_crash_pipeline(self) -> None:
        schema = Path(__file__).parents[1] / "schemas" / "bike_match.schema.json"
        resolver = CodexMatchResolver(
            executable="definitely-missing-codex-binary",
            schema_path=str(schema),
            timeout_seconds=1,
        )
        offer = RetailOffer("Shop", "Trek Marlin 7", 20000, "https://shop.example/bike")
        with self.assertRaises(CodexUnavailable):
            resolver.resolve(identify_bike("Trek Marlin 7"), [offer])


if __name__ == "__main__":
    unittest.main()
