"""Characterization tests for issue #25, PR 1: existing task contracts and prompts.

These tests intentionally describe current behavior. Prompt-profile refactors must
preserve the legacy paths unless an explicit later PR changes their contract.
"""
import json
import sqlite3

import pytest

from qiqi_delegate.core import build_task_packet, render_task_prompt
from qiqi_delegate.task_graph_runtime import GraphRuntime, task_graph_from_payload
from qiqi_delegate.task_graph_store import GraphRuntimeStore


def _packet():
    return build_task_packet(
        objective="Trace retry logic",
        scope=["backend/requests.py", "shared/client.py"],
        acceptance_criteria=["Cite verified behavior", "Report unknowns"],
        out_of_scope=["Deployment"],
        context={
            "trusted_facts": [{"fact": "Public API must stay stable", "source": "user"}],
            "claims_to_investigate": [
                {"claim": "Retry occurs twice", "source": "peer-turn-1"},
            ],
        },
        constraints=["Do not invent results"],
        known_unknowns=["Whether timeout is retried"],
    )


def test_legacy_packet_json_and_optional_fields_are_stable():
    minimal = build_task_packet(
        objective="Fix retry", scope=["api"],
        acceptance_criteria=["Response stable"],
    )
    assert minimal.as_dict() == {
        "objective": "Fix retry",
        "scope": ["api"],
        "acceptance_criteria": ["Response stable"],
    }
    assert minimal.to_json() == (
        '{"objective":"Fix retry","scope":["api"],'
        '"acceptance_criteria":["Response stable"]}'
    )

    packet = _packet()
    expected = {
        "objective": "Trace retry logic",
        "scope": ["backend/requests.py", "shared/client.py"],
        "acceptance_criteria": ["Cite verified behavior", "Report unknowns"],
        "out_of_scope": ["Deployment"],
        "context": {
            "trusted_facts": [{"fact": "Public API must stay stable", "source": "user"}],
            "claims_to_investigate": [
                {"claim": "Retry occurs twice", "source": "peer-turn-1"},
            ],
        },
        "constraints": ["Do not invent results"],
        "known_unknowns": ["Whether timeout is retried"],
    }
    assert packet.as_dict() == expected
    assert packet.to_json() == json.dumps(
        expected, ensure_ascii=False, separators=(",", ":"),
    )
    assert build_task_packet(**packet.as_dict()) == packet


def test_prompt_common_sections_have_stable_order_and_provenance():
    prompt = render_task_prompt(_packet())
    headings = [
        "Repository task delegated by QiQi",
        "## Repository objective",
        "## Scope",
        "## Out of scope",
        "## Trusted facts",
        "## Claims to investigate",
        "## Constraints",
        "## Acceptance criteria",
        "## Known unknowns",
        "## Evidence and response quality",
        "## Repository execution boundary",
    ]
    positions = [prompt.index(heading) for heading in headings]
    assert positions == sorted(positions)
    assert "- Public API must stay stable\n  Provenance: user" in prompt
    assert "- Retry occurs twice\n  Provenance: peer-turn-1" in prompt
    assert "- Do not invent results" in prompt
    assert "- Whether timeout is retried" in prompt
    assert "cite repository-relative file paths" in prompt


@pytest.mark.parametrize(
    ("role", "kwargs", "boundary", "must_include"),
    [
        (
            "implementation", {}, "## Repository execution boundary",
            ("Operate only inside the current Git root",
             "return DEPENDENCY_REQUEST"),
        ),
        (
            "discovery", {"discovery_repositories": ("backend", "frontend")},
            "## Discovery repository boundary and no-write instruction",
            ("Read and investigate only these registered repositories: backend, frontend",
             "Do NOT create, modify, delete or rename any file",
             "This is a prompt instruction, not a filesystem sandbox"),
        ),
        (
            "evaluation", {"evaluation_repositories": ("backend", "frontend")},
            "## Independent native Final Evaluation",
            ("You are ONE new independent Evaluator, never Lead or Peer",
             "context/context.json and context/source_index.json",
             "verdict, requirement_results, cross_repository_checks",
             "A captured PASS is never automatically final delivery"),
        ),
    ],
)
def test_current_role_specific_prompt_boundaries(
    role, kwargs, boundary, must_include,
):
    prompt = render_task_prompt(_packet(), **kwargs)
    assert prompt.startswith("Repository task delegated by QiQi\n\n")
    assert "## Evidence and response quality" in prompt
    assert prompt.count(boundary) == 1
    for statement in must_include:
        assert statement in prompt, (role, statement)
    for other in (
        "## Repository execution boundary",
        "## Discovery repository boundary and no-write instruction",
        "## Independent native Final Evaluation",
    ):
        if other != boundary:
            assert other not in prompt, (role, other)


def test_persisted_legacy_graph_json_and_fingerprint_survive_restart(tmp_path):
    """Pin the fingerprint of a packet without task_type for PR 2 migration."""
    raw = {"nodes": [{
        "node_id": "A",
        "repository": "backend",
        "route": "codex-balanced",
        "task_packet": {
            "objective": "Fix retry", "scope": ["api"],
            "acceptance_criteria": ["Response stable"],
        },
    }]}
    db = tmp_path / "graph.sqlite3"
    graph = task_graph_from_payload(raw)
    runtime = GraphRuntime(GraphRuntimeStore(db))
    result = runtime.start_graph(graph, repository_names={"backend"})
    run_id = result["graph_run_id"]

    with sqlite3.connect(db) as conn:
        fingerprint, serialized = conn.execute(
            "SELECT graph_fingerprint, graph_json FROM graph_runs "
            "WHERE graph_run_id=?", (run_id,),
        ).fetchone()

    assert fingerprint == (
        "5196184553d7c58afd38ecd59256c5e9a6dee62941aab7d58339aea91bf94769"
    )
    assert json.loads(serialized) == {
        "nodes": [{
            "node_id": "A", "kind": "repo_task",
            "repository": "backend", "depends_on": [],
            "route": "codex-balanced",
            "task_packet": {
                "objective": "Fix retry", "scope": ["api"],
                "acceptance_criteria": ["Response stable"],
            },
        }],
    }
    restarted = GraphRuntime(GraphRuntimeStore(db))
    assert restarted._graph_for_run(run_id) == graph
    assert restarted.get_graph(run_id)["graph_state"] == "ready"
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT graph_fingerprint FROM graph_runs WHERE graph_run_id=?",
            (run_id,),
        ).fetchone()[0] == fingerprint
