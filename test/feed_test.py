"""Feed tests: build a throwaway kanban.db, assert the cubicle status mapping and room routing.

Run:  python3 -m unittest test.feed_test -v
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import feed, roster  # noqa: E402

SCHEMA = """
create table tasks (id text primary key, title text, body text, assignee text, status text,
  priority integer, created_by text, created_at integer, started_at integer, completed_at integer,
  workspace_kind text, workspace_path text, branch_name text, project_id text, claim_lock text,
  claim_expires integer, tenant text, result text, idempotency_key text, consecutive_failures integer,
  worker_pid integer, worker_started_at integer, last_failure_error text,
  max_runtime_seconds integer, last_heartbeat_at integer, current_run_id integer,
  workflow_template_id text, current_step_key text, skills text, model_override text,
  provider_override text, reasoning_effort text, max_retries integer, goal_mode integer,
  goal_max_turns integer, session_id text, block_kind text, block_recurrences integer,
  completion_contract text)
"""


class FeedTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.con = sqlite3.connect(self.path)
        self.con.executescript(SCHEMA)
        self.now = 1_800_000_000
        self.con.execute("insert into tasks (id,title,assignee,status,started_at,completed_at) values ('t1','a','coder','done',?,?)",
                         (self.now - 7200, self.now - 7000))
        self.con.commit()

    def tearDown(self):
        self.con.close()
        os.unlink(self.path)

    def add(self, tid, assignee, status, title="work", started=None, completed=None,
            expires=None, hb=None, block=None):
        self.con.execute(
            "insert into tasks (id,title,assignee,status,started_at,completed_at,claim_expires,last_heartbeat_at,block_kind)"
            " values (?,?,?,?,?,?,?,?,?)",
            (tid, title, assignee, status, started, completed, expires, hb, block))
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
        self.add("t4", "maya", "blocked", title="Waiting on Bart", started=self.now - 120, block="needs_input")
        data = feed.build_feed(feed.read_tasks(self.path), now=self.now)
        maya = self.by_id(data, "maya")
        self.assertEqual(maya["status"], "error")
        self.assertIn("BLOCKED", maya["error"])

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
