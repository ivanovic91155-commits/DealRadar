from __future__ import annotations

import unittest
from typing import Any

from deal_radar.ai.client import (
    AIAuthError,
    AIInvalidResponse,
    AIUnavailable,
    OpenAIClient,
    estimate_cost_usd,
)
from deal_radar.config import AIConfig
from deal_radar.http import HttpError

SECRET = "sk-test-do-not-log-me"


def ok_response(text: str = '{"ok": true}', **usage: int) -> dict[str, Any]:
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": {
            "input_tokens": usage.get("input_tokens", 1200),
            "input_tokens_details": {"cached_tokens": usage.get("cached_tokens", 0)},
            "output_tokens": usage.get("output_tokens", 300),
        },
    }


class FakePoster:
    """Транспорт вместо сети: отдаёт заготовленные ответы или исключения."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int = 90
    ) -> dict[str, Any]:
        self.calls.append({"url": url, "payload": payload, "headers": headers, "timeout": timeout})
        item = self.responses[min(len(self.calls), len(self.responses)) - 1]
        if isinstance(item, Exception):
            raise item
        return item


def client(poster: FakePoster, **overrides: Any) -> tuple[OpenAIClient, list[float]]:
    config = AIConfig(api_key=SECRET, **overrides)
    config.validate()
    slept: list[float] = []
    return OpenAIClient(config, poster=poster, sleeper=slept.append), slept


def call(instance: OpenAIClient):
    return instance.structured(
        system="system", user="user", schema_name="listing_analysis", schema={"type": "object"}
    )


class RequestShapeTest(unittest.TestCase):
    def test_request_uses_strict_structured_outputs_and_disables_storage(self) -> None:
        poster = FakePoster(ok_response())
        instance, _ = client(poster)
        call(instance)
        sent = poster.calls[0]["payload"]
        self.assertEqual(sent["model"], "gpt-5.6-luna")
        self.assertEqual(sent["text"]["format"]["type"], "json_schema")
        self.assertTrue(sent["text"]["format"]["strict"])
        self.assertEqual(sent["text"]["format"]["name"], "listing_analysis")
        self.assertIs(sent["store"], False)
        self.assertEqual(sent["input"][0]["role"], "developer")
        self.assertEqual(poster.calls[0]["url"], "https://api.openai.com/v1/responses")
        self.assertEqual(poster.calls[0]["timeout"], 30)

    def test_successful_call_reports_payload_and_tokens(self) -> None:
        poster = FakePoster(
            ok_response('{"brand": "Trek"}', input_tokens=1500, cached_tokens=900, output_tokens=250)
        )
        instance, _ = client(poster)
        result = call(instance)
        self.assertEqual(result.payload, {"brand": "Trek"})
        self.assertEqual(result.model_name, "gpt-5.6-luna")
        self.assertFalse(result.used_fallback)
        self.assertEqual(result.input_tokens, 1500)
        self.assertEqual(result.cached_input_tokens, 900)
        self.assertEqual(result.output_tokens, 250)
        self.assertEqual(result.total_tokens, 1750)
        self.assertEqual(result.attempts, 1)


class RetryAndFallbackTest(unittest.TestCase):
    def test_429_is_retried_with_exponential_backoff(self) -> None:
        poster = FakePoster(HttpError("POST failed with HTTP 429: slow down", 429), ok_response())
        instance, slept = client(poster)
        result = call(instance)
        self.assertEqual(len(poster.calls), 2)
        self.assertEqual(slept, [1.0])
        self.assertEqual(result.attempts, 2)

    def test_500_is_retried_then_falls_back_to_the_stronger_model(self) -> None:
        poster = FakePoster(HttpError("POST failed with HTTP 500: boom", 500))
        instance, slept = client(poster)
        with self.assertRaises(AIUnavailable):
            call(instance)
        # Три попытки основной модели (max_retries=2) плюс одна на fallback.
        self.assertEqual(len(poster.calls), 4)
        self.assertEqual([entry["payload"]["model"] for entry in poster.calls][-1], "gpt-5.6-terra")
        self.assertEqual(slept, [1.0, 2.0])

    def test_timeout_without_status_code_is_retried(self) -> None:
        poster = FakePoster(HttpError("POST failed: timed out"), ok_response())
        instance, _ = client(poster)
        self.assertEqual(call(instance).attempts, 2)

    def test_fallback_model_result_is_flagged(self) -> None:
        poster = FakePoster(
            HttpError("HTTP 503", 503),
            HttpError("HTTP 503", 503),
            HttpError("HTTP 503", 503),
            ok_response('{"brand": "Cube"}'),
        )
        instance, _ = client(poster)
        result = call(instance)
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.model_name, "gpt-5.6-terra")

    def test_disabled_fallback_stops_after_primary_model(self) -> None:
        poster = FakePoster(HttpError("HTTP 500", 500))
        instance, _ = client(poster, fallback_enabled=False)
        with self.assertRaises(AIUnavailable):
            call(instance)
        self.assertEqual(len(poster.calls), 3)

    def test_400_is_not_retried(self) -> None:
        poster = FakePoster(HttpError("POST failed with HTTP 400: bad schema", 400))
        instance, slept = client(poster)
        with self.assertRaises(AIUnavailable):
            call(instance)
        self.assertEqual(len(poster.calls), 1)
        self.assertEqual(slept, [])

    def test_401_raises_auth_error_without_retry_or_fallback(self) -> None:
        poster = FakePoster(HttpError("POST failed with HTTP 401: bad key", 401))
        instance, _ = client(poster)
        with self.assertRaises(AIAuthError):
            call(instance)
        self.assertEqual(len(poster.calls), 1)

    def test_missing_api_key_raises_auth_error_before_any_request(self) -> None:
        poster = FakePoster(ok_response())
        instance = OpenAIClient(AIConfig(api_key=""), poster=poster)
        with self.assertRaises(AIAuthError):
            call(instance)
        self.assertEqual(poster.calls, [])


class InvalidResponseTest(unittest.TestCase):
    def assert_invalid(self, response: Any) -> None:
        poster = FakePoster(response)
        instance, _ = client(poster, fallback_enabled=False, max_retries=0)
        with self.assertRaises(AIInvalidResponse):
            call(instance)

    def test_non_json_output_is_invalid(self) -> None:
        self.assert_invalid(ok_response("I think it is a Trek."))

    def test_json_array_output_is_invalid(self) -> None:
        self.assert_invalid(ok_response("[1, 2, 3]"))

    def test_empty_output_is_invalid(self) -> None:
        self.assert_invalid({"status": "completed", "output": [], "usage": {}})

    def test_refusal_is_invalid(self) -> None:
        self.assert_invalid(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "refusal", "refusal": "I cannot help"}],
                    }
                ],
            }
        )

    def test_incomplete_response_is_invalid(self) -> None:
        self.assert_invalid(
            {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}
        )

    def test_invalid_output_triggers_fallback_model(self) -> None:
        poster = FakePoster(ok_response("not json"), ok_response('{"brand": "Scott"}'))
        instance, _ = client(poster, max_retries=0)
        result = call(instance)
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.payload, {"brand": "Scott"})


class SecretHandlingTest(unittest.TestCase):
    def test_api_key_is_sent_as_bearer_but_never_leaks_into_errors(self) -> None:
        poster = FakePoster(HttpError(f"POST failed with HTTP 400: key {SECRET} rejected", 400))
        instance, _ = client(poster)
        with self.assertRaises(AIUnavailable) as caught:
            call(instance)
        self.assertEqual(poster.calls[0]["headers"]["Authorization"], f"Bearer {SECRET}")
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertIn("***", str(caught.exception))


class CostTest(unittest.TestCase):
    def test_cached_tokens_are_billed_separately_from_fresh_input(self) -> None:
        config = AIConfig()
        cost = estimate_cost_usd(
            config,
            used_fallback=False,
            input_tokens=1_000_000,
            cached_input_tokens=400_000,
            output_tokens=1_000_000,
        )
        # 600k свежих по $0.20/1M + 400k кэша по $0.02/1M + 1M выходных по $1.20/1M
        self.assertAlmostEqual(cost, 0.12 + 0.008 + 1.20, places=6)

    def test_fallback_model_uses_its_own_price_list(self) -> None:
        config = AIConfig()
        cost = estimate_cost_usd(
            config,
            used_fallback=True,
            input_tokens=1_000_000,
            cached_input_tokens=0,
            output_tokens=1_000_000,
        )
        self.assertAlmostEqual(cost, 2.00 + 12.00, places=6)

    def test_cached_tokens_are_clamped_to_the_input_total(self) -> None:
        config = AIConfig()
        cost = estimate_cost_usd(
            config,
            used_fallback=False,
            input_tokens=1000,
            cached_input_tokens=5000,
            output_tokens=0,
        )
        self.assertAlmostEqual(cost, 1000 / 1_000_000 * 0.02, places=10)

    def test_typical_listing_stays_far_below_a_cent(self) -> None:
        config = AIConfig()
        cost = estimate_cost_usd(
            config,
            used_fallback=False,
            input_tokens=1500,
            cached_input_tokens=0,
            output_tokens=400,
        )
        self.assertLess(cost, 0.001)


if __name__ == "__main__":
    unittest.main()
