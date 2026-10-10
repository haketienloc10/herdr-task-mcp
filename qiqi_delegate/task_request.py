"""Persisted, source-optional task intent and readiness for evidence-grounded delegation.

This is a structural/provenance gate, not a semantic correctness oracle.
Legacy graph runs without a request binding intentionally remain unassessed.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path, PurePosixPath

from qiqi_delegate.core import CAPTURE_MAX_RESPONSE_CHARS
from typing import Any, Callable

MAX_SOURCE_BYTES = 100_000
MAX_REQUEST_CHARS = 100_000
DECISIONS = {"direct", "targeted_discovery", "full_discovery", "blocked"}


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
                    created_at_ns INTEGER NOT NULL
                );
            """)

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
        if sources is not None and (not isinstance(sources, list) or len(sources) > 16):
            raise ValueError("sources must be a list of at most 16 entries")
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
               source: dict[str, Any]) -> dict[str, Any]:
        current = self.get(request_id)
        if current["revision"] != expected_revision:
            raise RuntimeError("stale task context revision")
        resolved = self._resolve_source(source)
        if len(current["sources"]) >= 16:
            raise ValueError("too many context sources")
        return self._write(request_id, expected_revision,
                           sources=current["sources"] + [resolved])

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
            db.execute(
                "INSERT INTO task_discoveries VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?)",
                (discovery_id, request_id, mode, json.dumps(repositories),
                 json.dumps(questions), route, "requested", time.time_ns()),
            )
        return discovery_id

    def finish_discovery(self, discovery_id: str, state: str,
                         turn_id: str | None = None,
                         detail: str | None = None) -> None:
        if state not in {"settled", "failed", "blocked", "capture_ambiguous"}:
            raise ValueError("invalid discovery state")
        with self._connect() as db:
            result = db.execute(
                "UPDATE task_discoveries SET state=?, turn_id=?, detail=? "
                "WHERE discovery_id=? AND state='requested'",
                (state, turn_id, detail[:1000] if detail else None, discovery_id),
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
