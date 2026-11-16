"""Render-determinism tests: the theme must own the canvas geometry, and the room plaques must
stay out of the band the agent chips occupy.

Both invariants are about a race and a collision that only showed up in a real browser, so the
guard is on the source that caused them:

* `applySettings` used to end in `resizeCanvas()`, which repaints with whatever CW/CH the stock
  layout last chose. When the feed answered before the theme chain (measured on ~15% of loads), the
  mansion was drawn into the stock 352x208 canvas — the ~2x-cropped render with the labels clipped
  and the ticker off-frame — and stayed there, because `layoutFor` early-returns while the agent
  count is unchanged and only a window resize re-ran it. Applying a theme must re-run the layout.
* The room plaques sat at office y 35..48, which is exactly where an agent's task+name chips land
  (measured, y 34..48), so every room name was half covered on a correct load too.

Run:  python3 -m unittest test.theme_geometry_test -v
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = open(os.path.join(ROOT, "public", "index.html")).read()
THEME = open(os.path.join(ROOT, "public", "themes", "busybee.js")).read()

# Office geometry, as the client defines it.
T = int(re.search(r"const T = (\d+), BASE_CW", INDEX).group(1))
CHIP_TOP_Y = 34        # live-measured: the top of an agent's task chip, in office units
PLAQUE_H = 13          # the plaque bar height the theme draws


def apply_settings_body():
    """The body of `async function applySettings()`."""
    start = INDEX.index("async function applySettings() {")
    end = INDEX.index("\n  }\n", start)
    return INDEX[start:end]


def wall_numbers():
    """(window y/h, sign baseline, plaque y values) parsed out of drawRoom."""
    matched = re.search(r"const T = (\d+), BASE_CW", INDEX)
    assert matched, "the client's tile size is unparseable — the test needs updating"
    windows = [(int(m.group(1)), int(m.group(2)))
               for m in re.finditer(r"K\.window\(c,\s*[^,]+,\s*(\d+),\s*[^,]+,\s*(\d+),", THEME)]
    sign_m = re.search(r"K\.sign\(c,\s*CW / 2,\s*(\d+)", THEME)
    assert sign_m, "the company sign's baseline is unparseable"
    sign = int(sign_m.group(1))
    plaques = [(int(m.group(1)), int(m.group(2)))
               for m in re.finditer(r"K\.rr\('#12151c',\s*[^,]+,\s*(\d+),\s*[^,]+,\s*(\d+),", THEME)]
    return [(int(a), int(b)) for a, b in windows], sign, [(int(a), int(b)) for a, b in plaques]


class ThemeOwnsTheLayout(unittest.TestCase):
    def test_apply_settings_reapplies_the_theme_plan(self):
        """A theme that replaces the floor plan must also set the canvas, not just repaint it."""
        body = apply_settings_body()
        self.assertRegex(body, r"layoutFor\(\s*AGENTS\s*\|\|\s*0\s*,\s*true\s*\)",
                         "applySettings must re-run layoutFor(force) after loading a theme")
        self.assertNotIn("resizeCanvas();", body,
                         "a bare resizeCanvas() keeps the stock CW/CH — the cropped-render bug")

    def test_layout_for_ends_in_resize_canvas(self):
        """The force path is only enough because layoutFor sizes the canvas itself."""
        start = INDEX.index("function layoutFor(")
        body = INDEX[start:INDEX.index("\n  }\n", start)]
        self.assertIn("resizeCanvas();", body)
        self.assertIn("theme.plan(", body)

    def test_the_plan_guard_can_be_overridden(self):
        """layoutFor early-returns for an unchanged agent count; force must bypass that guard."""
        start = INDEX.index("function layoutFor(")
        body = INDEX[start:INDEX.index("\n  }\n", start)]
        self.assertRegex(body, r"if \(!force && rows === ROWS")


class PlaquesClearTheChips(unittest.TestCase):
    def test_plaques_are_above_the_chip_band(self):
        windows, sign, plaques = wall_numbers()
        self.assertEqual(len(plaques), 2, "one call in the room loop (five rooms) plus the lounge")
        for y, h in plaques:
            self.assertEqual(h, PLAQUE_H)
            self.assertLess(y + h - 1, CHIP_TOP_Y,
                            "plaque %d..%d sits under the agent chips (they start at %d)"
                            % (y, y + h - 1, CHIP_TOP_Y))

    def test_plaques_stay_inside_the_wall_strip(self):
        """WALL_TOP is where the floor begins; a plaque below it would sit in the room."""
        wall_top = int(re.search(r"const WALL_TOP = ([\d.]+) \* T", THEME).group(1)) * T
        _, _, plaques = wall_numbers()
        for y, h in plaques:
            self.assertLessEqual(y + h, wall_top,
                                 "plaque %d..%d runs past the wall strip (0..%d)" % (y, y + h - 1, wall_top - 1))

    def test_wall_stack_does_not_collide_with_itself(self):
        windows, sign, plaques = wall_numbers()
        self.assertEqual(len(windows), 2)
        plaque_top = min(y for y, _ in plaques)
        for y, h in windows:
            self.assertLessEqual(y + h, plaque_top, "a window overlaps the plaque line")
        self.assertLessEqual(sign, plaque_top, "the company sign sits on the plaque line")

    def test_every_plaque_shares_one_band(self):
        """Six plaques, one line — a lone y constant here is how a room ends up covered again."""
        _, _, plaques = wall_numbers()
        self.assertEqual({y for y, _ in plaques}, {min(y for y, _ in plaques)})


if __name__ == "__main__":
    unittest.main()
