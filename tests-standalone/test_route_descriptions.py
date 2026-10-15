"""Route metadata improves Lead selection without breaking legacy routing."""
import asyncio

import pytest

from test_lead_orchestration import environment


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


@pytest.mark.parametrize("override", [
    ["--settings", "{}"],
    ["hooks.Stop=something"],
    ["features.hooks=true"],
])
def test_user_route_cannot_override_native_capture(environment, override):
    _server, runtime, *_ = environment
    import yaml
    (runtime.root / "agent-routing.yaml").write_text(
        yaml.safe_dump({"routes": {"bad": {
            "agent": "claude", "args": override,
            "description": "Attempt to override capture.",
        }}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="runtime-owned"):
        runtime.route("bad")
