"""The page must know which office it is BEFORE its first script runs.

Run:  python3 -m unittest test.static_theme_test -v

Why this file exists: the theme used to be decided by a fetch. /config.json named the mansion, but
the settings chain resolved the catalog first on 4 of 5 loads and painted the stock pixel office,
which is the "two different dashboards" Bart saw on the pve-host browser. The answer now travels in
the served HTML, so these tests pin the stamping — placement, idempotence and the sanitising of an
operator-set OFFICE_THEME, because that string is copied into a <script> in a page we serve.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import office  # noqa: E402

HTML = (b"<html><head><title>Cubicle</title></head>"
        b"<body><script>boot()</script></body></html>")


class StampThemeTest(unittest.TestCase):
    def setUp(self):
        self.env = os.environ.get("OFFICE_THEME")

    def tearDown(self):
        if self.env is None:
            os.environ.pop("OFFICE_THEME", None)
        else:
            os.environ["OFFICE_THEME"] = self.env

    def test_the_stamp_lands_before_the_page_script(self):
        out = office.stamp_server_theme(HTML, "busybee")
        stamp, body = out.find(b"window.__cubicleServerTheme="), out.find(b"<body>")
        self.assertGreater(stamp, -1, "the decision must be in the page")
        self.assertLess(stamp, body, "...and ahead of the first script, not after it")
        self.assertIn(b'window.__cubicleServerTheme="busybee"', out)
        self.assertEqual(out.count(b"__cubicleServerTheme"), 1)
        self.assertIn(b"<title>Cubicle</title>", out)              # the head is still a head

    def test_a_page_without_a_head_still_gets_the_line(self):
        out = office.stamp_server_theme(b"<html><body>x</body></html>", "busybee")
        self.assertTrue(out.startswith(b'<script data-office-theme="1">window.__cubicleServerTheme="busybee";</script>'))

    def test_an_embedded_head_string_in_the_body_cannot_strand_the_stamp(self):
        # The real page is one file that embeds HTML in its template strings, and the office stamp
        # must precede every script. This fixture mirrors the shape that bit us: a literal "</head>"
        # sitting inside a JS string in the body, plus a page script that references the variable.
        page = ('<html><head><title>t</title><script>var T = "</head>"; '
                'window.__cubicleServerTheme = T;</script></head>'
                '<body><script>boot()</script></body></html>').encode()
        out = office.stamp_server_theme(page, "busybee")
        self.assertLess(out.find(b"data-office-theme"), out.find(b"var T"),
                        "the stamp must run before the page's first script")
        self.assertEqual(out.count(b"data-office-theme"), 1)
        self.assertEqual(office.stamp_server_theme(out, "pixel"), out)   # stamped once, idempotent

    def test_the_deployment_theme_is_sanitised_for_a_script_tag(self):
        # OFFICE_THEME is operator-set; a quote or a bracket must not smuggle markup into the page
        os.environ["OFFICE_THEME"] = 'busy"><script>alert(1)</script>bee'
        theme = office.office_theme()
        self.assertEqual(theme, "busyscriptalert1scriptbee")   # letters/digits/hyphen only
        out = office.stamp_server_theme(HTML, theme)
        self.assertEqual(out.count(b"<script"), 2)                 # ours + the page's own
        self.assertNotIn(b"alert(1)</script>bee", out)

    def test_the_default_office_is_the_mansion(self):
        os.environ.pop("OFFICE_THEME", None)
        self.assertEqual(office.office_theme(), "busybee")


if __name__ == "__main__":
    unittest.main()
