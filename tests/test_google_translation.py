import os
import unittest
from unittest.mock import patch

import scraper


class FakeResponse:
    status_code = 200
    headers = {}
    text = "ok"

    def __init__(self, translated):
        self.translated = translated

    def raise_for_status(self):
        return None

    def json(self):
        return [[[self.translated, "source", None, None]]]


class FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse("中译" + kwargs["params"]["q"])


class GoogleTranslationTests(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()
        self.env = patch.dict(
            os.environ,
            {"GOOGLE_TRANSLATE_DELAY_SECONDS": "0"},
            clear=False,
        )
        self.quick_session = patch.object(scraper, "QUICK_SESSION", self.session)
        self.env.start()
        self.quick_session.start()

    def tearDown(self):
        self.quick_session.stop()
        self.env.stop()

    def test_uses_google_no_key_endpoint(self):
        result = scraper.google_translate_text("Alpha")
        self.assertEqual(result, "中译Alpha")
        url, kwargs = self.session.calls[0]
        self.assertEqual(url, "https://translate.googleapis.com/translate_a/single")
        self.assertEqual(kwargs["params"]["tl"], "zh-CN")

    def test_replaces_old_false_english_translation(self):
        rows = [
            {
                "unique_key": "1",
                "title": "China update",
                "title_zh": "China update",
                "state": "已翻译",
            }
        ]
        scraper.translate_google(rows)
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


if __name__ == "__main__":
    unittest.main()
