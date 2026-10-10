"""Operator-only maintenance commands. Never exposed through the QiQi MCP server."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from qiqi_delegate.runtime import DelegateRuntime
from qiqi_delegate.task_graph_store import GraphRuntimeStore
from qiqi_delegate.task_request import TaskRequestStore


def _validated_runtime(
    workspace: Path, repository: str, *, allow_legacy_claim: bool = False,
) -> DelegateRuntime:
    if not repository or not repository.strip():
        raise ValueError("repository must be a registered non-empty name")
    if not workspace.is_dir():
        raise ValueError(f"workspace directory does not exist: {workspace}")
    runtime = DelegateRuntime(workspace)
    registered = runtime.repos()
    if repository not in registered:
        if not allow_legacy_claim:
            raise ValueError(f"unregistered repository: {repository!r}")
        # Only permit old names for exact, unmapped claims retained during
        # an upgrade. Do not turn this into arbitrary CLI repo targeting.
        with runtime._connect() as db:
            legacy = db.execute(
                "SELECT 1 FROM write_claims "
                "WHERE repository=? AND repository_root IS NULL",
                (repository,),
            ).fetchone()
        if legacy is None:
            raise ValueError(f"unregistered repository: {repository!r}")
    return runtime


def show_claim(*, workspace: Path, repository: str) -> dict[str, Any]:
    """Read the exact current holder without modifying worker state."""
    runtime = _validated_runtime(workspace, repository, allow_legacy_claim=True)
    roots = runtime.repos()
    canonical = str(roots[repository]) if repository in roots else None
    with runtime._connect() as db:
        # A renamed registered alias looks up the claim by stable root. For
        # pre-migration rows, only the original exact name is available.
        row = db.execute(
            "SELECT repository, repository_root, claim_id, created_at_ns "
            "FROM write_claims WHERE repository_root = ? "
            "OR (repository = ? AND repository_root IS NULL) "
            "ORDER BY repository_root IS NULL ASC LIMIT 1",
            (canonical, repository),
        ).fetchone()
    return {
        "repository": repository,
        "claim_id": row["claim_id"] if row else None,
        "created_at_ns": row["created_at_ns"] if row else None,
    }


def release_stale_claim(
    *,
    workspace: Path,
    repository: str,
    claim_id: str,
    worker_termination_confirmed: bool,
) -> dict[str, Any]:
    """Clear exactly one claim after an operator has verified the worker stopped.

    The flag records operator confirmation; it does not inspect Herdr or prove
    termination. There is no force release, fallback repository, or wildcard.
    """
    if worker_termination_confirmed is not True:
        raise ValueError(
            "refusing to release claim: first verify the Herdr worker has stopped "
            "and pass --worker-termination-confirmed"
        )
    if not isinstance(claim_id, str) or not claim_id.strip():
        raise ValueError("claim_id must be an exact non-empty value")
    runtime = _validated_runtime(workspace, repository, allow_legacy_claim=True)
    roots = runtime.repos()
    canonical = str(roots[repository]) if repository in roots else None
    with runtime._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute(
            "SELECT repository, repository_root, claim_id FROM write_claims "
            "WHERE repository_root = ? OR "
            "(repository = ? AND repository_root IS NULL)",
            (canonical, repository),
        ).fetchall()
        if not current:
            raise RuntimeError(f"no write claim exists for repository {repository!r}")
        matches = [row for row in current if row["claim_id"] == claim_id]
        if len(matches) != 1:
            raise RuntimeError(
                f"claim_id mismatch for repository {repository!r}; "
                "refusing to release a different writer"
            )
        holder = matches[0]

        db.execute("""
            CREATE TABLE IF NOT EXISTS write_claim_recovery_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repository TEXT NOT NULL,
                claim_id TEXT NOT NULL,
                worker_termination_confirmed INTEGER NOT NULL CHECK (worker_termination_confirmed = 1),
                released_at_ns INTEGER NOT NULL
            )
        """)
        released = db.execute(
            "DELETE FROM write_claims WHERE repository = ? AND claim_id = ? "
            "AND repository_root IS ?",
            (holder["repository"], claim_id, holder["repository_root"]),
        )
        if released.rowcount != 1:
            raise RuntimeError("write claim changed during recovery")
        db.execute(
            "INSERT INTO write_claim_recovery_audit "
            "(repository, claim_id, worker_termination_confirmed, released_at_ns) "
            "VALUES (?, ?, 1, ?)",
            (holder["repository"], claim_id, time.time_ns()),
        )
    return {"repository": repository, "claim_id": claim_id, "released": True}


def show_attempt(
    *,
    workspace: Path,
    repository: str,
    graph_run_id: str,
    attempt_id: str,
) -> dict[str, Any]:
    """Inspect a persisted attempt before attempting manual recovery."""
    runtime = _validated_runtime(workspace, repository)
    store = GraphRuntimeStore(runtime.db)
    graph = store.load_graph(graph_run_id)
    attempt = store.get_attempt(attempt_id)
    if attempt is None or attempt["graph_run_id"] != graph_run_id:
        raise RuntimeError("no exact attempt exists in the requested graph run")
    matching = [
        node for node in graph.nodes
        if node.node_id == attempt["node_id"] and node.repository == repository
    ]
    if len(matching) != 1:
        raise RuntimeError("attempt is not owned by the registered repository")
    active = store.get_run(graph_run_id)
    with runtime._connect() as db:
        holder = db.execute(
            "SELECT claim_id FROM write_claims WHERE repository = ?",
            (repository,),
        ).fetchone()
    return {
        "graph_run_id": graph_run_id,
        "wave_id": attempt["wave_id"],
        "node_id": attempt["node_id"],
        "attempt_id": attempt_id,
        "repository": repository,
        "runtime_state": attempt["runtime_state"],
        "dispatch_state": attempt["dispatch_state"],
        "retry_plan": (
            json.loads(attempt["retry_plan_json"])
            if attempt.get("retry_plan_json") else None
        ),
        "active_wave_id": active["current_wave_id"] if active else None,
        "write_claim_id": holder["claim_id"] if holder else None,
    }


def recover_interrupted_attempt(
    *,
    workspace: Path,
    repository: str,
    graph_run_id: str,
    wave_id: str,
    node_id: str,
    attempt_id: str,
    worker_termination_confirmed: bool,
) -> dict[str, Any]:
    """Fail closed unless an operator attests the old worker has terminated."""
    if worker_termination_confirmed is not True:
        raise ValueError(
            "refusing attempt recovery: verify worker termination first and "
            "pass --worker-termination-confirmed"
        )
    runtime = _validated_runtime(workspace, repository)
    return GraphRuntimeStore(runtime.db).recover_interrupted_attempt(
        graph_run_id=graph_run_id,
        wave_id=wave_id,
        node_id=node_id,
        attempt_id=attempt_id,
        repository=repository,
        worker_termination_confirmed=worker_termination_confirmed,
    )


def _task_request_store(workspace: Path) -> TaskRequestStore:
    if not workspace.is_dir():
        raise ValueError(f"workspace directory does not exist: {workspace}")
    runtime = DelegateRuntime(workspace)
    return TaskRequestStore(runtime.db, runtime.root, runtime.repos)


def show_discovery(*, workspace: Path, discovery_id: str) -> dict[str, Any]:
    """Inspect one Discovery reservation without requiring repo access."""
    return _task_request_store(workspace).inspect_discovery(discovery_id)


def recover_ownerless_discovery(
    *, workspace: Path, discovery_id: str,
    worker_termination_confirmed: bool,
) -> dict[str, Any]:
    """Release ONLY an exact legacy ownerless Discovery after operator confirmation."""
    if worker_termination_confirmed is not True:
        raise ValueError(
            "verify the legacy Discovery worker has terminated before "
            "passing --worker-termination-confirmed"
        )
    return _task_request_store(workspace).recover_ownerless_discovery(
        discovery_id, worker_termination_confirmed=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Operator-only QiQi maintenance; not an MCP tool",
    )
    actions = parser.add_subparsers(dest="action", required=True)

    show = actions.add_parser("show-claim", help="Inspect a repository write claim")
    show.add_argument("--workspace", required=True, type=Path)
    show.add_argument("--repository", required=True)

    release = actions.add_parser(
        "release-claim",
        help="Release an exact stale claim only after confirming worker termination",
    )
    release.add_argument("--workspace", required=True, type=Path)
    release.add_argument("--repository", required=True)
    release.add_argument("--claim-id", required=True)
    release.add_argument(
        "--worker-termination-confirmed", action="store_true",
        help="I verified that the old Herdr worker is no longer running",
    )

    attempt = actions.add_parser(
        "show-attempt", help="Inspect one persisted graph attempt and its wave",
    )
    attempt.add_argument("--workspace", required=True, type=Path)
    attempt.add_argument("--repository", required=True)
    attempt.add_argument("--graph-run-id", required=True)
    attempt.add_argument("--attempt-id", required=True)

    recover = actions.add_parser(
        "recover-attempt",
        help="Terminalize one exact interrupted attempt after verified worker termination",
    )
    recover.add_argument("--workspace", required=True, type=Path)
    recover.add_argument("--repository", required=True)
    recover.add_argument("--graph-run-id", required=True)
    recover.add_argument("--wave-id", required=True)
    recover.add_argument("--node-id", required=True)
    recover.add_argument("--attempt-id", required=True)
    recover.add_argument(
        "--worker-termination-confirmed", action="store_true",
        help="I verified the old worker stopped and released any repository write claim",
    )

    show_discovery_action = actions.add_parser(
        "show-discovery",
        help="Inspect one exact Discovery record, including legacy ownerless reservations",
    )
    show_discovery_action.add_argument("--workspace", required=True, type=Path)
    show_discovery_action.add_argument("--discovery-id", required=True)

    legacy_recovery = actions.add_parser(
        "recover-ownerless-discovery",
        help="Recover an exact ownerless legacy Discovery after verifying worker termination",
    )
    legacy_recovery.add_argument("--workspace", required=True, type=Path)
    legacy_recovery.add_argument("--discovery-id", required=True)
    legacy_recovery.add_argument(
        "--worker-termination-confirmed", action="store_true",
        help="I have verified the old MCP/Herdr Discovery worker has stopped",
    )

    args = parser.parse_args(argv)
    try:
        if args.action == "show-claim":
            output = show_claim(workspace=args.workspace, repository=args.repository)
        elif args.action == "release-claim":
            output = release_stale_claim(
                workspace=args.workspace,
                repository=args.repository,
                claim_id=args.claim_id,
                worker_termination_confirmed=args.worker_termination_confirmed,
            )
        elif args.action == "show-discovery":
            output = show_discovery(
                workspace=args.workspace, discovery_id=args.discovery_id,
            )
        elif args.action == "recover-ownerless-discovery":
            output = recover_ownerless_discovery(
                workspace=args.workspace, discovery_id=args.discovery_id,
                worker_termination_confirmed=args.worker_termination_confirmed,
            )
        elif args.action == "show-attempt":
            output = show_attempt(
                workspace=args.workspace,
                repository=args.repository,
                graph_run_id=args.graph_run_id,
                attempt_id=args.attempt_id,
            )
        else:
            output = recover_interrupted_attempt(
                workspace=args.workspace,
                repository=args.repository,
                graph_run_id=args.graph_run_id,
                wave_id=args.wave_id,
                node_id=args.node_id,
                attempt_id=args.attempt_id,
                worker_termination_confirmed=args.worker_termination_confirmed,
            )
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"qiqi maintenance error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
