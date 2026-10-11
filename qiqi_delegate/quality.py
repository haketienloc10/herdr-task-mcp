"""Durable, fail-closed quality obligations for context-bound TaskGraphs.

Peer reports and Lead reviews are provisional. Only the independent final
evaluation can authorize delivery. Keep obligations across retry and replan.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Any

REPORT_STATES = frozenset({
    "done", "not_done", "not_verified", "failed",
    "unable_to_execute", "unable_to_verify", "blocked",
})
UNRESOLVED = REPORT_STATES - {"done"}
REVIEW_STATES = frozenset({"supported", "unsupported", "unknown"})


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def install(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS quality_policies (
            graph_run_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL,
            request_revision INTEGER NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS quality_items (
            graph_run_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('requirement','criterion','discovered')),
            node_id TEXT,
            criterion_index INTEGER,
            requirement_ids_json TEXT NOT NULL,
            description TEXT NOT NULL,
            state TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '',
            evidence_json TEXT NOT NULL DEFAULT '[]',
            successor_node_id TEXT,
            updated_at_ns INTEGER NOT NULL,
            PRIMARY KEY (graph_run_id, item_id)
        );
        CREATE TABLE IF NOT EXISTS quality_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            graph_run_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            event TEXT NOT NULL,
            attempt_id TEXT,
            reason TEXT NOT NULL,
            created_at_ns INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS quality_reviews (
            graph_run_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            attempt_id TEXT NOT NULL,
            report_json TEXT NOT NULL,
            review_json TEXT NOT NULL,
            created_at_ns INTEGER NOT NULL,
            PRIMARY KEY (graph_run_id, node_id, attempt_id)
        );
    """)


def is_strict(conn: sqlite3.Connection, run_id: str) -> bool:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='quality_policies'"
    ).fetchone()
    return bool(exists and conn.execute(
        "SELECT 1 FROM quality_policies WHERE graph_run_id=?", (run_id,)
    ).fetchone())


def seed(db_path: Any, run_id: str, request_id: str, request_revision: int,
         requirements: list[dict[str, Any]], graph: Any,
         requirement_map: dict[str, list[str]]) -> None:
    """Seed all original requirements and authored criteria, never mark verified."""
    with sqlite3.connect(db_path) as db:
        # install() uses executescript; call it before starting the CAS transaction.
        install(db)
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM quality_policies WHERE graph_run_id=?",
                      (run_id,)).fetchone():
            raise RuntimeError("quality ledger is already initialized")
        db.execute("INSERT INTO quality_policies VALUES (?,?,?,1)",
                   (run_id, request_id, request_revision))
        now = time.time_ns()
        for req in requirements:
            db.execute(
                "INSERT INTO quality_items(graph_run_id,item_id,kind,node_id,"
                "criterion_index,requirement_ids_json,description,state,updated_at_ns)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, "req:" + req["id"], "requirement", None, None,
                 _json([req["id"]]), req["text"], "not_verified", now),
            )
        for node in graph.nodes:
            for index, criterion in enumerate(node.task_packet.acceptance_criteria):
                db.execute(
                    "INSERT INTO quality_items(graph_run_id,item_id,kind,node_id,"
                    "criterion_index,requirement_ids_json,description,state,updated_at_ns)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (run_id, "criterion:" + node.node_id + ":" + str(index),
                     "criterion", node.node_id, index,
                     _json(requirement_map[node.node_id]), criterion, "not_done", now),
                )


