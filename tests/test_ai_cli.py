from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from deal_radar.cli import main

SECRET = "sk-cli-secret-value"


def write_config(directory: Path, ai: dict[str, object] | None = None) -> Path:
    payload = {
        "database_path": str(directory / "state.sqlite3"),
        "profiles": [
            {"name": "test", "rss_url": "https://sport.bazos.cz/rss.php?hledat=kolo"}
        ],
        "retail": {"enabled": False},
        "market_pricing": {"enabled": False},
        "ai": ai or {},
    }
    path = directory / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def run(argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main(argv)
    return code, buffer.getvalue()


class AiCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_missing_key_names_the_variable_and_fails(self) -> None:
        config = write_config(self.root, {"enabled": True})
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False):
            code, output = run(["--config", str(config), "ai-check"])
        self.assertEqual(code, 1)
        self.assertIn("API key configured: no (OPENAI_API_KEY)", output)
        self.assertIn("Structured Output test: skipped (no API key)", output)

    def test_configured_key_is_reported_as_present_but_never_printed(self) -> None:
        config = write_config(self.root, {"enabled": True})
        with patch.dict(os.environ, {"OPENAI_API_KEY": SECRET}, clear=False):
            code, output = run(["--config", str(config), "ai-check"])
        self.assertEqual(code, 0)
        self.assertIn("API key configured: yes", output)
        self.assertNotIn(SECRET, output)
        self.assertIn("pass --live", output)

    def test_check_reports_prompt_and_schema_versions(self) -> None:
        config = write_config(self.root, {"enabled": True})
        with patch.dict(os.environ, {"OPENAI_API_KEY": SECRET}, clear=False):
            _, output = run(["--config", str(config), "ai-check"])
        self.assertIn("Prompt version: listing-analysis-v1.1.0", output)
        self.assertIn("Schema version: dealradar.ai-analysis.v1", output)
        self.assertIn("Primary model configured: gpt-5.6-luna", output)

    def test_check_does_not_touch_the_database(self) -> None:
        config = write_config(self.root, {"enabled": True})
        with patch.dict(os.environ, {"OPENAI_API_KEY": SECRET}, clear=False):
            run(["--config", str(config), "ai-check"])
        self.assertFalse((self.root / "state.sqlite3").exists())


class AiTestListingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_prefiltered_listing_spends_nothing_and_says_so(self) -> None:
        config = write_config(self.root, {"enabled": True})
        with patch.dict(os.environ, {"OPENAI_API_KEY": SECRET}, clear=False):
            code, output = run(
                ["--config", str(config), "ai-test-listing", "--title", "Koupím kolo Trek"]
            )
        self.assertEqual(code, 0)
        self.assertIn("WANTED_AD", output)
        self.assertIn("nothing was spent", output)
        self.assertFalse((self.root / "state.sqlite3").exists())

    def test_missing_title_is_reported(self) -> None:
        config = write_config(self.root, {"enabled": True})
        code, output = run(["--config", str(config), "ai-test-listing"])
        self.assertEqual(code, 1)
        self.assertIn("--fixture", output)

    def test_repository_fixtures_are_loadable(self) -> None:
        for name in ("trek_marlin.json", "vague_scott.json"):
            path = Path(__file__).parent / "fixtures" / "ai" / name
            with self.subTest(fixture=name):
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertIn("title", data)
                self.assertIn("description", data)


if __name__ == "__main__":
    unittest.main()
