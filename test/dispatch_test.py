"""Nudge tests: a throwaway Hermes dashboard on localhost, then the office's side of the call.

Run:  python3 -m unittest test.dispatch_test -v

What this proves matters more than usual: the route POSTs to the LIVE dashboard at :9119 with the
credentials in creds/dashboard.json, so the two things that must never happen are (a) the password
leaving this process in a response body or in the actions log, and (b) a double-click spawning two
dispatcher passes. Both are asserted here against a fake server, and (b) again live by the
orchestrator. Same discipline as the other tests: stdlib only, env resolved per call (dispatch and
actions both read it at call time), and dispatch.reset() in setUp so the 15 s slot never leaks
between tests.
"""
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import dispatch, office  # noqa: E402

PW = "pw-canary-4f9c"                                   # grep for this in everything that gets written
# THE LIVE SHAPE, verbatim from a probe of :9119/api/plugins/kanban/dispatch on 2026-11-16: spawned
# is a list of (task, assignee, workspace) TRIPLES and auto_blocked / crashed / stale / timed_out are
# LISTS of task ids, not counts. int() of a non-empty list raises TypeError, so the old int()-only
# reader reported 0 for every one of them while cards sat there blocked — hence these fixtures carry
# the lists, and the assertions below read their LENGTHS.
ANSWER = {"spawned": [["t_one", "coder", "/w/one"], ["t_two", "maya", ""]], "reclaimed": 1,
          "promoted": 0, "reconciled_orphans": [], "reaped_terminal_workers": [],
          "auto_blocked": [], "crashed": ["t_three"], "stale": [], "timed_out": ["t_four"],
          "skipped_unassigned": [], "auto_assigned_default": [], "skipped_nonspawnable": [],
          "skipped_per_profile_capped": [], "skipped_locked": False, "memory_pressure": None}
EMPTY = {"spawned": [], "reclaimed": 0, "promoted": 0, "reconciled_orphans": [],
         "reaped_terminal_workers": [], "auto_blocked": [], "crashed": [], "stale": [],
         "timed_out": [], "skipped_unassigned": [], "auto_assigned_default": [],
         "skipped_nonspawnable": [], "skipped_per_profile_capped": [], "skipped_locked": False,
         "memory_pressure": None}


class FakeServer(http.server.ThreadingHTTPServer):
    """State the handler reads. Declared here so nothing is invented on an instance at runtime."""

    hits: list = []
    logins: list = []
    answer: dict = {}
    refuse_401: int = 0
    login_status: int = 200


