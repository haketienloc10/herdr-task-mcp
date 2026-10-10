"""Regression tests for Codex review #5479006484: migration races and ownerless workers."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import subprocess
import sys
from threading import Barrier

import pytest

from test_task_readiness import workspace
from qiqi_delegate.maintenance import recover_ownerless_discovery, show_discovery
from qiqi_delegate.task_request import TaskRequestStore


def _old_schema(db_path):
    with sqlite3.connect(db_path) as db:
        db.execute("DROP TABLE task_discoveries")
        db.execute("""
            CREATE TABLE task_discoveries (
                discovery_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                repository_names_json TEXT NOT NULL,
                questions_json TEXT NOT NULL,
                route TEXT NOT NULL,
                turn_id TEXT,
                state TEXT NOT NULL,
                detail TEXT,
                created_at_ns INTEGER NOT NULL
            )
        """)


def test_concurrent_mcp_initialization_serializes_ownership_upgrade(tmp_path):
    runtime, _store = workspace(tmp_path)
    _old_schema(runtime.db)
    barrier = Barrier(12)

    def initialize(_):
        barrier.wait(timeout=20)
        store = TaskRequestStore(runtime.db, runtime.root, runtime.repos)
        with store._connect() as db:
            columns = {row["name"] for row in db.execute(
                "PRAGMA table_info(task_discoveries)"
            )}
        assert {"owner_pid", "owner_start_token"}.issubset(columns)

    # All twelve processes/connections independently start against the same
    # legacy schema. Only one migration may inspect/ALTER at a time.
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(initialize, range(12)))
    # A further MCP startup on the upgraded DB is idempotent.
    initialize_done = TaskRequestStore(runtime.db, runtime.root, runtime.repos)
    assert initialize_done.recover_abandoned_discoveries() == 0


def test_ownerless_attaching_record_is_not_finalized_on_startup(tmp_path):
    runtime, store = workspace(tmp_path)
    request = store.create("Follow up on previous Discovery",
                           sources=[{"kind": "inline", "text": "requirements"}])
    with sqlite3.connect(runtime.db) as db:
        db.execute(
            "INSERT INTO task_discoveries "
            "(discovery_id, request_id, mode, repository_names_json, "
            "questions_json, route, turn_id, state, created_at_ns) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("ownerless-attaching", request["request_id"], "targeted_discovery",
             '["backend"]', '["Inspect contract"]', "codex-balanced",
             "legacy-turn", "attaching", 1),
        )
    # Another server may start while the pre-migration worker is mid-finalize.
    restored = TaskRequestStore(runtime.db, runtime.root, runtime.repos)
    assert restored.inspect_discovery("ownerless-attaching")["state"] == "attaching"
    with pytest.raises(ValueError, match="worker has terminated"):
        restored.recover_ownerless_discovery(
            "ownerless-attaching", worker_termination_confirmed=False,
        )
    recovered = restored.recover_ownerless_discovery(
        "ownerless-attaching", worker_termination_confirmed=True,
    )
    assert recovered["state"] == "settled"
    assert recovered["turn_id"] == "legacy-turn"


def test_exact_operator_recovery_preserves_captured_native_turn(tmp_path):
    runtime, store = workspace(tmp_path)
    request = store.create(
        "Inspect and retry with an accepted Peer",
        sources=[{"kind": "inline", "text": "context"}],
    )
    rid = request["request_id"]
    with sqlite3.connect(runtime.db) as db:
        db.execute(
            "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("legacy-complete-turn", "native-session", "backend", "codex-balanced",
             "settled", "Verified in backend/src/orders.py:19", 567),
        )
        db.execute(
            "INSERT INTO task_discoveries "
            "(discovery_id, request_id, mode, repository_names_json, "
            "questions_json, route, turn_id, state, created_at_ns) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("legacy-completed", rid, "targeted_discovery",
             '["backend"]', '["Inspect"]', "codex-balanced",
             "legacy-complete-turn", "requested", 2),
        )
        db.execute(
            "INSERT INTO task_discoveries "
            "(discovery_id, request_id, mode, repository_names_json, "
            "questions_json, route, state, created_at_ns) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("other-legacy", rid, "targeted_discovery",
             '["backend"]', '["Inspect"]', "codex-balanced", "requested", 3),
        )
    # Neither live legacy operation can be inferred dead from missing PID.
    restarted = TaskRequestStore(runtime.db, runtime.root, runtime.repos)
    assert len(restarted.get(rid)["sources"]) == 1
    assert restarted.inspect_discovery("legacy-completed")["state"] == "requested"
    restored = recover_ownerless_discovery(
        workspace=runtime.root, discovery_id="legacy-completed",
        worker_termination_confirmed=True,
    )
    assert restored["state"] == "settled"
    state = restarted.get(rid)
    assert state["sources"][-1]["turn_id"] == "legacy-complete-turn"
    assert "orders.py:19" in state["sources"][-1]["content"]
    assert state["revision"] == 2
    assert restarted.inspect_discovery("other-legacy")["state"] == "requested"
    with pytest.raises(ValueError, match="ownerless in-flight"):
        recover_ownerless_discovery(
            workspace=runtime.root, discovery_id="legacy-completed",
            worker_termination_confirmed=True,
        )


def test_operator_cli_requires_exact_id_and_confirmation(tmp_path):
    runtime, store = workspace(tmp_path)
    rid = store.create("Inspect old Discovery")["request_id"]
    with sqlite3.connect(runtime.db) as db:
        db.execute(
            "INSERT INTO task_discoveries "
            "(discovery_id, request_id, mode, repository_names_json, "
            "questions_json, route, state, created_at_ns) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("old-discovery", rid, "full_discovery",
             '["backend"]', '["Inspect"]', "codex-balanced", "requested", 1),
        )
    command = [sys.executable, "-m", "qiqi_delegate.maintenance"]
    args = ["--workspace", str(runtime.root), "--discovery-id", "old-discovery"]
    show = subprocess.run(command + ["show-discovery", *args],
                          capture_output=True, text=True)
    assert show.returncode == 0, show.stderr
    assert json.loads(show.stdout)["owner_pid"] is None
    rejected = subprocess.run(
        command + ["recover-ownerless-discovery", *args],
        capture_output=True, text=True,
    )
    assert rejected.returncode != 0
    assert store.inspect_discovery("old-discovery")["state"] == "requested"
    accepted = subprocess.run(
        command + ["recover-ownerless-discovery", *args,
                   "--worker-termination-confirmed"],
        capture_output=True, text=True,
    )
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout)["state"] == "interrupted"
