"""Strict native CLI profiles for independent Final Evaluation.

Claude uses a restricted evaluation harness, only built-in read-only file
tools, no MCP tools/skills, and no interactive approval. This policy is not
a kernel-enforced filesystem sandbox.
"""
from __future__ import annotations

CODEX_EVALUATOR_ARGS = (
    ("--sandbox", "read-only"),
    ("--sandbox=read-only",),
)
CLAUDE_EVALUATOR_ARGS = (
    "--restricted",
    "--permission-mode", "dontAsk",
    "--tools", "Read,Glob,Grep",
    "--allowedTools", "Read,Glob,Grep",
    "--disallowedTools", "mcp__*",
    "--disable-slash-commands",
)


def validate_evaluator_route(adapter: str, args: list[str]) -> None:
    """Reject CLI overrides, bypass flags, and weaker evaluator profiles."""
    if adapter == "codex" and tuple(args) in CODEX_EVALUATOR_ARGS:
        return
    if adapter == "claude" and tuple(args) == CLAUDE_EVALUATOR_ARGS:
        return
    raise ValueError(
        "Final Evaluation requires a safe Codex or Claude evaluator route: "
        "Codex --sandbox read-only or exact restricted Claude profile "
        "(--restricted, read-only tools, no MCP/skills, dontAsk); "
        "unsafe route or CLI overrides are forbidden"
    )
