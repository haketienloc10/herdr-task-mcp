"""Issue #25 PR 1: characterize native resume and durable retry behavior.

No Herdr server or native agent is launched. Only external process interactions
are faked; graph persistence and runtime session/capture logic remain real.
"""
import asyncio
import sqlite3
import subprocess

import pytest

from qiqi_delegate.core import build_task_packet
from qiqi_delegate.runtime import DelegateRuntime
from qiqi_delegate.task_graph_runtime import (
    GraphRuntime,
    decisions_from_payload,
    task_graph_from_payload,
)
from qiqi_delegate.task_graph_store import GraphRuntimeStore


def _one_node_graph():
    return task_graph_from_payload({"nodes": [{
        "node_id": "fix", "repository": "backend", "route": "codex-balanced",
        "task_packet": {
            "objective": "Fix retry", "scope": ["api"],
            "acceptance_criteria": ["Response stable"],
        },
    }]})


def test_retry_feedback_and_resume_intent_survive_graph_restart(tmp_path):
    db = tmp_path / "graph.sqlite3"
    runtime = GraphRuntime(GraphRuntimeStore(db))
    started = runtime.start_graph(
        _one_node_graph(), repository_names={"backend"},
    )
    run_id = started["graph_run_id"]

    async def initial(node):
        assert node.task_packet.objective == "Fix retry"
        return {
            "state": "settled", "session_id": "native-session-1",
            "turn_id": "turn-1", "agent_response": "Initial implementation",
        }

    first = asyncio.run(runtime.delegate_next(run_id, executor=initial))
    assert first["graph_state"] == "awaiting_review"
    assert first["results"][0]["session_id"] == "native-session-1"

    retry = runtime.submit_decisions(
        run_id,
        decisions_from_payload([{
            "node_id": "fix", "action": "retry", "resume_session": True,
            "feedback": ["Cover timeout regression", "Check backoff calculation"],
        }]),
        expected_revision=first["revision"],
    )
    assert retry["graph_state"] == "ready"
    assert retry["decision_outcomes"][0]["retry_execution"] == {
        "resume_session": True,
        "session_id": "native-session-1",
        "feedback": ["Cover timeout regression", "Check backoff calculation"],
    }

    restarted = GraphRuntime(GraphRuntimeStore(db))
    pending = restarted._pending_retry_plans(run_id)["fix"]
    assert pending.resume_session is True
    assert pending.session_id == "native-session-1"
    assert pending.feedback == (
        "Cover timeout regression", "Check backoff calculation",
    )
    assert pending.task_packet.objective == "Fix retry"
    assert pending.task_packet.acceptance_criteria == ("Response stable",)
    assert pending.task_packet.context is not None
    assert [entry.as_dict() for entry in pending.task_packet.context.claims_to_investigate] == [
        {"claim": "Cover timeout regression", "source": "QiQi semantic review"},
        {"claim": "Check backoff calculation", "source": "QiQi semantic review"},
    ]

    async def unexpected_start(_node):
        raise AssertionError("RESUME must not fall back to a fresh executor")

    async def resume(node, session_id):
        assert session_id == "native-session-1"
        assert node.task_packet == pending.task_packet
        return {
            "state": "settled", "session_id": session_id,
            "turn_id": "turn-2", "agent_response": "Updated implementation",
        }

    second = asyncio.run(restarted.delegate_next(
        run_id, executor=unexpected_start, resume_executor=resume,
    ))
    assert second["results"][0]["resume_session"] is True
    assert second["results"][0]["session_id"] == "native-session-1"
    assert second["graph_state"] == "awaiting_review"

    fresh = restarted.submit_decisions(
        run_id,
        decisions_from_payload([{
            "node_id": "fix", "action": "retry", "resume_session": False,
            "feedback": ["Add coverage for HTTP 503"],
        }]),
        expected_revision=second["revision"],
    )
    assert fresh["decision_outcomes"][0]["retry_execution"] == {
        "resume_session": False, "session_id": None,
        "feedback": ["Add coverage for HTTP 503"],
    }
    restarted_again = GraphRuntime(GraphRuntimeStore(db))

    async def fresh_start(node):
        assert node.task_packet.objective == "Fix retry"
        assert node.task_packet.context is not None
        claims = node.task_packet.context.claims_to_investigate
        assert [entry.claim for entry in claims] == ["Add coverage for HTTP 503"]
        return {
            "state": "settled", "session_id": "native-session-2",
            "turn_id": "turn-3", "agent_response": "Fresh retry result",
        }

    async def unexpected_resume(_node, _session_id):
        raise AssertionError("Fresh retry must not reuse the native session")

    third = asyncio.run(restarted_again.delegate_next(
        run_id, executor=fresh_start, resume_executor=unexpected_resume,
    ))
    assert third["results"][0]["resume_session"] is False
    assert third["results"][0]["session_id"] == "native-session-2"


