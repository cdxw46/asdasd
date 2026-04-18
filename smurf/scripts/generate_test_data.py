"""Generate baseline PBX data for SMURF installation."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.config import load_config
from core.db import Database


def ensure_test_data(db: Database, reset: bool = False):
    if reset:
        db.execute("DELETE FROM dialplan_rules")
        db.execute("DELETE FROM call_queues")
        db.execute("DELETE FROM ring_groups")
        db.execute("DELETE FROM trunks WHERE name = 'default-trunk'")

    # Ring group 600 -> 1000,1001
    db.execute(
        """
        INSERT INTO ring_groups (group_number, name, strategy, members_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(group_number) DO NOTHING
        """,
        ("600", "Sales Group", "round_robin", '["1000","1001"]'),
    )
    # Queue 700 -> 1000,1001
    db.execute(
        """
        INSERT INTO call_queues (queue_number, name, strategy, members_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(queue_number) DO NOTHING
        """,
        ("700", "Support Queue", "least_busy", '["1000","1001"]'),
    )
    # Dialplan example
    db.execute(
        """
        INSERT INTO dialplan_rules (name, pattern, action, target, priority, enabled)
        VALUES (?, ?, ?, ?, ?, 1)
        """,
        ("Internal direct calls", r"^[1-9][0-9]{3}$", "extension", "1000", 10),
    )
    # Trunk example
    db.execute(
        """
        INSERT INTO trunks (
            name, host, port, transport, auth_type, username, password,
            outbound_prefix, priority, max_channels, active
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(name) DO NOTHING
        """,
        ("default-trunk", "sip.provider.example", 5060, "udp", "credentials", "", "", "9", 100, 30),
    )


def main():
    parser = argparse.ArgumentParser(description="Generate SMURF test data")
    parser.add_argument("--config", default=None, help="Path to SMURF config")
    parser.add_argument("--reset", action="store_true", help="Reset seeded entities first")
    args = parser.parse_args()

    cfg = load_config(args.config)
    db_path = cfg.database.sqlite_path
    db = Database(db_path)
    ensure_test_data(db, reset=args.reset)
    print("SMURF test data ready.")


if __name__ == "__main__":
    main()

