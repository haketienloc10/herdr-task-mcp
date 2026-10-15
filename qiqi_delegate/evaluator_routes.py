"""Legacy evaluator route helper.

The evaluator CLI profile is user-configured via agent-routing.yaml. This module
is retained for callers importing its old symbols, but the native runtime no
longer requires a fixed permissions, model or effort profile.
"""
from __future__ import annotations

# Recommended starter defaults, not a security boundary or a required profile.
CLAUDE_EVALUATOR_ARGS = ("--permission-mode", "auto")
CODEX_EVALUATOR_ARGS = (("--sandbox", "read-only"), ("--sandbox=read-only",))


def validate_evaluator_route(adapter: str, args: list[str]) -> None:
    """Compatibility shape check; does not enforce evaluator CLI flags."""
    if adapter not in {"claude", "codex"}:
        raise ValueError("evaluator agent must be claude or codex")
    if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
        raise ValueError("evaluator args must be a list of CLI strings")
