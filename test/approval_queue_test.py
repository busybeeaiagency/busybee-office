"""Approval-queue wiring: an answered card must clear the SHARED queue, not just the DB.

Run:  python3 -m unittest test.approval_queue_test -v

Two layers on purpose:
  * the loader and the action path run against `test/approval_queue_stub.py`, a fixture that
    implements the canonical SPEC-1 API on temp files (the real module lives on the Hermes side and
    is not in this repo, so the unit tests can never depend on it being there);
  * one integration test drives the REAL `/opt/data/scripts/approval_queue.py` through the same
    loader and skips with a reason when that file is absent or does not load, so a machine without a
    Hermes mount stays green instead of failing on someone else's deploy.

Nothing here writes to /opt/data: OFFICE_* / APPROVAL_* are all pointed at temp paths, and an
explicitly configured OFFICE_APPROVAL_MODULE is authoritative (server/approval_queue.py), so the
"queue is dark" cases really do have no queue.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import actions, approval_queue as queue_loader  # noqa: E402

STUB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "approval_queue_stub.py")
REAL_MODULE = "/opt/data/scripts/approval_queue.py"
SCHEMA = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema_live.sql")).read()
# ^ the REAL kanban.db DDL, same as test/actions_test.py: the action path must survive the real
#   columns while it publishes to the queue.

ENV_KEYS = ("OFFICE_DB_RW", "OFFICE_ALLOW_WRITES", "OFFICE_STATE", "OFFICE_ACTIONS_LOG",
            "OFFICE_APPROVAL_MODULE", "APPROVAL_STATE_DIR", "APPROVAL_GATE_STATE",
            "APPROVAL_KANBAN_DB")


class QueueHarness(unittest.TestCase):
    """The throwaway DB + temp queue, with the route's guard order mirrored in attempt()."""

    def make_harness(self, module_path):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.state = tempfile.mktemp(suffix=".json")
        self.log = tempfile.mktemp(suffix=".log")
        self.state_dir = tempfile.mkdtemp(prefix="approvals-")
        os.environ["OFFICE_DB_RW"] = self.db
        os.environ["OFFICE_ALLOW_WRITES"] = "1"
        os.environ["OFFICE_STATE"] = self.state
        os.environ["OFFICE_ACTIONS_LOG"] = self.log
        os.environ["OFFICE_APPROVAL_MODULE"] = module_path
        os.environ["APPROVAL_STATE_DIR"] = self.state_dir
        os.environ["APPROVAL_GATE_STATE"] = os.path.join(self.state_dir, "review_gate_state.json")
        os.environ["APPROVAL_KANBAN_DB"] = self.db      # never the live /opt/data/kanban.db
        self.now = 1_800_000_000
        self.con = sqlite3.connect(self.db)
        self.con.executescript(SCHEMA)
        self.con.commit()

    def drop_harness(self):
        self.con.close()
        for path in (self.db, self.state, self.log):
            try:
                os.unlink(path)
            except OSError:
                pass
        shutil.rmtree(self.state_dir, ignore_errors=True)
        for key in ENV_KEYS:
            os.environ.pop(key, None)

    # ---------- helpers (same shape as test/actions_test.py) ----------
    def add_card(self, tid="t_aaaa01", status="blocked", assignee="coder", block=None):
        self.con.execute("insert into tasks (id,title,assignee,status,created_at,block_kind)"
                         " values (?,?,?,?,?,?)",
                         (tid, f"work {tid}", assignee, status, self.now, block))
        self.con.commit()

    def task_row(self, tid):
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        try:
            return con.execute("select * from tasks where id=?", (tid,)).fetchone()
        finally:
            con.close()

    def attempt(self, action, task, note=None):
        """Mirror the route: rate (read-only) -> token -> record -> perform."""
        self.assertTrue(actions.check_rate(action, task, self.now)[0])
        self.assertTrue(actions.consume_token(action, task, actions.issue_token(action, task,
                                                                               self.now),
                                              self.now)[0])
        actions.record_hit(action, task, self.now)
        return actions.perform(action, task, note=note)

    def overlay_rows(self):
        path = os.path.join(self.state_dir, "decisions_overlay.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(line) for line in f.read().splitlines() if line.strip()]

    def seed_request(self, q, tid, action="review_decision", marker=True):
        """A pending request + the review gate's announcement marker, as the cron leaves them."""
        q.register(tid, f"Approve the PR for {tid}?", "coding", action, surface="review-gate")
        st = q.gate_state()
        if marker:
            st["posted"][tid] = f"posted approval to #coding: post_1 (task={tid})"
        st["decisions_offset"] = 42                      # must survive a marker pop
        st["overlay_offset"] = 7
        q.save_gate_state(st)
        return tid


class QueueLoaderTest(QueueHarness):
    """`server/approval_queue.py` must hand back the module, or None — never an exception."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aql-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for key in ("OFFICE_APPROVAL_MODULE",):
            os.environ.pop(key, None)

    def write_module(self, name, source):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write(source)
        return path

    def test_configured_module_is_loaded(self):
        path = self.write_module("ok.py", "MARK = 'configured'\ndef decide(*a, **k):\n    return {}\n")
        os.environ["OFFICE_APPROVAL_MODULE"] = path
        self.assertEqual(queue_loader.queue().MARK, "configured")

    def test_the_configured_path_is_used_alone(self):
        """A deliberate override must not fall back to the container/host default copy."""
        path = self.write_module("only.py", "MARK = 'chosen'\ndef decide(*a, **k):\n    return {}\n")
        self.write_module("decoy.py", "MARK = 'decoy'\ndef decide(*a, **k):\n    return {}\n")
        os.environ["OFFICE_APPROVAL_MODULE"] = path
        self.assertEqual(queue_loader.candidate_paths(), [path])
        self.assertEqual(queue_loader.queue().MARK, "chosen")

    def test_missing_file_is_none_not_an_error(self):
        os.environ["OFFICE_APPROVAL_MODULE"] = os.path.join(self.tmp, "nope.py")
        self.assertIsNone(queue_loader.queue())
        self.assertFalse(queue_loader.available())

    def test_a_file_that_does_not_parse_is_none_not_an_error(self):
        path = self.write_module("broken.py", "def decide(:\n    pass\n")
        os.environ["OFFICE_APPROVAL_MODULE"] = path
        self.assertIsNone(queue_loader.queue())

    def test_a_module_that_raises_on_import_is_none_not_an_error(self):
        path = self.write_module("boom.py", "raise RuntimeError('half-deployed')\n")
        os.environ["OFFICE_APPROVAL_MODULE"] = path
        self.assertIsNone(queue_loader.queue())

    def test_default_is_the_container_mount_then_the_host_checkout(self):
        """Pinned: the container reads the queue off the read-only Hermes mount first."""
        os.environ.pop("OFFICE_APPROVAL_MODULE", None)
        self.assertEqual(queue_loader.DEFAULT_PATHS,
                         ("/hermes/scripts/approval_queue.py", "/opt/data/scripts/approval_queue.py"))
        self.assertEqual(queue_loader.candidate_paths(), list(queue_loader.DEFAULT_PATHS))

    def test_the_stub_implements_the_surface_the_dashboard_calls(self):
        os.environ["OFFICE_APPROVAL_MODULE"] = STUB
        q = queue_loader.queue()
        for name in ("decide", "register", "prune", "load_pending", "append_overlay",
                     "clear_gate_marker", "gate_state", "save_gate_state", "pending_rows",
                     "sweep_stale"):
            self.assertTrue(callable(getattr(q, name)), f"fixture missing {name}")
        self.assertEqual(q.DECISIONS, ("approve", "reject", "restart", "dismiss", "decision"))


class ActionPublishesDecisionTest(QueueHarness):
    """approve/reject/restart answer the queue; dismiss does not; a dark queue never breaks a click."""

    def setUp(self):
        self.make_harness(STUB)
        self.q = queue_loader.queue()
        self.assertIsNotNone(self.q, "the fixture queue must load from OFFICE_APPROVAL_MODULE")

    def tearDown(self):
        self.drop_harness()

    def test_approve_on_review_writes_an_overlay_row_for_the_dashboard_surface(self):
        tid = self.seed_request(self.q, "t_c0ffee01")
        self.add_card(tid=tid, status="review")
        res = self.attempt("approve", tid, note="Merged, looks right.")
        self.assertTrue(res["ok"])
        rows = self.overlay_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["task_id"], tid)
        self.assertEqual(row["decision"], "approve")
        self.assertEqual(row["action"], "approve")               # legacy key, kept on purpose
        self.assertEqual(row["surface"], "dashboard")
        self.assertEqual(row["source"], "dashboard")
        self.assertEqual(row["channel"], "coding")
        self.assertEqual(row["actor"], "bart")
        self.assertEqual(row["detail"], "Merged, looks right.")

    def test_a_card_without_a_pending_request_is_still_decided(self):
        """The dashboard answers decisions made anywhere (a blocked card never entered the queue)."""
        tid = "t_c0ffee02"
        self.add_card(tid=tid, block="needs_input")
        res = self.attempt("approve", tid, note="yes, go")
        self.assertTrue(res["ok"])
        self.assertEqual(res["after"], "ready")
        self.assertEqual(len(self.overlay_rows()), 1)
        self.assertFalse(res["approval_queue"]["pruned"])         # nothing to drop
        self.assertEqual(self.q.load_pending(), {})

    def test_no_note_still_records_an_honest_default_detail(self):
        tid = self.seed_request(self.q, "t_c0ffee03")
        self.add_card(tid=tid, status="review")
        self.attempt("approve", tid)
        self.assertEqual(self.overlay_rows()[0]["detail"],
                         "Approved from the BUSY BEE office dashboard")

    def test_reject_and_restart_are_recorded_as_their_own_decisions(self):
        for tid, action, status in (("t_c0ffee04", "reject", "review"), ("t_c0ffee05", "restart", "review")):
            self.seed_request(self.q, tid)
            self.add_card(tid=tid, status=status)
            res = self.attempt(action, tid, note="because")
            self.assertTrue(res["ok"], res)
        rows = {r["task_id"]: r for r in self.overlay_rows()}
        self.assertEqual(rows["t_c0ffee04"]["decision"], "reject")
        self.assertEqual(rows["t_c0ffee04"]["detail"], "because")
        self.assertEqual(rows["t_c0ffee05"]["decision"], "restart")

    def test_the_pending_entry_is_removed_and_the_removal_is_logged(self):
        tid = self.seed_request(self.q, "t_c0ffee06")
        self.add_card(tid=tid, status="review")
        res = self.attempt("reject", tid, note="Tests were not run.")
        self.assertNotIn(tid, self.q.load_pending())
        self.assertTrue(res["approval_queue"]["pruned"])
        self.assertTrue(res["approval_queue"]["ok"])
        self.assertIsNone(res["approval_queue"]["error"])
        with open(os.path.join(self.state_dir, "prune_log.jsonl")) as f:
            logged = [json.loads(line) for line in f.read().splitlines() if line.strip()]
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0]["task_id"], tid)
        self.assertEqual(logged[0]["reason"], "dashboard:reject")   # the audit trail says who decided

    def test_the_review_gate_marker_is_cleared_without_losing_the_offsets(self):
        tid = self.seed_request(self.q, "t_c0ffee07")
        self.add_card(tid=tid, status="review")
        res = self.attempt("restart", tid)
        self.assertTrue(res["approval_queue"]["marker"])
        st = self.q.gate_state()
        self.assertNotIn(tid, st["posted"])                        # so it re-announces on return
        self.assertEqual(st["decisions_offset"], 42)
        self.assertEqual(st["overlay_offset"], 7)

    def test_the_payload_carries_the_queue_block_for_every_decision(self):
        for action, status, tid in (("approve", "review", "t_c0ffee08"),
                                    ("reject", "review", "t_c0ffee09"),
                                    ("restart", "review", "t_c0ffee0a"),
                                    ("approve", "blocked", "t_c0ffee0b")):
            self.add_card(tid=tid, status=status, block=("needs_input" if status == "blocked" else None))
            res = self.attempt(action, tid)
            self.assertEqual(set(res["approval_queue"]), {"ok", "pruned", "marker", "error"}, res)
            self.assertTrue(res["approval_queue"]["ok"], res)

    def test_dismiss_records_no_decision_and_leaves_the_queue_alone(self):
        tid = self.seed_request(self.q, "t_c0ffee0c")
        self.add_card(tid=tid, block=None)
        res = self.attempt("dismiss", tid)
        self.assertTrue(res["ok"])
        self.assertNotIn("approval_queue", res)                    # not an answer to the request
        self.assertEqual(self.overlay_rows(), [])
        self.assertIn(tid, self.q.load_pending())                  # still awaiting a real decision
        self.assertIn(tid, self.q.gate_state()["posted"])

    def test_a_denied_action_publishes_nothing(self):
        tid = self.seed_request(self.q, "t_c0ffee0d")
        self.add_card(tid=tid, status="running")                   # reject needs a review card
        with self.assertRaises(actions.ActionError):
            self.attempt("reject", tid)
        self.assertEqual(self.overlay_rows(), [])
        self.assertIn(tid, self.q.load_pending())

    def test_a_dark_queue_still_moves_the_card_and_says_why(self):
        empty_dir = tempfile.mkdtemp(prefix="aquadead-")          # a dir with no module in it
        self.addCleanup(shutil.rmtree, empty_dir, True)
        self.drop_harness()                                        # rebuild the harness, queue absent
        self.make_harness(os.path.join(empty_dir, "approval_queue.py"))
        tid = "t_c0ffee0e"
        self.add_card(tid=tid, status="review")
        res = self.attempt("approve", tid, note="go")
        self.assertTrue(res["ok"])
        self.assertEqual(self.task_row(tid)["status"], "done")     # the click worked
        block = res["approval_queue"]
        self.assertFalse(block["ok"])
        self.assertEqual(block["error"], "approval_queue unavailable")
        self.assertFalse(block["pruned"])
        self.assertFalse(block["marker"])

    def test_a_queue_that_raises_is_reported_and_never_breaks_the_request(self):
        path = self.write_stub("raising.py", """
def decide(task_id, decision, **kw):
    raise RuntimeError("disk gone")
""")
        self.swap_module(path)
        tid = "t_c0ffee0f"
        self.add_card(tid=tid, status="review")
        res = self.attempt("approve", tid)
        self.assertTrue(res["ok"])
        self.assertEqual(self.task_row(tid)["status"], "done")
        self.assertFalse(res["approval_queue"]["ok"])
        self.assertIn("disk gone", res["approval_queue"]["error"])

    def test_a_queue_that_reports_failure_passes_the_reason_through(self):
        path = self.write_stub("failing.py", """
def decide(task_id, decision, **kw):
    return {"ok": False, "pruned": {"removed": None}, "marker": False,
            "error": "OSError: read-only"}
""")
        self.swap_module(path)
        tid = "t_c0ffee10"
        self.add_card(tid=tid, status="review")
        res = self.attempt("reject", tid)
        self.assertTrue(res["ok"])
        self.assertFalse(res["approval_queue"]["ok"])
        self.assertEqual(res["approval_queue"]["error"], "OSError: read-only")
        self.assertFalse(res["approval_queue"]["pruned"])
        self.assertFalse(res["approval_queue"]["marker"])

    def test_a_queue_returning_a_bare_bool_pruned_is_understood(self):
        path = self.write_stub("boolpruned.py", """
def decide(task_id, decision, **kw):
    return {"ok": True, "pruned": True, "marker": True}
""")
        self.swap_module(path)
        tid = "t_c0ffee11"
        self.add_card(tid=tid, status="review")
        res = self.attempt("approve", tid)
        self.assertTrue(res["approval_queue"]["pruned"])
        self.assertTrue(res["approval_queue"]["marker"])
        self.assertIsNone(res["approval_queue"]["error"])

    def write_stub(self, name, source):
        path = os.path.join(self.state_dir, name)
        with open(path, "w") as f:
            f.write(source)
        return path

    def swap_module(self, path):
        """Point at another module file; the loader keys its cache by path, so no global reset."""
        os.environ["OFFICE_APPROVAL_MODULE"] = path
        self.assertIsNotNone(queue_loader.queue())

    def test_the_fixture_honours_the_api_it_stands_in_for(self):
        """A fixture nobody checks is a fixture that quietly lies about the other agent's contract."""
        tid = self.seed_request(self.q, "t_c0ffee12")
        self.add_card(tid=tid, status="review")
        rows = self.q.pending_rows()
        self.assertEqual([r["task_id"] for r in rows], [tid])
        self.assertEqual(rows[0]["status"], "review")            # the live card status, not just the file
        again = self.q.register(tid, "re-announced?", "coding", "review_decision", surface="cron")
        self.assertFalse(again["changed"])                       # exactly-once, even across surfaces
        self.assertEqual(again["entry"]["label"], f"Approve the PR for {tid}?")
        self.assertEqual(self.q.sweep_stale(), [])               # a review card is not stale
        self.con.execute("update tasks set status = 'done' where id = ?", (tid,))
        self.con.commit()
        self.assertEqual(self.q.sweep_stale(), [{"task_id": tid, "status": "done",
                                                 "pending_removed": True, "marker_removed": True}])
        self.assertEqual(self.q.load_pending(), {})
        self.assertNotIn(tid, self.q.gate_state()["posted"])
        replaced = self.q.register(tid, "restart it?", "coding", "restart_decision", surface="cron")
        self.assertTrue(replaced["changed"])                     # a DIFFERENT action replaces the entry

    def test_decision_actions_are_the_three_the_review_gate_offers(self):
        self.assertEqual(set(actions.DECISION_ACTIONS), {"approve", "reject", "restart"})
        self.assertNotIn("dismiss", actions.DECISION_ACTIONS)


class RealModuleIntegrationTest(QueueHarness):
    """Same contract against the canonical module, when this machine has one."""

    def setUp(self):
        self.make_harness(REAL_MODULE)

    def tearDown(self):
        self.drop_harness()

    def test_the_real_module_answers_the_calls_the_dashboard_makes(self):
        if not os.path.isfile(REAL_MODULE):
            self.skipTest(f"{REAL_MODULE} is not on this machine (no Hermes mount, or the module is "
                          "still being written); test/approval_queue_stub.py covers this contract")
        q = queue_loader.queue()
        if q is None:
            self.skipTest(f"{REAL_MODULE} exists but did not load — the other half of the SPEC is "
                          "still mid-deploy")
        for name in ("decide", "register", "save_gate_state", "load_pending", "gate_state"):
            if not callable(getattr(q, name, None)):
                self.fail(f"{REAL_MODULE} does not implement {name}() — the API the dashboard codes "
                          "against has drifted")
        tid = self.seed_request(q, "t_c0ffee21")
        self.add_card(tid=tid, status="review")
        res = self.attempt("approve", tid, note="Merged.")
        self.assertTrue(res["approval_queue"]["ok"], res["approval_queue"])
        self.assertTrue(res["approval_queue"]["pruned"], "the real module left the request pending")
        self.assertTrue(res["approval_queue"]["marker"], "the real module left the gate marker")
        self.assertNotIn(tid, q.load_pending())
        self.assertNotIn(tid, q.gate_state()["posted"])
        rows = self.overlay_rows()
        self.assertEqual(rows[-1]["surface"], "dashboard")
        self.assertEqual(rows[-1]["decision"], "approve")
        self.assertEqual(self.task_row(tid)["status"], "done")


class QueueStatusTest(QueueHarness):
    """The status endpoints must tell an empty queue apart from an unreachable one."""

    def setUp(self):
        self.make_harness(STUB)
        self.q = queue_loader.queue()
        if self.q is None:
            self.skipTest("test/approval_queue_stub.py did not load through server/approval_queue.py")

    def tearDown(self):
        self.drop_harness()

    def _pending_file(self):
        return os.path.join(self.state_dir, "pending.json")

    def test_open_requests_are_counted_with_their_cards(self):
        self.q.register("t_c0ffee31", "Approve the PR?", "coding", "review_decision",
                        surface="review-gate")
        self.q.register("t_c0ffee32", "Reject the deploy?", "coding", "review_decision",
                        surface="review-gate")
        self.assertEqual(actions.queue_status(),
                         {"open": 2, "ids": ["t_c0ffee31", "t_c0ffee32"]})

    def test_an_empty_queue_is_zero_not_none(self):
        with open(self._pending_file(), "w") as f:
            f.write("{}\n")
        self.assertEqual(actions.queue_status(), {"open": 0, "ids": []})

    def test_a_queue_that_cannot_be_loaded_is_none(self):
        """No module loaded => None: the UI says 'not wired', never 'nothing to decide'."""
        os.environ["OFFICE_APPROVAL_MODULE"] = os.path.join(self.state_dir, "absent.py")
        self.assertIsNone(actions.queue_status())

    def test_a_pending_file_that_is_not_json_is_none(self):
        """A torn write is a fault to report, not a queue that happens to be empty."""
        with open(self._pending_file(), "w") as f:
            f.write('{"t_c0ffee33": {"label": "trunc')
        self.assertIsNone(actions.queue_status())


if __name__ == "__main__":
    unittest.main()
