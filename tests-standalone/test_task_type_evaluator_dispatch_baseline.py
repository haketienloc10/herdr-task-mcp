"""Issue #25 PR 1: capture Final Evaluator's complete native dispatch prompt.

The evaluator renderer is not the last writer of the prompt. This test observes
the actual message handed to Herdr and locks the runtime-owned appendix too.
No real Herdr server or native evaluator is started.
"""
import asyncio
import json
from pathlib import Path

from qiqi_delegate.core import render_task_prompt
from qiqi_delegate.final_eval_manifest import manifest_digest
from test_final_evaluation import setup_graph


def test_native_final_evaluator_receives_full_legacy_prompt(tmp_path, monkeypatch):
    runtime, _requests, graph_runtime, coordinator, graph_run_id = setup_graph(tmp_path)
    roots = runtime.repos()
    seen = {"closed": []}

    async def ensure_server():
        return None

    async def create_workspace(*args, **_kwargs):
        assert args[:2] == ("workspace", "create")
        native_cwd = Path(args[args.index("--cwd") + 1])
        assert native_cwd != roots["backend"] and native_cwd != roots["frontend"]
        assert (native_cwd / "context" / "context.json").is_file()
        assert (native_cwd / "context" / "source_index.json").is_file()
        seen["cwd"] = native_cwd
        return {
            "workspace": {"workspace_id": "eval-workspace-1"},
            "root_pane": {"pane_id": "eval-pane-1"},
        }

    async def start_agent(_pane_id, adapter, args):
        assert adapter == "codex"
        seen["agent_args"] = args
        return "evaluator-agent", {"agent_status": "idle"}

    async def prompt_agent(_agent_name, prompt, adapter):
        assert adapter == "codex"
        seen["prompt"] = prompt
        return "done", {
            "agent_status": "done",
            "agent_session": {
                "kind": "id", "agent": "codex", "value": "fresh-evaluator-native-session",
            },
        }

    async def capture(_sink, _nonce, adapter, native_session, timeout=15):
        assert adapter == "codex"
        assert native_session == "fresh-evaluator-native-session"
        seen["captured"] = True
        # Deliberately invalid report: this test covers prompt dispatch only,
        # not PASS or finalization.
        return {"state": "settled", "agent_response": "not-a-final-evaluation-report"}

    async def run_command(*args, **_kwargs):
        assert args == ("workspace", "close", "eval-workspace-1")
        seen["closed"].append(args[-1])
        return 0, "", ""

    monkeypatch.setattr("qiqi_delegate.runtime.shutil.which", lambda _cmd: "/bin/true")
    monkeypatch.setattr(runtime, "_ensure_herdr_server", ensure_server)
    monkeypatch.setattr(runtime, "_json", create_workspace)
    monkeypatch.setattr(runtime, "_start_agent", start_agent)
    monkeypatch.setattr(runtime, "_prompt", prompt_agent)
    monkeypatch.setattr(runtime, "_capture", capture)
    monkeypatch.setattr(runtime, "_run", run_command)

    revision = graph_runtime.get_graph(graph_run_id)["revision"]
    evaluation = asyncio.run(coordinator.start(
        graph_run_id, "codex-evaluator", revision,
    ))
    assert evaluation["status"] == "inconclusive"
    assert seen["captured"] is True
    assert seen["closed"] == ["eval-workspace-1"]

    record = coordinator.store.latest(graph_run_id)
    names = ("backend", "frontend")
    authored_packet = coordinator._packet(names, manifest_digest(record["manifest"]))
    rendered_base = render_task_prompt(
        authored_packet, evaluation_repositories=names,
    )
    golden_file = Path(__file__).parent / "fixtures" / "task_prompt_baseline.json"
    golden = json.loads(golden_file.read_text(encoding="utf-8"))
    normalized = seen["prompt"]
    for name in names:
        normalized = normalized.replace(str(roots[name]), f"<{name}-root>")
    expected = rendered_base + "\n\n" + golden["evaluation_dispatch_appendix"]
    # This is the complete prompt passed to _prompt(), NOT merely the output
    # of core.render_task_prompt(). Any removed/edited runtime appendix fails.
    assert normalized == expected

    args = seen["agent_args"]
    directory_args = [
        args[index + 1] for index, arg in enumerate(args[:-1])
        if arg == "--add-dir"
    ]
    assert directory_args == [str(roots[name]) for name in names]
    assert not any(arg in {"resume", "--resume"} for arg in args)
    assert not seen["cwd"].exists()
    with runtime._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM write_claims").fetchone()[0] == 0
