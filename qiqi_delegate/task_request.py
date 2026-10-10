"""Persisted, source-optional task intent and readiness for evidence-grounded delegation.

This is a structural/provenance gate, not a semantic correctness oracle.
Legacy graph runs without a request binding intentionally remain unassessed.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path, PurePosixPath

from qiqi_delegate.core import CAPTURE_MAX_RESPONSE_CHARS
from typing import Any, Callable

MAX_SOURCE_BYTES = 100_000
MAX_CONTEXT_SOURCES = 16
MAX_REQUEST_CHARS = 100_000
DECISIONS = {"direct", "targeted_discovery", "full_discovery", "blocked"}


def _process_start_token(pid: int) -> str | None:
    """Linux process birth token prevents PID reuse from retaining stale claims.

    On systems without /proc, PID liveness remains a best-effort fallback.
    """
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(") ", 1)[1].split()
        return fields[19] if len(fields) > 19 else None
    except (OSError, IndexError, ValueError):
        return None


def _owner_alive(pid: int | None, token: str | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Another owner's process exists but is not inspectable.
    except OSError:
        return True  # Fail conservatively on unknown permission/OS errors.
    actual_token = _process_start_token(pid)
    return not (token is not None and actual_token is not None and token != actual_token)


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonempty text")
    return value.strip()


def _checked_relative_file(root: Path, path: str) -> Path:
    path = _text(path, "source path")
    if "\\" in path:
        raise ValueError("source path must use forward slashes")
    parts = PurePosixPath(path).parts
    if (PurePosixPath(path).is_absolute() or not parts or
            any(p in {".", "..", ""} for p in parts) or
            any(p.startswith(".") for p in parts)):
        raise ValueError("source path traversal/hidden paths are not allowed")
    current = root.resolve()
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("source symlink not allowed")
    resolved = current.resolve()
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise ValueError("source file missing or outside registered root")
    return resolved


def _read_file(root: Path, path: str) -> tuple[str, str]:
    target = _checked_relative_file(root, path)
    with target.open("rb") as handle:
        data = handle.read(MAX_SOURCE_BYTES + 1)
    if len(data) > MAX_SOURCE_BYTES:
        raise ValueError("source file exceeds maximum size")
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("source must be UTF-8 text") from exc
    return decoded, hashlib.sha256(data).hexdigest()


class TaskRequestStore:
    def __init__(self, database: Path, workspace: Path,
                 repos: Callable[[], dict[str, Path]]):
        self.database, self.workspace, self.repos = database, workspace, repos
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS task_requests (
                    request_id TEXT PRIMARY KEY,
                    user_request TEXT NOT NULL,
                    sources_json TEXT NOT NULL,
                    assessment_json TEXT,
                    revision INTEGER NOT NULL,
                    created_at_ns INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_graph_bindings (
                    graph_run_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    request_revision INTEGER NOT NULL,
                    requirement_map_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_discoveries (
                    discovery_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    repository_names_json TEXT NOT NULL,
                    questions_json TEXT NOT NULL,
                    route TEXT NOT NULL,
                    turn_id TEXT,
                    state TEXT NOT NULL,
                    detail TEXT,
                    created_at_ns INTEGER NOT NULL,
                    owner_pid INTEGER,
                    owner_start_token TEXT
                );
            """)
            # Serialize the read/ALTER cycle across concurrently starting MCP
            # processes. executescript() commits before returning, so the lock
            # must be acquired AFTER table creation but BEFORE PRAGMA.
            db.execute("BEGIN IMMEDIATE")
            # Backward-compatible migration of databases written by Issue #6
            # before Discovery process ownership was tracked.
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(task_discoveries)")
            }
            for name, field_type in (
                ("owner_pid", "INTEGER"),
                ("owner_start_token", "TEXT"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE task_discoveries ADD COLUMN {name} {field_type}")
        self.recover_abandoned_discoveries()

    def _recover_abandoned_locked(
        self, db: sqlite3.Connection, request_id: str | None = None,
        *, operator_confirmed_discovery_id: str | None = None,
    ) -> int:
        """Reconcile dead-owner reservations with native captures atomically.

        Runtime assigns the native turn ID to the Discovery BEFORE launching
        the Peer and saves its turn into SQLite before returning. A crash after
        the turn is captured but before append() must not lose the evidence.
        Recovery treats a committed settled turn as a durable source, while
        leaving claims owned by live processes untouched.
        """
        sql = (
            "SELECT discovery_id, request_id, state, turn_id, "
            "repository_names_json, route, owner_pid, owner_start_token "
            "FROM task_discoveries WHERE state IN ('requested', 'attaching')"
        )
        params: tuple[str, ...] = ()
        if request_id is not None:
            sql += " AND request_id=?"
            params = (request_id,)
        if operator_confirmed_discovery_id is not None:
            sql += " AND discovery_id=?"
            params += (operator_confirmed_discovery_id,)
        recovered = 0
        for row in db.execute(sql, params).fetchall():
            # An ownerless legacy row is *not* evidence that the old worker
            # stopped: a pre-migration MCP may still be running Discovery.
            # Only explicit operator recovery can release such reservations.
            if row["owner_pid"] is None:
                if row["discovery_id"] != operator_confirmed_discovery_id:
                    continue
            elif _owner_alive(row["owner_pid"], row["owner_start_token"]):
                continue

            if row["state"] == "attaching":
                # The source append and attaching state were one transaction.
                update = db.execute(
                    "UPDATE task_discoveries SET state='settled', detail=? "
                    "WHERE discovery_id=? AND state='attaching'",
                    ("Owner exited after captured evidence was attached; "
                     "finalized durable result", row["discovery_id"]),
                )
                recovered += update.rowcount
                continue

            turn = (db.execute(
                "SELECT turn_id, repository, route, state, response "
                "FROM turns WHERE turn_id=?",
                (row["turn_id"],),
            ).fetchone() if row["turn_id"] else None)
            valid_capture = (
                turn is not None
                and turn["state"] == "settled"
                and isinstance(turn["response"], str)
                and bool(turn["response"])
                and len(turn["response"]) <= CAPTURE_MAX_RESPONSE_CHARS
                and turn["route"] == row["route"]
                and turn["repository"] in json.loads(row["repository_names_json"])
            )
            if valid_capture:
                request = db.execute(
                    "SELECT revision, sources_json FROM task_requests "
                    "WHERE request_id=?",
                    (row["request_id"],),
                ).fetchone()
                if request is not None:
                    sources = json.loads(request["sources_json"])
                    already_present = any(
                        source.get("kind") == "peer_turn"
                        and source.get("turn_id") == row["turn_id"]
                        for source in sources
                    )
                    if already_present or len(sources) < MAX_CONTEXT_SOURCES:
                        if not already_present:
                            response = turn["response"]
                            sources.append({
                                "id": "source:" + uuid.uuid4().hex,
                                "kind": "peer_turn",
                                "turn_id": row["turn_id"],
                                "repository": turn["repository"],
                                "verification": "peer_observed",
                                "content": response,
                                "sha256": hashlib.sha256(
                                    response.encode("utf-8")
                                ).hexdigest(),
                                "captured_at_ns": time.time_ns(),
                            })
                            db.execute(
                                "UPDATE task_requests "
                                "SET sources_json=?, assessment_json=NULL, "
                                "revision=revision+1 WHERE request_id=?",
                                (json.dumps(sources, ensure_ascii=False),
                                 row["request_id"]),
                            )
                        done = db.execute(
                            "UPDATE task_discoveries "
                            "SET state='settled', detail=? "
                            "WHERE discovery_id=? AND state='requested'",
                            ("Recovered native captured turn after owner exit",
                             row["discovery_id"]),
                        )
                        recovered += done.rowcount
                        continue
                # The turn remains addressable using the persisted turn_id even
                # if an unexpected source capacity or request problem occurs.
                detail = ("Captured result exists but could not be attached; "
                          "use the recorded turn_id with a new task request")
                state = "failed"
            else:
                detail = ("Discovery owner process exited before a recoverable "
                          "settled native capture; abandoned source reservation recovered")
                state = "interrupted"

            updated = db.execute(
                "UPDATE task_discoveries SET state=?, detail=? "
                "WHERE discovery_id=? AND state='requested'",
                (state, detail, row["discovery_id"]),
            )
            recovered += updated.rowcount
        return recovered

    def recover_abandoned_discoveries(self) -> int:
        """Recover abandoned reservations after a restart without touching live owners."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._recover_abandoned_locked(db)

    def inspect_discovery(self, discovery_id: str) -> dict[str, Any]:
        """Operator inspection of an exact persisted Discovery, without side effects."""
        with self._connect() as db:
            row = db.execute(
                "SELECT discovery_id, request_id, state, turn_id, "
                "owner_pid, owner_start_token, detail "
                "FROM task_discoveries WHERE discovery_id=?",
                (_text(discovery_id, "discovery_id"),),
            ).fetchone()
        if row is None:
            raise ValueError("unknown Discovery ID")
        return dict(row)

    def recover_ownerless_discovery(
        self, discovery_id: str, *, worker_termination_confirmed: bool,
    ) -> dict[str, Any]:
        """Operator-only recovery of one ownerless legacy Discovery.

        Caller must verify the pre-migration MCP worker/Herdr task has stopped.
        A successful native capture is preserved by normal recovery logic.
        """
        if worker_termination_confirmed is not True:
            raise ValueError(
                "verify the legacy Discovery worker has terminated before "
                "passing --worker-termination-confirmed"
            )
        discovery_id = _text(discovery_id, "discovery_id")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state, owner_pid FROM task_discoveries "
                "WHERE discovery_id=?", (discovery_id,),
            ).fetchone()
            if (row is None or row["owner_pid"] is not None
                    or row["state"] not in {"requested", "attaching"}):
                raise ValueError(
                    "Discovery must be an ownerless in-flight legacy reservation"
                )
            recovered = self._recover_abandoned_locked(
                db, operator_confirmed_discovery_id=discovery_id,
            )
            if recovered != 1:
                raise RuntimeError("legacy Discovery recovery failed; no state changed")
            db.execute("""
                CREATE TABLE IF NOT EXISTS task_discovery_recovery_audit (
                    recovery_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    discovery_id TEXT NOT NULL,
                    worker_termination_confirmed INTEGER NOT NULL
                        CHECK (worker_termination_confirmed=1),
                    recovered_at_ns INTEGER NOT NULL
                )
            """)
            db.execute(
                "INSERT INTO task_discovery_recovery_audit "
                "(discovery_id, worker_termination_confirmed, recovered_at_ns) "
                "VALUES (?, 1, ?)",
                (discovery_id, time.time_ns()),
            )
        return self.inspect_discovery(discovery_id)

    def _connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _resolve_source(self, raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("source must be an object")
        kind = _text(raw.get("kind"), "source.kind")
        if kind not in {"inline", "repo_file", "workspace_file", "peer_turn",
                        "accepted_graph_node"}:
            raise ValueError(f"unsupported source kind: {kind}")
        permitted = {
            "inline": {"kind", "text", "label"},
            "repo_file": {"kind", "repository", "path"},
            "workspace_file": {"kind", "path"},
            "peer_turn": {"kind", "turn_id"},
            "accepted_graph_node": {"kind", "graph_run_id", "node_id"},
        }[kind]
        if set(raw) - permitted:
            raise ValueError(f"unexpected {kind} source fields: {sorted(set(raw)-permitted)}")
        entry: dict[str, Any] = {"id": "source:" + uuid.uuid4().hex, "kind": kind}
        if kind == "inline":
            data = raw.get("text")
            if not isinstance(data, str) or not data.strip():
                raise ValueError("inline source text must be nonempty")
            entry["label"] = _text(raw.get("label", "inline"), "inline label")
            entry["verification"] = "reported"
        elif kind in {"repo_file", "workspace_file"}:
            path = _text(raw.get("path"), "source.path")
            if kind == "repo_file":
                repository = _text(raw.get("repository"), "source.repository")
                roots = self.repos()
                if repository not in roots:
                    raise ValueError(f"unknown repository: {repository}")
                root = roots[repository]
                entry["repository"] = repository
            else:
                root = self.workspace
                target = _checked_relative_file(root, path)
                if any(target.is_relative_to(p) for p in self.repos().values()):
                    raise ValueError("workspace_file cannot bypass registered repository boundary")
            data, digest = _read_file(root, path)
            entry["path"] = path
            entry["sha256"] = digest
            entry["verification"] = "reported"
        elif kind == "accepted_graph_node":
            from qiqi_delegate.task_graph_store import GraphRuntimeStore
            graph_id = _text(raw.get("graph_run_id"), "source.graph_run_id")
            node_id = _text(raw.get("node_id"), "source.node_id")
            graph_store = GraphRuntimeStore(self.database)
            node = graph_store.get_node(graph_id, node_id)
            if not node or node.get("semantic_state") != "satisfied":
                raise ValueError("accepted graph node source requires semantic ACCEPT")
            attempt_id = node.get("current_attempt_id")
            attempt = graph_store.get_attempt(attempt_id) if attempt_id else None
            result = (attempt or {}).get("result")
            if (not isinstance(result, dict) or result.get("state") != "settled"
                    or not isinstance(result.get("agent_response"), str)
                    or not result["agent_response"].strip()):
                raise ValueError("accepted graph source lacks captured evidence")
            data = result["agent_response"]
            entry.update(graph_run_id=graph_id, node_id=node_id,
                         attempt_id=attempt_id, verification="accepted_peer_evidence")
        else:
            turn_id = _text(raw.get("turn_id"), "source.turn_id")
            with self._connect() as db:
                turn = db.execute(
                    "SELECT response, state, repository FROM turns WHERE turn_id=?",
                    (turn_id,),
                ).fetchone()
            if turn is None or turn["state"] != "settled" or not turn["response"]:
                raise ValueError("source peer turn must be a settled captured response")
            data = turn["response"]
            entry.update(turn_id=turn_id, repository=turn["repository"],
                         verification="peer_observed")
        # A native Peer capture can be larger than the 100k-byte document
        # limit. Preserve it in the task request so the Lead can inspect the
        # complete Discovery result and reassess, even when the result cannot
        # fit into a subsequent 100k-character implementation TaskPacket.
        # Do not silently truncate captured evidence.
        if kind in {"peer_turn", "accepted_graph_node"}:
            if len(data) > CAPTURE_MAX_RESPONSE_CHARS:
                raise ValueError("captured peer source exceeds native capture limit")
        elif len(data.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise ValueError("source content exceeds maximum size")
        entry["content"] = data
        entry["sha256"] = hashlib.sha256(data.encode("utf-8")).hexdigest()
        entry["captured_at_ns"] = time.time_ns()
        return entry

    def create(self, user_request: str, sources: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if not isinstance(user_request, str) or not user_request.strip() or len(user_request) > MAX_REQUEST_CHARS:
            raise ValueError("user_request must be nonempty and within limit")
        if sources is not None and (not isinstance(sources, list) or len(sources) > MAX_CONTEXT_SOURCES):
            raise ValueError(f"sources must be a list of at most {MAX_CONTEXT_SOURCES} entries")
        resolved = [self._resolve_source(s) for s in (sources or [])]
        request_id = str(uuid.uuid4())
        with self._connect() as db:
            db.execute(
                "INSERT INTO task_requests VALUES (?, ?, ?, NULL, 1, ?)",
                (request_id, user_request, json.dumps(resolved, ensure_ascii=False),
                 time.time_ns()),
            )
        return self.get(request_id)

    def get(self, request_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM task_requests WHERE request_id=?",
                             (_text(request_id, "request_id"),)).fetchone()
        if row is None:
            raise ValueError("unknown task request")
        sources = json.loads(row["sources_json"])
        stale = []
        for source in sources:
            if source["kind"] not in {"repo_file", "workspace_file"}:
                continue
            try:
                root = (self.repos()[source["repository"]]
                        if source["kind"] == "repo_file" else self.workspace)
                _, current_digest = _read_file(root, source["path"])
                if current_digest != source["sha256"]:
                    stale.append(source["id"])
            except (ValueError, KeyError, OSError):
                stale.append(source["id"])
        with self._connect() as db:
            discovery_rows = db.execute(
                "SELECT * FROM task_discoveries WHERE request_id=? ORDER BY created_at_ns",
                (request_id,),
            ).fetchall()
        discoveries = [
            {
                "discovery_id": item["discovery_id"], "mode": item["mode"],
                "repository_names": json.loads(item["repository_names_json"]),
                "questions": json.loads(item["questions_json"]),
                "route": item["route"], "state": item["state"],
                "turn_id": item["turn_id"], "detail": item["detail"],
            }
            for item in discovery_rows
        ]
        return {
            "request_id": row["request_id"], "user_request": row["user_request"],
            "revision": row["revision"], "sources": sources,
            "stale_sources": stale, "discoveries": discoveries,
            "assessment": json.loads(row["assessment_json"]) if row["assessment_json"] else None,
        }

    def _write(self, request_id: str, expected_revision: int, *,
               sources: list[dict[str, Any]] | None = None,
               assessment: dict[str, Any] | None = None) -> dict[str, Any]:
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
            raise ValueError("expected_revision must be positive integer")
        with self._connect() as db:
            row = db.execute("SELECT revision FROM task_requests WHERE request_id=?",
                             (request_id,)).fetchone()
            if row is None or row["revision"] != expected_revision:
                raise RuntimeError("stale task context revision")
            updated = db.execute(
                "UPDATE task_requests SET revision=revision+1, "
                "sources_json=COALESCE(?, sources_json), assessment_json=? "
                "WHERE request_id=? AND revision=?",
                (json.dumps(sources, ensure_ascii=False) if sources is not None else None,
                 json.dumps(assessment, ensure_ascii=False) if assessment is not None else None,
                 request_id, expected_revision),
            )
            if updated.rowcount != 1:
                raise RuntimeError("concurrent task context modification")
        return self.get(request_id)

    def append(self, request_id: str, expected_revision: int,
               source: dict[str, Any], *,
               discovery_id: str | None = None) -> dict[str, Any]:
        """Append a source without consuming a slot reserved by running Discovery.

        A Discovery result must use its own active reservation; other writers
        cannot occupy that reserved slot. Check and update atomically so
        concurrent sessions cannot overfill the source list.
        """
        resolved = self._resolve_source(source)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Recovery may attach a native capture, increment revision and
            # invalidate readiness. Never base an append on pre-recovery state.
            recovered = self._recover_abandoned_locked(db, request_id)
            if recovered:
                # Recovery is authoritative even if the caller supplied an
                # outdated revision. Commit it before a stale-request error
                # can roll back its durable native capture.
                db.commit()
                db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT sources_json, revision FROM task_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None or row["revision"] != expected_revision:
                raise RuntimeError("stale task context revision")
            sources = json.loads(row["sources_json"])
            pending = db.execute(
                "SELECT discovery_id FROM task_discoveries "
                "WHERE request_id=? AND state='requested'",
                (request_id,),
            ).fetchall()
            reserved = {item["discovery_id"] for item in pending}
            if discovery_id is None:
                if len(sources) + len(reserved) >= MAX_CONTEXT_SOURCES:
                    raise ValueError(
                        "no unreserved context source slot; Discovery has reserved "
                        "capacity or the request has reached its source limit"
                    )
            else:
                if discovery_id not in reserved:
                    raise ValueError("Discovery result requires an active source reservation")
                if source.get("kind") != "peer_turn":
                    raise ValueError("Discovery reservation accepts only captured Peer evidence")
                if len(sources) + len(reserved) > MAX_CONTEXT_SOURCES:
                    raise ValueError("Discovery source reservation capacity exhausted")
            changed_request = db.execute(
                "UPDATE task_requests SET revision=revision+1, sources_json=?, "
                "assessment_json=NULL WHERE request_id=? AND revision=?",
                (json.dumps(sources + [resolved], ensure_ascii=False),
                 request_id, expected_revision),
            )
            if changed_request.rowcount != 1:
                raise RuntimeError("concurrent task request revision update")
            if discovery_id is not None:
                # Reservation is consumed atomically with the source append;
                # finish_discovery will finalize this captured result afterward.
                changed = db.execute(
                    "UPDATE task_discoveries SET state='attaching', turn_id=? "
                    "WHERE discovery_id=? AND request_id=? AND state='requested'",
                    (source["turn_id"], discovery_id, request_id),
                )
                if changed.rowcount != 1:
                    raise RuntimeError("Discovery reservation was concurrently recovered")
        return self.get(request_id)

    def assess(self, request_id: str, expected_revision: int,
               assessment: dict[str, Any]) -> dict[str, Any]:
        current = self.get(request_id)
        if current["revision"] != expected_revision:
            raise RuntimeError("stale task context revision")
        if not isinstance(assessment, dict) or set(assessment) != {
            "requirements", "blocking_unknowns", "decision", "rationale"
        }:
            raise ValueError("assessment needs requirements, blocking_unknowns, decision, rationale")
        decision = assessment["decision"]
        if decision not in DECISIONS:
            raise ValueError("invalid readiness decision")
        requirements = assessment["requirements"]
        unknowns = assessment["blocking_unknowns"]
        _text(assessment["rationale"], "assessment.rationale")
        if not isinstance(requirements, list) or not requirements:
            raise ValueError("at least one requirement is needed")
        if (not isinstance(unknowns, list) or
                any(not isinstance(u, str) or not u.strip() for u in unknowns)):
            raise ValueError("blocking_unknowns must be a list of texts")
        if decision == "direct" and unknowns:
            raise ValueError("direct decision may not have blocking unknowns")
        if decision != "direct" and not unknowns:
            raise ValueError("non-direct decision needs blocking unknowns")
        valid_refs = {"request:current"} | {s["id"] for s in current["sources"]}
        used_ids: set[str] = set()
        for item in requirements:
            if not isinstance(item, dict) or set(item) != {"id", "text", "evidence_refs"}:
                raise ValueError("requirement needs id, text, evidence_refs")
            ident = _text(item["id"], "requirement.id")
            _text(item["text"], "requirement.text")
            refs = item["evidence_refs"]
            if ident in used_ids:
                raise ValueError("duplicate requirement ID")
            used_ids.add(ident)
            if (not isinstance(refs, list) or not refs or
                    any(not isinstance(ref, str) or ref not in valid_refs for ref in refs)):
                raise ValueError("invalid requirement evidence reference")
        if decision == "direct":
            used_sources = {ref for item in requirements for ref in item["evidence_refs"]}
            if used_sources.intersection(current["stale_sources"]):
                raise ValueError("assessment depends on stale sources")
        return self._write(request_id, expected_revision, assessment=assessment)

    def assert_ready(self, request_id: str, revision: int | None = None) -> dict[str, Any]:
        current = self.get(request_id)
        if revision is not None and current["revision"] != revision:
            raise RuntimeError("stale assessment/context revision")
        assessment = current["assessment"]
        if (not assessment or assessment["decision"] != "direct" or
                assessment["blocking_unknowns"]):
            raise ValueError("implementation blocked until task readiness is direct")
        used = {ref for r in assessment["requirements"] for ref in r["evidence_refs"]}
        if used.intersection(current["stale_sources"]):
            raise ValueError("implementation blocked by stale source")
        return current

    def begin_discovery(self, request_id: str, mode: str,
                        repositories: list[str], questions: list[str],
                        route: str) -> str:
        current = self.get(request_id)
        if (mode not in {"targeted_discovery", "full_discovery"} or
                not current["assessment"] or
                current["assessment"]["decision"] != mode):
            raise ValueError("Discovery requires matching readiness decision")
        discovery_id = str(uuid.uuid4())
        with self._connect() as db:
            # Recover first, then verify readiness and count capacity from the
            # same transaction snapshot. Recovery may attach a captured Peer
            # result, increment revision and invalidate the assessment.
            db.execute("BEGIN IMMEDIATE")
            recovered = self._recover_abandoned_locked(db, request_id)
            if recovered:
                # Recovery is authoritative even if the caller supplied an
                # outdated revision. Commit it before a stale-request error
                # can roll back its durable native capture.
                db.commit()
                db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT sources_json, revision, assessment_json "
                "FROM task_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None or row["revision"] != current["revision"]:
                raise RuntimeError(
                    "task context changed during Discovery recovery; "
                    "reload and reassess before dispatch"
                )
            latest = (json.loads(row["assessment_json"])
                      if row["assessment_json"] is not None else None)
            if (latest is None or latest["decision"] != mode or
                    not latest["blocking_unknowns"]):
                raise ValueError(
                    "Discovery readiness invalidated by recovery; reassess before dispatch"
                )
            pending = db.execute(
                "SELECT COUNT(*) FROM task_discoveries "
                "WHERE request_id=? AND state='requested'",
                (request_id,),
            ).fetchone()[0]
            if len(json.loads(row["sources_json"])) + pending >= MAX_CONTEXT_SOURCES:
                raise ValueError(
                    "Discovery cannot start: no free context source slot for "
                    "captured evidence; create a new request with the relevant "
                    "sources before dispatch"
                )
            db.execute(
                "INSERT INTO task_discoveries "
                "(discovery_id, request_id, mode, repository_names_json, "
                "questions_json, route, turn_id, state, detail, created_at_ns, "
                "owner_pid, owner_start_token) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?)",
                (discovery_id, request_id, mode, json.dumps(repositories),
                 json.dumps(questions), route, "requested", time.time_ns(),
                 os.getpid(), _process_start_token(os.getpid())),
            )
        return discovery_id

    def finish_discovery(self, discovery_id: str, state: str,
                         turn_id: str | None = None,
                         detail: str | None = None) -> None:
        if state not in {"settled", "failed", "blocked", "capture_ambiguous"}:
            raise ValueError("invalid discovery state")
        # A successful Peer capture first appends its source (requested ->
        # attaching). Other terminal outcomes release an unused reservation.
        previous = "attaching" if state == "settled" and turn_id else "requested"
        with self._connect() as db:
            # On cancellation the runtime might have *already* stored its
            # native settled turn, but the caller has no response/turn_id.
            # Preserve the pre-launch binding instead of replacing it with NULL.
            # An explicit turn must also match the one already bound.
            result = db.execute(
                "UPDATE task_discoveries "
                "SET state=?, turn_id=COALESCE(turn_id, ?), detail=? "
                "WHERE discovery_id=? AND state=? "
                "AND (? IS NULL OR turn_id IS NULL OR turn_id=?)",
                (state, turn_id, detail[:1000] if detail else None,
                 discovery_id, previous, turn_id, turn_id),
            )
            if result.rowcount != 1:
                raise ValueError("unknown or completed Discovery")

    def _check_map(self, current: dict[str, Any], node_ids: list[str],
                   requirement_map: dict[str, list[str]]) -> None:
        if not isinstance(requirement_map, dict) or set(requirement_map) != set(node_ids):
            raise ValueError("requirement_map must cover every graph node")
        allowed = {r["id"] for r in current["assessment"]["requirements"]}
        mapped: set[str] = set()
        for refs in requirement_map.values():
            if not isinstance(refs, list) or not refs or any(r not in allowed for r in refs):
                raise ValueError("invalid graph requirement refs")
            mapped.update(refs)
        if mapped != allowed:
            raise ValueError("graph does not cover all resolved requirements")

    def bind_graph(self, graph_run_id: str, request_id: str, revision: int,
                   node_ids: list[str], requirement_map: dict[str, list[str]],
                   *, replace: bool = False) -> None:
        current = self.assert_ready(request_id, revision)
        self._check_map(current, node_ids, requirement_map)
        with self._connect() as db:
            if replace:
                row = db.execute(
                    "SELECT request_id FROM task_graph_bindings WHERE graph_run_id=?",
                    (graph_run_id,),
                ).fetchone()
                if row is None or row["request_id"] != request_id:
                    raise ValueError("graph is not bound to this task request")
                db.execute(
                    "UPDATE task_graph_bindings SET request_revision=?, "
                    "requirement_map_json=? WHERE graph_run_id=?",
                    (revision, json.dumps(requirement_map), graph_run_id),
                )
            else:
                db.execute(
                    "INSERT INTO task_graph_bindings VALUES (?, ?, ?, ?)",
                    (graph_run_id, request_id, revision, json.dumps(requirement_map)),
                )

    def graph_binding(self, graph_run_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM task_graph_bindings WHERE graph_run_id=?",
                (graph_run_id,),
            ).fetchone()
        if row is None:
            return None
        return {"graph_run_id": graph_run_id, "request_id": row["request_id"],
                "request_revision": row["request_revision"],
                "requirement_map": json.loads(row["requirement_map_json"])}

    def assert_graph_ready(self, graph_run_id: str) -> None:
        binding = self.graph_binding(graph_run_id)
        if binding is not None:
            self.assert_ready(binding["request_id"], binding["request_revision"])
