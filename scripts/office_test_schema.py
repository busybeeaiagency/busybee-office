#!/usr/bin/env python3
"""Emit the live kanban.db DDL for the tables the office reads/writes.

  python3 scripts/office_test_schema.py /opt/data/kanban.db > test/schema_live.sql

The office container talks to a real kanban.db, so the tests must run against the real DDL
(columns, types, NOT NULLs, AUTOINCREMENT ids) — a hand-written toy schema hides exactly the
mismatches that break a deploy (e.g. task_links is (parent_id, child_id); task_comments.id is
INTEGER PRIMARY KEY and rejects a text id).
"""
import sqlite3
import sys

TABLES = ("tasks", "task_events", "task_runs", "task_links", "task_comments")
db = sys.argv[1] if len(sys.argv) > 1 else "/opt/data/kanban.db"
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
parts = []
for name in TABLES:
    row = con.execute("select sql from sqlite_master where type='table' and name=?", (name,)).fetchone()
    if row and row[0]:
        parts.append(row[0].strip().rstrip(";"))
if not parts:
    sys.exit(f"no tables found in {db}")
print("-- copied from the live kanban.db by scripts/office_test_schema.py — do not hand-edit")
print(";\n".join(parts) + ";")
