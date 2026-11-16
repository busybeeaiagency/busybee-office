"""Render-determinism tests: the theme must own the canvas geometry, and the agent labels must
clear the wall band, the canvas edge and each other.

All of these are about collisions that only showed up in a real browser, so the guard is on the
source that caused them:

* `applySettings` used to end in `resizeCanvas()`, which repaints with whatever CW/CH the stock
  layout last chose. When the feed answered before the theme chain (measured on ~15% of loads), the
  mansion was drawn into the stock 352x208 canvas — the ~2x-cropped render with the labels clipped
  and the ticker off-frame — and stayed there, because `layoutFor` early-returns while the agent
  count is unchanged and only a window resize re-ran it. Applying a theme must re-run the layout.
* The room plaques sat at office y 35..48, which is exactly where an agent's task+name chips land,
  so every room name was half covered on a correct load too. They now own y 18..31.
* The rebuilt plan is 12.5-tile rooms (ROOM_W), a desk pair at 1.8 / 7.9 tiles and DESK_Y 5.4,
  sized from the measured label footprint: a name chip is at worst 40.6 css px wide on a side
  (29.5 px at the 8 px font floor), a task bubble is 80 px across, and a seated sprite sits at
  (desk_x*T + 22, desk_y*T + 12) with the stacked block reaching ~60 px above itself. See
  LabelsClearTheGeometry for the three inequalities those numbers imply.

Run:  python3 -m unittest test.theme_geometry_test -v
"""
import math
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
    sign_m = re.search(r"K\.sign\(c,\s*[^,]+,\s*(\d+)", THEME)
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
        self.assertEqual(len(windows), 1,
                         "one window call site (the room loop) hangs a window on every room wall; "
                         "a second one is how a window ends up in an unrelated place again")
        plaque_top = min(y for y, _ in plaques)
        for y, h in windows:
            self.assertLessEqual(y + h, plaque_top, "a window overlaps the plaque line")
        self.assertLessEqual(sign, plaque_top, "the company sign sits on the plaque line")

    def test_every_plaque_shares_one_band(self):
        """Six plaques, one line — a lone y constant here is how a room ends up covered again."""
        _, _, plaques = wall_numbers()
        self.assertEqual({y for y, _ in plaques}, {min(y for y, _ in plaques)})


ROOM_W = float(re.search(r"const ROOM_W = ([\d.]+);", THEME).group(1))


class LabelsClearTheGeometry(unittest.TestCase):
    """The label footprint, measured in a live browser at the fit view (k = clientWidth / CW):
    a name chip is at worst 40.6 css px wide on a side (11 px cap), 29.5 px at the 8 px floor; a
    task bubble is capped at 80 px; the stacked block reaches ~56-60 px above the sprite. The chips
    scale with the canvas, so clearing them at fit (k >= ~0.63, 46 px half-width) clears them at
    every zoom.
    """

    def test_chips_top_out_below_the_plaque_band(self):
        desk_y = float(re.search(r"const DESK_Y = ([\d.]+);", THEME).group(1))
        self.assertGreater(desk_y * T + 12 - 66, 34,
                           "a seated agent's stacked chips reach into the plaque band (y 18..31)")

    def test_desk_pair_clears_the_edge_the_room_and_each_other(self):
        pairs = re.findall(r"desks: \[\[(\d+(?:\.\d+)?), DESK_Y\], \[(\d+(?:\.\d+)?), DESK_Y\]\]", THEME)
        self.assertEqual(len(pairs), len(re.findall(r"\{ id: '", THEME)),
                         "every room must carry the same desk pair")
        for d1_s, d2_s in pairs:
            d1, d2 = float(d1_s), float(d2_s)
            # the seated sprite is at desk_x * T + 22; its worst-case chip half must stay on canvas
            self.assertGreaterEqual(d1 * T + 22, 48,
                                    "the left desk's label clips off the canvas edge at fit")
            # ...and inside its own room, so two rooms' labels never overlap
            self.assertLessEqual(d2 * T + 22 + 46, ROOM_W * T,
                                 "the right desk's worst-case name chip reaches into the next room")
            # two worst-case 8 px name chips on the same row must never touch
            self.assertGreaterEqual((d2 - d1) * T, 93.4,
                                    "the two name chips in a room collide")


    def test_idle_loiter_spots_clear_every_chip_box(self):
        """Idle agents loiter at the LOUNGE spots; their own name + one zZ line stack ~40 px above
        the feet and a worst-case name half is 52 px wide. No spot may stand under a desk's stacked
        chips, share a band with another loiter spot closer than two worst-case names, sink into the
        overlay panels (y 170), or clip the canvas edge."""
        m = re.search(r"const loiter = \[(.*?)\];", THEME, re.S)
        self.assertIsNotNone(m, "the loiter spot table vanished from plan()")
        spots = [(float(a), float(b)) for a, b in re.findall(r"\[([\d.]+), ([\d.]+)\]", m.group(1))]
        self.assertGreaterEqual(len(spots), 6, "one loiter spot per agent slot")
        desk_y = float(re.search(r"const DESK_Y = ([\d.]+);", THEME).group(1))
        offs = [float(v) for v in re.search(r"desks: \[\[(\d+(?:\.\d+)?), DESK_Y\], \[(\d+(?:\.\d+)?), DESK_Y\]\]", THEME).groups()]
        desk_sits = [(i * ROOM_W + off) * T + 22 for i in range(5) for off in offs]
        cw = math.ceil((5 * ROOM_W + 9) * T)
        for x, y in spots:
            feet, top, half = y * T, y * T - 40.0, 52.0
            self.assertLessEqual(feet, 169.5, f"loiter spot {x},{y} sinks its name into the panel band")
            self.assertGreaterEqual(x * T - half, 0, f"loiter spot {x},{y} clips the left canvas edge")
            self.assertLessEqual(x * T + half, cw, f"loiter spot {x},{y} clips the right canvas edge")
            for dx in desk_sits:
                if abs(x * T - dx) < 100.0:
                    self.assertGreaterEqual(top, desk_y * T + 12 + 4,
                                            f"loiter spot {x},{y} stands under a seated agent's chips")
            for ox, oy in spots:
                if (ox, oy) == (x, y):
                    continue
                bands_overlap = abs(y - oy) * T < 40.0
                if bands_overlap:
                    self.assertGreaterEqual(abs(x - ox) * T, 88.0,
                                            f"loiter spots {x},{y} and {ox},{oy} share a chip row too closely")


if __name__ == "__main__":
    unittest.main()