def parse_peer_report(raw: Any, criteria: tuple[str, ...],
                      allowed_requirements: set[str]) -> dict[str, Any]:
    """Strict structural review; untrusted self-reported success is never proof."""
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 256_000:
        raise ValueError("Peer report is absent or over native capture limit")
    try:
        report = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError("Peer response is not TaskExecutionReport JSON") from exc
    if not isinstance(report, dict) or set(report) != {
        "schema_version", "summary", "criterion_results", "unresolved_items"
    } or report["schema_version"] != 1:
        raise ValueError("Peer must return TaskExecutionReport v1")
    if not isinstance(report["summary"], str) or not report["summary"].strip():
        raise ValueError("Peer report summary is missing")
    results = report["criterion_results"]
    if not isinstance(results, list) or len(results) != len(criteria):
        raise ValueError("Peer report must cover every acceptance criterion")
    seen = set()
    for item in results:
        if not isinstance(item, dict) or set(item) != {
            "criterion_index", "status", "evidence", "verification", "details"
        }:
            raise ValueError("malformed Peer criterion result")
        idx = item["criterion_index"]
        if (isinstance(idx, bool) or not isinstance(idx, int)
                or idx < 0 or idx >= len(criteria) or idx in seen
                or item["status"] not in REPORT_STATES):
            raise ValueError("invalid/duplicate Peer criterion index or status")
        seen.add(idx)
        if not isinstance(item["details"], str):
            raise ValueError("Peer criterion details must be text")
        ev = item["evidence"]
        if not isinstance(ev, list):
            raise ValueError("Peer criterion evidence must be a list")
        for e in ev:
            if not isinstance(e, dict) or set(e) != {"kind", "repository", "path", "locator"}:
                raise ValueError("malformed Peer evidence locator")
            if e["kind"] != "repo_file" or any(
                not isinstance(e[k], str) or not e[k].strip()
                for k in ("repository", "path", "locator")
            ) or e["path"].startswith("/") or ".." in e["path"].split("/"):
                raise ValueError("invalid Peer evidence locator")
        verification = item["verification"]
        if (not isinstance(verification, dict)
                or set(verification) != {"method", "performed", "outcome", "artifact_ref"}
                or not isinstance(verification["method"], str)
                or not isinstance(verification["performed"], bool)
                or verification["outcome"] not in ("pass", "fail", "inconclusive")
                or (verification["artifact_ref"] is not None and
                    not isinstance(verification["artifact_ref"], str))):
            raise ValueError("malformed Peer verification descriptor")
        if item["status"] == "done" and (not ev or not verification["performed"]
                                          or verification["outcome"] != "pass"):
            raise ValueError("Peer done claim lacks reviewable evidence/verification")
    unresolved = report["unresolved_items"]
    if not isinstance(unresolved, list):
        raise ValueError("Peer unresolved_items must be an array")
    ids = set()
    for item in unresolved:
        if not isinstance(item, dict) or set(item) != {
            "id", "kind", "description", "affected_requirement_ids",
            "reason", "attempted_methods", "next_action"
        }:
            raise ValueError("malformed unresolved work item")
        if (not isinstance(item["id"], str) or not item["id"].strip()
                or item["id"] in ids or item["kind"] not in UNRESOLVED
                or not isinstance(item["affected_requirement_ids"], list)
                or not item["affected_requirement_ids"]
                or not set(item["affected_requirement_ids"]) <= allowed_requirements
                or any(not isinstance(item[k], str) or not item[k].strip()
                       for k in ("description", "reason", "next_action"))
                or not isinstance(item["attempted_methods"], list)
                or any(not isinstance(v, str) or not v.strip()
                       for v in item["attempted_methods"])):
            raise ValueError("invalid unresolved work item")
        ids.add(item["id"])
    # A non-done criterion must remain visible as unresolved work, even if
    # Peer omitted it from the separate discovered-issues list.
    return report


def validate_lead_review(report: dict[str, Any], reviews: tuple[dict[str, Any], ...]) -> None:
    results = report["criterion_results"]
    if len(reviews) != len(results):
        raise ValueError("ACCEPT requires a Lead review of every criterion")
    seen = set()
    for review in reviews:
        if not isinstance(review, dict) or set(review) != {
            "criterion_index", "status", "evidence_refs", "rationale"
        }:
            raise ValueError("invalid Lead criterion review")
        idx = review["criterion_index"]
        if (isinstance(idx, bool) or not isinstance(idx, int)
                or idx < 0 or idx >= len(results) or idx in seen
                or review["status"] not in REVIEW_STATES
                or not isinstance(review["rationale"], str)
                or not review["rationale"].strip()
                or not isinstance(review["evidence_refs"], list)
                or not review["evidence_refs"]
                or any(not isinstance(v, str) or not v.strip()
                       for v in review["evidence_refs"])):
            raise ValueError("incomplete Lead criterion review")
        seen.add(idx)
        peer = next(item for item in results if item["criterion_index"] == idx)
        if peer["status"] != "done" or review["status"] != "supported":
            raise ValueError("ACCEPT cannot waive unfinished/unverified criterion")
        if any(not v.startswith("peer_turn:") for v in review["evidence_refs"]):
            raise ValueError("Lead evidence must reference captured Peer turn")
    if report["unresolved_items"]:
        raise ValueError("ACCEPT cannot waive unresolved work; RETRY/REPLAN/BLOCK")
    return None


