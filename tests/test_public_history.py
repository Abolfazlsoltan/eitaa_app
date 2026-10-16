import unittest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from public_history import PublicHistoryError, parse_public_channel_html


class PublicHistoryParserTests(unittest.TestCase):
    def test_parses_public_post_text_date_views_and_link(self):
        page = """
        <html><body>
          <div class="etme_widget_message_wrap">
            <div class="etme_widget_message" data-post="sample_channel/123">
              <div class="etme_widget_message_text">سلام <b>دنیا</b></div>
              <time class="time" datetime="2026-10-16T12:30:00+00:00"></time>
              <span class="etme_widget_message_views" data-count="42">42</span>
            </div>
          </div>
        </body></html>
        """
        posts = parse_public_channel_html("sample_channel", page)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["source_post_id"], 123)
        self.assertEqual(posts[0]["text"], "سلام دنیا")
        self.assertEqual(posts[0]["views"], 42)
        self.assertEqual(posts[0]["source_url"], "https://eitaa.com/sample_channel/123")
        self.assertTrue(posts[0]["is_external"])

    def test_ignores_posts_from_other_channels(self):
        page = '<div class="etme_widget_message" data-post="other/12"><div class="etme_widget_message_text">x</div></div>'
        self.assertEqual(parse_public_channel_html("sample_channel", page), [])

    def test_rejects_invalid_username(self):
        with self.assertRaises(PublicHistoryError):
            parse_public_channel_html("https://example.com", "<html></html>")


if __name__ == "__main__":
    unittest.main()
