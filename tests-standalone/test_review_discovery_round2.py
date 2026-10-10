"""Regression tests for Codex review #5478819900 (PR #7)."""
import asyncio
import hashlib
import importlib

import pytest

from test_task_readiness import workspace
from qiqi_delegate.core import render_task_prompt
from qiqi_delegate.task_request import TaskRequestStore


def _server(tmp_path, monkeypatch):
    runtime, store = workspace(tmp_path)
    monkeypatch.setenv("QIQI_WORKSPACE_ROOT", str(runtime.root))
    server = importlib.import_module("qiqi_delegate.server")
    monkeypatch.setattr(server, "runtime", runtime)
    monkeypatch.setattr(server, "task_requests", store)
    return server, runtime, store


def _discovery_assessment(source_ref):
    return {
        "requirements": [
            {"id": "R1", "text": "Find the idempotency contract",
             "evidence_refs": [source_ref]}
        ],
        "blocking_unknowns": ["Current idempotency behavior is unknown"],
        "decision": "targeted_discovery",
        "rationale": "Need an evidence-backed inspection before implementation",
    }


def test_discovery_only_sends_assessed_source_refs(tmp_path, monkeypatch):
    server, runtime, store = _server(tmp_path, monkeypatch)
    # Each source is valid on its own, but all sources together exceed the
    # global TaskPacket limit. The tenth source is the only one required.
    sources = [
        {"kind": "inline", "label": f"irrelevant-{i}",
         "text": f"IRRELEVANT_SOURCE_{i}:" + ("z" * 14000)}
        for i in range(9)
    ]
    relevant_text = "REQUIRED_SPEC_TAIL:" + ("y" * 8000) + ":idempotency-key"
    sources.append({"kind": "inline", "label": "required", "text": relevant_text})
    request = store.create("Investigate idempotency before modifying the order flow",
                           sources=sources)
    source_ref = request["sources"][9]["id"]
    ready = store.assess(request["request_id"], request["revision"],
                         _discovery_assessment(source_ref))
    recorded = {}

    async def fake_delegate(**kwargs):
        recorded.update(kwargs)
        return {"state": "blocked", "turn_id": "fake", "agent_response": None}

    monkeypatch.setattr(runtime, "delegate", fake_delegate)
    result = asyncio.run(server.delegate_discovery(
        ready["request_id"], ["backend", "frontend"], "codex-balanced",
        ["Inspect idempotency and cite file:line"],
    ))
    assert result["result"]["state"] == "blocked"
    prompt = render_task_prompt(
        recorded["packet"], discovery_repositories=recorded["discovery_repositories"]
    )
    assert relevant_text in prompt
    assert source_ref in prompt
    assert "IRRELEVANT_SOURCE_0" not in prompt
    assert "IRRELEVANT_SOURCE_8" not in prompt
    assert recorded["discovery_repositories"] == ("backend", "frontend")


def test_discovery_can_run_without_optional_sources(tmp_path, monkeypatch):
    server, runtime, store = _server(tmp_path, monkeypatch)
    request = store.create("Investigate duplicate orders")
    store.assess(request["request_id"], 1,
                 _discovery_assessment("request:current"))
    sent = {}

    async def fake_delegate(**kwargs):
        sent.update(kwargs)
        return {"state": "blocked", "agent_response": None}

    monkeypatch.setattr(runtime, "delegate", fake_delegate)
    asyncio.run(server.delegate_discovery(
        request["request_id"], ["backend"], "codex-balanced",
        ["Find source of duplicate orders"],
    ))
    prompt = render_task_prompt(
        sent["packet"], discovery_repositories=sent["discovery_repositories"]
    )
    assert "Investigate duplicate orders" in prompt
    assert "Source source:" not in prompt


@pytest.mark.parametrize("response", [
    "E" * 160_000 + " CRITICAL_EVIDENCE_AT_END",
    "多" * 120_000 + " CONTRACT_IN_UTF8_TAIL",
])
def test_oversized_native_discovery_output_is_persisted_in_full(
    tmp_path, monkeypatch, response,
):
    server, runtime, store = _server(tmp_path, monkeypatch)
    request = store.create("Investigate payment retry behavior")
    store.assess(request["request_id"], 1,
                 _discovery_assessment("request:current"))

    async def fake_delegate(**kwargs):
        with runtime._connect() as db:
            db.execute(
                "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("discovery-large-turn", "native-session-abc", "backend",
                 "codex-balanced", "settled", response, 1234),
            )
        return {"state": "settled", "turn_id": "discovery-large-turn",
                "agent_response": response}

    monkeypatch.setattr(runtime, "delegate", fake_delegate)
    result = asyncio.run(server.delegate_discovery(
        request["request_id"], ["backend"], "codex-balanced",
        ["Trace retry handling with file:line"],
    ))
    assert result["result"]["agent_response"] == response
    assert result["task_request"]["assessment"] is None
    assert result["task_request"]["revision"] == 3
    assert result["task_request"]["discoveries"][0]["state"] == "settled"
    source = result["task_request"]["sources"][0]
    assert source["content"] == response
    assert source["verification"] == "peer_observed"
    assert source["sha256"] == hashlib.sha256(response.encode("utf-8")).hexdigest()

    # A new MCP session can retrieve the same evidence and reassess it.
    reloaded = TaskRequestStore(runtime.db, runtime.root, runtime.repos)
    source_reload = reloaded.get(request["request_id"])["sources"][0]
    assert source_reload["content"] == response
    reassessed = reloaded.assess(
        request["request_id"], 3,
        {"requirements": [
            {"id": "R1", "text": "Implement the verified retry contract",
             "evidence_refs": [source_reload["id"]]},
        ], "blocking_unknowns": [], "decision": "direct",
         "rationale": "Discovery identified the implementation task"},
    )
    assert reassessed["assessment"]["decision"] == "direct"

