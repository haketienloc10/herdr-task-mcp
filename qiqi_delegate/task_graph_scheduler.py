from __future__ import annotations

from dataclasses import dataclass, replace

from qiqi_delegate.task_graph import GraphNode, TaskGraph

SEMANTIC_STATES = frozenset({"pending", "satisfied", "blocked", "cancelled"})
RUNTIME_STATES = frozenset(
    {"idle", "running", "settled", "failed", "blocked", "awaiting_review"}
)
GRAPH_STATES = frozenset(
    {"ready", "running", "awaiting_review", "blocked", "complete"}
)
REVIEWABLE_RUNTIME_STATES = frozenset(
    {"settled", "failed", "blocked", "awaiting_review"}
)
DECISION_ACTIONS = frozenset({"accept", "retry", "replan", "block"})
TERMINAL_SEMANTIC_STATES = frozenset({"satisfied", "cancelled"})


@dataclass(frozen=True)
class NodeState:
    """Pure current state for one authored graph node.

    Runtime persistence, attempts, sessions, timestamps, and evidence belong to later
    runtime-store phases rather than this deterministic state machine.
    """

    node_id: str
    semantic_state: str = "pending"
    runtime_state: str = "idle"


@dataclass(frozen=True)
class GraphSnapshot:
    """An authored TaskGraph paired with immutable current node states."""

    graph: TaskGraph
    node_states: tuple[NodeState, ...]


@dataclass(frozen=True)
class NodeDecision:
    """QiQi semantic transition for one node after reviewable runtime output."""

    node_id: str
    action: str


def initial_graph_snapshot(graph: TaskGraph) -> GraphSnapshot:
    """Create the deterministic initial state without mutating the authored graph."""

    return GraphSnapshot(
        graph=graph,
        node_states=tuple(NodeState(node_id=node.node_id) for node in graph.nodes),
    )


def _state_map(snapshot: GraphSnapshot) -> dict[str, NodeState]:
    if not isinstance(snapshot, GraphSnapshot):
        raise ValueError("scheduler input must be a GraphSnapshot")
    if not isinstance(snapshot.graph, TaskGraph):
        raise ValueError("graph snapshot must contain a TaskGraph")
    if not isinstance(snapshot.node_states, tuple):
        raise ValueError("graph node_states must be a tuple")

    graph_ids = [node.node_id for node in snapshot.graph.nodes]
    if len(graph_ids) != len(set(graph_ids)):
        raise ValueError("task graph contains duplicate node IDs; validate graph first")

    result: dict[str, NodeState] = {}
    for state in snapshot.node_states:
        if not isinstance(state, NodeState):
            raise ValueError("graph node_states must contain NodeState values")
        if not isinstance(state.node_id, str) or not state.node_id.strip():
            raise ValueError("node state node_id must not be empty")
        if state.node_id in result:
            raise ValueError(f"duplicate node state for {state.node_id!r}")
        if state.semantic_state not in SEMANTIC_STATES:
            raise ValueError(
                f"node {state.node_id!r} has invalid semantic_state "
                f"{state.semantic_state!r}"
            )
        if state.runtime_state not in RUNTIME_STATES:
            raise ValueError(
                f"node {state.node_id!r} has invalid runtime_state "
                f"{state.runtime_state!r}"
            )
        result[state.node_id] = state

    graph_id_set = set(graph_ids)
    state_id_set = set(result)
    if graph_id_set != state_id_set:
        missing = sorted(graph_id_set - state_id_set)
        extra = sorted(state_id_set - graph_id_set)
        details: list[str] = []
        if missing:
            details.append("missing states for " + ", ".join(missing))
        if extra:
            details.append("states for unknown nodes " + ", ".join(extra))
        raise ValueError("graph snapshot node-state mismatch: " + "; ".join(details))

    return result


