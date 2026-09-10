"""Print n8n executions newer than a given id, oldest first.

Used by watch-pipeline.ps1 to surface workflow activity in the live view.
Opens the database read-only so it can never interfere with the running n8n.

Usage:  python scripts/n8n_recent.py [--since ID] [--limit N]
Output: one line per execution:  <id>\t<HH:MM:SS>\t<status>\t<workflow name>
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path


def database_path() -> Path:
    """Mirror n8n's own resolution: N8N_USER_FOLDER, else the home directory."""
    base = os.environ.get("N8N_USER_FOLDER") or os.path.expanduser("~")
    return Path(base) / ".n8n" / "database.sqlite"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=int, default=0)
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    db = database_path()
    if not db.exists():
        return 0

    try:
        # mode=ro keeps this strictly a reader; n8n keeps the file in WAL mode.
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2)
        rows = conn.execute(
            """select e.id, e.startedAt, e.status, w.name
                 from execution_entity e
                 left join workflow_entity w on w.id = e.workflowId
                where e.id > ?
             order by e.id
                limit ?""",
            (args.since, args.limit),
        ).fetchall()
    except sqlite3.Error:
        # A locked or half-written database is not worth failing the view over.
        return 0

    for eid, started, status, name in rows:
        clock = (started or "")[11:19] or "--:--:--"
        print(f"{eid}\t{clock}\t{status or '?'}\t{name or 'unknown'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