class FakeDashboard(http.server.BaseHTTPRequestHandler):
    """The two routes server-side dispatch calls: the password login and the kanban dispatch."""

    def do_POST(self):                                    # noqa: N802 (http.server naming)
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        srv = self.server
        srv.hits.append(self.path)
        if self.path == dispatch.LOGIN_PATH:
            srv.logins.append(json.loads(body or b"{}"))
            if srv.login_status != 200:
                return self._send(srv.login_status, {"ok": False, "error": "bad credentials"})
            return self._send(200, {"ok": True}, cookie=True)
        if self.path.startswith(dispatch.DISPATCH_PATH):
            if srv.refuse_401 > 0:                        # a session that aged out mid-flight
                srv.refuse_401 -= 1
                return self._send(401, {"ok": False, "error": "session expired"})
            return self._send(200, srv.answer)
        return self._send(404, {"ok": False, "error": "no such route"})

    def _send(self, status, payload, cookie=False):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        if cookie:
            self.send_header("Set-Cookie", "hermes_session_test=1; Path=/")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
        return True

    def log_message(self, format, *args):                 # the fake must not print into the test run
        pass


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="office-dispatch-")
        self.srv = FakeServer(("127.0.0.1", 0), FakeDashboard)
        self.srv.daemon_threads = True
        self.srv.hits, self.srv.logins = [], []
        self.srv.answer, self.srv.refuse_401, self.srv.login_status = dict(ANSWER), 0, 200
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()
        self.creds = os.path.join(self.dir, "dashboard.json")
        self._creds({"username": "hermes", "password": PW, "url": self.base, "provider": "basic"})
        self.log = os.path.join(self.dir, "office_actions.log")
        self.env = {}
        # Every knob dispatch/actions reads is resolved per call, so a temp dir steers the whole leg.
        for key, value in (("OFFICE_DASHBOARD_CREDS", self.creds), ("OFFICE_ACTIONS_LOG", self.log),
                           ("OFFICE_HERMES_DIR", self.dir), ("OFFICE_DISPATCH_MAX", "4")):
            self.env[key] = os.environ.get(key)
            os.environ[key] = value
        self.env["OFFICE_DASHBOARD_URL"] = os.environ.get("OFFICE_DASHBOARD_URL")
        os.environ.pop("OFFICE_DASHBOARD_URL", None)      # the creds file's url decides the target
        dispatch.reset()

    def tearDown(self):
        dispatch.reset()
        for key, before in self.env.items():
            if before is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = before
        self.srv.shutdown()
        self.srv.server_close()
        for name in os.listdir(self.dir):
            os.chmod(os.path.join(self.dir, name), 0o644)
            os.unlink(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    # ---------- helpers ----------
    def _creds(self, blob):
        with open(self.creds, "w") as fh:
            json.dump(blob, fh)

    def _log_lines(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def _dispatch_hits(self):
        return [h for h in self.srv.hits if h.startswith(dispatch.DISPATCH_PATH)]

    # ---------- the happy path ----------
    def test_a_pass_logs_in_once_and_answers_the_page(self):
        code, body, retry = dispatch.nudge({})
        self.assertEqual((code, retry), (200, None))
        self.assertTrue(body["ok"])
        self.assertEqual(body["spawned"], [{"task": "t_one", "assignee": "coder"},
                                           {"task": "t_two", "assignee": "maya"}])
        self.assertEqual(body["spawned_n"], 2)
        self.assertEqual([body["reclaimed"], body["promoted"], body["auto_blocked"]], [1, 0, 0])
        # the LIVE-shape pin: crashed/stale/timed_out arrive as LISTS of ids, so a counter that
        # int()-coerces them reads 0 and a blocked card is invisible. Length is the count.
        self.assertEqual(body["crashed"], 1)                       # ["t_three"] -> 1, not 0
        self.assertEqual(body["stale"], 0)                         # ... and an empty list stays 0
        self.assertEqual(body["message"], "spawned 2 · reclaimed 1 · promoted 0")
        self.assertEqual((body["deferred"], body["deferred_n"], body["suppression"]),
                         ([], 0, ""))                             # nothing was held back here
        self.assertEqual(len(self.srv.logins), 1)
        self.assertEqual(self.srv.logins[0], {"provider": "basic", "username": "hermes",
                                              "password": PW})
        self.assertEqual(self._dispatch_hits(), [dispatch.DISPATCH_PATH + "?dry_run=false&max=4"])

    def test_an_empty_board_answers_honestly(self):
        self.srv.answer = dict(EMPTY)
        code, body, _ = dispatch.nudge({})
        self.assertEqual(code, 200)
        self.assertEqual((body["spawned_n"], body["message"]), (0, "nothing to do"))
        self.assertFalse(any(body[key] for key in dispatch.COUNTERS))

    def test_a_stale_session_buys_exactly_one_relogin_and_one_retry(self):
        self.srv.refuse_401 = 1
        code, body, _ = dispatch.nudge({})
        self.assertEqual(code, 200)
        self.assertEqual(len(self.srv.logins), 2)          # one re-login, not a loop
        self.assertEqual(len(self._dispatch_hits()), 2)    # the refused one and the retry

    # ---------- why nothing was spawned: the deferred half of the answer ----------
    def test_a_saturated_pass_names_every_reason_it_held_work_back(self):
        """The t_b41a882a case: 200, zero spawns, and the page had nothing honest to say about it."""
        self.srv.answer = dict(EMPTY, memory_pressure="critical", skipped_host_capped=[8, 5],
                               skipped_per_profile_capped=[["t_a", "coder", 1]],
                               respawn_guarded=[["t_b", "active_pr"]])
        code, body, _ = dispatch.nudge({})
        self.assertEqual(code, 200)
        self.assertEqual(body["spawned_n"], 0)
        self.assertTrue(body["deferred_n"] >= 3)                    # both cards + both board holds
        ids = [row[0] for row in body["deferred"]]
        self.assertEqual(ids, ["t_a", "t_b"])                       # capped at 8, but naming both
        self.assertEqual(body["deferred"][1][1], "active_pr")        # the guard reason travels too
        suppression = body["suppression"]
        self.assertIn("memory_pressure=critical", suppression)       # the dispatcher's own words
        self.assertIn("host cap reached (8/5 in progress)", suppression)
        self.assertIn("per_profile_capped=1", suppression)
        self.assertIn("active_pr=1", suppression)
        self.assertTrue(len(suppression) < 180)                      # a line has to fit on the TV
        self.assertIn("deferred:", body["message"])
        self.assertIn("nothing to do", body["message"].split(" · deferred: ")[0])
        self.assertTrue(len(body["message"]) <= 240)
        self.assertNotIn(PW, json.dumps(body))
        # ... and the log line keeps the same truth, ids only, still no credential
        line = self._log_lines()[0]
        self.assertEqual(line["deferred"], ["t_a", "t_b"])
        self.assertEqual(line["deferred_n"], body["deferred_n"])
        self.assertEqual(line["suppression"], suppression)
        self.assertNotIn(PW, json.dumps(line))

    def test_an_empty_board_defers_nothing_and_says_so_plainly(self):
        self.srv.answer = dict(EMPTY)
        code, body, _ = dispatch.nudge({})
        self.assertEqual(code, 200)
        self.assertEqual(body["suppression"], "")                   # nothing to name
        self.assertEqual(body["message"], "nothing to do")          # and nothing else on the line
        self.assertEqual((body["deferred"], body["deferred_n"]), ([], 0))
        self.assertNotIn(PW, json.dumps(body))
        self.assertNotIn(PW, json.dumps(self._log_lines()))

    def test_a_dashboard_that_sends_rubbish_in_a_bucket_still_answers_200(self):
        """Every bucket is a .get() on a payload we do not own: none of these may raise."""
        for key, value in (("skipped_per_profile_capped", "not-a-list"),
                           ("respawn_guarded", [[None], ["t_b"], 7, {"task": "t_c"}]),
                           ("skipped_host_capped", ["x", "y"]), ("memory_pressure", {"a": 1}),
                           ("rate_limited", None), ("auto_blocked", {"t_z": "why"})):
            self.srv.answer = dict(EMPTY, **{key: value})
            code, body, _ = dispatch.nudge({}, dry_run=True)
            self.assertEqual(code, 200, f"{key}={value!r} must not raise")
            self.assertTrue(len(body["message"]) <= 240)
            self.assertNotIn(PW, json.dumps(body))

    def test_a_reason_that_echoed_the_dashboard_password_is_masked(self):
        """Rule 1 of AGENTS.md is about the response body, and the reason text is the dashboard's."""
        self.srv.answer = dict(EMPTY, respawn_guarded=[["t_b", f"https://hermes:{PW}@10.10.10.202"]])
        code, body, _ = dispatch.nudge({})
        self.assertEqual(code, 200)
        self.assertNotIn(PW, json.dumps(body))
        self.assertNotIn(PW, json.dumps(self._log_lines()))

    # ---------- the two things that must never happen ----------
    def test_the_password_never_leaves_the_process(self):
        code, body, _ = dispatch.nudge({})
        self.assertEqual(code, 200)
        self.assertNotIn(PW, json.dumps(body))
        lines = self._log_lines()
        self.assertEqual(len(lines), 1)                    # exactly one line per attempt
        line = lines[0]
        self.assertNotIn(PW, json.dumps(line))
        self.assertEqual((line["action"], line["task"], line["ok"]), ("dispatch", "queue", True))
        self.assertEqual(line["spawned"], ["t_one", "t_two"])
        self.assertEqual(line["spawned_n"], 2)

    def test_one_real_pass_per_15_seconds(self):
        first = dispatch.nudge({})
        self.assertEqual(first[0], 200)
        code, body, retry = dispatch.nudge({})             # the double-click this guard exists for
        self.assertEqual((code, body["error"], body["ok"]), (429, "rate_limited", False))
        self.assertTrue(retry >= 1)
        self.assertEqual(len(self._dispatch_hits()), 1)    # the second attempt never reached it
        self.assertEqual(len(self._log_lines()), 2)        # ... but a denial is still logged

    def test_the_dry_run_preview_is_not_limited(self):
        self.assertEqual(dispatch.nudge({}, dry_run=True)[0], 200)
        code, body, _ = dispatch.nudge({}, dry_run=True)
        self.assertEqual(code, 200)
        self.assertTrue(body["dry_run"])
        self.assertEqual(len(self._dispatch_hits()), 2)
        self.assertEqual(len(self._log_lines()), 2)

    # ---------- when the dashboard or the credentials are not there ----------
    def test_missing_credentials_never_open_a_socket(self):
        os.environ["OFFICE_DASHBOARD_CREDS"] = os.path.join(self.dir, "not-here.json")
        code, body, _ = dispatch.nudge({})
        self.assertEqual((code, body["error"], body["ok"]), (503, "creds_missing", False))
        self.assertEqual(self.srv.hits, [])

    def test_a_dashboard_that_is_not_listening_is_a_502(self):
        srv = FakeServer(("127.0.0.1", 0), FakeDashboard)
        dead = "http://127.0.0.1:%d" % srv.server_address[1]
        srv.server_close()                                 # the port was bound, now nobody answers
        self._creds({"username": "hermes", "password": PW, "url": dead})
        code, body, _ = dispatch.nudge({})
        self.assertEqual((code, body["error"]), (502, "dashboard_unreachable"))
        self.assertNotIn(PW, json.dumps(body))

    def test_refused_credentials_are_reported_without_echoing_them(self):
        self.srv.login_status = 401
        code, body, _ = dispatch.nudge({})
        self.assertEqual((code, body["error"]), (502, "dashboard_unreachable"))
        self.assertNotIn(PW, json.dumps(body))
        self.assertNotIn(PW, json.dumps(self._log_lines()))

    # ---------- the capability key the button is gated on ----------
    def test_the_capability_key_is_honest_and_credential_free(self):
        cap = dispatch.capability()
        self.assertTrue(cap["available"])
        self.assertEqual(cap["base"], self.base)
        self.assertEqual(cap["dry_run_path"], dispatch.DRY_RUN_PATH)
        self.assertNotIn(PW, json.dumps(cap))
        # a creds file whose url carries userinfo must not put the secret on the page either
        self._creds({"username": "hermes", "password": PW, "url": "http://hermes:%s@10.10.10.202:9119" % PW})
        self.assertEqual(dispatch.capability()["base"], "http://10.10.10.202:9119")
        os.environ["OFFICE_DASHBOARD_CREDS"] = os.path.join(self.dir, "not-here.json")
        dark = dispatch.capability()
        self.assertFalse(dark["available"])
        self.assertIn("not-here.json", dark["reason"])     # the reason names the file we could not read


class StockCeoRouteTest(unittest.TestCase):
    """The vendored client GETs /api/cubicle/dispatch on EVERY page load; we must answer, not 404.

    No second server: BaseHTTPRequestHandler starts talking HTTP inside its own constructor, so the
    fixture is `Handler.__new__` plus the three attributes the two do_* methods actually read
    (headers, path, rfile), and `_send` is monkeypatched ON THE INSTANCE — the real one wants a live
    socket to write to. That is enough to pin the status, the body and the route order.
    """

    def _handler(self, path, body=b""):
        h = office.Handler.__new__(office.Handler)
        h.headers = {"Content-Length": str(len(body)), "x-cubicle": "1"}
        h.path = path
        h.rfile = io.BytesIO(body)
        h.sent = []
        h._send = lambda code, payload, **kw: h.sent.append((code, payload, kw))
        return h

    def test_the_stock_ceo_poll_gets_a_200_that_switches_the_feature_off(self):
        h = self._handler("/api/cubicle/dispatch")
        h.do_GET()
        (code, body, _) = h.sent[0]
        self.assertEqual(code, 200)                              # was 404 on every single page load
        self.assertIs(body["enabled"], False)                    # ... so the dialog stays closed
        self.assertEqual(body["runs"], [])                       # and the run list renders empty
        self.assertIs(body["available"], False)
        self.assertIn("Check queue", body["reason"])             # the route that DOES exist

    def test_a_stray_post_is_answered_and_its_body_drained(self):
        h = self._handler("/api/cubicle/dispatch", b'{"text":"do something"}')
        h.do_POST()
        (code, body, _) = h.sent[0]
        self.assertEqual(code, 405)                              # an answer, not a bare 404
        self.assertEqual((body["ok"], body["error"]), (False, "not_supported"))
        self.assertEqual(body["message"], office.CEO_OFF)           # names the route that does exist
        self.assertEqual(h.rfile.read(), b"")                    # keep-alive: nothing left in the socket

    def test_the_catch_all_404_still_covers_every_other_api_path(self):
        h = self._handler("/api/cubicle/not-a-route")
        h.do_GET()
        self.assertEqual(h.sent[0][0], 404)                      # the new route did not swallow it
        h2 = self._handler("/api/cubicle/settings")
        h2.do_GET()
        self.assertEqual(h2.sent[0][0], 200)                     # the older precedent still answers


if __name__ == "__main__":
    unittest.main()
