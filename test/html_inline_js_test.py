"""Gate the inline JS in `public/index.html` so one bad token cannot ship a dead page.

Run:  python3 -m unittest test.html_inline_js_test -v
  or as a CLI:  python3 test/html_inline_js_test.py [path-to-index.html]

`public/index.html` is a single file — all CSS and ES2019 JS inline, no build step — so nothing
between an editor save and the deployed page ever parses it. One stray token inside an inline
`<script>` is a whole-file parse error: the page still renders, and every handler on it is dead.

Same three checks as /opt/data/scripts/check_html_js.py, ported into the suite so CI (which already
runs `python3 -m unittest discover -s test -p '*_test.py'`) stops it on the pull request:

  * every `<script>` WITHOUT src= is handed to `node --check` — a parse error is real breakage;
  * every `type="application/ld+json"` block must json.loads — a torn block is broken markup;
  * duplicate element ids, because a dupe silently shadows a node in getElementById.

`type="text/template"` and `type="text/x-*"` blocks are inert markup, not scripts, and are skipped;
ids containing `${` are built inside JS template literals (runtime-unique), so they never count as
duplicates. Python stdlib plus `node` from PATH only — no new dependencies.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_HTML = os.path.join(REPO_ROOT, "public", "index.html")

SCRIPT_RE = re.compile(r"<script([^>]*)>(.*?)</script>", re.S | re.I)


def _line_of(src, pos):
    """1-based line number of `pos` in `src`, so a failure points at the tag in the real file."""
    return src.count("\n", 0, pos) + 1


def _is_inert_type(attrs):
    return bool(re.search(r"type\s*=\s*[\"'](text/template|text/x-)", attrs, re.I))


def _is_json_ld(attrs):
    return bool(re.search(r"type\s*=\s*[\"']application/ld\+json", attrs, re.I))


def inline_script_blocks(src):
    """Every inline `<script>` in `src` as (html line number, body).

    Skips src= tags, `application/ld+json` (checked as JSON instead) and inert text/template and
    text/x-* blocks.
    """
    blocks = []
    for m in SCRIPT_RE.finditer(src):
        attrs = m.group(1)
        if re.search(r"\bsrc\s*=", attrs, re.I) or _is_json_ld(attrs) or _is_inert_type(attrs):
            continue
        blocks.append((_line_of(src, m.start(2)), m.group(2)))
    return blocks


def _iter_blocks(src):
    """(attrs, body_start, body) for every tag without src=, in document order."""
    for m in SCRIPT_RE.finditer(src):
        attrs = m.group(1)
        if re.search(r"\bsrc\s*=", attrs, re.I):
            continue
        yield attrs, m.start(2), m.group(2)


def _json_ld_blocks(src):
    return [(_line_of(src, start), body) for attrs, start, body in _iter_blocks(src) if _is_json_ld(attrs)]


def _id_attrs(src):
    """Every id attribute value, minus the `${…}` ones built inside JS template literals."""
    raw = re.findall(r'\bid\s*=\s*"([^"]+)"', src) + re.findall(r"\bid\s*=\s*'([^']+)'", src)
    return [i for i in raw if "${" not in i]


def _first_inline(src):
    """(attrs, match) for the first REAL inline <script> — the block the bite test corrupts.

    Same skip rules as inline_script_blocks(): src= tags, application/ld+json and inert
    text/template / text/x-* blocks are not scripts.
    """
    for m in SCRIPT_RE.finditer(src):
        attrs = m.group(1)
        if re.search(r"\bsrc\s*=", attrs, re.I) or _is_json_ld(attrs) or _is_inert_type(attrs):
            continue
        yield attrs, m


def check_html(path):
    """(problems, summary) for one HTML file. `problems` are human-readable; empty means clean."""
    problems = []
    with open(path, encoding="utf-8") as fh:
        src = fh.read()

    for ln, body in _json_ld_blocks(src):
        try:
            json.loads(body)
        except ValueError as exc:
            problems.append(f"JSON-LD parse error at html line {ln}: {exc}")

    blocks = inline_script_blocks(src)
    with tempfile.TemporaryDirectory() as td:
        for i, (ln, body) in enumerate(blocks):
            js = os.path.join(td, f"block{i}.js")
            with open(js, "w", encoding="utf-8") as fh:
                fh.write(body)
            try:
                r = subprocess.run(["node", "--check", js], capture_output=True, text=True)
            except OSError as exc:                        # no node on PATH: the gate cannot vouch
                problems.append(f"node --check could not run for inline <script> at html line {ln}: {exc}")
                continue
            if r.returncode:
                problems.append(
                    f"JS PARSE FAIL — inline <script> starting at html line {ln} "
                    f"({len(body.splitlines())} lines):\n{(r.stderr or r.stdout).strip()[:1200]}")

    ids = _id_attrs(src)
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        problems.append(f"DUPLICATE ids: {dupes}")

    summary = (f"{path}: {len(blocks)} inline script block(s) parsed, "
               f"{len(set(ids))} unique ids ({len(ids)} id attrs), "
               f"{'OK' if not problems else str(len(problems)) + ' PROBLEM(S)'}")
    return problems, summary


class InlineJsGateTest(unittest.TestCase):
    def check_string(self, html_text):
        """check_html() for a synthetic page (written to a temp file, gone after the test)."""
        td = tempfile.mkdtemp(prefix="html-inline-js-")
        self.addCleanup(lambda: shutil.rmtree(td, ignore_errors=True))
        path = os.path.join(td, "index.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html_text)
        return check_html(path)

    def test_the_real_page_parses_clean(self):
        """The shipped page must be clean; a failing inline block means every handler is dead."""
        problems, summary = check_html(INDEX_HTML)
        self.assertEqual(problems, [], summary)

    def test_a_broken_token_is_caught(self):
        """`const x = ;` in the first inline block must be reported, naming that block's html line."""
        with open(INDEX_HTML, encoding="utf-8") as fh:
            src = fh.read()
        first = next(m for _a, m in _first_inline(src) if m)
        ln = _line_of(src, first.start(2))                # the line the checker must name
        # injected at the end of that body, so no line above it moves and `ln` stays the real one
        broken = src[:first.end(2)] + "\nconst x = ;\n" + src[first.end(2):]
        problems, summary = self.check_string(broken)
        self.assertTrue(problems, summary)
        self.assertTrue(any(f"inline <script> starting at html line {ln}" in p for p in problems),
                        f"no problem names html line {ln}: {[p.splitlines()[0] for p in problems]}")

    def test_a_duplicate_id_is_caught(self):
        problems, summary = self.check_string(
            '<!doctype html><div id="vitals"></div><span id=\'vitals\'></span>'
            '<b id="kiosk-${n}"></b><i id="kiosk-${n}"></i>'
            "<script>const a = 1;</script>")
        self.assertEqual(len(problems), 1, summary)
        self.assertIn("DUPLICATE ids", problems[0])
        self.assertIn("vitals", problems[0])
        self.assertNotIn("kiosk-", problems[0])           # ${…} ids are runtime-unique, not dupes

    def test_a_bad_json_ld_block_is_caught(self):
        problems, summary = self.check_string(
            '<!doctype html><script type="application/ld+json">{"@type":</script>'
            '<script type="text/x-not-js">const x = ;</script>')
        self.assertEqual(len(problems), 1, summary)       # the inert text/x-* block is not parsed
        self.assertIn("JSON-LD parse error", problems[0])

    def test_inert_and_external_blocks_are_skipped(self):
        problems, summary = self.check_string(
            '<!doctype html><script type="text/template">not js {{{</script>'
            '<script src="themes/kit.js">ignored</script>')
        self.assertEqual(problems, [], summary)


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else INDEX_HTML
    found, one_line = check_html(target)
    for p in found:
        print(p)
    print(one_line)
    sys.exit(1 if found else 0)
