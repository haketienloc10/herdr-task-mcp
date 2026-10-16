"""Route metadata improves Lead selection without breaking legacy routing."""
import asyncio

import pytest

from test_lead_orchestration import environment as make_environment


@pytest.fixture
def environment(tmp_path, monkeypatch):
    return make_environment(tmp_path, monkeypatch)


def test_workspace_info_descriptions_and_cli_model_effort(environment):
    server, runtime, *_ = environment
    path = runtime.root / "agent-routing.yaml"
    path.write_text(
        'routes:\n'
        '  quick:\n'
        '    agent: claude\n'
        '    args: ["--model", "claude-sonnet-5-5", "--effort", "low", '
        '"--permission-mode", "auto"]\n'
        '    description: "Fast lookup of a known file or symbol."\n'
        '  inherited:\n'
        '    agent: claude\n'
        '    args: ["--permission-mode", "auto"]\n',
        encoding="utf-8",
    )
    result = asyncio.run(server.workspace_info())
    assert result["routes"] == {"quick": "claude", "inherited": "claude"}
    assert result["route_names"] == ["inherited", "quick"]
    assert result["route_details"]["quick"] == {
        "agent": "claude",
        "args": ["--model", "claude-sonnet-5-5", "--effort", "low",
                 "--permission-mode", "auto"],
        "description": "Fast lookup of a known file or symbol.",
        "model": "claude-sonnet-5-5",
        "effort": "low",
    }
    assert result["route_details"]["inherited"]["description"] == ""
    assert result["route_details"]["inherited"]["model"] is None
    assert result["route_details"]["inherited"]["effort"] is None
    assert "least effort" in result["route_usage"]


@pytest.mark.parametrize("description", ["", "  ", 7, [], "x" * 501])
def test_route_rejects_invalid_optional_description(environment, description):
    _server, runtime, *_ = environment
    import yaml
    config = {"routes": {"bad": {
        "agent": "claude", "args": [], "description": description,
    }}}
    (runtime.root / "agent-routing.yaml").write_text(
        yaml.safe_dump(config), encoding="utf-8",
    )
    with pytest.raises(ValueError, match="description"):
        runtime.route("bad")


def test_legacy_routes_accept_missing_descriptions(environment):
    _server, runtime, *_ = environment
    assert runtime.route("codex-balanced") == ("codex", [])
    assert runtime.route_details("codex-balanced")["description"] == ""


@pytest.mark.parametrize("agent,override", [
    ("claude", ["--settings", "{}"]),
    ("claude", ["--settings={\"hooks\":{}}"]),
    ("codex", ["--config=hooks.Stop=[]"]),
    ("codex", ["--config", "hooks.state=unsafe"]),
    ("codex", ["-c", "hooks.Stop=[]"]),
    ("codex", ["-chooks.Stop=[]"]),
    ("codex", ["-c=features.hooks=false"]),
    ("codex", ["-c", " hooks.Stop=[]"]),
    ("codex", ["--config", ' "hooks"."Stop" = [] ']),
    ("codex", ["-c", ' "hooks.Stop" = [] ']),
    ("codex", ["-c", '"features" . "hooks" = false']),
    ("codex", ["-c", '"hooks" = {}']),
    ("codex", ["--config='hooks'.state={}"]),
    # Ancestor override replaces the runtime's earlier features.hooks=true
    # even when the new table does not explicitly include "hooks".
    ("codex", ["-c", "features={hooks=false}"]),
    ("codex", ["--config=features={hooks=false}"]),
    ("codex", ["--config", ' features = { "hooks" = false }']),
    ("codex", ["-c", "features={}"]),
    ("codex", ['--config="features"={other=true}']),
    ("codex", ["-c", '"features" = "override"']),
    ("codex", ["--disable", "hooks"]),
    ("codex", ["--disable=hooks"]),
    ("codex", ["--enable", "hooks"]),
    ("codex", ["--enable=hooks"]),
    ("codex", ["--disable", "CoDeX_HoOkS"]),
    ("codex", ["hooks.Stop=something"]),
    ("codex", ["features.hooks=true"]),
])
def test_user_route_cannot_override_native_capture(environment, agent, override):
    _server, runtime, *_ = environment
    import yaml
    (runtime.root / "agent-routing.yaml").write_text(
        yaml.safe_dump({"routes": {"bad": {
            "agent": agent, "args": override,
            "description": "Attempt to override capture.",
        }}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="runtime-owned"):
        runtime.route("bad")

def test_codex_toml_override_allows_unrelated_config(environment):
    _server, runtime, *_ = environment
    import yaml
    config = {"routes": {"custom": {
        "agent": "codex",
        "args": ["-c", ' model_reasoning_effort = "high" ',
                 "-c", "model_reasoning_effort=high",
                 "--config=shell_environment_policy.inherit=all",
                 "--config=custom.feature=true",
                 "--config=features.other_feature=true"],
        "description": "Valid TOML overrides without native capture changes",
    }}}
    (runtime.root / "agent-routing.yaml").write_text(
        yaml.safe_dump(config), encoding="utf-8",
    )
    assert runtime.route("custom")[0] == "codex"


def test_claude_native_hook_settings_capture_stop_without_policy_overrides(
    environment,
):
    _server, runtime, *_ = environment
    import json
    from pathlib import Path

    argv = runtime._hook_args("claude", Path("/tmp"), "capture")
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert set(settings) == {"hooks"}
    assert {"Stop", "StopFailure"} <= set(settings["hooks"])