@pytest.mark.parametrize(
    ("adapter", "expected_resume_args"),
    [
        ("codex", ["resume", "native-session-1"]),
        ("claude", ["--resume", "native-session-1"]),
    ],
)
def test_runtime_resume_uses_native_session_and_preserves_capture(
    tmp_path, monkeypatch, adapter, expected_resume_args,
):
    control = tmp_path / "control"
    control.mkdir()
    backend = tmp_path / "backend"
    backend.mkdir()
    subprocess.run(["git", "-C", str(backend), "init", "-q"], check=True)
    (control / "repos.yaml").write_text(
        "repositories:\n  - name: backend\n    path: ../backend\n",
        encoding="utf-8",
    )
    (control / "agent-routing.yaml").write_text(
        f"routes:\n  codex-balanced:\n    agent: {adapter}\n    args: []\n",
        encoding="utf-8",
    )
    runtime = DelegateRuntime(control)
    monkeypatch.setattr("qiqi_delegate.runtime.shutil.which", lambda _bin: "/bin/true")

    starts = []
    prompts = []
    captures = []
    workspace_ids = []

    async def server_ready():
        return None

    async def create_workspace(*args, **_kwargs):
        assert args[:2] == ("workspace", "create")
        assert args[2:4] == ("--cwd", str(backend.resolve()))
        workspace_id = f"workspace-{len(workspace_ids) + 1}"
        workspace_ids.append(workspace_id)
        return {"workspace": {"workspace_id": workspace_id},
                "root_pane": {"pane_id": "pane-1"}}

    async def start_agent(_pane, agent_adapter, argv):
        assert agent_adapter == adapter
        starts.append(list(argv))
        return "agent-1", {"agent_status": "idle"}

    async def prompt_agent(_name, prompt, agent_adapter):
        assert agent_adapter == adapter
        prompts.append(prompt)
        return "done", {
            "agent_status": "done",
            "agent_session": {
                "kind": "id", "agent": adapter, "value": "native-session-1",
            },
        }

    async def capture_result(_sink, _nonce, capture_adapter, session_id):
        assert capture_adapter == adapter
        assert session_id == "native-session-1"
        captures.append(session_id)
        return {
            "state": "settled",
            "agent_response": f"Captured result #{len(captures)}",
        }

    async def run_command(*args, **_kwargs):
        assert args[:2] == ("workspace", "close")
        assert args[2] in workspace_ids
        return 0, "", ""

    monkeypatch.setattr(runtime, "_ensure_herdr_server", server_ready)
    monkeypatch.setattr(runtime, "_json", create_workspace)
    monkeypatch.setattr(runtime, "_start_agent", start_agent)
    monkeypatch.setattr(runtime, "_prompt", prompt_agent)
    monkeypatch.setattr(runtime, "_capture", capture_result)
    monkeypatch.setattr(runtime, "_run", run_command)

    packet = build_task_packet(
        objective="Fix retry", scope=["api"],
        acceptance_criteria=["Response stable"],
    )
    first = asyncio.run(runtime.delegate(
        repository="backend", route="codex-balanced", packet=packet,
    ))
    second = asyncio.run(runtime.delegate(
        repository="backend", route="codex-balanced", packet=packet,
        session_id=first["session_id"],
    ))
    assert first["state"] == second["state"] == "settled"
    assert first["session_id"] == second["session_id"] == "native-session-1"
    assert first["turn_id"] != second["turn_id"]
    assert first["agent_response"] == "Captured result #1"
    assert second["agent_response"] == "Captured result #2"
    assert starts[0][-2:] != expected_resume_args
    assert starts[1][-2:] == expected_resume_args
    # Characterize current behavior: resume re-renders the same full task prompt.
    assert prompts[0] == prompts[1]
    assert "## Repository execution boundary" in prompts[1]

    with sqlite3.connect(runtime.db) as conn:
        rows = conn.execute(
            "SELECT turn_id, session_id, response FROM turns ORDER BY created_at_ns",
        ).fetchall()
        assert {row[0] for row in rows} == {first["turn_id"], second["turn_id"]}
        assert all(row[1] == "native-session-1" for row in rows)
        assert {row[2] for row in rows} == {
            "Captured result #1", "Captured result #2",
        }
        assert conn.execute("SELECT COUNT(*) FROM write_claims").fetchone()[0] == 0
