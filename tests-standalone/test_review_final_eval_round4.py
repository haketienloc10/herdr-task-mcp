"""Review #5479551861: stale request races, oversized intent, metadata collisions."""
import asyncio
import json
import sqlite3
import subprocess

import pytest

from qiqi_delegate.final_eval_snapshot import EvaluationSnapshot
from test_final_evaluation import setup_graph, report


def _native_capture(runtime, turn_id, body):
    with sqlite3.connect(runtime.db) as db:
        db.execute(
            "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
            (turn_id, "final-session", "backend", "codex-evaluator",
             "settled", body, 987),
        )


@pytest.mark.parametrize("new_requirement_id", ["R1", "R2"])
def test_changed_task_request_mid_evaluation_terminalizes_and_can_retry(
    tmp_path, monkeypatch, new_requirement_id,
):
    runtime, requests, graph_runtime, coordinator, gid = setup_graph(tmp_path)
    request_id = requests.graph_binding(gid)["request_id"]
    calls = []

    async def first_delegate(**kwargs):
        calls.append(kwargs["evaluation_id"])
        coordinator.store.bind_turn(kwargs["evaluation_id"], "stale-capture")
        # The original assessment is still R1 when the Evaluator started.
        # Lead changes it while the native worker is running. Even if the
        # requirement IDs happen to remain equal, its revision has changed.
        revision = requests.get(request_id)["revision"]
        changed = requests.assess(request_id, revision, {
            "requirements": [{
                "id": new_requirement_id,
                "text": "Updated user acceptance criterion",
                "evidence_refs": ["request:current"],
            }],
            "blocking_unknowns": [],
            "decision": "direct",
            "rationale": "Newer requirement review from user",
        })
        assert changed["revision"] == revision + 1
        frozen = coordinator.store.get(kwargs["evaluation_id"])["manifest"]
        body = json.dumps(report(frozen))
        _native_capture(runtime, "stale-capture", body)
        return {
            "state": "settled", "turn_id": "stale-capture",
            "agent_response": body,
        }

    monkeypatch.setattr(runtime, "delegate", first_delegate)
    graph_revision = graph_runtime.get_graph(gid)["revision"]
    result = asyncio.run(
        coordinator.start(gid, "codex-evaluator", graph_revision)
    )
    assert result["status"] == "inconclusive"
    assert "Stale final evaluation" in result["detail"]
    assert result["is_current"] is False
    assert result["turn_id"] == "stale-capture"
    assert result["raw_response"]
    assert result["report"]["verdict"] == "pass"
    assert coordinator.store.get(result["evaluation_id"])["native_capture"]["state"] == "settled"
    with coordinator.store._connect() as db:
        active = db.execute(
            "SELECT COUNT(*) FROM final_evaluations "
            "WHERE graph_run_id=? AND status IN ('requested','evaluating')",
            (gid,),
        ).fetchone()[0]
    assert active == 0

    # Rebind the updated assessment, then launch one fresh whole-graph
    # evaluation. The stale attempt cannot monopolize the active slot.
    updated = requests.get(request_id)
    binding = requests.graph_binding(gid)
    updated_map = {
        node: [new_requirement_id]
        for node in binding["requirement_map"]
    }
    requests.bind_graph(
        gid, request_id, updated["revision"],
        list(updated_map), updated_map, replace=True,
    )

    async def second_delegate(**kwargs):
        calls.append(kwargs["evaluation_id"])
        coordinator.store.bind_turn(kwargs["evaluation_id"], "fresh-capture")
        manifest = coordinator.store.get(kwargs["evaluation_id"])["manifest"]
        payload = report(manifest)
        payload["requirement_results"][0]["requirement_id"] = new_requirement_id
        body = json.dumps(payload)
        _native_capture(runtime, "fresh-capture", body)
        return {
            "state": "settled", "turn_id": "fresh-capture",
            "agent_response": body,
        }

    monkeypatch.setattr(runtime, "delegate", second_delegate)
    renewed = asyncio.run(
        coordinator.start(gid, "codex-evaluator", graph_revision)
    )
    assert renewed["status"] == "passed"
    assert renewed["is_current"] is True
    assert len(calls) == 2
    assert renewed["evaluation_id"] != result["evaluation_id"]