def _runnable_nodes(
    graph: TaskGraph,
    states: dict[str, NodeState],
) -> tuple[GraphNode, ...]:
    runnable: list[GraphNode] = []
    for node in graph.nodes:
        state = states[node.node_id]
        if state.semantic_state != "pending" or state.runtime_state != "idle":
            continue

        dependencies_satisfied = True
        for dependency in node.depends_on:
            dependency_state = states.get(dependency)
            if dependency_state is None:
                raise ValueError(
                    f"node {node.node_id!r} references unknown dependency "
                    f"{dependency!r}; validate graph first"
                )
            if dependency_state.semantic_state != "satisfied":
                dependencies_satisfied = False
                break

        if dependencies_satisfied:
            runnable.append(node)
    return tuple(runnable)


def runnable_nodes(snapshot: GraphSnapshot) -> tuple[GraphNode, ...]:
    """Return runnable nodes in authored graph order.

    A node is runnable only when it is semantically pending, runtime-idle, and every
    dependency is semantically satisfied. Runtime-settled/failed/blocked nodes are not
    automatically retried; they require a semantic decision first.
    """

    states = _state_map(snapshot)
    return _runnable_nodes(snapshot.graph, states)


def derive_graph_state(snapshot: GraphSnapshot) -> str:
    """Derive one explicit graph lifecycle state from the current node snapshot."""

    states = _state_map(snapshot)

    if any(state.runtime_state == "running" for state in states.values()):
        return "running"

    if any(
        state.semantic_state == "pending"
        and state.runtime_state in REVIEWABLE_RUNTIME_STATES
        for state in states.values()
    ):
        return "awaiting_review"

    if any(state.semantic_state == "blocked" for state in states.values()):
        return "blocked"

    if states and all(
        state.semantic_state in TERMINAL_SEMANTIC_STATES
        for state in states.values()
    ):
        return "complete"

    if _runnable_nodes(snapshot.graph, states):
        return "ready"

    # A validated non-empty DAG that still has pending work but no runnable,
    # running, or reviewable node cannot make deterministic progress. This also
    # covers pending work whose prerequisite was semantically cancelled.
    return "blocked"


def apply_decisions(
    snapshot: GraphSnapshot,
    decisions: tuple[NodeDecision, ...],
) -> GraphSnapshot:
    """Apply QiQi per-node review decisions immutably.

    `accept` satisfies the node, `retry` returns it to pending+idle, and `block`
    stops progress on an external/user dependency. `replan` is a fail-closed
    Phase-7 boundary: it also blocks the current authored graph so it cannot keep
    executing stale semantics. Phase 10 adds the graph mutation/reconciliation API
    that will replace this blocked graph with QiQi's newly authored topology.
    """

    states = _state_map(snapshot)
    if not isinstance(decisions, tuple):
        raise ValueError("graph decisions must be a tuple")

    updated = dict(states)
    seen: set[str] = set()
    for decision in decisions:
        if not isinstance(decision, NodeDecision):
            raise ValueError("graph decisions must contain NodeDecision values")
        if not isinstance(decision.node_id, str) or not decision.node_id.strip():
            raise ValueError("decision node_id must not be empty")
        if decision.node_id in seen:
            raise ValueError(f"duplicate decision for node {decision.node_id!r}")
        seen.add(decision.node_id)

        if decision.node_id not in updated:
            raise ValueError(f"decision references unknown node {decision.node_id!r}")
        if decision.action not in DECISION_ACTIONS:
            raise ValueError(
                f"unsupported decision action {decision.action!r}"
            )

        current = updated[decision.node_id]
        if (
            current.semantic_state != "pending"
            or current.runtime_state not in REVIEWABLE_RUNTIME_STATES
        ):
            raise ValueError(
                f"node {decision.node_id!r} is not awaiting a semantic decision"
            )

        if decision.action == "accept":
            updated[decision.node_id] = replace(
                current,
                semantic_state="satisfied",
                runtime_state="idle",
            )
        elif decision.action == "retry":
            updated[decision.node_id] = replace(
                current,
                semantic_state="pending",
                runtime_state="idle",
            )
        else:  # block | replan
            updated[decision.node_id] = replace(
                current,
                semantic_state="blocked",
                runtime_state="idle",
            )

    return GraphSnapshot(
        graph=snapshot.graph,
        node_states=tuple(updated[state.node_id] for state in snapshot.node_states),
    )
