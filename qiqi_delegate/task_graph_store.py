from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from qiqi_delegate.core import build_task_packet
from qiqi_delegate.task_graph import GraphNode, TaskGraph
from qiqi_delegate.task_graph_scheduler import GraphSnapshot, NodeState, derive_graph_state

ATTEMPT_TERMINAL_STATES = frozenset({"settled", "failed", "blocked"})
ATTEMPT_STATES = frozenset({"running", *ATTEMPT_TERMINAL_STATES})


def new_graph_run_id() -> str:
    return str(uuid.uuid4())


def new_wave_id() -> str:
    return str(uuid.uuid4())


def new_attempt_id() -> str:
    return str(uuid.uuid4())


def _required_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _optional_id(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _required_id(value, label)


def _required_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("expected_revision must be a non-negative integer")
    return value


def _result_json(result: Any) -> str:
    if not isinstance(result, dict):
        raise ValueError("normalized node result must be an object")
    try:
        return json.dumps(
            result,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("normalized node result must be JSON-serializable") from exc


def _graph_fingerprint(graph: TaskGraph) -> str:
    if not isinstance(graph, TaskGraph):
        raise ValueError("graph must be a TaskGraph")
    payload = [
        {
            "node_id": node.node_id,
            "kind": node.kind,
            "repository": node.repository,
            "route": node.route,
            "depends_on": list(node.depends_on),
            "task_packet_json": node.task_packet.to_json(),
        }
        for node in graph.nodes
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validated_states(snapshot: GraphSnapshot) -> dict[str, NodeState]:
    derive_graph_state(snapshot)
    return {state.node_id: state for state in snapshot.node_states}


def _graph_json(graph: TaskGraph) -> str:
    return _result_json(graph.as_dict())


def _graph_from_json(encoded: str) -> TaskGraph:
    """Decode persisted authored semantics without relying on process memory."""
    try:
        payload = json.loads(encoded)
        if not isinstance(payload, dict) or not isinstance(payload.get("nodes"), list):
            raise ValueError("authored graph JSON must contain nodes")
        nodes = []
        for item in payload["nodes"]:
            if not isinstance(item, dict):
                raise ValueError("authored node must be an object")
            nodes.append(GraphNode(
                node_id=item["node_id"],
                repository=item["repository"],
                route=item.get("route"),
                kind=item.get("kind", "repo_task"),
                depends_on=tuple(item.get("depends_on", [])),
                task_packet=build_task_packet(**item["task_packet"]),
            ))
        return TaskGraph(nodes=tuple(nodes))
    except (TypeError, ValueError, KeyError) as exc:
        raise RuntimeError("persisted authored TaskGraph is invalid") from exc


class GraphRuntimeStore:
    """Durable execution state for TaskGraph runs.

    Persist authored TaskGraph, pending retry intent and execution facts atomically.
    A stable fingerprint verifies restored graph semantics. Retired nodes
    stay in runtime storage with active=0 so graph mutation never deletes attempt/session/
    result history merely because a semantic work unit is no longer material.
    """

    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        self._ensure_schema(conn)
        return conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS graph_runs (
                graph_run_id TEXT PRIMARY KEY,
                graph_fingerprint TEXT NOT NULL,
                graph_json TEXT,
                current_wave_id TEXT,
                revision INTEGER NOT NULL CHECK (revision >= 0),
                created_at_ns INTEGER NOT NULL,
                updated_at_ns INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS graph_node_states (
                graph_run_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                semantic_state TEXT NOT NULL
                    CHECK (semantic_state IN ('pending', 'satisfied', 'blocked', 'cancelled')),
                runtime_state TEXT NOT NULL
                    CHECK (runtime_state IN ('idle', 'running', 'settled', 'failed', 'blocked', 'awaiting_review')),
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                current_attempt_id TEXT,
                session_id TEXT,
                turn_id TEXT,
                updated_at_ns INTEGER NOT NULL,
                PRIMARY KEY (graph_run_id, node_id),
                FOREIGN KEY (graph_run_id) REFERENCES graph_runs(graph_run_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS graph_attempts (
                attempt_id TEXT PRIMARY KEY,
                graph_run_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                wave_id TEXT NOT NULL,
                attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
                runtime_state TEXT NOT NULL
                    CHECK (runtime_state IN ('running', 'settled', 'failed', 'blocked')),
                resume_session INTEGER NOT NULL
                    CHECK (resume_session IN (0, 1)),
                session_id TEXT,
                turn_id TEXT,
                result_json TEXT,
                created_at_ns INTEGER NOT NULL,
                updated_at_ns INTEGER NOT NULL,
                UNIQUE (graph_run_id, node_id, attempt_number),
                FOREIGN KEY (graph_run_id, node_id)
                    REFERENCES graph_node_states(graph_run_id, node_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS graph_retry_plans (
                graph_run_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                updated_at_ns INTEGER NOT NULL,
                PRIMARY KEY (graph_run_id, node_id),
                FOREIGN KEY (graph_run_id, node_id)
                    REFERENCES graph_node_states(graph_run_id, node_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS graph_attempts_run_wave_idx
                ON graph_attempts(graph_run_id, wave_id, attempt_number);
            CREATE INDEX IF NOT EXISTS graph_attempts_run_node_idx
                ON graph_attempts(graph_run_id, node_id, attempt_number);
            CREATE UNIQUE INDEX IF NOT EXISTS graph_attempts_one_running_per_node_idx
                ON graph_attempts(graph_run_id, node_id)
                WHERE runtime_state = 'running';
            """
        )
        columns = {
            row["name"] if isinstance(row, sqlite3.Row) else row[1]
            for row in conn.execute("PRAGMA table_info(graph_node_states)").fetchall()
        }
        run_columns = {
            row["name"] if isinstance(row, sqlite3.Row) else row[1]
            for row in conn.execute("PRAGMA table_info(graph_runs)").fetchall()
        }
        if "graph_json" not in run_columns:
            conn.execute("ALTER TABLE graph_runs ADD COLUMN graph_json TEXT")
        if "active" not in columns:
            conn.execute(
                "ALTER TABLE graph_node_states ADD COLUMN active INTEGER NOT NULL "
                "DEFAULT 1 CHECK (active IN (0, 1))"
            )

    def create_run(
        self,
        snapshot: GraphSnapshot,
        *,
        graph_run_id: str | None = None,
    ) -> str:
        states = _validated_states(snapshot)
        if any(state.runtime_state != "idle" for state in states.values()):
            raise ValueError("new graph run must start without active/runtime output state")

        run_id = _required_id(graph_run_id or new_graph_run_id(), "graph_run_id")
        fingerprint = _graph_fingerprint(snapshot.graph)
        authored_json = _graph_json(snapshot.graph)
        now = time.time_ns()
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO graph_runs("
                    "graph_run_id, graph_fingerprint, graph_json, current_wave_id, revision, "
                    "created_at_ns, updated_at_ns"
                    ") VALUES (?, ?, ?, NULL, 0, ?, ?)",
                    (run_id, fingerprint, authored_json, now, now),
                )
                conn.executemany(
                    "INSERT INTO graph_node_states("
                    "graph_run_id, node_id, semantic_state, runtime_state, active, "
                    "current_attempt_id, session_id, turn_id, updated_at_ns"
                    ") VALUES (?, ?, ?, ?, 1, NULL, NULL, NULL, ?)",
                    [
                        (
                            run_id,
                            state.node_id,
                            state.semantic_state,
                            state.runtime_state,
                            now,
                        )
                        for state in snapshot.node_states
                    ],
                )
        except sqlite3.IntegrityError as exc:
            raise RuntimeError(f"graph run already exists: {run_id!r}") from exc
        return run_id

    def get_run(self, graph_run_id: str) -> dict[str, Any] | None:
        run_id = _required_id(graph_run_id, "graph_run_id")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT graph_run_id, graph_fingerprint, graph_json, current_wave_id, revision, "
                "created_at_ns, updated_at_ns FROM graph_runs WHERE graph_run_id = ?",
                (run_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def load_graph(self, graph_run_id: str) -> TaskGraph:
        """Fail closed for pre-migration runs whose authored definition was never stored."""
        run = self.get_run(graph_run_id)
        if run is None:
            raise RuntimeError(f"unknown graph_run_id: {graph_run_id!r}")
        encoded = run["graph_json"]
        if not isinstance(encoded, str) or not encoded:
            raise RuntimeError(
                "persisted graph definition unavailable (legacy run); "
                "cannot safely reconstruct TaskGraph from its fingerprint"
            )
        graph = _graph_from_json(encoded)
        if _graph_fingerprint(graph) != run["graph_fingerprint"]:
            raise RuntimeError("persisted authored TaskGraph fingerprint mismatch")
        return graph

    def get_retry_plans(self, graph_run_id: str) -> dict[str, dict[str, Any]]:
        run_id = _required_id(graph_run_id, "graph_run_id")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT node_id, plan_json FROM graph_retry_plans WHERE graph_run_id = ?",
                (run_id,),
            ).fetchall()
        result = {}
        for row in rows:
            plan = json.loads(row["plan_json"])
            if not isinstance(plan, dict):
                raise RuntimeError("persisted retry plan must be an object")
            result[row["node_id"]] = plan
        return result

    def get_node(self, graph_run_id: str, node_id: str) -> dict[str, Any] | None:
        run_id = _required_id(graph_run_id, "graph_run_id")
        clean_node_id = _required_id(node_id, "node_id")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM graph_node_states WHERE graph_run_id = ? AND node_id = ?",
                (run_id, clean_node_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def load_snapshot_with_revision(
        self,
        graph_run_id: str,
        graph: TaskGraph,
    ) -> tuple[GraphSnapshot, int]:
        run_id = _required_id(graph_run_id, "graph_run_id")
        fingerprint = _graph_fingerprint(graph)

        with self._connect() as conn:
            run = conn.execute(
                "SELECT graph_fingerprint, revision FROM graph_runs WHERE graph_run_id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise RuntimeError(f"unknown graph_run_id: {run_id!r}")
            if run["graph_fingerprint"] != fingerprint:
                raise RuntimeError("TaskGraph semantics do not match persisted graph run")
            rows = conn.execute(
                "SELECT node_id, semantic_state, runtime_state "
                "FROM graph_node_states WHERE graph_run_id = ? AND active = 1",
                (run_id,),
            ).fetchall()

        persisted = {row["node_id"]: row for row in rows}
        graph_ids = [node.node_id for node in graph.nodes]
        graph_id_set = set(graph_ids)
        persisted_id_set = set(persisted)
        if graph_id_set != persisted_id_set:
            missing = sorted(graph_id_set - persisted_id_set)
            extra = sorted(persisted_id_set - graph_id_set)
            details: list[str] = []
            if missing:
                details.append("missing persisted states for " + ", ".join(missing))
            if extra:
                details.append("persisted states for unknown nodes " + ", ".join(extra))
            raise RuntimeError("graph run does not match TaskGraph: " + "; ".join(details))

        snapshot = GraphSnapshot(
            graph=graph,
            node_states=tuple(
                NodeState(
                    node_id=node_id,
                    semantic_state=persisted[node_id]["semantic_state"],
                    runtime_state=persisted[node_id]["runtime_state"],
                )
                for node_id in graph_ids
            ),
        )
        _validated_states(snapshot)
        return snapshot, int(run["revision"])

    def load_snapshot(self, graph_run_id: str, graph: TaskGraph) -> GraphSnapshot:
        snapshot, _ = self.load_snapshot_with_revision(graph_run_id, graph)
        return snapshot

    def _save_snapshot_in_transaction(
        self,
        conn: sqlite3.Connection,
        graph_run_id: str,
        snapshot: GraphSnapshot,
        *,
        expected_revision: int,
    ) -> None:
        run_id = _required_id(graph_run_id, "graph_run_id")
        clean_expected_revision = _required_revision(expected_revision)
        states = _validated_states(snapshot)
        fingerprint = _graph_fingerprint(snapshot.graph)
        now = time.time_ns()

        run = conn.execute(
            "SELECT graph_fingerprint, revision FROM graph_runs WHERE graph_run_id = ?",
            (run_id,),
        ).fetchone()
        if run is None:
            raise RuntimeError(f"unknown graph_run_id: {run_id!r}")
        if run["graph_fingerprint"] != fingerprint:
            raise RuntimeError("TaskGraph semantics do not match persisted graph run")
        current_revision = int(run["revision"])
        if current_revision != clean_expected_revision:
            raise RuntimeError(
                "stale graph snapshot revision: "
                f"expected {clean_expected_revision}, current {current_revision}"
            )

        rows = conn.execute(
            "SELECT node_id, current_attempt_id FROM graph_node_states "
            "WHERE graph_run_id = ? AND active = 1",
            (run_id,),
        ).fetchall()
        persisted_ids = {row["node_id"] for row in rows}
        if persisted_ids != set(states):
            raise RuntimeError("graph snapshot node set differs from persisted graph run")

        for row in rows:
            node_id = row["node_id"]
            current_attempt_id = row["current_attempt_id"]
            if current_attempt_id is None:
                if states[node_id].runtime_state == "running":
                    raise RuntimeError(
                        f"node {node_id!r} cannot be persisted as running without an active attempt"
                    )
                continue
            attempt = conn.execute(
                "SELECT runtime_state FROM graph_attempts WHERE attempt_id = ?",
                (current_attempt_id,),
            ).fetchone()
            if attempt is None:
                raise RuntimeError(
                    f"node {node_id!r} references missing attempt {current_attempt_id!r}"
                )
            if attempt["runtime_state"] == "running" and states[node_id].runtime_state != "running":
                raise RuntimeError(
                    f"node {node_id!r} has a running attempt and cannot leave runtime_state='running'"
                )
            if attempt["runtime_state"] != "running" and states[node_id].runtime_state == "running":
                raise RuntimeError(
                    f"node {node_id!r} cannot be persisted as running after its attempt completed"
                )

        conn.executemany(
            "UPDATE graph_node_states SET semantic_state = ?, runtime_state = ?, updated_at_ns = ? "
            "WHERE graph_run_id = ? AND node_id = ? AND active = 1",
            [
                (
                    state.semantic_state,
                    state.runtime_state,
                    now,
                    run_id,
                    state.node_id,
                )
                for state in snapshot.node_states
            ],
        )
        updated = conn.execute(
            "UPDATE graph_runs SET updated_at_ns = ?, revision = revision + 1 "
            "WHERE graph_run_id = ? AND revision = ?",
            (now, run_id, clean_expected_revision),
        )
        if updated.rowcount != 1:
            raise RuntimeError("graph run revision changed while saving snapshot")

    def save_snapshot(
        self,
        graph_run_id: str,
        snapshot: GraphSnapshot,
        *,
        expected_revision: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._save_snapshot_in_transaction(
                conn,
                graph_run_id,
                snapshot,
                expected_revision=expected_revision,
            )

    def save_snapshot_with_dispositions(
        self,
        graph_run_id: str,
        snapshot: GraphSnapshot,
        *,
        expected_revision: int,
        dispositions: tuple[dict[str, Any], ...],
        decision_node_ids: tuple[str, ...] = (),
        retry_plans: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Persist Lead decisions and pending retry intent in one SQLite transaction."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""CREATE TABLE IF NOT EXISTS lead_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                graph_run_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                action TEXT NOT NULL,
                reason TEXT NOT NULL,
                node_id TEXT,
                attempt_id TEXT,
                created_at_ns INTEGER NOT NULL
            )""")
            for decision in dispositions:
                if not isinstance(decision, dict):
                    raise ValueError("decision must be an object")
                if decision.get("action") == "accept" and not decision.get("turn_id"):
                    raise RuntimeError("accept requires exact captured turn")
            self._save_snapshot_in_transaction(
                conn, graph_run_id, snapshot, expected_revision=expected_revision
            )
            if retry_plans and not set(retry_plans).issubset(set(decision_node_ids)):
                raise ValueError("retry plans must belong to submitted decisions")
            for node_id in decision_node_ids:
                conn.execute(
                    "DELETE FROM graph_retry_plans WHERE graph_run_id = ? AND node_id = ?",
                    (graph_run_id, node_id),
                )
            for node_id, plan in (retry_plans or {}).items():
                conn.execute(
                    "INSERT INTO graph_retry_plans(graph_run_id, node_id, plan_json, updated_at_ns) "
                    "VALUES (?, ?, ?, ?)",
                    (graph_run_id, node_id, _result_json(plan), time.time_ns()),
                )
            for decision in dispositions:
                conn.execute(
                    "INSERT INTO lead_decisions(graph_run_id, turn_id, action, reason, node_id, attempt_id, created_at_ns) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (graph_run_id, decision["turn_id"], decision["action"],
                     decision["reason"], decision.get("node_id"),
                     decision.get("attempt_id"), time.time_ns())
                )

    def reconcile_graph(
        self,
        graph_run_id: str,
        previous_graph: TaskGraph,
        snapshot: GraphSnapshot,
        *,
        expected_revision: int,
        reset_node_ids: set[str],
    ) -> None:
        """Atomically replace the active semantic graph while retaining execution history."""

        run_id = _required_id(graph_run_id, "graph_run_id")
        clean_expected_revision = _required_revision(expected_revision)
        states = _validated_states(snapshot)
        previous_fingerprint = _graph_fingerprint(previous_graph)
        next_fingerprint = _graph_fingerprint(snapshot.graph)
        next_authored_json = _graph_json(snapshot.graph)
        previous_ids = {node.node_id for node in previous_graph.nodes}
        next_ids = set(states)
        reset_ids = set(reset_node_ids)
        unknown_reset_ids = sorted(reset_ids - next_ids)
        if unknown_reset_ids:
            raise ValueError(
                "reset_node_ids contains nodes outside the next TaskGraph: "
                + ", ".join(unknown_reset_ids)
            )
        now = time.time_ns()

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute(
                "SELECT graph_fingerprint, current_wave_id, revision "
                "FROM graph_runs WHERE graph_run_id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise RuntimeError(f"unknown graph_run_id: {run_id!r}")
            if run["graph_fingerprint"] != previous_fingerprint:
                raise RuntimeError("current TaskGraph semantics do not match persisted graph run")
            current_revision = int(run["revision"])
            if current_revision != clean_expected_revision:
                raise RuntimeError(
                    "stale graph snapshot revision: "
                    f"expected {clean_expected_revision}, current {current_revision}"
                )
            if run["current_wave_id"] is not None:
                raise RuntimeError("graph reconciliation cannot run while a wave is active")
            running = conn.execute(
                "SELECT COUNT(*) FROM graph_attempts "
                "WHERE graph_run_id = ? AND runtime_state = 'running'",
                (run_id,),
            ).fetchone()[0]
            if running:
                raise RuntimeError("graph reconciliation cannot run while node attempts are active")

            active_rows = conn.execute(
                "SELECT node_id FROM graph_node_states "
                "WHERE graph_run_id = ? AND active = 1",
                (run_id,),
            ).fetchall()
            persisted_active_ids = {row["node_id"] for row in active_rows}
            if persisted_active_ids != previous_ids:
                raise RuntimeError("active persisted node set differs from current TaskGraph")

            existing_rows = conn.execute(
                "SELECT node_id FROM graph_node_states WHERE graph_run_id = ?",
                (run_id,),
            ).fetchall()
            existing_ids = {row["node_id"] for row in existing_rows}

            removed_ids = previous_ids - next_ids
            if removed_ids:
                conn.executemany(
                    "UPDATE graph_node_states SET active = 0, semantic_state = 'cancelled', "
                    "runtime_state = 'idle', updated_at_ns = ? "
                    "WHERE graph_run_id = ? AND node_id = ? AND active = 1",
                    [(now, run_id, node_id) for node_id in sorted(removed_ids)],
                )

            for state in snapshot.node_states:
                if state.node_id in existing_ids:
                    if state.node_id in reset_ids:
                        conn.execute(
                            "UPDATE graph_node_states SET active = 1, semantic_state = ?, "
                            "runtime_state = ?, current_attempt_id = NULL, "
                            "session_id = NULL, turn_id = NULL, updated_at_ns = ? "
                            "WHERE graph_run_id = ? AND node_id = ?",
                            (
                                state.semantic_state,
                                state.runtime_state,
                                now,
                                run_id,
                                state.node_id,
                            ),
                        )
                    else:
                        conn.execute(
                            "UPDATE graph_node_states SET active = 1, semantic_state = ?, "
                            "runtime_state = ?, updated_at_ns = ? "
                            "WHERE graph_run_id = ? AND node_id = ?",
                            (
                                state.semantic_state,
                                state.runtime_state,
                                now,
                                run_id,
                                state.node_id,
                            ),
                        )
                else:
                    conn.execute(
                        "INSERT INTO graph_node_states("
                        "graph_run_id, node_id, semantic_state, runtime_state, active, "
                        "current_attempt_id, session_id, turn_id, updated_at_ns"
                        ") VALUES (?, ?, ?, ?, 1, NULL, NULL, NULL, ?)",
                        (
                            run_id,
                            state.node_id,
                            state.semantic_state,
                            state.runtime_state,
                            now,
                        ),
                    )

            conn.execute(
                "DELETE FROM graph_retry_plans WHERE graph_run_id = ? AND node_id IN "
                "(SELECT node_id FROM graph_node_states "
                "WHERE graph_run_id = ? AND active = 0)",
                (run_id, run_id),
            )
            if reset_ids:
                conn.executemany(
                    "DELETE FROM graph_retry_plans WHERE graph_run_id = ? AND node_id = ?",
                    [(run_id, node_id) for node_id in reset_ids],
                )
            updated = conn.execute(
                "UPDATE graph_runs SET graph_fingerprint = ?, graph_json = ?, "
                "updated_at_ns = ?, revision = revision + 1 "
                "WHERE graph_run_id = ? AND revision = ?",
                (next_fingerprint, next_authored_json, now, run_id, clean_expected_revision),
            )
            if updated.rowcount != 1:
                raise RuntimeError("graph run revision changed while reconciling graph")

    def start_attempt(
        self,
        graph_run_id: str,
        node_id: str,
        wave_id: str,
        *,
        resume_session: bool = False,
        session_id: str | None = None,
        attempt_id: str | None = None,
        retry_plan: dict[str, Any] | None = None,
    ) -> str:
        run_id = _required_id(graph_run_id, "graph_run_id")
        clean_node_id = _required_id(node_id, "node_id")
        clean_wave_id = _required_id(wave_id, "wave_id")
        clean_attempt_id = _required_id(attempt_id or new_attempt_id(), "attempt_id")
        if not isinstance(resume_session, bool):
            raise ValueError("resume_session must be a boolean")
        clean_session_id = _optional_id(session_id, "session_id")
        if resume_session and clean_session_id is None:
            raise ValueError("resume_session requires an existing session_id")
        if not resume_session and clean_session_id is not None:
            raise ValueError("fresh START must not pre-bind a session_id")

        now = time.time_ns()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute(
                "SELECT current_wave_id FROM graph_runs WHERE graph_run_id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise RuntimeError(f"unknown graph_run_id: {run_id!r}")
            if run["current_wave_id"] is None:
                reused_wave = conn.execute(
                    "SELECT 1 FROM graph_attempts WHERE graph_run_id = ? AND wave_id = ? LIMIT 1",
                    (run_id, clean_wave_id),
                ).fetchone()
                if reused_wave is not None:
                    raise RuntimeError(
                        f"wave_id {clean_wave_id!r} has already been used and closed"
                    )
            elif run["current_wave_id"] != clean_wave_id:
                raise RuntimeError(
                    "graph run already has a different active wave: "
                    f"{run['current_wave_id']!r}"
                )

            persisted_retry = conn.execute(
                "SELECT plan_json FROM graph_retry_plans "
                "WHERE graph_run_id = ? AND node_id = ?",
                (run_id, clean_node_id),
            ).fetchone()
            expected_plan = _result_json(retry_plan) if retry_plan is not None else None
            actual_plan = persisted_retry["plan_json"] if persisted_retry is not None else None
            if expected_plan != actual_plan:
                raise RuntimeError(
                    f"retry plan changed for node {clean_node_id!r} before attempt claim"
                )

            node = conn.execute(
                "SELECT semantic_state, runtime_state, session_id, active "
                "FROM graph_node_states WHERE graph_run_id = ? AND node_id = ?",
                (run_id, clean_node_id),
            ).fetchone()
            if node is None or not bool(node["active"]):
                raise RuntimeError(f"unknown active graph node for run: {clean_node_id!r}")
            if node["semantic_state"] != "pending" or node["runtime_state"] != "idle":
                raise RuntimeError(
                    f"node {clean_node_id!r} is not pending+idle and cannot start an attempt"
                )
            if resume_session:
                if node["session_id"] is None:
                    raise RuntimeError(
                        f"node {clean_node_id!r} has no previously bound session to resume"
                    )
                if clean_session_id != node["session_id"]:
                    raise RuntimeError(
                        f"resume session does not match node {clean_node_id!r} latest session"
                    )

            attempt_number = int(
                conn.execute(
                    "SELECT COALESCE(MAX(attempt_number), 0) + 1 "
                    "FROM graph_attempts WHERE graph_run_id = ? AND node_id = ?",
                    (run_id, clean_node_id),
                ).fetchone()[0]
            )
            try:
                conn.execute(
                    "INSERT INTO graph_attempts("
                    "attempt_id, graph_run_id, node_id, wave_id, attempt_number, "
                    "runtime_state, resume_session, session_id, turn_id, result_json, "
                    "created_at_ns, updated_at_ns"
                    ") VALUES (?, ?, ?, ?, ?, 'running', ?, ?, NULL, NULL, ?, ?)",
                    (
                        clean_attempt_id,
                        run_id,
                        clean_node_id,
                        clean_wave_id,
                        attempt_number,
                        int(resume_session),
                        clean_session_id,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise RuntimeError(
                    f"attempt identity conflicts with existing runtime state: {clean_attempt_id!r}"
                ) from exc

            conn.execute(
                "UPDATE graph_node_states SET runtime_state = 'running', "
                "current_attempt_id = ?, session_id = ?, turn_id = NULL, updated_at_ns = ? "
                "WHERE graph_run_id = ? AND node_id = ? AND active = 1",
                (clean_attempt_id, clean_session_id, now, run_id, clean_node_id),
            )
            conn.execute(
                "DELETE FROM graph_retry_plans WHERE graph_run_id = ? AND node_id = ?",
                (run_id, clean_node_id),
            )
            conn.execute(
                "UPDATE graph_runs SET current_wave_id = ?, updated_at_ns = ?, "
                "revision = revision + 1 WHERE graph_run_id = ?",
                (clean_wave_id, now, run_id),
            )
        return clean_attempt_id

    def bind_attempt_session(self, attempt_id: str, session_id: str) -> None:
        clean_attempt_id = _required_id(attempt_id, "attempt_id")
        clean_session_id = _required_id(session_id, "session_id")
        now = time.time_ns()

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            attempt = conn.execute(
                "SELECT graph_run_id, node_id, runtime_state, session_id "
                "FROM graph_attempts WHERE attempt_id = ?",
                (clean_attempt_id,),
            ).fetchone()
            if attempt is None:
                raise RuntimeError(f"unknown graph attempt: {clean_attempt_id!r}")
            if attempt["runtime_state"] != "running":
                raise RuntimeError("session can only be bound while an attempt is running")
            if attempt["session_id"] not in {None, clean_session_id}:
                raise RuntimeError("attempt session identity cannot change")

            node = conn.execute(
                "SELECT current_attempt_id FROM graph_node_states "
                "WHERE graph_run_id = ? AND node_id = ? AND active = 1",
                (attempt["graph_run_id"], attempt["node_id"]),
            ).fetchone()
            if node is None or node["current_attempt_id"] != clean_attempt_id:
                raise RuntimeError("attempt is no longer the current node execution")

            conn.execute(
                "UPDATE graph_attempts SET session_id = ?, updated_at_ns = ? "
                "WHERE attempt_id = ?",
                (clean_session_id, now, clean_attempt_id),
            )
            conn.execute(
                "UPDATE graph_node_states SET session_id = ?, updated_at_ns = ? "
                "WHERE graph_run_id = ? AND node_id = ? AND active = 1",
                (
                    clean_session_id,
                    now,
                    attempt["graph_run_id"],
                    attempt["node_id"],
                ),
            )
            conn.execute(
                "UPDATE graph_runs SET updated_at_ns = ?, revision = revision + 1 "
                "WHERE graph_run_id = ?",
                (now, attempt["graph_run_id"]),
            )

    def finish_attempt(
        self,
        attempt_id: str,
        *,
        runtime_state: str,
        result: dict[str, Any],
        session_id: str | None = None,
        turn_id: str | None = None,
    ) -> None:
        clean_attempt_id = _required_id(attempt_id, "attempt_id")
        if runtime_state not in ATTEMPT_TERMINAL_STATES:
            raise ValueError(f"unsupported terminal attempt state: {runtime_state!r}")
        clean_session_id = _optional_id(session_id, "session_id")
        clean_turn_id = _optional_id(turn_id, "turn_id")
        encoded_result = _result_json(result)
        now = time.time_ns()

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            attempt = conn.execute(
                "SELECT graph_run_id, node_id, runtime_state, session_id "
                "FROM graph_attempts WHERE attempt_id = ?",
                (clean_attempt_id,),
            ).fetchone()
            if attempt is None:
                raise RuntimeError(f"unknown graph attempt: {clean_attempt_id!r}")
            if attempt["runtime_state"] != "running":
                raise RuntimeError("graph attempt is already terminal")
            if attempt["session_id"] is not None:
                if clean_session_id is None:
                    clean_session_id = attempt["session_id"]
                elif clean_session_id != attempt["session_id"]:
                    raise RuntimeError("attempt session identity cannot change")

            node = conn.execute(
                "SELECT current_attempt_id FROM graph_node_states "
                "WHERE graph_run_id = ? AND node_id = ? AND active = 1",
                (attempt["graph_run_id"], attempt["node_id"]),
            ).fetchone()
            if node is None or node["current_attempt_id"] != clean_attempt_id:
                raise RuntimeError("attempt is no longer the current node execution")

            conn.execute(
                "UPDATE graph_attempts SET runtime_state = ?, session_id = ?, turn_id = ?, "
                "result_json = ?, updated_at_ns = ? WHERE attempt_id = ?",
                (
                    runtime_state,
                    clean_session_id,
                    clean_turn_id,
                    encoded_result,
                    now,
                    clean_attempt_id,
                ),
            )
            conn.execute(
                "UPDATE graph_node_states SET runtime_state = ?, session_id = ?, turn_id = ?, "
                "updated_at_ns = ? WHERE graph_run_id = ? AND node_id = ? AND active = 1",
                (
                    runtime_state,
                    clean_session_id,
                    clean_turn_id,
                    now,
                    attempt["graph_run_id"],
                    attempt["node_id"],
                ),
            )
            conn.execute(
                "UPDATE graph_runs SET updated_at_ns = ?, revision = revision + 1 "
                "WHERE graph_run_id = ?",
                (now, attempt["graph_run_id"]),
            )

    def recover_quiescent_wave(self, graph_run_id: str) -> bool:
        """Close a wave with no running attempts after an interrupted coordinator.

        Never infer worker termination from process restart: an attempt still marked
        running keeps its wave locked for external recovery.
        """
        run_id = _required_id(graph_run_id, "graph_run_id")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT current_wave_id FROM graph_runs WHERE graph_run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"unknown graph_run_id: {run_id!r}")
            wave_id = row["current_wave_id"]
            if wave_id is None:
                return False
            active = conn.execute(
                "SELECT COUNT(*) FROM graph_attempts "
                "WHERE graph_run_id = ? AND wave_id = ? AND runtime_state = 'running'",
                (run_id, wave_id),
            ).fetchone()[0]
            if active:
                return False
            conn.execute(
                "UPDATE graph_runs SET current_wave_id = NULL, updated_at_ns = ?, "
                "revision = revision + 1 WHERE graph_run_id = ? AND current_wave_id = ?",
                (time.time_ns(), run_id, wave_id),
            )
            return True

    def close_wave(self, graph_run_id: str, wave_id: str) -> None:
        run_id = _required_id(graph_run_id, "graph_run_id")
        clean_wave_id = _required_id(wave_id, "wave_id")
        now = time.time_ns()

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute(
                "SELECT current_wave_id FROM graph_runs WHERE graph_run_id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise RuntimeError(f"unknown graph_run_id: {run_id!r}")
            if run["current_wave_id"] is None:
                # Another reader may already have completed safe quiescent recovery.
                prior = conn.execute(
                    "SELECT COUNT(*) FROM graph_attempts "
                    "WHERE graph_run_id = ? AND wave_id = ?",
                    (run_id, clean_wave_id),
                ).fetchone()[0]
                if prior:
                    active_prior = conn.execute(
                        "SELECT COUNT(*) FROM graph_attempts "
                        "WHERE graph_run_id = ? AND wave_id = ? AND runtime_state = 'running'",
                        (run_id, clean_wave_id),
                    ).fetchone()[0]
                    if not active_prior:
                        return
            if run["current_wave_id"] != clean_wave_id:
                raise RuntimeError(
                    f"wave {clean_wave_id!r} is not the current wave for graph run"
                )
            active = conn.execute(
                "SELECT COUNT(*) FROM graph_attempts "
                "WHERE graph_run_id = ? AND wave_id = ? AND runtime_state = 'running'",
                (run_id, clean_wave_id),
            ).fetchone()[0]
            if active:
                raise RuntimeError("cannot close a wave while node attempts are running")
            conn.execute(
                "UPDATE graph_runs SET current_wave_id = NULL, updated_at_ns = ?, "
                "revision = revision + 1 WHERE graph_run_id = ?",
                (now, run_id),
            )

    def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        clean_attempt_id = _required_id(attempt_id, "attempt_id")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM graph_attempts WHERE attempt_id = ?",
                (clean_attempt_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["resume_session"] = bool(result["resume_session"])
        raw_result = result.pop("result_json")
        result["result"] = json.loads(raw_result) if raw_result is not None else None
        return result

    def list_attempts(self, graph_run_id: str, node_id: str) -> list[dict[str, Any]]:
        run_id = _required_id(graph_run_id, "graph_run_id")
        clean_node_id = _required_id(node_id, "node_id")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM graph_attempts WHERE graph_run_id = ? AND node_id = ? "
                "ORDER BY attempt_number ASC",
                (run_id, clean_node_id),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["resume_session"] = bool(item["resume_session"])
            raw_result = item.pop("result_json")
            item["result"] = json.loads(raw_result) if raw_result is not None else None
            result.append(item)
        return result
