"""Operator-action tests: build a throwaway DB, point the actions module at it, assert semantics.

Run:  python3 -m unittest test.actions_test -v

The actions module resolves its paths at CALL time (server/actions.py:_paths), so every test just
rewrites the OFFICE_* env in setUp. No network, no live /opt/data/kanban.db.
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import actions, state  # noqa: E402

SCHEMA = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema_live.sql")).read()
# ^ the REAL kanban.db DDL (scripts/office_test_schema.py copies it), so these tests fail on the
#   same column/type mismatches the container would hit — a toy schema hides them.


class ActionsTest(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.state = tempfile.mktemp(suffix=".json")
        self.log = tempfile.mktemp(suffix=".log")
        os.environ["OFFICE_DB_RW"] = self.db
        os.environ["OFFICE_ALLOW_WRITES"] = "1"
        os.environ["OFFICE_STATE"] = self.state
        os.environ["OFFICE_ACTIONS_LOG"] = self.log
        self.now = 1_800_000_000
        self.con = sqlite3.connect(self.db)
        self.con.executescript(SCHEMA)
        self.con.commit()

    def tearDown(self):
        self.con.close()
        for p in (self.db, self.state, self.log):
            try:
                os.unlink(p)
            except OSError:
                pass
        for k in ("OFFICE_DB_RW", "OFFICE_ALLOW_WRITES", "OFFICE_STATE", "OFFICE_ACTIONS_LOG"):
            os.environ.pop(k, None)

    # ---------- helpers ----------
    def add_card(self, tid="t_aaaa01", status="blocked", assignee="coder", block=None, fails=None,
                 ferr=None, started=None, expires=None, hb=None, current_run=None, completed=None,
                 recurrences=None):
        self.con.execute(
            "insert into tasks (id,title,assignee,status,created_at,block_kind,consecutive_failures,"
            " last_failure_error,started_at,claim_expires,last_heartbeat_at,current_run_id,completed_at,"
            " block_recurrences) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, f"work {tid}", assignee, status, self.now, block, fails or 0, ferr, started, expires,
             hb, current_run, completed, recurrences or 0))
        self.con.commit()

    def add_run(self, rid, tid, started=None, ended=None, status="running", outcome=None,
                summary=None, hb=None, expires=None, pid=1234):
        self.con.execute(
            "insert into task_runs (id,task_id,started_at,ended_at,status,outcome,summary,"
            " last_heartbeat_at,claim_expires,worker_pid) values (?,?,?,?,?,?,?,?,?,?)",
            (rid, tid, started, ended, status, outcome, summary, hb, expires, pid))
        self.con.commit()

    def add_event(self, tid, kind, created_at, payload=None, run_id=None):
        self.con.execute(
            "insert into task_events (task_id,run_id,kind,payload,created_at) values (?,?,?,?,?)",
            (tid, run_id, kind, payload, created_at))
        self.con.commit()

    def add_parent(self, tid, parent_id):
        # task_links is (parent_id, child_id) in the live schema — the card under test is the child.
        self.con.execute("insert into task_links (parent_id, child_id) values (?,?)", (parent_id, tid))
        self.con.commit()

    def set_parent_status(self, pid, status):
        self.con.execute("insert into tasks (id,title,assignee,status,created_at) values (?,?,?,?,?)",
                         (pid, f"parent {pid}", "coder", status, self.now))
        self.con.commit()

    def task_row(self, tid):
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        try:
            return con.execute("select * from tasks where id=?", (tid,)).fetchone()
        finally:
            con.close()

    def run_row(self, rid):
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        try:
            return con.execute("select * from task_runs where id=?", (rid,)).fetchone()
        finally:
            con.close()

    def events(self, tid):
        con = sqlite3.connect(self.db)
        try:
            return [r[0] for r in con.execute(
                "select kind from task_events where task_id=? order by created_at", (tid,))]
        finally:
            con.close()

    def token(self, action, task, now=None):
        return actions.issue_token(action, task, now or self.now)

    def attempt(self, action, task, tok, note=None):
        """Mirror the route's guard order: rate (read-only) -> token -> record -> perform."""
        allowed, retry = actions.check_rate(action, task, self.now)
        if not allowed:
            return False, actions.ActionError("rate_limited", 429, f"retry in {retry}s")
        ok, reason = actions.consume_token(action, task, tok, self.now)
        if not ok:
            return False, actions.ActionError(reason, 409, reason)
        actions.record_hit(action, task, self.now)
        return True, actions.perform(action, task, note=note)

    # ---------- restart ----------
    def test_restart_landed_ready_when_parents_done(self):
        self.set_parent_status("t_parent1", "done")
        self.add_card(tid="t_aaaa01", block="needs_input", fails=2, ferr="pid 999 boom", started=self.now - 500,
                      current_run=9)
        self.add_parent("t_aaaa01", "t_parent1")
        self.add_run(9, "t_aaaa01", started=self.now - 400, ended=None, hb=self.now - 100)
        ok, res = self.attempt("restart", "t_aaaa01", self.token("restart", "t_aaaa01"))
        self.assertTrue(ok, res)
        self.assertEqual(res["before"], "blocked")
        self.assertEqual(res["after"], "ready")
        row = self.task_row("t_aaaa01")
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["consecutive_failures"], 0)
        self.assertIsNone(row["last_failure_error"])
        self.assertIsNone(row["current_run_id"])
        self.assertEqual(row["block_kind"], "needs_input")          # kept, deliberately (upstream)
        run = self.run_row(9)
        self.assertEqual(run["status"], "reclaimed")
        self.assertEqual(run["outcome"], "reclaimed")
        self.assertEqual(run["summary"], "invariant recovery on unblock")
        self.assertIsNone(run["claim_lock"])
        self.assertEqual(self.events("t_aaaa01"), ["unblocked"])

    def test_restart_landed_todo_when_a_parent_is_open(self):
        self.set_parent_status("t_parent2", "running")
        self.add_card(tid="t_bbbb02", block="needs_input", started=self.now - 500)
        self.add_parent("t_bbbb02", "t_parent2")
        ok, res = self.attempt("restart", "t_bbbb02", self.token("restart", "t_bbbb02"))
        self.assertTrue(ok, res)
        self.assertEqual(res["after"], "todo")
        self.assertEqual(self.task_row("t_bbbb02")["status"], "todo")

    def test_restart_review_when_the_newest_lifecycle_event_says_review(self):
        self.add_card(tid="t_cccc03", block=None, fails=1, ferr="crashed", started=self.now - 500)
        # upstream keys (`_resume_status_from_events`): resume_status/retry_status/source_status
        self.add_event("t_cccc03", "blocked", self.now - 60, payload='{"resume_status": "review"}')
        ok, res = self.attempt("restart", "t_cccc03", self.token("restart", "t_cccc03"))
        self.assertTrue(ok, res)
        self.assertEqual(res["after"], "review")

    def test_restart_ignores_an_unrelated_payload_saying_review(self):
        self.add_card(tid="t_cccc04", block=None, fails=1, ferr="crashed", started=self.now - 500)
        self.add_event("t_cccc04", "commented", self.now - 60, payload='{"review": true}')
        ok, res = self.attempt("restart", "t_cccc04", self.token("restart", "t_cccc04"))
        self.assertTrue(ok, res)
        self.assertEqual(res["after"], "ready")          # 'commented' is not a lifecycle event

    def test_second_restart_on_the_same_card_is_a_conflict(self):
        self.add_card(tid="t_dddd04", block="needs_input", started=self.now - 500)
        ok, res = self.attempt("restart", "t_dddd04", self.token("restart", "t_dddd04"))
        self.assertTrue(ok, res)
        before = len(self.events("t_dddd04"))
        with self.assertRaises(actions.ActionError) as ctx:
            actions.perform("restart", "t_dddd04", note=None)       # bypass token/rate: test the guard
        self.assertEqual(ctx.exception.code, "not_in_expected_state")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(len(self.events("t_dddd04")), before)      # no new event

    # ---------- approve ----------
    def test_approve_adds_a_note_and_unblocks(self):
        self.add_card(tid="t_eeee05", block="needs_input", started=self.now - 500)
        ok, res = self.attempt("approve", "t_eeee05", self.token("approve", "t_eeee05"),
                               note="ship it")
        self.assertTrue(ok, res)
        con = sqlite3.connect(self.db)
        try:
            rows = con.execute("select author, body from task_comments where task_id='t_eeee05'").fetchall()
        finally:
            con.close()
        self.assertEqual(rows, [("bart (office dashboard)", "ship it")])
        self.assertIn("commented", self.events("t_eeee05"))
        self.assertIn("unblocked", self.events("t_eeee05"))
        self.assertEqual(self.task_row("t_eeee05")["status"], "ready")

    def test_approve_default_note(self):
        self.add_card(tid="t_ffff06", block="needs_input", started=self.now - 500)
        ok, _ = self.attempt("approve", "t_ffff06", self.token("approve", "t_ffff06"))
        self.assertTrue(ok)
        con = sqlite3.connect(self.db)
        try:
            body = con.execute("select body from task_comments where task_id='t_ffff06'").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(body, "Approved from the BUSY BEE office dashboard.")

    # ---------- dismiss ----------
    def test_dismiss_closes_the_run_and_keeps_the_card(self):
        self.add_card(tid="t_0aaa07", block=None, fails=1, ferr="worker died", started=self.now - 100,
                      current_run=5)
        self.add_run(5, "t_0aaa07", started=self.now - 90, ended=None, hb=self.now - 40)
        ok, res = self.attempt("dismiss", "t_0aaa07", self.token("dismiss", "t_0aaa07"))
        self.assertTrue(ok, res)
        run = self.run_row(5)
        self.assertEqual(run["status"], "reclaimed")
        self.assertEqual(run["outcome"], "reclaimed")
        self.assertEqual(run["summary"], "dismissed from the office dashboard")
        self.assertEqual(run["claim_lock"], None)
        self.assertIn("office_dismissed", self.events("t_0aaa07"))
        self.assertEqual(self.task_row("t_0aaa07")["status"], "blocked")    # card stays blocked
        self.assertIsNone(self.task_row("t_0aaa07")["current_run_id"])
        with open(self.state) as f:
            data = json.load(f)
        self.assertEqual(data["dismissed"]["t_0aaa07"]["block_ts"], self.now - 100)
        self.assertEqual(data["dismissed"]["t_0aaa07"]["run_id"], 5)

    def test_dismiss_requires_a_blocked_card(self):
        self.add_card(tid="t_1bbb08", status="ready", started=self.now - 100)
        with self.assertRaises(actions.ActionError) as ctx:
            actions.perform("dismiss", "t_1bbb08")
        self.assertEqual(ctx.exception.code, "not_in_expected_state")

    # ---------- tokens ----------
    def test_missing_token_makes_no_change(self):
        self.add_card(tid="t_2ccc09", block="needs_input", started=self.now - 100)
        ok, reason = actions.consume_token("restart", "t_2ccc09", None, self.now)
        self.assertFalse(ok)
        self.assertEqual(reason, "no_token")
        self.assertEqual(self.task_row("t_2ccc09")["status"], "blocked")
        self.assertEqual(self.events("t_2ccc09"), [])

    def test_wrong_token_makes_no_change(self):
        self.add_card(tid="t_3ddd10", block="needs_input", started=self.now - 100)
        real = self.token("restart", "t_3ddd10")
        ok, reason = actions.consume_token("restart", "t_3ddd10", real[:-1] + "0", self.now)
        self.assertFalse(ok)
        self.assertEqual(reason, "bad_token")
        self.assertEqual(self.task_row("t_3ddd10")["status"], "blocked")

    def test_same_token_twice_is_refused(self):
        self.add_card(tid="t_4eee11", block="needs_input", started=self.now - 100)
        tok = self.token("restart", "t_4eee11")
        self.assertTrue(actions.consume_token("restart", "t_4eee11", tok, self.now)[0])
        ok, reason = actions.consume_token("restart", "t_4eee11", tok, self.now)
        self.assertFalse(ok)
        self.assertEqual(reason, "bad_token")

    def test_expired_token_is_refused(self):
        self.add_card(tid="t_5fff12", block="needs_input", started=self.now - 100)
        tok = self.token("restart", "t_5fff12")
        ok, reason = actions.consume_token("restart", "t_5fff12", tok, self.now + actions.TOKEN_TTL_S + 1)
        self.assertFalse(ok)
        self.assertEqual(reason, "expired_token")

    # ---------- rate limit + log ----------
    def test_rate_limit_per_task(self):
        self.add_card(tid="t_6aaa13", block="needs_input", started=self.now - 100)
        ok, _ = self.attempt("restart", "t_6aaa13", self.token("restart", "t_6aaa13"))
        self.assertTrue(ok)
        allowed, retry = actions.check_rate("restart", "t_6aaa13", self.now + 2)
        self.assertFalse(allowed)
        self.assertGreater(retry, 0)

    def test_rate_limit_is_per_task_not_global(self):
        self.add_card(tid="t_7bbb14", block="needs_input", started=self.now - 100)
        self.add_card(tid="t_7bbb15", block="needs_input", started=self.now - 100)
        ok, _ = self.attempt("restart", "t_7bbb14", self.token("restart", "t_7bbb14"))
        self.assertTrue(ok)
        ok, _ = self.attempt("restart", "t_7bbb15", self.token("restart", "t_7bbb15"))
        self.assertTrue(ok)

    def test_log_action_writes_one_json_line_per_attempt(self):
        self.add_card(tid="t_8ccc16", block="needs_input", started=self.now - 100)
        actions.log_action({"ok": True, "action": "restart", "task": "t_8ccc16"})
        actions.log_action({"ok": False, "action": "restart", "task": "t_8ccc16", "error": "rate_limited"})
        with open(self.log) as f:
            lines = [l for l in f.read().splitlines() if l.strip()]
        self.assertEqual(len(lines), 2)
        for line in lines:
            rec = json.loads(line)
            self.assertEqual(rec["task"], "t_8ccc16")

    # ---------- capability / validation ----------
    def test_not_writable_when_disabled(self):
        os.environ.pop("OFFICE_ALLOW_WRITES")
        cap = actions.capability()
        self.assertFalse(cap["writable"])
        self.add_card(tid="t_9ddd17", block="needs_input", started=self.now - 100)
        with self.assertRaises(actions.ActionError) as ctx:
            actions.perform("restart", "t_9ddd17")
        self.assertEqual(ctx.exception.code, "not_writable")
        self.assertEqual(ctx.exception.status, 503)

    def test_unknown_action_and_bad_id(self):
        with self.assertRaises(actions.ActionError) as ctx:
            actions.perform("nope", "t_aaaa01")
        self.assertEqual(ctx.exception.code, "unknown_action")
        with self.assertRaises(actions.ActionError) as ctx:
            actions.perform("restart", "not-a-task")
        self.assertEqual(ctx.exception.code, "bad_task_id")

    def test_task_missing(self):
        with self.assertRaises(actions.ActionError) as ctx:
            actions.perform("restart", "t_deadbeef")
        self.assertEqual(ctx.exception.code, "task_missing")
        self.assertEqual(ctx.exception.status, 404)

    def test_capability_shape(self):
        cap = actions.capability()
        self.assertTrue(cap["writable"])
        self.assertIsNone(cap["reason"])
        self.assertEqual(set(cap["actions"]), {"restart", "approve", "dismiss"})

    def test_token_ttl_is_120(self):
        self.assertEqual(actions.TOKEN_TTL_S, 120)

    def test_performed_count_counts_writes(self):
        self.add_card(tid="t_abcd18", block="needs_input", started=self.now - 100)
        self.assertEqual(actions.performed_count(), 0)
        ok, _ = self.attempt("restart", "t_abcd18", self.token("restart", "t_abcd18"))
        self.assertTrue(ok)
        self.assertEqual(actions.performed_count(), 1)


if __name__ == "__main__":
    unittest.main()
