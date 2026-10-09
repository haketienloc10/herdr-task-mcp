from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from qiqi_delegate.core import TaskPacket


@dataclass(frozen=True)
class GraphNode:
    """One durable graph work unit around the existing TaskPacket contract.

    Phase 1 intentionally models structure only. Validation, runnable calculation,
    semantic/runtime state transitions, persistence, and execution belong to later
    phases.
    """

    node_id: str
    repository: str
    task_packet: TaskPacket
    depends_on: tuple[str, ...] = ()
    route: str | None = None
    kind: str = "repo_task"

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "node_id": self.node_id,
            "kind": self.kind,
            "repository": self.repository,
            "depends_on": list(self.depends_on),
            "task_packet": self.task_packet.as_dict(),
        }
        if self.route is not None:
            result["route"] = self.route
        return result


@dataclass(frozen=True)
class TaskGraph:
    """Authored semantic work breakdown; execution behavior is added later."""

    nodes: tuple[GraphNode, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"nodes": [node.as_dict() for node in self.nodes]}
