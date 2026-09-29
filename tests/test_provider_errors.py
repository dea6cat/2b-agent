"""Provider HTTP errors read as one actionable line: a plain-language cause (credits, auth,
missing model, rate limit, context size) plus the provider's own message pulled out of its
JSON body — not a raw JSON dump. Bodies below are trimmed from real responses.
Run: `python -m unittest tests.test_provider_errors`.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from two_b.providers.base import http_error_message  # noqa: E402


class HttpErrorMessage(unittest.TestCase):
    def test_openrouter_credits(self):
        body = json.dumps({"error": {"message": "This request requires more credits, or fewer max_tokens.", "code": 402}})
        msg = http_error_message(402, body, "Payment Required")
        self.assertTrue(msg.startswith("HTTP 402 — out of credits"), msg)
        self.assertIn("requires more credits", msg)
        self.assertNotIn('{"error"', msg)                       # no raw JSON

    def test_deepseek_balance_and_cerebras_payment(self):
        self.assertIn("out of credits", http_error_message(
            402, json.dumps({"error": {"message": "Insufficient Balance"}}), ""))
        self.assertIn("out of credits", http_error_message(
            402, json.dumps({"message": "Payment required to access this resource."}), ""))

    def test_missing_model(self):
        body = json.dumps({"error": {"code": 404, "message": "This model models/gemini-2.0-flash is no longer available."}})
        msg = http_error_message(404, body, "Not Found")
        self.assertIn("model not found or not available to this account", msg)
        self.assertIn("/models", msg)
        self.assertIn("no longer available", msg)

    def test_nvidia_detail_field(self):
        body = json.dumps({"status": 404, "title": "Not Found", "detail": "Function '5beba52c' Not found for account"})
        self.assertIn("Not found for account", http_error_message(404, body, ""))

    def test_rate_limit(self):
        body = json.dumps({"object": "error", "message": "Rate limit exceeded", "type": "rate_limited"})
        self.assertTrue(http_error_message(429, body, "").startswith("HTTP 429 — rate limited"))

    def test_auth(self):
        self.assertIn("check the API key", http_error_message(401, '{"error": {"message": "invalid x-api-key"}}', ""))

    def test_context_too_long(self):
        body = json.dumps({"error": {"message": "prompt is too long: 250000 tokens > 200000 maximum"}})
        self.assertIn("too large for the model's context", http_error_message(400, body, ""))

    def test_plain_text_body_and_empty_body(self):
        self.assertEqual(http_error_message(502, "Bad gateway\n\n", "Bad Gateway"),
                         "HTTP 502 — provider error, usually temporary: Bad gateway")
        self.assertEqual(http_error_message(400, "", "Bad Request"), "HTTP 400: Bad Request")

    def test_long_messages_are_capped(self):
        msg = http_error_message(400, json.dumps({"error": {"message": "x" * 2000}}), "")
        self.assertLess(len(msg), 320)


if __name__ == "__main__":
    unittest.main()
