"""Native Final Evaluation report capture and acceptance-gate regressions."""
import asyncio
import json
import sqlite3

import pytest

from qiqi_delegate.core import render_task_prompt
from qiqi_delegate.final_eval import _parse_native_report
from qiqi_delegate.final_eval_git_state import review_scope
from test_final_evaluation import report, setup_graph


@pytest.mark.parametrize("presentation", ["plain", "fenced", "fenced_uppercase"])
def test_single_json_report_capture_can_pass_without_pre_e2e(
    tmp_path, monkeypatch, presentation,
):
    """No unrequested E2E gate; a single fenced native JSON is still validated."""
    runtime, _requests, graph_rt, coordinator, gid = setup_graph(
        tmp_path, multi=False,
    )

    async def delegate(**kwargs):
        coordinator.store.bind_turn(kwargs["evaluation_id"], "json-capture-native")
        manifest = coordinator.store.get(kwargs["evaluation_id"])["manifest"]
        payload = report(manifest)
        assert payload["verification_runs"] == []
        if presentation == "fenced_uppercase":
            payload["verdict"] = "PASS"
            payload["requirement_results"][0]["status"] = "PASS"
        response = json.dumps(payload)
        if presentation.startswith("fenced"):
            fence = chr(96) * 3
            response = fence + "json\n" + response + "\n" + fence
        with sqlite3.connect(runtime.db) as db:
            db.execute(
                "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("json-capture-native", "native-evaluator", "backend",
                 "codex-evaluator", "settled", response, 200),
            )
        return {
            "state": "settled", "turn_id": "json-capture-native",
            "agent_response": response,
        }

    monkeypatch.setattr(runtime, "delegate", delegate)
    revision = graph_rt.get_graph(gid)["revision"]
    result = asyncio.run(coordinator.start(gid, "codex-evaluator", revision))
    assert result["status"] == "passed"
    assert result["report"]["verdict"] == "pass"
    assert result["report"]["requirement_results"][0]["status"] == "pass"
    assert coordinator.finalize(gid, result["evaluation_id"], revision)[
        "finalized_at_ns"
    ]


@pytest.mark.parametrize("invalid", [
    "Here is my report: {\"verdict\":\"pass\"}",
    chr(96) * 3 + "json\n{\"verdict\":\"pass\"}\n" + chr(96) * 3 + "\nThanks",
    chr(96) * 3 + "json\n{\"verdict\":\"pass\"}\n" + chr(96) * 3
        + "\n" + chr(96) * 3 + "json\n{}\n" + chr(96) * 3,
])
def test_report_parser_does_not_extract_json_from_prose(invalid):
    with pytest.raises(ValueError):
        _parse_native_report(invalid)


def test_v3_report_rejects_unverifiable_per_file_hashes(tmp_path):
    """The v3 Git manifest has no file hash list; never imply it does."""
    runtime, _requests, _graph_rt, coordinator, _gid = setup_graph(
        tmp_path, multi=False,
    )
    manifest = review_scope(runtime.repos())
    payload = report(manifest)
    payload["requirement_results"][0]["evidence"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="malformed evaluation evidence fields"):
        coordinator.store.validate_report(payload, manifest, {"R1"})


def test_native_evaluator_prompt_respects_requested_verification_scope(tmp_path):
    """Pre-deployment E2E is not silently added to a code implementation task."""
    runtime, _requests, _graph_rt, coordinator, _gid = setup_graph(
        tmp_path, multi=False,
    )
    packet = coordinator._packet(("backend",), "captured-fingerprint")
    rendered = render_task_prompt(
        packet, evaluation_repositories=("backend",),
    )
    assert "Pre/staging/E2E verification is mandatory for PASS ONLY when" in rendered
    assert "original user requirement" in rendered
    assert "an empty list is valid and is not by itself a blocker" in rendered
    assert "Existing untracked working-tree files are present for review" in rendered
    assert "NO sha256" in rendered
    assert "Return ONE raw JSON object only" in rendered
