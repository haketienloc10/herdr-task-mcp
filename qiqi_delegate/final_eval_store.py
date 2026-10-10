"""Durable graph-level Final Evaluation attempts and finalization audit.

One active attempt per graph, bound to graph+request revision and repository manifest.
A completed Node graph is not a finalized user task.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from qiqi_delegate.final_eval_snapshot import evidence_is_in_manifest
from qiqi_delegate.task_request import _owner_alive, _process_start_token

ACTIVE = ("requested", "evaluating")
FINISHED = ("passed", "failed", "inconclusive", "errored", "interrupted")


class FinalEvaluationStore:
    def __init__(self, database: Path):
        self.database = database
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS final_evaluations (
                    evaluation_id TEXT PRIMARY KEY,
                    graph_run_id TEXT NOT NULL,
                    graph_revision INTEGER NOT NULL,
                    request_id TEXT NOT NULL,
                    request_revision INTEGER NOT NULL,
                    route TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    turn_id TEXT,
                    raw_response TEXT,
                    report_json TEXT,
                    detail TEXT,
                    owner_pid INTEGER NOT NULL,
                    owner_start_token TEXT,
                    created_at_ns INTEGER NOT NULL,
                    updated_at_ns INTEGER NOT NULL,
                    finalized_at_ns INTEGER
                );
                CREATE INDEX IF NOT EXISTS final_evaluations_run_idx
                    ON final_evaluations(graph_run_id, created_at_ns);
                CREATE UNIQUE INDEX IF NOT EXISTS one_running_final_evaluation
                    ON final_evaluations(graph_run_id)
                    WHERE status IN ('requested', 'evaluating');
            """)
            db.execute("BEGIN IMMEDIATE")
            columns = {
                row["name"] for row in db.execute(
                    "PRAGMA table_info(final_evaluations)"
                )
            }
            if "owner_start_token" not in columns:
                db.execute(
                    "ALTER TABLE final_evaluations ADD COLUMN owner_start_token TEXT"
                )
        self.recover_abandoned()

    def recover_abandoned(self) -> int:
        """Recover native settled captures from dead owners; never restart a Peer.

        If the outcome cannot be proven, mark interrupted and require explicit
        operator confirmation before permitting another evaluation dispatch.
        """
        changed = 0
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT * FROM final_evaluations "
                "WHERE status IN ('requested','evaluating')"
            ).fetchall()
            for row in rows:
                if _owner_alive(row["owner_pid"], row["owner_start_token"]):
                    continue
                turn = db.execute(
                    "SELECT state, response FROM turns WHERE turn_id=?",
                    (row["turn_id"],),
                ).fetchone() if row["turn_id"] else None
                response = turn["response"] if turn and turn["state"] == "settled" else None
                report = None
                status = "interrupted"
                detail = "Owner exited before verifiable native completion; operator recovery required"
                if response:
                    try:
                        assessment = db.execute(
                            "SELECT assessment_json FROM task_requests WHERE request_id=?",
                            (row["request_id"],),
                        ).fetchone()
                        requirements = (
                            json.loads(assessment["assessment_json"])["requirements"]
                            if assessment and assessment["assessment_json"] else []
                        )
                        report = self.validate_report(
                            json.loads(response),
                            json.loads(row["manifest_json"]),
                            {r["id"] for r in requirements},
                        )
                        status = {"pass": "passed", "fail": "failed",
                                  "inconclusive": "inconclusive"}[report["verdict"]]
                        detail = "Recovered durable native capture from abandoned evaluator"
                    except (KeyError, ValueError, TypeError) as exc:
                        status = "inconclusive"
                        detail = "Native capture persisted but report invalid: " + str(exc)
                updated = db.execute(
                    "UPDATE final_evaluations SET status=?, raw_response=?, "
                    "report_json=?, detail=?, updated_at_ns=? WHERE evaluation_id=? "
                    "AND status IN ('requested','evaluating')",
                    (status, response, json.dumps(report) if report else None,
                     detail, time.time_ns(), row["evaluation_id"]),
                )
                changed += updated.rowcount
        return changed

    def inspect_interrupted(self, evaluation_id: str) -> dict[str, Any]:
        return self.get(evaluation_id)

    def release_interrupted(self, evaluation_id: str, *,
                            worker_termination_confirmed: bool) -> dict[str, Any]:
        """Operator-only exact-ID clearance; never exposed to Lead MCP."""
        if worker_termination_confirmed is not True:
            raise ValueError("verify evaluator worker termination before recovery")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status FROM final_evaluations WHERE evaluation_id=?",
                (evaluation_id,),
            ).fetchone()
            if row is None or row["status"] != "interrupted":
                raise ValueError("only an interrupted evaluation can be released")
            db.execute("""
                CREATE TABLE IF NOT EXISTS final_evaluation_recovery_audit (
                    evaluation_id TEXT NOT NULL,
                    recovered_at_ns INTEGER NOT NULL,
                    worker_termination_confirmed INTEGER NOT NULL
                    CHECK(worker_termination_confirmed=1)
                )
            """)
            db.execute(
                "UPDATE final_evaluations SET status='errored', detail=?, "
                "updated_at_ns=? WHERE evaluation_id=? AND status='interrupted'",
                ("Operator confirmed worker termination; safe to retry",
                 time.time_ns(), evaluation_id),
            )
            db.execute(
                "INSERT INTO final_evaluation_recovery_audit VALUES (?, ?, 1)",
                (evaluation_id, time.time_ns()),
            )
        return self.get(evaluation_id)

    def _connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def get(self, evaluation_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM final_evaluations WHERE evaluation_id=?",
                (evaluation_id,),
            ).fetchone()
        if row is None:
            raise ValueError("unknown final evaluation ID")
        result = dict(row)
        result["manifest"] = json.loads(result.pop("manifest_json"))
        result["report"] = (json.loads(result.pop("report_json"))
                            if result["report_json"] else None)
        if result["turn_id"]:
            # Read-only recovery locator: even if cancellation happened after
            # native persistence but before returning to the coordinator, the
            # complete durable captured turn remains inspectable.
            with self._connect() as db:
                native = db.execute(
                    "SELECT state, response FROM turns WHERE turn_id=?",
                    (result["turn_id"],),
                ).fetchone()
            if native is not None:
                result["native_capture"] = {
                    "state": native["state"], "response": native["response"],
                }
        return result

    def latest(self, graph_run_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT evaluation_id FROM final_evaluations "
                "WHERE graph_run_id=? ORDER BY created_at_ns DESC, rowid DESC LIMIT 1",
                (graph_run_id,),
            ).fetchone()
        return self.get(row["evaluation_id"]) if row else None

    def reserve(
        self, graph_run_id: str, graph_revision: int, request_id: str,
        request_revision: int, route: str,
        manifest: dict[str, Any], digest: str,
    ) -> tuple[dict[str, Any], bool]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT revision FROM graph_runs WHERE graph_run_id=?",
                (graph_run_id,),
            ).fetchone()
            context = db.execute(
                "SELECT revision FROM task_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            binding = db.execute(
                "SELECT request_id, request_revision FROM task_graph_bindings "
                "WHERE graph_run_id=?", (graph_run_id,),
            ).fetchone()
            if (row is None or row["revision"] != graph_revision or
                    context is None or context["revision"] != request_revision or
                    binding is None or binding["request_id"] != request_id or
                    binding["request_revision"] != request_revision):
                raise RuntimeError("stale graph/request binding for final evaluation")
            interrupted = db.execute(
                "SELECT evaluation_id FROM final_evaluations "
                "WHERE graph_run_id=? AND status='interrupted' LIMIT 1",
                (graph_run_id,),
            ).fetchone()
            if interrupted:
                raise RuntimeError(
                    "previous evaluator was interrupted; operator must confirm "
                    "worker termination and release its exact evaluation ID"
                )
            existing = db.execute(
                "SELECT * FROM final_evaluations "
                "WHERE graph_run_id=? AND status IN ('requested','evaluating') "
                "LIMIT 1", (graph_run_id,),
            ).fetchone()
            if existing is not None:
                if (existing["graph_revision"] != graph_revision or
                        existing["manifest_digest"] != digest or
                        existing["request_revision"] != request_revision):
                    raise RuntimeError(
                        "another final evaluation is active for a different version"
                    )
                return_id = existing["evaluation_id"]
                created = False
            else:
                return_id = str(uuid.uuid4())
                now = time.time_ns()
                db.execute(
                    "INSERT INTO final_evaluations "
                    "(evaluation_id,graph_run_id,graph_revision,request_id,"
                    "request_revision,route,manifest_json,manifest_digest,"
                    "status,owner_pid,owner_start_token,created_at_ns,updated_at_ns) "
                    "VALUES (?,?,?,?,?,?,?,?,'requested',?,?,?,?)",
                    (return_id, graph_run_id, graph_revision, request_id,
                     request_revision, route, json.dumps(manifest, sort_keys=True),
                     digest, os.getpid(), _process_start_token(os.getpid()), now, now),
                )
                created = True
        return self.get(return_id), created

    def bind_turn(self, evaluation_id: str, turn_id: str) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute(
                "UPDATE final_evaluations SET status='evaluating', turn_id=?, "
                "updated_at_ns=? "
                "WHERE evaluation_id=? AND status='requested' AND turn_id IS NULL",
                (turn_id, time.time_ns(), evaluation_id),
            )
            if result.rowcount != 1:
                raise RuntimeError("final evaluation is not eligible for native launch")

    @staticmethod
    def validate_report(
        payload: Any, manifest: dict[str, Any], required_ids: set[str],
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("FinalEvaluationReport must be a JSON object")
        expected = {"verdict", "requirement_results", "cross_repository_checks",
                    "verification_runs", "findings", "unknowns"}
        if set(payload) != expected:
            raise ValueError("FinalEvaluationReport fields are missing or unexpected")
        verdict = payload["verdict"]
        if verdict not in {"pass", "fail", "inconclusive"}:
            raise ValueError("unknown final evaluation verdict")
        requirements = payload["requirement_results"]
        findings = payload["findings"]
        unknowns = payload["unknowns"]
        checks = payload["cross_repository_checks"]
        runs = payload["verification_runs"]
        if not all(isinstance(x, list) for x in (
            requirements, findings, unknowns, checks, runs
        )):
            raise ValueError("report result collections must be arrays")
        if any(not isinstance(x, str) or not x.strip() for x in unknowns):
            raise ValueError("unknowns must contain nonempty strings")
        observed = set()
        def check_evidence(items: Any, *, mandatory: bool) -> None:
            if not isinstance(items, list) or (mandatory and not items):
                raise ValueError("evaluation evidence is absent")
            for item in items:
                if not isinstance(item, dict) or set(item) != {
                    "repository", "path", "sha256", "locator"
                }:
                    raise ValueError("evidence needs repository, path, sha256, locator")
                if (not isinstance(item["locator"], str)
                        or not item["locator"].strip()
                        or not all(isinstance(item[k], str) for k in (
                            "repository", "path", "sha256"
                        ))
                        or not evidence_is_in_manifest(
                            manifest, item["repository"], item["path"], item["sha256"]
                        )):
                    raise ValueError("evidence does not match captured repository manifest")
        for item in requirements:
            if not isinstance(item, dict) or set(item) != {
                "requirement_id", "status", "evidence", "rationale"
            }:
                raise ValueError("malformed evaluation requirement result")
            rid = item["requirement_id"]
            if (not isinstance(rid, str) or rid not in required_ids
                    or rid in observed or item["status"] not in {
                        "pass", "fail", "inconclusive"
                    }
                    or not isinstance(item["rationale"], str)
                    or not item["rationale"].strip()):
                raise ValueError("unknown/duplicate or malformed requirement result")
            observed.add(rid)
            check_evidence(item["evidence"], mandatory=item["status"] == "pass")
        if observed != required_ids:
            raise ValueError("final evaluation does not cover all original requirements")
        seen_findings = set()
        for finding in findings:
            if not isinstance(finding, dict) or set(finding) != {
                "id", "severity", "repositories", "affected_requirements",
                "evidence", "proposed_follow_up"
            }:
                raise ValueError("malformed evaluation finding")
            if (not isinstance(finding["id"], str) or
                    not finding["id"].strip() or finding["id"] in seen_findings or
                    finding["severity"] not in {"blocking", "major", "minor"} or
                    not isinstance(finding["repositories"], list) or
                    not finding["repositories"] or
                    any(not isinstance(n, str) or n not in manifest
                        for n in finding["repositories"]) or
                    not isinstance(finding["affected_requirements"], list) or
                    any(r not in required_ids for r in finding["affected_requirements"]) or
                    not isinstance(finding["proposed_follow_up"], str) or
                    not finding["proposed_follow_up"].strip()):
                raise ValueError("invalid finding or unexpected repository/requirement")
            seen_findings.add(finding["id"])
            check_evidence(finding["evidence"], mandatory=False)
        for check in checks:
            if (not isinstance(check, dict) or set(check) != {
                "name", "status", "evidence", "rationale"
            } or check["status"] not in {"pass", "fail", "inconclusive"} or
                    not isinstance(check["name"], str) or
                    not check["name"].strip() or
                    not isinstance(check["rationale"], str)):
                raise ValueError("invalid cross-repository check")
            check_evidence(check["evidence"], mandatory=check["status"] == "pass")
        for run in runs:
            if (not isinstance(run, dict) or set(run) != {
                "command", "cwd_repo", "exit_code", "artifact_ref"
            } or not isinstance(run["command"], str) or
                    not isinstance(run["cwd_repo"], str) or
                    run["cwd_repo"] not in manifest or
                    (run["exit_code"] is not None and
                     (isinstance(run["exit_code"], bool)
                      or not isinstance(run["exit_code"], int))) or
                    not isinstance(run["artifact_ref"], str)):
                raise ValueError("invalid verification run descriptor")
        if verdict == "pass" and (
            (len(manifest) > 1 and (
                not checks or
                # Every successful integration check must genuinely compare
                # evidence from two or more distinct repositories. Merely
                # covering them in separate single-module checks is not an
                # independent cross-module verification.
                any(
                    len({e["repository"] for e in check["evidence"]}) < 2
                    for check in checks
                ) or
                # Multiple cross-repo checks are allowed, but collectively
                # they must cover every repository in the TaskGraph.
                set(manifest) - {
                    e["repository"]
                    for check in checks
                    for e in check["evidence"]
                }
            )) or
            any(r["status"] != "pass" for r in requirements) or
            any(f["severity"] in {"blocking", "major"} for f in findings) or
            any(c["status"] != "pass" for c in checks) or
            unknowns or any(r["exit_code"] != 0 for r in runs)
        ):
            raise ValueError("claimed PASS conflicts with requirement/check evidence")
        if verdict == "pass" and not requirements:
            raise ValueError("PASS requires requirements")
        return payload

    def complete(
        self, evaluation_id: str, *, raw_response: str | None,
        report: dict[str, Any] | None, status: str, detail: str | None = None,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        if status not in FINISHED:
            raise ValueError("invalid terminal final evaluation state")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status, turn_id FROM final_evaluations WHERE evaluation_id=?",
                (evaluation_id,),
            ).fetchone()
            if row is None or row["status"] not in ACTIVE:
                raise RuntimeError("final evaluation already completed or missing")
            if turn_id is not None and row["turn_id"] not in (None, turn_id):
                raise RuntimeError("native turn binding mismatch")
            if status == "passed":
                bound_turn = row["turn_id"] or turn_id
                native = db.execute(
                    "SELECT state, response, repository, route "
                    "FROM turns WHERE turn_id=?",
                    (bound_turn,),
                ).fetchone()
                metadata = db.execute(
                    "SELECT manifest_json, request_id, route "
                    "FROM final_evaluations WHERE evaluation_id=?", (evaluation_id,),
                ).fetchone()
                context = db.execute(
                    "SELECT assessment_json FROM task_requests WHERE request_id=?",
                    (metadata["request_id"],),
                ).fetchone()
                if (not isinstance(raw_response, str) or
                        native is None or native["state"] != "settled" or
                        native["response"] != raw_response or report is None or
                        native["route"] != metadata["route"] or
                        native["repository"] not in json.loads(
                            metadata["manifest_json"]
                        ) or context is None or context["assessment_json"] is None):
                    raise ValueError(
                        "PASS requires an exact persisted native settled turn, "
                        "the same complete response, and assessed requirements"
                    )
                required = {
                    item["id"] for item in json.loads(
                        context["assessment_json"]
                    )["requirements"]
                }
                validated = self.validate_report(
                    report, json.loads(metadata["manifest_json"]), required,
                )
                if validated["verdict"] != "pass":
                    raise ValueError("PASS status conflicts with validated report")
            updated = db.execute(
                "UPDATE final_evaluations SET status=?, raw_response=?, "
                "report_json=?, detail=?, turn_id=COALESCE(turn_id, ?), "
                "updated_at_ns=? WHERE evaluation_id=? AND status IN "
                "('requested','evaluating')",
                (status, raw_response, json.dumps(report) if report else None,
                 detail, turn_id, time.time_ns(), evaluation_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError("concurrent final evaluation transition")
        return self.get(evaluation_id)

    def finalize(
        self, evaluation_id: str, graph_revision: int,
        request_revision: int, manifest_digest: str,
    ) -> dict[str, Any]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            entry = db.execute(
                "SELECT * FROM final_evaluations WHERE evaluation_id=?",
                (evaluation_id,),
            ).fetchone()
            if entry is None:
                raise ValueError("unknown final evaluation")
            run = db.execute(
                "SELECT revision FROM graph_runs WHERE graph_run_id=?",
                (entry["graph_run_id"],),
            ).fetchone()
            binding = db.execute(
                "SELECT request_id, request_revision FROM task_graph_bindings "
                "WHERE graph_run_id=?", (entry["graph_run_id"],),
            ).fetchone()
            request = db.execute(
                "SELECT revision FROM task_requests WHERE request_id=?",
                (entry["request_id"],),
            ).fetchone()
            if (entry["status"] != "passed" or entry["finalized_at_ns"] is not None
                    or entry["graph_revision"] != graph_revision
                    or entry["request_revision"] != request_revision
                    or entry["manifest_digest"] != manifest_digest
                    or run is None or run["revision"] != graph_revision
                    or binding is None or binding["request_id"] != entry["request_id"]
                    or binding["request_revision"] != request_revision
                    or request is None or request["revision"] != request_revision):
                raise RuntimeError("final evaluation PASS is absent, stale or already finalized")
            result = db.execute(
                "UPDATE final_evaluations SET finalized_at_ns=?, updated_at_ns=? "
                "WHERE evaluation_id=? AND status='passed' AND finalized_at_ns IS NULL",
                (time.time_ns(), time.time_ns(), evaluation_id),
            )
            if result.rowcount != 1:
                raise RuntimeError("final evaluation was concurrently finalized")
        return self.get(evaluation_id)
