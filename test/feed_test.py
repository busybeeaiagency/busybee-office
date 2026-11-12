"""Feed tests: build a throwaway kanban.db, assert the cubicle status mapping and room routing.

Run:  python3 -m unittest test.feed_test -v
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import feed, roster  # noqa: E402

# The REAL kanban.db DDL, copied by scripts/office_test_schema.py: the tests must meet
# the same columns/NOT NULLs as the container, not a friendlier toy schema.
SCHEMA = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema_live.sql")).read()


class FeedTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.con = sqlite3.connect(self.path)
        self.con.executescript(SCHEMA)
        self.now = 1_800_000_000
        self.con.execute("insert into tasks (id,title,assignee,status,created_at,started_at,completed_at) values ('t1','a','coder','done',?,?,?)",
                         (self.now - 7200, self.now - 7200, self.now - 7000))
        self.con.commit()

    def tearDown(self):
        self.con.close()
        os.unlink(self.path)

    def add(self, tid, assignee, status, title="work", started=None, completed=None,
            expires=None, hb=None, block=None):
        self.con.execute(
            "insert into tasks (id,title,assignee,status,created_at,started_at,completed_at,claim_expires,last_heartbeat_at,block_kind)"
            " values (?,?,?,?,?,?,?,?,?,?)",
            (tid, title, assignee, status, self.now - 86400, started, completed, expires, hb, block))
        self.con.commit()

    def fail(self, tid, fails=1, ferr="boom"):
        self.con.execute("update tasks set consecutive_failures=?, last_failure_error=? where id=?",
                         (fails, ferr, tid))
        self.con.commit()

    def add_run(self, rid, tid, started=None, ended=None, status="running", outcome=None,
            hb=None, expires=None):
        self.con.execute(
            "insert into task_runs (id,task_id,started_at,ended_at,status,outcome,last_heartbeat_at,claim_expires)"
            " values (?,?,?,?,?,?,?,?)", (rid, tid, started, ended, status, outcome, hb, expires))
        self.con.commit()

    def ev(self, tid, kind, ts, run_id=None, payload=None):
        self.con.execute("insert into task_events (task_id,run_id,kind,payload,created_at) values (?,?,?,?,?)",
                         (tid, run_id, kind, payload, ts))
        self.con.commit()

    def say(self, tid, body, ts):
        # task_comments.id is INTEGER PRIMARY KEY in the real schema: let SQLite assign it.
        self.con.execute("insert into task_comments (task_id,author,body,created_at) values (?,?,?,?)",
                         (tid, "w", body, ts))
        self.con.commit()

    def by_id(self, data, aid):
        return next(a for a in data["agents"] if a["id"] == aid)

    def test_idle_with_completed_at(self):
        data = feed.build_feed(feed.read_tasks(self.path), now=self.now)
        coder = self.by_id(data, "coder")
        self.assertEqual(coder["status"], "idle")
        self.assertIsNotNone(coder["completedAt"])
        self.assertEqual(coder["room"], "coding")

    def test_running_task_goes_to_running_and_room_by_title(self):
        self.add("t2", "coder", "running", title="Build the Godot level editor", started=self.now - 60,
                 expires=self.now + 600)
        data = feed.build_feed(feed.read_tasks(self.path), now=self.now)
        coder = self.by_id(data, "coder")
        self.assertEqual(coder["status"], "running")
        self.assertEqual(coder["room"], "gamedev")
        self.assertEqual(coder["task"]["title"], "Build the Godot level editor")

    def test_stale_claim_is_not_running(self):
        self.add("t3", "coder", "running", started=self.now - 900, expires=self.now - 10, hb=None)
        data = feed.build_feed(feed.read_tasks(self.path), now=self.now)
        self.assertNotEqual(self.by_id(data, "coder")["status"], "running")

    def test_blocked_is_red(self):
        # A needs_input block is now AMBER (waiting), not red — rule 2 splits by *why*.
        self.add("t4", "maya", "blocked", title="Waiting on Bart", started=self.now - 120, block="needs_input")
        data = feed.build_feed(feed.read_tasks(self.path), now=self.now)
        maya = self.by_id(data, "maya")
        self.assertEqual(maya["status"], "waiting")
        self.assertIn("BLOCKED", maya["error"])
        self.assertEqual(maya["attention"]["task"]["kind"], "needs_input")
        self.assertNotIn("task", maya)                     # a blocked card is never the "working on" line

    def test_queued_is_paused(self):
        self.add("t5", "researcher", "ready", title="Survey the literature", started=self.now - 30)
        data = feed.build_feed(feed.read_tasks(self.path), now=self.now)
        self.assertEqual(self.by_id(data, "researcher")["status"], "paused")

    def test_assignee_aliases_collapse_to_orchestrator(self):
        self.add("t6", "default", "running", title="Coordinate the fleet", started=self.now - 60,
                 expires=self.now + 600)
        self.add("t7", "hermes", "todo", title="Delegate", started=self.now - 10)
        self.add("t8", "orchestrator", "todo", title="Plan", started=self.now - 10)
        data = feed.build_feed(feed.read_tasks(self.path), now=self.now)
        self.assertEqual(len([a for a in data["agents"] if a["id"] == "orchestrator"]), 1)
        self.assertEqual(self.by_id(data, "orchestrator")["status"], "running")
        # "Coordinate the fleet" matches no room keyword -> the agent's home room (the studio).
        self.assertEqual(self.by_id(data, "orchestrator")["room"], "art")
        self.add("t9", "orchestrator", "running", title="Fix the deployment pipeline", started=self.now - 30,
                 expires=self.now + 600)
        data = feed.build_feed(feed.read_tasks(self.path), now=self.now)
        self.assertEqual(self.by_id(data, "orchestrator")["room"], "coding")

    def test_every_roster_agent_is_present(self):
        data = feed.build_feed(feed.read_tasks(self.path), now=self.now)
        self.assertEqual([a["id"] for a in data["agents"]], [a["id"] for a in roster.AGENTS])

    def test_chat_is_attached_per_mm_user(self):
        data = feed.build_feed(feed.read_tasks(self.path), now=self.now, chat_by_agent={"coder": "shipping it"})
        self.assertEqual(self.by_id(data, "coder")["chat"], "shipping it")

    # ---------- rule 2: blocked split by *why* ----------
    def test_crash_blocked_is_error_and_names_the_crash(self):
        self.add("t_c1", "auditor", "blocked", title="Audit the deploy", started=self.now - 300, block=None)
        self.fail("t_c1", fails=2, ferr="pid 4242 worker exited nonzero\ncrash detail")
        data = feed.build_feed(feed.read_tasks(self.path), now=self.now)
        aud = self.by_id(data, "auditor")
        self.assertEqual(aud["status"], "error")
        self.assertTrue(aud["error"].startswith("CRASHED:"))
        self.assertNotIn("needs input", aud["error"])          # a crash must never say needs input
        self.assertNotIn("t_c1", aud["error"])                 # error text never names a foreign id
        self.assertEqual(aud["attention"]["task"]["kind"], "crashed")

    def test_crash_blocked_with_a_later_completed_task_goes_stale(self):
        self.add("t_c2", "auditor", "blocked", title="Old audit", started=self.now - 500, block=None)
        self.fail("t_c2", fails=1, ferr="died")
        self.add("t_c3", "auditor", "done", title="Newer audit", started=self.now - 200, completed=self.now - 100)
        data = feed.build_feed(feed.read_tasks(self.path), now=self.now)
        aud = self.by_id(data, "auditor")
        self.assertEqual(aud["status"], "idle")                 # the newer finish wins the present
        self.assertNotEqual((aud.get("error") or "").split(":")[0], "CRASHED")
        self.assertIsNone(aud["attention"])
        self.assertIn("t_c2", [s["id"] for s in aud["stale"]])  # still reported, not hidden
        self.assertEqual(aud["stale"][0]["kind"], "crashed")

    def test_blocked_with_a_live_running_claim_still_runs(self):
        self.add("t_c4", "auditor", "blocked", title="Waiting", started=self.now - 60, block="needs_input")
        self.add("t_c5", "auditor", "running", title="Shipping now", started=self.now - 30, expires=self.now + 600)
        data = feed.build_feed(feed.read_tasks(self.path), now=self.now)
        self.assertEqual(self.by_id(data, "auditor")["status"], "running")

    def test_waiting_beats_crashed_when_both_are_current(self):
        self.add("t_c6", "maya", "blocked", title="Crashed one", started=self.now - 60, block=None)
        self.fail("t_c6", fails=1, ferr="oops")
        self.add("t_c7", "maya", "blocked", title="Approve please", started=self.now - 40, block="needs_input")
        data = feed.build_feed(feed.read_tasks(self.path), now=self.now)
        maya = self.by_id(data, "maya")
        self.assertEqual(maya["status"], "waiting")             # needs_input outranks a crash
        self.assertEqual(maya["attention"]["task"]["kind"], "needs_input")
        kinds = {a["kind"] for a in maya["attention"]["actions"]}
        self.assertEqual(kinds, {"restart", "approve", "dismiss"})
        # a crashed card's actions never include approve
        self.add("t_c8", "researcher", "blocked", title="Only crashed", started=self.now - 40, block=None)
        self.fail("t_c8", fails=1, ferr="oops")
        r = self.by_id(feed.build_feed(feed.read_tasks(self.path), now=self.now), "researcher")
        self.assertEqual({a["kind"] for a in r["attention"]["actions"]}, {"restart", "dismiss"})

    # ---------- spec 3: run-row truth ----------
    def test_needs_input_stays_attention_while_the_agent_is_live(self):
        # The badge follows liveness (a claimed, heartbeating run = working); the blocked card keeps
        # its banner and buttons, because an unanswered question is still waiting on the operator.
        self.add("t_live", "auditor", "running", title="Live audit", started=self.now - 300,
                 expires=self.now + 600, hb=self.now - 20)
        self.add_run(300, "t_live", started=self.now - 300, ended=None, hb=self.now - 20,
                     expires=self.now + 600)
        self.add("t_q1", "auditor", "blocked", title="Audit needs a call", block="needs_input",
                 started=self.now - 900)
        self.ev("t_q1", "blocked", self.now - 600)
        aud = self.by_id(feed.build_feed(feed.read_tasks(self.path), now=self.now), "auditor")
        self.assertEqual(aud["status"], "running")                 # truth: it IS working
        self.assertEqual(aud["attention"]["task"]["id"], "t_q1")   # and the card is still surfaced
        self.assertIn("approve", [a["kind"] for a in aud["attention"]["actions"]])

    def test_badge_clears_within_one_build_once_the_card_is_unblocked(self):
        # The acceptance rule in test form: blocked -> waiting; unblocked -> working/idle next poll.
        self.add("t_q2", "maya", "blocked", title="Ship it?", block="needs_input", started=self.now - 600)
        self.ev("t_q2", "blocked", self.now - 500)
        f1 = feed.build_feed(feed.read_tasks(self.path), now=self.now)
        maya = self.by_id(f1, "maya")
        self.assertEqual(maya["status"], "waiting")
        self.assertEqual(maya["attention"]["task"]["id"], "t_q2")
        self.con.execute("update tasks set status='ready', block_kind=NULL where id='t_q2'")
        self.con.execute("insert into task_events (task_id, kind, created_at) values ('t_q2','unblocked',?)",
                         (self.now - 60,))
        self.con.commit()
        maya = self.by_id(feed.build_feed(feed.read_tasks(self.path), now=self.now + 6), "maya")
        self.assertNotIn(maya["status"], ("waiting", "error"))
        self.assertIsNone(maya.get("attention"))
        self.assertNotIn("t_q2", [s["id"] for s in maya.get("stale", [])])

    def test_run_row_is_the_liveness_truth(self):
        # task columns say the claim expired; the OPEN run row's recent heartbeat says alive.
        self.add("t_r1", "auditor", "running", title="Live audit", started=self.now - 120,
                 expires=self.now - 100, hb=self.now - 10000)
        self.add_run(279, "t_r1", started=self.now - 3000, ended=self.now - 2900, status="failed", outcome="crashed")
        self.add_run(280, "t_r1", started=self.now - 2000, ended=self.now - 1900, status="failed", outcome="crashed")
        self.add_run(281, "t_r1", started=self.now - 120, ended=None, hb=self.now - 45, expires=self.now + 600)
        self.ev("t_r1", "crashed", self.now - 2900, run_id=279)
        self.ev("t_r1", "crashed", self.now - 1900, run_id=280)
        self.ev("t_r1", "claimed", self.now - 120, run_id=281)
        self.ev("t_r1", "heartbeat", self.now - 45, run_id=281)
        aud = self.by_id(feed.build_feed(feed.read_tasks(self.path), now=self.now), "auditor")
        self.assertEqual(aud["status"], "running")
        self.assertEqual(aud["run"]["id"], 281)
        self.assertTrue(aud["run"]["alive"])
        self.assertEqual(aud["run"]["heartbeatAgeS"], 45)
        self.assertEqual((aud["run"]["attempt"], aud["run"]["attempts"]), (3, 3))
        # recent == current attempt ONLY (run 281 events); previous holds the crashed attempt
        self.assertEqual([s[1].split()[0] for s in aud["recent"]], ["claimed", "heartbeat"])
        self.assertEqual(aud["previous"][0]["id"], 280)
        self.assertEqual(aud["previous"][0]["outcome"], "crashed")
        self.assertTrue(aud["previous"][0]["steps"][0][1].startswith("crashed"))

    def test_dead_run_is_not_running(self):
        self.add("t_r2", "auditor", "running", title="Stalled audit", started=self.now - 3600,
                 expires=self.now - 100, hb=self.now - 3600)
        self.add_run(282, "t_r2", started=self.now - 3600, ended=None, hb=self.now - 10800, expires=self.now - 100)
        aud = self.by_id(feed.build_feed(feed.read_tasks(self.path), now=self.now), "auditor")
        self.assertNotEqual(aud["status"], "running")
        self.assertNotIn("run", aud)                            # no live run -> no run field

    def test_steps_fall_back_to_comments_when_events_missing(self):
        self.add("t_r3", "coder", "running", title="Build it", started=self.now - 200,
                 expires=self.now + 600, hb=self.now - 10)
        self.add_run(290, "t_r3", started=self.now - 200, ended=None, hb=self.now - 10, expires=self.now + 600)
        self.say("t_r3", "installing deps\nmore", self.now - 100)
        self.say("t_r3", "compiling", self.now - 50)
        coder = self.by_id(feed.build_feed(feed.read_tasks(self.path), now=self.now), "coder")
        self.assertEqual([s[1] for s in coder["recent"]], ["installing deps", "compiling"])

    # ---------- spec 3: archived / dismissedTasks ----------
    def test_archived_card_is_never_work_and_shows_as_dismissed(self):
        self.con.execute("insert into tasks (id,title,assignee,status,created_at) values ('t_arch','gone','auditor','archived',1799999000)")
        self.con.commit()
        prev = [{"id": "auditor", "task": {"id": "t_arch"}}]
        data = feed.build_feed(feed.read_tasks(self.path), now=self.now, previous_agents=prev)
        aud = self.by_id(data, "auditor")
        self.assertNotIn("task", aud)
        self.assertIn("t_arch", data["dismissedTasks"])

    # ---------- spec 3: orchestrator is chat-driven ----------
    def test_orchestrator_orchestrates_when_gateway_is_alive(self):
        self.add("t_ceo", "default", "blocked", title="Approve the deploy", started=self.now - 100, block="needs_input")
        aud = self.by_id(feed.build_feed(feed.read_tasks(self.path), now=self.now,
                                         gateway=(True, 12, "log")), "orchestrator")
        self.assertEqual(aud["status"], "running")
        self.assertTrue(aud["task"]["title"].startswith("orchestrating"))
        self.assertNotIn("id", aud["task"])                     # not a claimed card
        self.assertEqual(aud["attention"]["task"]["id"], "t_ceo")  # banner stays, just not the line

    def test_orchestrator_without_gateway_falls_back(self):
        self.add("t_ceo2", "default", "blocked", title="Approve the deploy", started=self.now - 100, block="needs_input")
        aud = self.by_id(feed.build_feed(feed.read_tasks(self.path), now=self.now,
                                         gateway=(False, 0, "no signal")), "orchestrator")
        self.assertNotEqual(aud["status"], "running")
        self.assertEqual(aud["status"], "waiting")
        self.assertNotIn("task", aud)                           # no "orchestrating" line

    # ---------- dismiss action suppresses the badge (spec 1) ----------
    def test_dismissed_card_badge_gone_then_returns(self):
        state_file = tempfile.mktemp(suffix=".json")
        os.environ["OFFICE_STATE"] = state_file
        try:
            self.add("t_d1", "maya", "blocked", title="Needs", started=self.now - 120, block="needs_input")
            with open(state_file, "w") as f:
                json.dump({"dismissed": {"t_d1": {"at": self.now - 1, "block_ts": self.now - 120, "run_id": None}},
                           "tokens": {}, "hits": []}, f)
            maya = self.by_id(feed.build_feed(feed.read_tasks(self.path), now=self.now), "maya")
            self.assertIsNone(maya["attention"])                # dismissed: badge suppressed
            self.assertEqual(maya["status"], "idle")
            self.ev("t_d1", "blocked", self.now - 10)           # a NEWER block lifts block_ts
            maya = self.by_id(feed.build_feed(feed.read_tasks(self.path), now=self.now), "maya")
            self.assertIsNotNone(maya["attention"])             # ... so the badge comes back
            self.assertEqual(maya["attention"]["task"]["id"], "t_d1")
            self.assertEqual(maya["status"], "waiting")
        finally:
            os.environ.pop("OFFICE_STATE", None)
            try:
                os.unlink(state_file)
            except OSError:
                pass


class RosterTest(unittest.TestCase):
    def test_route_room(self):
        self.assertEqual(roster.route_room("Fix the Godot shader pipeline", "coding"), "gamedev")
        self.assertEqual(roster.route_room("Send the newsletter", "comms"), "comms")
        self.assertEqual(roster.route_room("Design the logo", "coding"), "art")
        self.assertEqual(roster.route_room("Whatever", "research"), "research")

    def test_weak_words_do_not_override_home_room(self):
        # "by design" must not drag the coder to the art room; "DB" is not a keyword either.
        self.assertEqual(
            roster.route_room("Decide LiteLLM DB mode: virtual keys + spend tracking, or confirm DB-less by design", "coding"),
            "coding")
        self.assertEqual(roster.route_room("Phase 5: UI, dialogue & polish (HUD, menus, inventory)", "gamedev"), "gamedev")
        self.assertEqual(roster.route_room("Rotate/scrub postgres password in git history", "coding"), "coding")

    def test_canonical(self):
        self.assertEqual(roster.canonical_assignee("default"), "orchestrator")
        self.assertEqual(roster.canonical_assignee("Hermes"), "orchestrator")
        self.assertEqual(roster.canonical_assignee("auditor"), "auditor")


if __name__ == "__main__":
    unittest.main()
