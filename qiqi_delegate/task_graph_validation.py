from __future__ import annotations

from collections.abc import Collection
from typing import Any

from qiqi_delegate.core import (
    ClaimToInvestigate,
    TaskContext,
    TaskPacket,
    TrustedFact,
    build_task_packet,
)
from qiqi_delegate.task_graph import GraphNode, TaskGraph


def _tuple_as_builder_list(value: Any) -> Any:
    """Convert a valid TaskPacket tuple field without hiding invalid raw values."""

    if isinstance(value, tuple):
        return list(value)
    return value


def _task_context_as_builder_input(value: Any) -> Any:
    """Preserve every raw context field while adapting valid dataclasses."""

    if not isinstance(value, TaskContext):
        return value

    trusted_facts: Any = value.trusted_facts
    if isinstance(trusted_facts, tuple):
        trusted_facts = [
            {"fact": item.fact, "source": item.source}
            if isinstance(item, TrustedFact)
            else item
            for item in trusted_facts
        ]

    claims: Any = value.claims_to_investigate
    if isinstance(claims, tuple):
        claims = [
            {"claim": item.claim, "source": item.source}
            if isinstance(item, ClaimToInvestigate)
            else item
            for item in claims
        ]

    return {
        "trusted_facts": trusted_facts,
        "claims_to_investigate": claims,
    }


def _task_packet_as_builder_input(packet: TaskPacket) -> dict[str, Any]:
    """Return a lossless builder payload for an existing TaskPacket object.

    Unlike TaskPacket.as_dict(), this intentionally includes falsy optional fields.
    Valid in-memory tuple/dataclass representations are adapted to the public builder
    input shape, while malformed raw values are preserved so build_task_packet()
    rejects them through the canonical validation path.
    """

    return {
        "objective": packet.objective,
        "scope": _tuple_as_builder_list(packet.scope),
        "acceptance_criteria": _tuple_as_builder_list(packet.acceptance_criteria),
        "out_of_scope": _tuple_as_builder_list(packet.out_of_scope),
        "context": _task_context_as_builder_input(packet.context),
        "constraints": _tuple_as_builder_list(packet.constraints),
        "known_unknowns": _tuple_as_builder_list(packet.known_unknowns),
    }


def validate_task_graph(
    graph: TaskGraph,
    *,
    repository_names: Collection[str],
) -> None:
    """Validate authored graph structure without introducing a second task schema.

    `repository_names` must come from the canonical workspace repository registry.
    Task semantics are revalidated through the existing `build_task_packet()` path;
    this module owns only graph-specific invariants.
    """

    if not isinstance(graph, TaskGraph):
        raise ValueError("task graph must be a TaskGraph")
    if not isinstance(graph.nodes, tuple):
        raise ValueError("task graph nodes must be a tuple of GraphNode objects")
    if not graph.nodes:
        raise ValueError("task graph must contain at least one node")

    if isinstance(repository_names, (str, bytes)):
        raise ValueError("repository_names must be a collection of repository names")
    repositories = set(repository_names)
    if any(not isinstance(name, str) or not name.strip() for name in repositories):
        raise ValueError("repository_names must contain non-empty strings")

    node_ids: set[str] = set()

    for node in graph.nodes:
        if not isinstance(node, GraphNode):
            raise ValueError("task graph nodes must be GraphNode objects")
        if not isinstance(node.node_id, str) or not node.node_id.strip():
            raise ValueError("graph node_id must not be empty")
        if node.node_id in node_ids:
            raise ValueError(f"duplicate graph node_id: {node.node_id!r}")
        node_ids.add(node.node_id)

        if node.kind != "repo_task":
            raise ValueError(
                f"unsupported graph node kind {node.kind!r} for node {node.node_id!r}"
            )
        if not isinstance(node.repository, str) or not node.repository.strip():
            raise ValueError(f"node {node.node_id!r} repository must not be empty")
        if node.repository not in repositories:
            raise ValueError(
                f"node {node.node_id!r} references unknown repository {node.repository!r}"
            )
        if node.route is not None and (
            not isinstance(node.route, str) or not node.route.strip()
        ):
            raise ValueError(
                f"node {node.node_id!r} route must be a non-empty string when provided"
            )
        if not isinstance(node.task_packet, TaskPacket):
            raise ValueError(f"repo_task node {node.node_id!r} must contain a TaskPacket")
        if not isinstance(node.depends_on, tuple):
            raise ValueError(f"node {node.node_id!r} depends_on must be a tuple")
        for dependency in node.depends_on:
            if not isinstance(dependency, str) or not dependency.strip():
                raise ValueError(
                    f"node {node.node_id!r} dependencies must be non-empty strings"
                )

        try:
            build_task_packet(**_task_packet_as_builder_input(node.task_packet))
        except ValueError as exc:
            raise ValueError(
                f"node {node.node_id!r} has invalid TaskPacket: {exc}"
            ) from exc

    dependents: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    indegree: dict[str, int] = {}

    for node in graph.nodes:
        indegree[node.node_id] = len(node.depends_on)
        for dependency in node.depends_on:
            if dependency == node.node_id:
                raise ValueError(f"node {node.node_id!r} must not depend on itself")
            if dependency not in node_ids:
                raise ValueError(
                    f"node {node.node_id!r} references unknown dependency {dependency!r}"
                )
            dependents[dependency].append(node.node_id)

    roots = [node_id for node_id, degree in indegree.items() if degree == 0]
    ready = list(roots)
    visited = 0

    while ready:
        current = ready.pop()
        visited += 1
        for dependent in dependents[current]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)

    if visited != len(node_ids):
        cyclic = sorted(node_id for node_id, degree in indegree.items() if degree > 0)
        raise ValueError(f"graph contains a dependency cycle involving: {', '.join(cyclic)}")

    terminal_nodes = [node_id for node_id, items in dependents.items() if not items]
    if not roots or not terminal_nodes:
        raise ValueError("task graph has no executable root-to-terminal work path")
