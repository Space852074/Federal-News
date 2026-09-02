import os
import unittest
from unittest.mock import patch

import scraper


class FakeResponse:
    status_code = 200
    headers = {}
    text = "ok"

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        payload = [
            {"translations": [{"text": "中译" + item["text"]}]}
            for item in kwargs["json"]
        ]
        return FakeResponse(payload)


class AzureTranslationTests(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()
        self.env = patch.dict(
            os.environ,
            {
                "AZURE_TRANSLATOR_KEY": "test-key",
                "AZURE_TRANSLATOR_REGION": "eastasia",
                "AZURE_TRANSLATOR_ENDPOINT": "",
            },
        )
        self.quick_session = patch.object(scraper, "QUICK_SESSION", self.session)
        self.env.start()
        self.quick_session.start()

    def tearDown(self):
        self.quick_session.stop()
        self.env.stop()

    def test_uses_default_endpoint_and_region(self):
        result = scraper.azure_translate_texts(["Alpha", "Beta"])
        self.assertEqual(result, ["中译Alpha", "中译Beta"])
        url, kwargs = self.session.calls[0]
        self.assertEqual(url, "https://api.cognitive.microsofttranslator.com/translate")
        self.assertEqual(
            kwargs["headers"]["Ocp-Apim-Subscription-Region"], "eastasia"
        )

    def test_replaces_old_false_english_translation(self):
        rows = [
            {
                "unique_key": "1",
                "title": "China update",
                "title_zh": "China update",
                "state": "已翻译",
            }
        ]
        scraper.translate_azure(rows)
        self.assertEqual(rows[0]["title_zh"], "中译China update")
        self.assertEqual(rows[0]["state"], "已翻译")

    def test_marker_does_not_turn_english_into_chinese(self):
        row = {"title": "China update", "title_zh": "", "content": ""}
        scraper.apply_keyword_markers(row)
        self.assertEqual(row["title_zh"], "")
        self.assertEqual(row["keyword_markers"], "🔴")

    def test_translates_keyword_content_in_order(self):
        rows = [
            {
                "unique_key": "2",
                "title": "Vaccine report",
                "title_zh": "疫苗报告",
                "content": "First paragraph.\n\nSecond paragraph.",
                "content_zh": "",
            }
        ]
        scraper.translate_contents(rows)
        self.assertEqual(
            rows[0]["content_zh"],
            "中译First paragraph.\n\nSecond paragraph.",
        )

    def test_missing_key_fails_clearly(self):
        with patch.dict(os.environ, {"AZURE_TRANSLATOR_KEY": ""}):
            with self.assertRaisesRegex(RuntimeError, "AZURE_TRANSLATOR_KEY"):
                scraper.azure_translate_texts(["Title"])


if __name__ == "__main__":
    unittest.main()
