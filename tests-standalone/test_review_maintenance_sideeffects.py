"""Codex review #5479044211: inspection must not trigger global recovery."""
import json
import sqlite3
import subprocess
import sys

from qiqi_delegate.maintenance import recover_ownerless_discovery, show_discovery
from qiqi_delegate.task_request import TaskRequestStore
from test_task_readiness import workspace


def _pending_request(store, *, count=15):
    request = store.create(
        "Discover ownership of payment retry",
        sources=[{"kind": "inline", "text": f"Context {i}"} for i in range(count)],
    )
    assessed = store.assess(request["request_id"], 1, {
        "requirements": [{
            "id": "R1", "text": "Investigate payment retry flow",
            "evidence_refs": ["request:current"],
        }],
        "blocking_unknowns": ["Payment retry ownership is unknown"],
        "decision": "targeted_discovery",
        "rationale": "Need source inspection",
    })
    did = store.begin_discovery(
        request["request_id"], "targeted_discovery",
        ["backend"], ["Trace retries with file:line"], "codex-balanced",
    )
    return assessed["request_id"], did


def _dead_native_capture(runtime, discovery_id, turn_id):
    with sqlite3.connect(runtime.db) as db:
        db.execute(
            "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
            (turn_id, "native-session", "backend", "codex-balanced",
             "settled", "Captured contract: backend/retry.py:88", 123),
        )
        db.execute(
            "UPDATE task_discoveries SET owner_pid=?, owner_start_token=?, "
            "turn_id=? WHERE discovery_id=?",
            (987654321, "dead-owner", turn_id, discovery_id),
        )


def _db_snapshot(runtime, request_id, discovery_id):
    with sqlite3.connect(runtime.db) as db:
        request = db.execute(
            "SELECT revision, sources_json, assessment_json "
            "FROM task_requests WHERE request_id=?", (request_id,),
        ).fetchone()
        discovery = db.execute(
            "SELECT state, turn_id, detail FROM task_discoveries "
            "WHERE discovery_id=?", (discovery_id,),
        ).fetchone()
    return {
        "revision": request[0],
        "sources": json.loads(request[1]),
        "assessment": json.loads(request[2]) if request[2] else None,
        "discovery": discovery,
    }


def test_show_discovery_does_not_recover_unrelated_dead_owner(tmp_path):
    runtime, store = workspace(tmp_path)
    target_id, target_did = _pending_request(store)
    other_id, other_did = _pending_request(store)
    _dead_native_capture(runtime, other_did, "captured-but-unattached")
    before = _db_snapshot(runtime, other_id, other_did)

    # Both inspection paths must be read-only even if an unrelated Discovery
    # has durable native capture and a provably dead owner.
    result = show_discovery(workspace=runtime.root, discovery_id=target_did)
    assert result["discovery_id"] == target_did
    assert result["state"] == "requested"
    assert _db_snapshot(runtime, other_id, other_did) == before

    inspected_dead = show_discovery(workspace=runtime.root, discovery_id=other_did)
    assert inspected_dead["state"] == "requested"
    assert _db_snapshot(runtime, other_id, other_did) == before

    command = [
        sys.executable, "-m", "qiqi_delegate.maintenance",
        "show-discovery", "--workspace", str(runtime.root),
        "--discovery-id", target_did,
    ]
    cli = subprocess.run(command, capture_output=True, text=True)
    assert cli.returncode == 0, cli.stderr
    assert json.loads(cli.stdout)["discovery_id"] == target_did
    assert _db_snapshot(runtime, other_id, other_did) == before

    # Ordinary MCP startup must still perform abandoned-capture recovery.
    restarted = TaskRequestStore(runtime.db, runtime.root, runtime.repos)
    recovered = restarted.get(other_id)
    assert recovered["revision"] == before["revision"] + 1
    assert recovered["assessment"] is None
    assert recovered["sources"][-1]["turn_id"] == "captured-but-unattached"
    assert recovered["discoveries"][0]["state"] == "settled"


def test_exact_ownerless_operator_recovery_does_not_touch_other_dead_owner(tmp_path):
    runtime, store = workspace(tmp_path)
    other_id, other_did = _pending_request(store)
    _dead_native_capture(runtime, other_did, "unrelated-native")
    before_other = _db_snapshot(runtime, other_id, other_did)

    ownerless = store.create("Legacy Discovery needs operator recovery")
    ownerless_id = "legacy-exact-only"
    with sqlite3.connect(runtime.db) as db:
        db.execute(
            "INSERT INTO task_discoveries "
            "(discovery_id, request_id, mode, repository_names_json, "
            "questions_json, route, state, created_at_ns) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ownerless_id, ownerless["request_id"], "targeted_discovery",
             '["backend"]', '["Inspect"]', "codex-balanced", "requested", 1),
        )
    result = recover_ownerless_discovery(
        workspace=runtime.root, discovery_id=ownerless_id,
        worker_termination_confirmed=True,
    )
    assert result["state"] == "interrupted"
    assert _db_snapshot(runtime, other_id, other_did) == before_other
    with sqlite3.connect(runtime.db) as db:
        audited = db.execute(
            "SELECT discovery_id FROM task_discovery_recovery_audit"
        ).fetchall()
    assert audited == [(ownerless_id,)]


def test_maintenance_inspection_does_not_finalize_unrelated_attaching(tmp_path):
    runtime, store = workspace(tmp_path)
    target_id, target_did = _pending_request(store)
    other_id, other_did = _pending_request(store)
    with sqlite3.connect(runtime.db) as db:
        db.execute(
            "UPDATE task_discoveries SET owner_pid=?, owner_start_token=?, "
            "state='attaching', turn_id=? WHERE discovery_id=?",
            (987654321, "dead-owner", "already-attached-turn", other_did),
        )
    before = _db_snapshot(runtime, other_id, other_did)

    shown = show_discovery(workspace=runtime.root, discovery_id=target_did)
    assert shown["state"] == "requested"
    assert _db_snapshot(runtime, other_id, other_did) == before

    # Regular startup still finalizes abandoned 'attaching' records.
    TaskRequestStore(runtime.db, runtime.root, runtime.repos)
    assert _db_snapshot(runtime, other_id, other_did)["discovery"][0] == "settled"
