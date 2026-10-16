import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from api import _markdown_to_eitaa_html
from backend.eitaa import markdown_to_html


class MarkdownPublishingTests(unittest.TestCase):
    def test_heading_bold_italic_and_strikethrough(self):
        source = "# تیتر\n**پررنگ** و *کج* و ~~حذف~~"
        expected = "<b>تیتر</b>\n<b>پررنگ</b> و <i>کج</i> و <s>حذف</s>"
        self.assertEqual(_markdown_to_eitaa_html(source), expected)
        self.assertEqual(markdown_to_html(source), expected)

    def test_code_and_raw_html_are_safe(self):
        source = "نمونه `x < y` و <script>alert(1)</script>"
        expected = "نمونه <code>x &lt; y</code> و &lt;script&gt;alert(1)&lt;/script&gt;"
        self.assertEqual(_markdown_to_eitaa_html(source), expected)
        self.assertEqual(markdown_to_html(source), expected)

    def test_real_hashtag_is_preserved_and_heading_marker_removed(self):
        source = "# تیتر\nخبر #فناوری"
        expected = "<b>تیتر</b>\nخبر #فناوری"
        self.assertEqual(_markdown_to_eitaa_html(source), expected)
        self.assertEqual(markdown_to_html(source), expected)

    def test_links_and_lists(self):
        source = "- مورد اول\n[سایت](https://example.com)"
        expected = "• مورد اول\n<a href=\"https://example.com\">سایت</a>"
        self.assertEqual(_markdown_to_eitaa_html(source), expected)
        self.assertEqual(markdown_to_html(source), expected)


if __name__ == "__main__":
    unittest.main()
