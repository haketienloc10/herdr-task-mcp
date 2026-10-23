"""Large Lead-workspace documents travel as scoped, hash-checked snapshots."""
import asyncio
import importlib
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from qiqi_delegate.core import build_task_packet
from qiqi_delegate.runtime import DelegateRuntime
from qiqi_delegate.task_request import TaskRequestStore, copy_workspace_snapshot


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    lead = tmp_path / "lead"
    lead.mkdir()
    backend = tmp_path / "backend"
    backend.mkdir()
    subprocess.run(["git", "-C", str(backend), "init", "-q"], check=True)
    (lead / "repos.yaml").write_text(
        "repositories:\n  - name: backend\n    path: ../backend\n",
        encoding="utf-8",
    )
    (lead / "agent-routing.yaml").write_text(
        "routes:\n  codex-balanced:\n    agent: codex\n    args: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("QIQI_WORKSPACE_ROOT", str(lead))
    runtime = DelegateRuntime(lead)
    store = TaskRequestStore(runtime.db, lead, runtime.repos)
    server = importlib.import_module("qiqi_delegate.server")
    monkeypatch.setattr(server, "runtime", runtime)
    monkeypatch.setattr(server, "task_requests", store)
    return lead, runtime, store, server


def create_source(lead, store, name="large.md"):
    # Must exceed both legacy 100,000-byte source and packet limits.
    data = ("Specification section \u2713\n" * 9_000)
    (lead / name).write_text(data, encoding="utf-8")
    req = store.create("Implement as specified", [
        {"kind": "workspace_file", "path": name}
    ])
    return req, req["sources"][0], data


def test_large_file_is_referenced_without_inlining(workspace):
    lead, _, store, server = workspace
    req, source, content = create_source(lead, store)
    assert source["transfer_mode"] == "reference"
    assert source["size_bytes"] == len(content.encode("utf-8"))
    assert "content" not in source
    lines = server._source_context_lines([source], [
        {"id": "R1", "text": "Implement", "evidence_refs": [source["id"]]}
    ])
    assert source["sha256"] in lines[0]
    assert "Specification section" not in lines[0]
    packet = build_task_packet(
        objective="Implement", scope=["backend"],
        acceptance_criteria=["Tests"], constraints=lines,
    )
    assert len(packet.to_json()) < 2000
    assert "content" not in store.get(req["request_id"])["sources"][0]


def test_workspace_reference_is_scoped_and_fails_closed_when_changed(workspace, tmp_path):
    lead, _, store, server = workspace
    req, source, content = create_source(lead, store)
    (lead / "other.md").write_text("other private document", encoding="utf-8")
    snapshot = tmp_path / "snapshot.txt"
    assert copy_workspace_snapshot(lead, source, snapshot) == source["size_bytes"]
    assert snapshot.read_text(encoding="utf-8") == content
    assert b"other private document" not in snapshot.read_bytes()
    (lead / "large.md").write_text(content + "modified", encoding="utf-8")
    assert store.get(req["request_id"])["stale_sources"] == [source["id"]]
    with pytest.raises(ValueError, match="changed"):
        copy_workspace_snapshot(lead, source, tmp_path / "rejected.txt")
    assert not (tmp_path / "rejected.txt").exists()


def test_native_peer_gets_only_cited_snapshot(workspace, monkeypatch):
    lead, runtime, store, server = workspace
    req, source, content = create_source(lead, store)
    (lead / "unreferenced.md").write_text("UNREFERENCED SECRET", encoding="utf-8")
    req2 = store.append(req["request_id"], req["revision"],
                        {"kind": "workspace_file", "path": "unreferenced.md"})
    refs = server._workspace_source_refs(req2["sources"], [
        {"id": "R1", "evidence_refs": [source["id"]]}
    ])
    assert len(refs) == 1
    observed = {}

    async def fake_ensure():
        return None

    async def fake_json(*args, **kwargs):
        assert args[:2] == ("workspace", "create")
        return {"workspace": {"workspace_id": "ws-1"},
                "root_pane": {"pane_id": "pane-1"}}

    async def fake_start(pane_id, adapter, args):
        observed["argv"] = args
        return "native-agent", {}

    async def fake_prompt(name, prompt, adapter):
        match = re.search(r"- Source source:[0-9a-f]+: (\S+) \(sha256=", prompt)
        assert match is not None
        snapshot = Path(match.group(1))
        assert snapshot.read_text(encoding="utf-8") == content
        assert list(snapshot.parent.iterdir()) == [snapshot]
        assert str(lead / "large.md") not in prompt
        assert "UNREFERENCED SECRET" not in prompt
        observed["snapshot"] = snapshot
        return "settled", {}

    async def fake_capture(*args):
        return {"state": "settled", "agent_response": "Reviewed"}

    async def fake_run(*args, **kwargs):
        assert args[:2] == ("workspace", "close")
        return 0, "", ""

    monkeypatch.setattr("qiqi_delegate.runtime.shutil.which", lambda _: "/bin/true")
    monkeypatch.setattr(runtime, "_ensure_herdr_server", fake_ensure)
    monkeypatch.setattr(runtime, "_json", fake_json)
    monkeypatch.setattr(runtime, "_start_agent", fake_start)
    monkeypatch.setattr(runtime, "_prompt", fake_prompt)
    monkeypatch.setattr(runtime, "_native_id", lambda agent, adapter: "session-1")
    monkeypatch.setattr(runtime, "_capture", fake_capture)
    monkeypatch.setattr(runtime, "_run", fake_run)
    packet = build_task_packet(
        objective="Review", scope=["backend"], acceptance_criteria=["Review"],
    )
    result = asyncio.run(runtime.delegate(
        repository="backend", route="codex-balanced",
        packet=packet, workspace_sources=refs,
    ))
    assert result["state"] == "settled"
    assert "--add-dir" in observed["argv"]
    assert str(observed["snapshot"].parent) in observed["argv"]
    assert not observed["snapshot"].exists()
