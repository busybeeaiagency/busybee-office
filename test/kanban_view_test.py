"""Board-view tests: a throwaway kanban.db, then the per-agent card lists it should produce.

Run:  python3 -m unittest test.kanban_view_test -v

Same discipline as test/feed_test.py: the REAL kanban.db DDL (test/schema_live.sql) and a temp db,
pointed at through the env the module already reads (OFFICE_DB / OFFICE_HERMES_DIR) — the module
resolves the path at CALL time, so no module constant has to be monkeypatched. The one patch in
this file is the read-only proof (server/kanban_view.py:read_board), and the board's process-wide
cache is reset in setUp so a fixture db never leaks into the next test.
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import feed, kanban_view, roster  # noqa: E402

SCHEMA = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema_live.sql")).read()

NOW = 1_800_000_000
BASE = {s: 0 for s in kanban_view.STATUSES}
LONG = "z" * (kanban_view.TITLE_MAX + 12)       # 132 characters: 12 over the cap

# id, assignee, status, title, created_at, started_at, completed_at — one fixture, and every
# grouping/counting assertion below is read off it. The 31 `c_t*` todo rows and the 12 `c_d*`
# finished rows exist to prove the caps; counts stay truthful next to them.
FIXTURE = [
    ("b_todo", "coder", "todo", "Write the migration", NOW - 9000, None, None),
    ("b_ready", "coder", "ready", "  Queue the follow-up \n", NOW - 8900, None, None),
    ("b_running", "coder", "running", "Rebuild the container", NOW - 8800, NOW - 300, None),
    ("b_blocked", "coder", "blocked", "Which port?", NOW - 8700, NOW - 400, None),
    ("b_review", "coder", "review", "Approve the patch?", NOW - 8600, NOW - 500, None),
    ("b_done", "coder", "done", "Ship the dashboard", NOW - 8500, NOW - 600, NOW - 550),
    ("b_sched", "coder", "scheduled", "Review next week", NOW - 8400, None, None),
    ("b_wonder", "coder", "wondering", "Tabbed\t newline\ned and bell\u0007 control marks",
     NOW - 8300, None, None),
    ("b_arch", "coder", "archived", "Already thrown away", NOW - 8200, None, None),
    ("c_t00", "coder", "todo", LONG, NOW - 7999, None, None),
    ("o_def", "default", "ready", "Coordinate the fleet", NOW - 7000, None, None),
    ("o_herm", "hermes", "todo", "Delegate the audit", NOW - 6900, None, None),
    ("o_orc", "orchestrator", "running", "Plan the sprint", NOW - 6800, NOW - 60, None),
    ("o_barth", "bart", "blocked", "Sign the invoice", NOW - 6700, NOW - 70, None),
    ("o_ghost", "nobody", "ready", "Stray card", NOW - 6600, None, None),
] + [
    # 30 more todo cards for coder, created_at one minute apart: 25 of them may be listed.
    (f"c_t{i:02d}", "coder", "todo", f"Todo number {i:02d}", NOW - 8000 + i * 60, None, None)
    for i in range(1, 31)
] + [
    # 12 finished cards: the history column shows 8, and counts.done still says 12 (+ b_done = 13).
    (f"c_d{i:02d}", "coder", "done", f"Finished number {i:02d}",
     NOW - 5000 - i * 60, NOW - 5100 - i * 60, NOW - 4000 - i * 60)
    for i in range(12)
]


class KanbanViewTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="kb-view-")
        self.path = os.path.join(self.dir, "kanban.db")
        self.con = sqlite3.connect(self.path)
        self.con.executescript(SCHEMA)
        for row in FIXTURE:
            # FIXTURE rows are (id, assignee, status, title, created_at, started_at, completed_at);
            # the column list below is in the table's order, so the tuple is bound out of order.
            self.con.execute(
                "insert into tasks (id,title,assignee,status,created_at,started_at,completed_at)"
                " values (?,?,?,?,?,?,?)",
                (row[0], row[3], row[1], row[2], row[4], row[5], row[6]))
        self.con.commit()
        # The module resolves the db at call time from this env, exactly like office.py does.
        self.env = {}
        for key, value in (("OFFICE_DB", self.path), ("OFFICE_HERMES_DIR", self.dir)):
            self.env[key] = os.environ.get(key)
            os.environ[key] = value
        kanban_view._cache.update({"at": 0.0, "owners": None, "error": ""})

    def tearDown(self):
        self.con.close()
        for key, before in self.env.items():
            if before is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = before
        for name in os.listdir(self.dir):                        # a 0o444 db must still be deletable
            os.chmod(os.path.join(self.dir, name), 0o644)
        for name in ("kanban.db", "kanban.db-wal", "kanban.db-shm"):
            try:
                os.unlink(os.path.join(self.dir, name))
            except OSError:
                pass
        os.rmdir(self.dir)

    # ---------- helpers ----------
    def ids(self, shape, status):
        return [c["id"] for c in shape["tasks"][status]]

    def listed(self, data):
        """Every card id the payload shows, across all agents and the unassigned column."""
        shapes = list(data["agents"].values()) + [data["unassigned"]]
        return {c["id"] for s in shapes for column in s["tasks"].values() for c in column}

    def test_grouping_and_counts_for_one_agent_with_a_row_in_every_status(self):
        data = kanban_view.board("coder")
        coder = data["agents"]["coder"]
        self.assertEqual(data["agent"], "coder")
        self.assertEqual(list(coder["tasks"]), kanban_view.STATUSES)
        self.assertEqual(set(self.ids(coder, "todo")), {f"c_t{i:02d}" for i in range(6, 31)})
        # ^ the cap keeps the 25 NEWEST: coder's 9 oldest todo cards (b_todo, b_sched, b_wonder,
        #   c_t00…c_t05) are counted below but not listed.
        self.assertEqual(self.ids(coder, "ready"), ["b_ready"])
        self.assertEqual(self.ids(coder, "running"), ["b_running"])
        self.assertEqual(self.ids(coder, "blocked"), ["b_blocked"])
        self.assertEqual(self.ids(coder, "review"), ["b_review"])
        self.assertEqual(set(self.ids(coder, "done")),
                         {"b_done"} | {f"c_d{i:02d}" for i in range(7)})   # DONE_CAP keeps 8
        counts = coder["counts"]
        self.assertEqual(counts["todo"], 34)                     # b_todo + scheduled + wondering + 31
        self.assertEqual([counts[s] for s in ("ready", "running", "blocked", "review")], [1, 1, 1, 1])
        self.assertEqual(counts["done"], 13)                     # b_done + the 12 that were capped
        self.assertEqual(counts["total"], 51)                    # 34+1+1+1+1+13
        self.assertEqual(counts["total"], sum(counts[s] for s in kanban_view.STATUSES))
        self.assertNotIn("b_arch", self.listed(data))

    def test_archived_rows_are_excluded_everywhere(self):
        data = kanban_view.board()
        self.assertTrue(data["ok"])
        self.assertNotIn("b_arch", self.listed(data))
        # the same exclusion rule as the cubicle feed (read_tasks answers a dict; tasks is the rows)
        self.assertNotIn("b_arch", [t["id"] for t in feed.read_tasks(self.path)["tasks"]])
        self.assertNotIn("archived", data["counts"])             # and never a column of its own
        self.assertEqual(data["counts"]["total"], len(FIXTURE) - 1)   # one archived row out of 57

    def test_scheduled_folds_into_todo_and_keeps_its_flag(self):
        # read_board is the UNCAPPED column set: the two cards under test are older than coder's
        # 25-card listing cap, so they are looked up where they actually live.
        todo = kanban_view.read_board(self.path)["coder"]["todo"]
        card = next(c for c in todo if c["id"] == "b_sched")
        self.assertEqual(card["status"], "todo")                 # the column it lands in
        self.assertEqual(card["scheduled"], NOW - 8400)          # when it wakes up (its own timeline)
        plain = next(c for c in todo if c["id"] == "b_todo")
        self.assertNotIn("scheduled", plain)                     # a real todo never claims a wake-up

    def test_unknown_status_folds_into_todo_rather_than_disappearing(self):
        coder = kanban_view.board("coder")["agents"]["coder"]
        card = next(c for c in kanban_view.read_board(self.path)["coder"]["todo"]
                    if c["id"] == "b_wonder")                    # older than the listing cap
        self.assertEqual(card["status"], "todo")
        self.assertNotIn("scheduled", card)                      # unknown is not "scheduled" either
        self.assertNotIn("wondering", coder["counts"])           # no phantom column...
        self.assertNotIn("wondering", kanban_view.board()["counts"])
        self.assertEqual(coder["counts"]["todo"], 34)            # ...and the card is still counted

    def test_assignees_are_canonicalised_to_the_roster_id_the_feed_uses(self):
        # default / hermes / orchestrator are one person — the same mapping server/feed.py applies,
        # so a card can never be the orchestrator's in the feed and someone else's on the board.
        for raw, want in (("default", "orchestrator"), ("Hermes", "orchestrator"),
                          ("orchestrator", "orchestrator"), ("coder", "coder")):
            self.assertEqual(roster.canonical_assignee(raw), want)
        data = kanban_view.board("orchestrator")
        self.assertEqual(data["agent"], "orchestrator")
        self.assertEqual(self.ids(data["agents"]["orchestrator"], "ready"), ["o_def"])
        self.assertEqual(self.ids(data["agents"]["orchestrator"], "todo"), ["o_herm"])
        self.assertEqual(self.ids(data["agents"]["orchestrator"], "running"), ["o_orc"])
        self.assertEqual(data["agents"]["orchestrator"]["counts"]["total"], 3)

    def test_an_assignee_outside_the_fleet_lands_in_unassigned(self):
        data = kanban_view.board()
        un = [c["id"] for column in data["unassigned"]["tasks"].values() for c in column]
        self.assertEqual(sorted(un), ["o_barth", "o_ghost"])     # bart and "nobody" have no desk
        self.assertEqual(data["unassigned"]["counts"],
                         {**BASE, "blocked": 1, "ready": 1, "total": 2})
        self.assertEqual(data["agents"]["orchestrator"]["counts"]["total"], 3)    # nothing leaked
        self.assertEqual(list(data["agents"]), [a["id"] for a in roster.AGENTS])
        self.assertEqual(data["agents"]["maya"]["counts"], {**BASE, "total": 0})  # empty plate, honestly
        self.assertEqual(data["agents"]["maya"]["tasks"], {s: [] for s in kanban_view.STATUSES})

    def test_board_counts_are_true_totals_not_capped_list_lengths(self):
        data = kanban_view.board()
        expected = dict(BASE)
        for _, _, status, _, _, _, _ in FIXTURE:
            if status == "archived":
                continue
            expected["todo" if status in ("scheduled", "wondering") else status] += 1
        expected["total"] = sum(expected[s] for s in kanban_view.STATUSES)
        self.assertEqual(data["counts"], expected)
        self.assertEqual(expected["todo"], 35)                   # coder's 34 + the hermes card
        self.assertEqual(expected["done"], 13)
        for status in kanban_view.STATUSES:
            listed = sum(len(a["tasks"][status]) for a in data["agents"].values())
            listed += len(data["unassigned"]["tasks"][status])
            self.assertLessEqual(listed, data["counts"][status])  # a list never exceeds its count
        self.assertEqual(len(data["agents"]["coder"]["tasks"]["todo"]), kanban_view.LIVE_CAP)
        self.assertEqual(len(data["agents"]["coder"]["tasks"]["done"]), kanban_view.DONE_CAP)

    def test_caps_drop_the_oldest_and_the_lists_run_newest_first(self):
        coder = kanban_view.board("coder")["agents"]["coder"]
        self.assertEqual(len(coder["tasks"]["todo"]), 25)        # 31 c_t* + 3 others -> 25
        self.assertEqual(coder["counts"]["todo"], 34)
        self.assertEqual(len(coder["tasks"]["done"]), 8)
        self.assertEqual(coder["counts"]["done"], 13)
        for status in kanban_view.STATUSES:
            stamps = [c["updated_at"] for c in coder["tasks"][status]]
            self.assertEqual(stamps, sorted(stamps, reverse=True))
        # updated_at is completed_at else started_at else created_at, so b_done (finished at NOW-550)
        # outranks every c_d* card and c_t30/c_t29/c_t28 outrank the older todos.
        self.assertEqual(self.ids(coder, "todo")[:3], ["c_t30", "c_t29", "c_t28"])
        self.assertEqual(self.ids(coder, "done")[:3], ["b_done", "c_d00", "c_d01"])
        self.assertEqual([c["updated_at"] for c in coder["tasks"]["done"]],
                         [NOW - 550] + [NOW - 4000 - i * 60 for i in range(7)])
        self.assertNotIn("c_t01", self.ids(coder, "todo"))       # the cap drops the OLDEST first
        self.assertNotIn("b_todo", self.ids(coder, "todo"))
        self.assertNotIn("c_d11", self.ids(coder, "done"))

    def test_titles_have_control_chars_gone_are_trimmed_and_capped(self):
        cols = kanban_view.read_board(self.path)["coder"]       # uncapped, so the old cards are here
        control = next(c for c in cols["todo"] if c["id"] == "b_wonder")
        # every control mark became one plain space: the payload can never smuggle a tab/newline
        # into the card markup (the fixture title is "Tabbed\t newline\ned and bell\u0007 control marks")
        self.assertEqual(control["title"], "Tabbed  newline ed and bell  control marks")
        self.assertEqual(next(c for c in cols["ready"] if c["id"] == "b_ready")["title"],
                         "Queue the follow-up")                  # the fixture title is padded
        long = next(c for c in cols["todo"] if c["id"] == "c_t00")
        self.assertEqual(len(long["title"]), kanban_view.TITLE_MAX)   # 132 in, 120 out
        self.assertEqual(long["title"], LONG[:kanban_view.TITLE_MAX])
        for columns in kanban_view.read_board(self.path).values():      # every card, every column
            for column in columns.values():
                for card in column:
                    self.assertLessEqual(len(card["title"]), kanban_view.TITLE_MAX)
                    self.assertEqual(card["title"], card["title"].strip())
                    self.assertFalse([ch for ch in card["title"] if ord(ch) < 32 or ord(ch) == 127])

    # ---------- the board never writes the db ----------
    def test_the_connection_is_a_read_only_uri_dsn(self):
        seen, refused = [], []
        real, probe = sqlite3.connect, self.con

        def spy(dsn, *args, **kwargs):
            con = real(dsn, *args, **kwargs)
            seen.append(dsn)
            try:                                                 # can THIS connection write?
                con.execute("insert into tasks (id,title,assignee,status,created_at)"
                            " values ('spy_row','written by the board','coder','todo',1)")
                con.commit()
                refused.append(False)
            except sqlite3.OperationalError:
                refused.append(True)                             # read-only: the write is refused
            return con

        kanban_view.sqlite3.connect = spy
        try:
            data = kanban_view.board()
        finally:
            kanban_view.sqlite3.connect = real
        self.assertEqual(len(seen), 1, "one connection per rebuild, closed immediately")
        self.assertTrue(seen[0].startswith("file:"), seen[0])
        self.assertIn("mode=ro", seen[0])
        self.assertTrue(refused and all(refused), "the board's own connection must not be writable")
        self.assertEqual(probe.execute("select count(*) from tasks where id='spy_row'").fetchone()[0], 0)
        self.assertEqual(data["counts"]["total"], len(FIXTURE) - 1)

    def test_a_read_only_db_file_still_builds_the_board(self):
        # The container mounts the db read-only, so a 0o444 kanban.db must not fail the board.
        # (A db genuinely in WAL mode would also need its -wal sibling readable: the deployed mount
        # is read-only exactly like this chmod, which is a deployment fact a file-mode fixture cannot
        # prove — this fixture db is in delete mode, as its own pragma reports.)
        os.chmod(self.path, 0o444)
        try:
            data = kanban_view.board()
        finally:
            os.chmod(self.path, 0o644)
        self.assertTrue(data["ok"])
        self.assertNotIn("warning", data)                        # read-only is not a failure
        self.assertEqual(data["agents"]["coder"]["counts"]["todo"], 34)
        self.assertFalse([p for p in (self.path + "-wal", self.path + "-shm")
                          if os.path.exists(p)], "a read must not leave wal sidecars")

    def test_building_the_board_leaves_the_db_file_untouched(self):
        before = os.stat(self.path)
        data = kanban_view.board()
        after = os.stat(self.path)
        self.assertTrue(data["ok"])
        self.assertEqual((after.st_mtime_ns, after.st_size), (before.st_mtime_ns, before.st_size))

    def test_the_board_is_rebuilt_at_most_once_per_ttl(self):
        opens, real = [], sqlite3.connect

        def spy(dsn, *args, **kwargs):
            opens.append(dsn)
            return real(dsn, *args, **kwargs)

        kanban_view.sqlite3.connect = spy
        try:
            kanban_view.board()
            kanban_view.board("coder")
            self.assertEqual(len(opens), 1)                      # the second call reads the cache
            kanban_view._cache["at"] = time.time() - (kanban_view.TTL_S + 1)
            kanban_view.board()
            self.assertEqual(len(opens), 2)                      # a stale cache does re-read
        finally:
            kanban_view.sqlite3.connect = real

    def test_a_lost_db_answers_the_last_board_then_db_unavailable(self):
        good = kanban_view.board()                               # the snapshot a later failure serves
        self.assertTrue(good["ok"])
        os.environ["OFFICE_DB"] = os.path.join(self.dir, "not-here.db")
        try:
            kanban_view._cache["at"] = time.time() - (kanban_view.TTL_S + 1)
            stale = kanban_view.board()                          # snapshot kept: stale but usable
            self.assertTrue(stale["ok"])
            self.assertIn("warning", stale)
            self.assertEqual(stale["agents"]["coder"]["counts"]["todo"], 34)
            kanban_view._cache.update({"at": 0.0, "owners": None})
            fresh = kanban_view.board()                          # no snapshot: the route's 503
            self.assertFalse(fresh["ok"])
            self.assertEqual(fresh["error"], "db_unavailable")
            self.assertNotIn("agents", fresh)
        finally:
            os.environ["OFFICE_DB"] = self.path

    def test_an_agent_outside_the_fleet_is_answered_as_none_for_the_route_to_404(self):
        self.assertIsNone(kanban_view.board("nobody"))
        self.assertIsNone(kanban_view.board("bart"))             # a real person, but no desk
        self.assertEqual(kanban_view.board("HERMES")["agent"], "orchestrator")


if __name__ == "__main__":
    unittest.main()