def persist_decision(conn: sqlite3.Connection, run_id: str, node_id: str,
                     attempt_id: str | None, action: str, report: dict[str, Any] | None,
                     reviews: tuple[dict[str, Any], ...]) -> None:
    if not is_strict(conn, run_id):
        return
    now = time.time_ns()
    if report is None:
        return  # Native capture failure remains reviewable, never ACCEPT.
    if attempt_id is None:
        raise RuntimeError("quality review has no current attempt")
    prior = conn.execute(
        "SELECT 1 FROM quality_reviews WHERE graph_run_id=? AND node_id=? AND attempt_id=?",
        (run_id, node_id, attempt_id),
    ).fetchone()
    if prior:
        raise RuntimeError("quality review of this attempt is already persisted")
    conn.execute("INSERT INTO quality_reviews VALUES (?,?,?,?,?,?)",
                 (run_id, node_id, attempt_id, _json(report), _json(reviews), now))
    for item in report["criterion_results"]:
        cid = "criterion:" + node_id + ":" + str(item["criterion_index"])
        state = "not_verified" if item["status"] == "done" else item["status"]
        conn.execute(
            "UPDATE quality_items SET state=?,details=?,evidence_json=?,updated_at_ns=? "
            "WHERE graph_run_id=? AND item_id=?",
            (state, item["details"], _json(item["evidence"]), now, run_id, cid),
        )
        conn.execute("INSERT INTO quality_events(graph_run_id,item_id,event,attempt_id,reason,created_at_ns) "
                     "VALUES (?,?,?,?,?,?)",
                     (run_id, cid, "peer_report:" + action, attempt_id, item["details"], now))
    for item in report["unresolved_items"]:
        uid = "discovered:" + node_id + ":" + item["id"]
        previous = conn.execute(
            "SELECT state FROM quality_items WHERE graph_run_id=? AND item_id=?",
            (run_id, uid),
        ).fetchone()
        if previous is None:
            conn.execute(
                "INSERT INTO quality_items(graph_run_id,item_id,kind,node_id,"
                "requirement_ids_json,description,state,details,updated_at_ns)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, uid, "discovered", node_id,
                 _json(item["affected_requirement_ids"]), item["description"],
                 item["kind"], _json(item), now),
            )
        else:
            # Re-reporting does not lose an unresolved item or its history.
            conn.execute(
                "UPDATE quality_items SET state=?,details=?,updated_at_ns=? "
                "WHERE graph_run_id=? AND item_id=?",
                (item["kind"], _json(item), now, run_id, uid),
            )
        conn.execute("INSERT INTO quality_events(graph_run_id,item_id,event,attempt_id,reason,created_at_ns) "
                     "VALUES (?,?,?,?,?,?)",
                     (run_id, uid, "reported:" + action, attempt_id, item["reason"], now))
    if action == "accept":
        conn.execute(
            "UPDATE quality_items SET state='not_verified',updated_at_ns=? "
            "WHERE graph_run_id=? AND kind='requirement'",
            (now, run_id),
        )
    conn.execute("UPDATE quality_policies SET revision=revision+1 WHERE graph_run_id=?",
                 (run_id,))


def status(db_path: Any, run_id: str) -> dict[str, Any]:
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        if not is_strict(db, run_id):
            return {"quality_policy": "legacy_unassessed", "quality_state": "not_verified"}
        policy = db.execute(
            "SELECT request_id,request_revision,revision FROM quality_policies "
            "WHERE graph_run_id=?", (run_id,),
        ).fetchone()
        rows = db.execute("SELECT * FROM quality_items WHERE graph_run_id=? ORDER BY item_id",
                          (run_id,)).fetchall()
        items = [dict(row) for row in rows]
        for item in items:
            item["requirement_ids"] = json.loads(item.pop("requirement_ids_json"))
            item["evidence"] = json.loads(item.pop("evidence_json"))
        pending = [item for item in items if item["state"] != "verified"]
        blocked = any(item["state"] in ("blocked", "unable_to_execute", "unable_to_verify")
                      for item in pending)
        return {
            "quality_policy": "strict_v1",
            "quality_revision": policy["revision"],
            "request_id": policy["request_id"],
            "request_revision": policy["request_revision"],
            "quality_state": "blocked" if blocked else
                             "needs_work" if pending else "verified",
            "items": items,
        }


def assert_no_open_work(db_path: Any, run_id: str) -> dict[str, Any]:
    """Before independent evaluation, Lead must have closed node-level work.

    Requirements remain not_verified until independent final validation.
    """
    current = status(db_path, run_id)
    if current["quality_policy"] != "strict_v1":
        return current
    open_items = [
        item["item_id"] for item in current["items"]
        if item["kind"] != "requirement" and item["state"] != "not_verified"
    ]
    if open_items:
        raise ValueError("Quality Gate has unfinished/unresolved work: " +
                         ", ".join(open_items[:16]))
    return current