def test_near_limit_user_request_and_large_graph_use_snapshot_archive(
    tmp_path, monkeypatch,
):
    runtime, requests, graph_runtime, coordinator, gid = setup_graph(
        tmp_path, with_request=False,
    )
    request_text = (
        "Full original specification: "
        + "r" * (100_000 - len("Full original specification: "))
    )
    assert len(request_text) == 100_000
    request = requests.create(request_text)
    assessed = requests.assess(request["request_id"], 1, {
        "requirements": [{
            "id": "R1", "text": "Verify combined implementation",
            "evidence_refs": ["request:current"],
        }],
        "blocking_unknowns": [], "decision": "direct",
        "rationale": "Complete specification supplied",
    })
    requests.bind_graph(
        gid, assessed["request_id"], assessed["revision"],
        ["backend", "frontend"],
        {"backend": ["R1"], "frontend": ["R1"]},
    )
    seen = {}

    async def fake_delegate(**kwargs):
        primary = kwargs["evaluation_roots"]["backend"]
        archive_dir = primary / ".qiqi-final-task-sources"
        index = json.loads((archive_dir / "index.json").read_text(encoding="utf-8"))
        full_graph = json.loads(
            (archive_dir / "task-graph.json").read_text(encoding="utf-8")
        )
        assert index["original_user_request"] == request_text
        assert len(index["original_user_request"]) == 100_000
        assert index["requirements"][0]["id"] == "R1"
        assert {node["node_id"] for node in full_graph["task_graph"]} == {
            "backend", "frontend"
        }
        assert full_graph["task_graph"][1]["acceptance_criteria"] == [
            "Format backend total"
        ]
        # No duplication of user request/requirements/graph in bounded packet.
        prompt = kwargs["packet"].to_json()
        assert len(prompt) < 100_000
        assert request_text not in prompt
        assert "Full original specification:" not in prompt
        assert ".qiqi-final-task-sources/task-graph.json" in prompt
        seen["prompt_length"] = len(prompt)
        coordinator.store.bind_turn(kwargs["evaluation_id"], "large-input-turn")
        body = json.dumps(
            report(coordinator.store.get(kwargs["evaluation_id"])["manifest"])
        )
        _native_capture(runtime, "large-input-turn", body)
        return {
            "state": "settled", "turn_id": "large-input-turn",
            "agent_response": body,
        }

    monkeypatch.setattr(runtime, "delegate", fake_delegate)
    result = asyncio.run(coordinator.start(
        gid, "codex-evaluator", graph_runtime.get_graph(gid)["revision"]
    ))
    assert result["status"] == "passed"
    assert result["is_current"] is True
    assert seen["prompt_length"] < 5000


def test_large_graph_criteria_are_archived_not_copied_into_packet(tmp_path):
    runtime, requests, graph_runtime, coordinator, gid = setup_graph(tmp_path)
    graph = graph_runtime.get_graph(gid)
    authored = graph_runtime._graph_for_run(gid)
    binding = requests.graph_binding(gid)
    task = requests.get(binding["request_id"])
    from qiqi_delegate.final_eval_snapshot import EvaluationSnapshot
    with EvaluationSnapshot(runtime.repos()) as snap:
        full = coordinator._graph_context(
            graph, authored, binding, task, snap.digest,
        )
        long_criteria = "verify contract " * 10000
        full["task_graph"][0]["acceptance_criteria"].append(long_criteria)
        source_meta = coordinator._materialize_task_sources(
            task, snap.paths["backend"], graph_context=full,
        )
        packet = coordinator._packet(
            graph, authored, binding, task,
            ("backend", "frontend"), snap.digest, source_meta,
        )
        assert len(packet.to_json()) < 100_000
        assert long_criteria not in packet.to_json()
        persisted_graph = json.loads(
            (snap.paths["backend"] / source_meta["graph_path"]).read_text(
                encoding="utf-8"
            )
        )
        assert long_criteria in (
            persisted_graph["task_graph"][0]["acceptance_criteria"]
        )


@pytest.mark.parametrize("path", [
    ".qiqi-evaluation-manifest.json",
    ".qiqi-evaluation-manifest.json/part.json",
    ".qiqi-final-task-sources/index.json",
])
@pytest.mark.parametrize("tracked", [False, True])
def test_snapshot_fails_closed_on_generated_metadata_collision(
    tmp_path, path, tracked,
):
    runtime, _requests, _graph_runtime, _coordinator, _gid = setup_graph(tmp_path)
    backend = runtime.repos()["backend"]
    collision = backend / path
    collision.parent.mkdir(parents=True, exist_ok=True)
    original = "Product-owned file must never be silently overwritten\n"
    collision.write_text(original, encoding="utf-8")
    if tracked:
        subprocess.run(
            ["git", "-C", str(backend), "add", "-f", "--", path],
            check=True,
        )
    with pytest.raises(ValueError, match="reserved metadata path collision"):
        with EvaluationSnapshot({"backend": backend}):
            pass
    assert collision.read_text(encoding="utf-8") == original
