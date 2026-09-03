import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scraper
import translator
from translator import TranslationError


class TranslationTests(unittest.TestCase):
    def test_sentence_split_preserves_abbreviation(self):
        segments = translator.split_for_translation(
            "The U.S. announced a policy. Another sentence follows."
        )
        self.assertNotIn("The U.", segments)
        self.assertNotIn("S.", segments)
        self.assertEqual("".join(segments[:2]), "The U.S.announced a policy.")

    def test_long_text_is_split_to_400_characters(self):
        segments = translator.split_for_translation("Word " * 200)
        self.assertGreater(len(segments), 1)
        self.assertTrue(all(len(segment) <= 400 for segment in segments))

    def test_short_abbreviations_are_not_false_failures(self):
        self.assertFalse(translator.looks_untranslated("U.S.", "U.S."))
        self.assertFalse(translator.looks_untranslated("H.R. 123", "H.R. 123"))
        self.assertTrue(
            translator.looks_untranslated(
                "This complete sentence stayed English.",
                "This complete sentence stayed English.",
            )
        )

    def test_clients5_failure_uses_mobile_fallback(self):
        with patch.object(translator, "cache_get", return_value=""), patch.object(
            translator, "cache_put"
        ), patch.object(
            translator, "translate_clients5", side_effect=RuntimeError("429")
        ), patch.object(
            translator, "translate_mobile", return_value="备用翻译"
        ), patch.object(
            translator, "throttle"
        ):
            self.assertEqual(translator.translate_one_segment("Fallback title", "zh-CN"), "备用翻译")

    def test_sqlite_cache_round_trip(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(
            translator, "CACHE_PATH", Path(folder) / "cache.sqlite3"
        ):
            translator.cache_put("Source", "zh-CN", "译文")
            self.assertEqual(translator.cache_get("Source", "zh-CN"), "译文")

    def test_title_record_is_retried_once(self):
        rows = [{"unique_key": "1", "title": "China update", "title_zh": ""}]
        with patch.object(
            scraper,
            "translate_text",
            side_effect=[TranslationError("temporary"), "中国更新"],
        ), patch.object(scraper.time, "sleep"):
            result, stats = scraper.translate_records(rows)
        self.assertEqual(result[0]["title_zh"], "中国更新")
        self.assertEqual(stats["translation_retries"], 1)
        self.assertEqual(stats["translation_failed_after_retry"], 0)

    def test_second_title_failure_stays_pending(self):
        rows = [{"unique_key": "2", "title": "A complete English title", "title_zh": ""}]
        with patch.object(
            scraper, "translate_text", side_effect=TranslationError("blocked")
        ), patch.object(scraper.time, "sleep"):
            result, stats = scraper.translate_records(rows)
        self.assertEqual(result[0]["title_zh"], "")
        self.assertEqual(result[0]["state"], "待翻译")
        self.assertEqual(stats["translation_failed_after_retry"], 1)

    def test_content_failure_never_keeps_english(self):
        rows = [{"unique_key": "3", "content": "A complete English article body.", "content_zh": ""}]
        with patch.object(
            scraper, "translate_text", side_effect=TranslationError("blocked")
        ), patch.object(scraper.time, "sleep"):
            result, stats = scraper.translate_content_records(rows)
        self.assertEqual(result[0]["content_zh"], "")
        self.assertEqual(stats["translation_failed_after_retry"], 1)


if __name__ == "__main__":
    unittest.main()
