"""TaskGraph fixture shared by regression tests; no evaluator dependency."""
import sqlite3
from test_task_readiness import workspace
from qiqi_delegate.task_graph_runtime import GraphRuntime, task_graph_from_payload
from qiqi_delegate.task_graph_store import GraphRuntimeStore


def setup_graph(tmp_path, *, multi=True, with_request=True):
    runtime, requests = workspace(tmp_path)
    repos = runtime.repos()
    (repos["backend"] / "orders.py").write_text(
        "def total(n):\n    return n * 2\n", encoding="utf-8",
    )
    (repos["frontend"] / "client.py").write_text(
        "def show(total):\n    return str(total)\n", encoding="utf-8",
    )
    (runtime.root / "agent-routing.yaml").write_text(
        'routes:\n  codex-balanced:\n    agent: codex\n'
        '    args: ["--yolo"]\n',
        encoding="utf-8",
    )
    graph_rt = GraphRuntime(
        GraphRuntimeStore(runtime.db),
        repository_key=lambda n: str(repos[n]),
        readiness_guard=requests.assert_graph_ready,
    )
    nodes = [{
        "node_id": "backend", "repository": "backend",
        "route": "codex-balanced",
        "task_packet": {
            "objective": "Implement total", "scope": ["orders.py"],
            "acceptance_criteria": ["total doubles input"],
        },
    }]
    if multi:
        nodes.append({
            "node_id": "frontend", "repository": "frontend",
            "route": "codex-balanced", "depends_on": ["backend"],
            "task_packet": {
                "objective": "Display total", "scope": ["client.py"],
                "acceptance_criteria": ["Format backend total"],
            },
        })
    graph = graph_rt.start_graph(
        task_graph_from_payload({"nodes": nodes}),
        repository_names=repos.keys(),
    )
    gid = graph["graph_run_id"]
    if with_request:
        request = requests.create("Display prices correctly end to end")
        rid = request["request_id"]
        assessment = requests.assess(rid, 1, {
            "requirements": [
                {"id": "R1", "text": "Correct total and display",
                 "evidence_refs": ["request:current"]},
            ],
            "blocking_unknowns": [], "decision": "direct",
            "rationale": "Specified contract",
        })
        requests.bind_graph(
            gid, rid, assessment["revision"], [n["node_id"] for n in nodes],
            {n["node_id"]: ["R1"] for n in nodes},
        )
    with sqlite3.connect(runtime.db) as db:
        for node in nodes:
            db.execute(
                "UPDATE graph_node_states SET semantic_state='satisfied', "
                "runtime_state='idle', turn_id=? WHERE graph_run_id=? AND node_id=?",
                ("turn-" + node["node_id"], gid, node["node_id"]),
            )
    return runtime, requests, graph_rt, None, gid


