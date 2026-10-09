from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, replace
from typing import Any

from qiqi_delegate.core import TaskPacket, build_task_packet
from qiqi_delegate.runtime import AgentStartupBlocked
from qiqi_delegate.task_graph import GraphNode, TaskGraph
from qiqi_delegate.task_graph_scheduler import (
    REVIEWABLE_RUNTIME_STATES,
    GraphSnapshot,
    NodeDecision,
    NodeState,
    apply_decisions,
    derive_graph_state,
    initial_graph_snapshot,
    runnable_nodes,
)
from qiqi_delegate.task_graph_store import GraphRuntimeStore, new_wave_id
from qiqi_delegate.task_graph_validation import validate_task_graph

_GRAPH_FIELDS = frozenset({"nodes"})
_NODE_FIELDS = frozenset(
    {"node_id", "kind", "repository", "route", "depends_on", "task_packet"}
)
_NODE_REQUIRED_FIELDS = frozenset({"node_id", "repository", "task_packet"})
_DECISION_FIELDS = frozenset(
    {"node_id", "action", "resume_session", "feedback", "owner", "return_checkpoint"}
)
_DECISION_REQUIRED_FIELDS = frozenset({"node_id", "action"})
_EXECUTION_TERMINAL_STATES = frozenset({"settled", "failed", "blocked", "capture_ambiguous"})
_RETRY_FEEDBACK_SOURCE = "QiQi semantic review"
MAX_BATCH_REVIEW_HYDRATIONS = 8

RepoTaskExecutor = Callable[[GraphNode], Awaitable[dict[str, Any]]]
ResumeRepoTaskExecutor = Callable[[GraphNode, str], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ReviewDecision:
    """QiQi semantic decision plus retry-only execution guidance."""

    node_id: str
    action: str
    resume_session: bool = False
    feedback: tuple[str, ...] = ()
    owner: str | None = None
    return_checkpoint: str | None = None


@dataclass(frozen=True)
class RetryPlan:
    """Persisted execution plan for the next attempt of one reviewed node."""

    task_packet: TaskPacket
    resume_session: bool
    session_id: str | None
    feedback: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_packet": self.task_packet.as_dict(),
            "resume_session": self.resume_session,
            "session_id": self.session_id,
            "feedback": list(self.feedback),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RetryPlan":
        if set(payload) != {"task_packet", "resume_session", "session_id", "feedback"}:
            raise RuntimeError("persisted retry plan has invalid fields")
        resume = payload["resume_session"]
        session_id = payload["session_id"]
        feedback = payload["feedback"]
        if not isinstance(resume, bool):
            raise RuntimeError("persisted retry plan has invalid resume_session")
        if resume and (not isinstance(session_id, str) or not session_id.strip()):
            raise RuntimeError("persisted retry plan has no RESUME session")
        if not resume and session_id is not None:
            raise RuntimeError("persisted fresh retry plan unexpectedly has a session")
        if not isinstance(feedback, list):
            raise RuntimeError("persisted retry feedback must be an array")
        try:
            parsed_feedback = _feedback_from_payload(feedback, "retry.feedback")
            packet = build_task_packet(**payload["task_packet"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("persisted retry plan is invalid") from exc
        return cls(
            task_packet=packet, resume_session=resume,
            session_id=session_id, feedback=parsed_feedback,
        )


@dataclass(frozen=True)
class WaveAttempt:
    """One claimed node execution inside the current runtime wave."""

    node: GraphNode
    retry_plan: RetryPlan | None
    attempt_id: str


class RecoverableRepoTaskExecutionError(RuntimeError):
    """Execution failed after native session ownership was already captured."""

    def __init__(self, message: str, *, session_id: str):
        super().__init__(message)
        self.session_id = session_id


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"graph {label} must be an object")
    return value


def _reject_extra_fields(value: dict[str, Any], allowed: Collection[str], label: str) -> None:
    extra = sorted(set(value) - set(allowed))
    if extra:
        raise ValueError(
            f"graph {label} has unsupported fields: {', '.join(extra)}"
        )


def _required_execution_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"repo-task execution result {label} must be a non-empty string")
    return value


def _validated_execution_result(value: Any) -> dict[str, Any]:
    """Validate the normalized result returned by the existing repo-task primitive."""

    if not isinstance(value, dict):
        raise RuntimeError("repo-task executor returned a non-object result")
    state = value.get("state")
    if state not in _EXECUTION_TERMINAL_STATES:
        raise RuntimeError(
            f"repo-task executor returned unsupported terminal state: {state!r}"
        )
    _required_execution_id(value.get("session_id"), "session_id")
    _required_execution_id(value.get("turn_id"), "turn_id")
    if "agent_response" not in value:
        raise RuntimeError("repo-task execution result is missing agent_response")
    response = value["agent_response"]
    if response is not None and not isinstance(response, str):
        raise RuntimeError("repo-task execution result agent_response must be a string or null")
    if state == "capture_ambiguous":
        if response is not None:
            raise RuntimeError(
                "capture_ambiguous repo-task result must not include agent_response"
            )
        candidate_count = value.get("candidate_count")
        if (
            isinstance(candidate_count, bool)
            or not isinstance(candidate_count, int)
            or candidate_count < 2
        ):
            raise RuntimeError(
                "capture_ambiguous repo-task result candidate_count must be an integer >= 2"
            )
    return dict(value)


def _persisted_runtime_state(execution_state: str) -> str:
    """Map transport-only review states onto the durable scheduler state vocabulary."""

    return "settled" if execution_state == "capture_ambiguous" else execution_state


def _feedback_from_payload(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"graph {label} must be a list of strings")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(f"graph {label}[{index}] must be a string")
        cleaned = item.strip()
        if not cleaned:
            raise ValueError(f"graph {label}[{index}] must not be empty")
        result.append(cleaned)
    return tuple(result)


def _optional_decision_text(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"graph {label} must be a non-empty string")
    return value.strip()


def _retry_task_packet(packet: TaskPacket, feedback: tuple[str, ...]) -> TaskPacket:
    """Create a fresh delegated-turn TaskPacket without mutating authored semantics.

    Retry feedback is execution context, not a new Graph schema. It is represented through
    the existing TaskPacket context contract as claims to investigate, while objective,
    scope, acceptance criteria, exclusions, constraints, and known unknowns stay authored.
    Feedback that repeats an existing trusted fact or claim keeps the existing classification
    instead of creating a contradictory/duplicate context entry.
    """

    payload = packet.as_dict()
    if feedback:
        context = dict(payload.get("context", {}))
        trusted = list(context.get("trusted_facts", []))
        claims = list(context.get("claims_to_investigate", []))
        known_propositions = {
            item["fact"].casefold()
            for item in trusted
            if isinstance(item, dict) and isinstance(item.get("fact"), str)
        }
        known_propositions.update(
            item["claim"].casefold()
            for item in claims
            if isinstance(item, dict) and isinstance(item.get("claim"), str)
        )
        for item in feedback:
            key = item.casefold()
            if key in known_propositions:
                continue
            claims.append({"claim": item, "source": _RETRY_FEEDBACK_SOURCE})
            known_propositions.add(key)
        if claims:
            context["claims_to_investigate"] = claims
        payload["context"] = context
    return build_task_packet(**payload)


def task_graph_from_payload(payload: Any) -> TaskGraph:
    """Build a TaskGraph transport value without introducing another task schema.

    Graph transport shape is decoded here, while delegated-task semantics still flow
    through the existing build_task_packet() contract.
    """

    graph_value = _require_object(payload, "payload")
    _reject_extra_fields(graph_value, _GRAPH_FIELDS, "payload")
    if "nodes" not in graph_value:
        raise ValueError("graph payload is missing required field: nodes")

    raw_nodes = graph_value["nodes"]
    if not isinstance(raw_nodes, list):
        raise ValueError("graph nodes must be a list")

    nodes: list[GraphNode] = []
    for index, raw_node in enumerate(raw_nodes):
        label = f"nodes[{index}]"
        node_value = _require_object(raw_node, label)
        _reject_extra_fields(node_value, _NODE_FIELDS, label)
        missing = sorted(_NODE_REQUIRED_FIELDS - set(node_value))
        if missing:
            raise ValueError(
                f"graph {label} is missing required fields: {', '.join(missing)}"
            )

        raw_packet = node_value["task_packet"]
        if not isinstance(raw_packet, dict):
            raise ValueError(f"graph {label}.task_packet must be an object")
        try:
            packet = build_task_packet(**raw_packet)
        except TypeError as exc:
            raise ValueError(
                f"graph {label} has invalid TaskPacket fields: {exc}"
            ) from exc
        except ValueError as exc:
            raise ValueError(f"graph {label} has invalid TaskPacket: {exc}") from exc

        raw_dependencies = node_value.get("depends_on", [])
        depends_on: Any
        if isinstance(raw_dependencies, list):
            depends_on = tuple(raw_dependencies)
        else:
            # Preserve malformed containers so the canonical graph validator rejects
            # them instead of silently changing the authored topology.
            depends_on = raw_dependencies

        nodes.append(
            GraphNode(
                node_id=node_value["node_id"],
                repository=node_value["repository"],
                task_packet=packet,
                depends_on=depends_on,
                route=node_value.get("route"),
                kind=node_value.get("kind", "repo_task"),
            )
        )

    return TaskGraph(nodes=tuple(nodes))


def _graph_reconciliation_sets(
    previous_graph: TaskGraph,
    next_graph: TaskGraph,
    *,
    preserved_blocked_node_ids: Collection[str] = (),
) -> tuple[set[str], set[str], set[str], set[str]]:
    """Return added, removed, directly changed, and dependency-invalidated node IDs.

    An unchanged semantically blocked node is an explicit QiQi stop condition, so upstream
    material changes do not silently release it. Changing that node explicitly still resets
    it and allows invalidation to propagate through its descendants.
    """

    previous_nodes = {node.node_id: node for node in previous_graph.nodes}
    next_nodes = {node.node_id: node for node in next_graph.nodes}
    previous_ids = set(previous_nodes)
    next_ids = set(next_nodes)
    added = next_ids - previous_ids
    removed = previous_ids - next_ids
    changed = {
        node_id
        for node_id in previous_ids & next_ids
        if previous_nodes[node_id] != next_nodes[node_id]
    }

    blocked_barriers = set(preserved_blocked_node_ids) - changed
    affected = set(added | changed)
    dependency_invalidated: set[str] = set()
    changed_any = True
    while changed_any:
        changed_any = False
        for node in next_graph.nodes:
            if node.node_id in affected or node.node_id in blocked_barriers:
                continue
            if any(dependency in affected for dependency in node.depends_on):
                affected.add(node.node_id)
                dependency_invalidated.add(node.node_id)
                changed_any = True

    return added, removed, changed, dependency_invalidated


def decisions_from_payload(payload: Any) -> tuple[ReviewDecision, ...]:
    if not isinstance(payload, list):
        raise ValueError("graph decisions must be a list")
    decisions: list[ReviewDecision] = []
    for index, raw_decision in enumerate(payload):
        label = f"decisions[{index}]"
        decision = _require_object(raw_decision, label)
        _reject_extra_fields(decision, _DECISION_FIELDS, label)
        missing = sorted(_DECISION_REQUIRED_FIELDS - set(decision))
        if missing:
            raise ValueError(
                f"graph {label} is missing required fields: {', '.join(missing)}"
            )

        action = decision["action"]
        has_retry_metadata = "resume_session" in decision or "feedback" in decision
        has_defer_metadata = "owner" in decision or "return_checkpoint" in decision
        if action != "retry" and has_retry_metadata:
            raise ValueError(
                f"graph {label} retry metadata is only valid for action='retry'"
            )
        if action not in {"replan", "block"} and has_defer_metadata:
            raise ValueError(
                f"graph {label} defer metadata is only valid for action='replan' or 'block'"
            )

        resume_session = decision.get("resume_session", False)
        if not isinstance(resume_session, bool):
            raise ValueError(f"graph {label}.resume_session must be a boolean")
        feedback = _feedback_from_payload(decision.get("feedback"), f"{label}.feedback")
        owner = _optional_decision_text(decision.get("owner"), f"{label}.owner")
        return_checkpoint = _optional_decision_text(
            decision.get("return_checkpoint"),
            f"{label}.return_checkpoint",
        )
        if action in {"replan", "block"} and (
            owner is None or return_checkpoint is None
        ):
            raise ValueError(
                f"graph {label} action={action!r} requires owner and return_checkpoint"
            )

        decisions.append(
            ReviewDecision(
                node_id=decision["node_id"],
                action=action,
                resume_session=resume_session,
                feedback=feedback,
                owner=owner,
                return_checkpoint=return_checkpoint,
            )
        )
    if not decisions:
        raise ValueError("graph decisions must contain at least one decision")
    return tuple(decisions)


class GraphRuntime:
    """QiQi outer-loop runtime over scheduler, persistence, and repo-task execution.

    Authored graph, semantic states, execution attempts and retry plans are persisted.
    Completed/reviewable work survives server restart. In-flight waves remain
    fail-closed until external worker termination is confirmed.
    """

    def __init__(
        self,
        store: GraphRuntimeStore,
        *,
        repository_key: Callable[[str], str] | None = None,
    ):
        self.store = store
        # The MCP runtime supplies canonical Git-root identities. Standalone
        # scheduler tests may use logical names when no workspace exists.
        self.repository_key = repository_key or (lambda name: name)

    def _pending_retry_plans(self, graph_run_id: str) -> dict[str, RetryPlan]:
        return {
            node_id: RetryPlan.from_dict(payload)
            for node_id, payload in self.store.get_retry_plans(graph_run_id).items()
        }

    def start_graph(
        self,
        graph: TaskGraph,
        *,
        repository_names: Collection[str],
    ) -> dict[str, Any]:
        validate_task_graph(graph, repository_names=repository_names)
        snapshot = initial_graph_snapshot(graph)
        graph_run_id = self.store.create_run(snapshot)
        return self.get_graph(graph_run_id)

    def _graph_for_run(self, graph_run_id: str) -> TaskGraph:
        if not isinstance(graph_run_id, str) or not graph_run_id.strip():
            raise ValueError("graph_run_id must be a non-empty string")
        graph_run_id = graph_run_id.strip()
        return self.store.load_graph(graph_run_id)

    def _snapshot(self, graph_run_id: str) -> tuple[TaskGraph, GraphSnapshot, int]:
        graph = self._graph_for_run(graph_run_id)
        # Restart may happen after all attempts settled but before close_wave().
        # Close only proven-quiescent waves; running attempts remain fail-closed.
        self.store.recover_quiescent_wave(graph_run_id)
        snapshot, revision = self.store.load_snapshot_with_revision(graph_run_id, graph)
        return graph, snapshot, revision

    def get_graph(self, graph_run_id: str) -> dict[str, Any]:
        graph, snapshot, revision = self._snapshot(graph_run_id)
        run = self.store.get_run(graph_run_id)
        if run is None:
            raise RuntimeError(f"unknown graph_run_id: {graph_run_id!r}")

        graph_nodes = {node.node_id: node for node in graph.nodes}
        retry_plans = self._pending_retry_plans(graph_run_id)
        lead_decisions = self.store.get_latest_node_decisions(graph_run_id)
        execution_nodes: list[dict[str, Any]] = []
        review_required: list[dict[str, Any]] = []
        for state in snapshot.node_states:
            persisted = self.store.get_node(graph_run_id, state.node_id)
            if persisted is None:
                raise RuntimeError(
                    f"persisted graph run is missing node state for {state.node_id!r}"
                )
            current_attempt_id = persisted.get("current_attempt_id")
            attempt = (
                self.store.get_attempt(current_attempt_id)
                if isinstance(current_attempt_id, str) and current_attempt_id
                else None
            )
            attempt_result = attempt.get("result") if isinstance(attempt, dict) else None
            public_runtime_state = state.runtime_state
            candidate_count: int | None = None
            if (
                state.semantic_state == "pending"
                and state.runtime_state in REVIEWABLE_RUNTIME_STATES
                and isinstance(attempt_result, dict)
                and attempt_result.get("state") == "capture_ambiguous"
            ):
                public_runtime_state = "capture_ambiguous"
                raw_candidate_count = attempt_result.get("candidate_count")
                if isinstance(raw_candidate_count, int) and not isinstance(
                    raw_candidate_count, bool
                ):
                    candidate_count = raw_candidate_count
            retry_plan = retry_plans.get(state.node_id)
            execution_nodes.append(
                {
                    "node_id": state.node_id,
                    "semantic_state": state.semantic_state,
                    "runtime_state": public_runtime_state,
                    "current_attempt_id": current_attempt_id,
                    "last_lead_decision": lead_decisions.get(state.node_id),
                    **(
                        {
                            "candidate_count": candidate_count,
                        }
                        if candidate_count is not None
                        else {}
                    ),
                    "session_id": persisted.get("session_id"),
                    "turn_id": persisted.get("turn_id"),
                    "retry_pending": (
                        {
                            "resume_session": retry_plan.resume_session,
                            "session_id": retry_plan.session_id,
                            "feedback": list(retry_plan.feedback),
                        }
                        if retry_plan is not None
                        else None
                    ),
                }
            )

            if (
                state.semantic_state == "pending"
                and state.runtime_state in REVIEWABLE_RUNTIME_STATES
            ):
                authored = graph_nodes[state.node_id]
                review_required.append(
                    {
                        "node_id": state.node_id,
                        "repository": authored.repository,
                        "runtime_state": public_runtime_state,
                        "attempt_id": current_attempt_id,
                        **(
                            {
                                "candidate_count": candidate_count,
                            }
                            if candidate_count is not None
                            else {}
                        ),
                        "acceptance_criteria": list(
                            authored.task_packet.acceptance_criteria
                        ),
                    }
                )

        return {
            "graph_run_id": graph_run_id,
            "graph_state": derive_graph_state(snapshot),
            "revision": revision,
            "current_wave_id": run.get("current_wave_id"),
            "runnable_nodes": [node.node_id for node in runnable_nodes(snapshot)],
            "review_required": review_required,
            "nodes": execution_nodes,
            "authored_node_count": len(graph.nodes),
        }

    @staticmethod
    def _clean_review_locator(node_id: str, attempt_id: str) -> tuple[str, str]:
        if not isinstance(node_id, str) or not node_id.strip():
            raise ValueError("node_id must be a non-empty string")
        if not isinstance(attempt_id, str) or not attempt_id.strip():
            raise ValueError("attempt_id must be a non-empty string")
        return node_id.strip(), attempt_id.strip()

    def _review_payload(
        self,
        graph_run_id: str,
        graph: TaskGraph,
        snapshot: GraphSnapshot,
        revision: int,
        node_id: str,
        attempt_id: str,
    ) -> dict[str, Any]:
        """Validate and hydrate one exact current attempt without mutating graph state."""

        clean_node_id, clean_attempt_id = self._clean_review_locator(node_id, attempt_id)
        states = {state.node_id: state for state in snapshot.node_states}
        state = states.get(clean_node_id)
        if state is None:
            raise RuntimeError(
                f"unknown active graph node for review: {clean_node_id!r}"
            )
        is_current_review = (
            state.semantic_state == "pending"
            and state.runtime_state in REVIEWABLE_RUNTIME_STATES
        )
        is_accepted_evidence = state.semantic_state == "satisfied"
        if not (is_current_review or is_accepted_evidence):
            raise RuntimeError(
                f"node {clean_node_id!r} is not available for JIT evidence hydration"
            )

        persisted = self.store.get_node(graph_run_id, clean_node_id)
        if persisted is None:
            raise RuntimeError(
                f"persisted graph run is missing node state for {clean_node_id!r}"
            )
        if persisted.get("current_attempt_id") != clean_attempt_id:
            raise RuntimeError(
                f"stale review attempt for node {clean_node_id!r}: "
                f"current={persisted.get('current_attempt_id')!r}, "
                f"requested={clean_attempt_id!r}"
            )

        attempt = self.store.get_attempt(clean_attempt_id)
        if (
            attempt is None
            or attempt.get("graph_run_id") != graph_run_id
            or attempt.get("node_id") != clean_node_id
        ):
            raise RuntimeError(
                f"review attempt {clean_attempt_id!r} does not belong to "
                f"node {clean_node_id!r} in this graph run"
            )
        result = attempt.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(
                f"review attempt {clean_attempt_id!r} has no persisted result"
            )

        public_runtime_state = state.runtime_state
        if (
            is_current_review
            and result.get("state") == "capture_ambiguous"
        ):
            public_runtime_state = "capture_ambiguous"

        authored = next(node for node in graph.nodes if node.node_id == clean_node_id)
        return {
            "graph_run_id": graph_run_id,
            "revision": revision,
            "node_id": clean_node_id,
            "repository": authored.repository,
            "attempt_id": clean_attempt_id,
            "semantic_state": state.semantic_state,
            "runtime_state": public_runtime_state,
            "acceptance_criteria": list(authored.task_packet.acceptance_criteria),
            "result": result,
            "last_lead_decision": self.store.get_latest_node_decisions(
                graph_run_id
            ).get(clean_node_id),
        }

    def get_node_reviews(
        self,
        graph_run_id: str,
        reviews: Collection[tuple[str, str]],
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Atomically validate and hydrate a bounded set of exact review locators."""

        graph, snapshot, revision = self._snapshot(graph_run_id)
        if expected_revision is not None:
            if (
                isinstance(expected_revision, bool)
                or not isinstance(expected_revision, int)
                or expected_revision < 0
            ):
                raise ValueError("expected_revision must be a non-negative integer")
            if expected_revision != revision:
                raise RuntimeError(
                    "stale graph snapshot revision: "
                    f"expected={expected_revision}, current={revision}"
                )

        if isinstance(reviews, (str, bytes)) or not isinstance(reviews, Collection):
            raise ValueError("graph reviews must be a collection of review locators")
        locators = list(reviews)
        if not locators:
            raise ValueError("graph reviews must contain at least one review locator")
        if len(locators) > MAX_BATCH_REVIEW_HYDRATIONS:
            raise ValueError(
                "graph reviews exceed maximum batch size "
                f"{MAX_BATCH_REVIEW_HYDRATIONS}"
            )

        normalized: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for index, locator in enumerate(locators):
            if (
                not isinstance(locator, (tuple, list))
                or len(locator) != 2
            ):
                raise ValueError(
                    f"graph reviews[{index}] must be a (node_id, attempt_id) pair"
                )
            clean = self._clean_review_locator(locator[0], locator[1])
            if clean in seen:
                raise ValueError(
                    f"duplicate review locator at reviews[{index}]: "
                    f"node_id={clean[0]!r}, attempt_id={clean[1]!r}"
                )
            seen.add(clean)
            normalized.append(clean)

        # Build the complete validated set before returning any rich evidence. This keeps
        # stale/mismatched batches fail-closed rather than exposing a mixed partial result.
        payloads = [
            self._review_payload(
                graph_run_id,
                graph,
                snapshot,
                revision,
                node_id,
                attempt_id,
            )
            for node_id, attempt_id in normalized
        ]
        return {
            "graph_run_id": graph_run_id,
            "revision": revision,
            "reviews": [
                {
                    key: value
                    for key, value in payload.items()
                    if key not in {"graph_run_id", "revision"}
                }
                for payload in payloads
            ],
        }

    def reconcile_graph(
        self,
        graph_run_id: str,
        graph: TaskGraph,
        *,
        repository_names: Collection[str],
        expected_revision: int,
    ) -> dict[str, Any]:
        """Replace the authored graph and reconcile runtime state by explicit materiality.

        QiQi authors the complete next TaskGraph. Unchanged independent nodes preserve their
        semantic/runtime state and current evidence. New or directly changed nodes reset to
        pending+idle, and that invalidation propagates through descendants that depended on
        changed work. Unchanged blocked nodes remain blocked unless QiQi explicitly changes
        their authored semantics. Removed nodes are retired without deleting attempt history.
        """

        previous_graph, snapshot, revision = self._snapshot(graph_run_id)
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise ValueError("expected_revision must be a non-negative integer")
        if expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        if revision != expected_revision:
            raise RuntimeError(
                "stale graph snapshot revision: "
                f"expected {expected_revision}, current {revision}"
            )

        validate_task_graph(graph, repository_names=repository_names)
        previous_states = {state.node_id: state for state in snapshot.node_states}
        blocked_node_ids = {
            node_id
            for node_id, state in previous_states.items()
            if state.semantic_state == "blocked"
        }
        added, removed, changed, dependency_invalidated = _graph_reconciliation_sets(
            previous_graph,
            graph,
            preserved_blocked_node_ids=blocked_node_ids,
        )
        reset_ids = added | changed | dependency_invalidated
        next_states = tuple(
            (
                previous_states[node.node_id]
                if node.node_id in previous_states and node.node_id not in reset_ids
                else NodeState(node_id=node.node_id)
            )
            for node in graph.nodes
        )
        next_snapshot = GraphSnapshot(graph=graph, node_states=next_states)

        self.store.reconcile_graph(
            graph_run_id,
            previous_graph,
            next_snapshot,
            expected_revision=expected_revision,
            reset_node_ids=reset_ids,
        )
        current = self.get_graph(graph_run_id)
        previous_order = [node.node_id for node in previous_graph.nodes]
        next_order = [node.node_id for node in graph.nodes]
        reset_existing = changed | dependency_invalidated
        preserved = [
            node_id
            for node_id in next_order
            if node_id in previous_states and node_id not in reset_ids
        ]
        return {
            **current,
            "reconciliation": {
                "preserved_nodes": preserved,
                "added_nodes": [node_id for node_id in next_order if node_id in added],
                "removed_nodes": [
                    node_id for node_id in previous_order if node_id in removed
                ],
                "changed_nodes": [node_id for node_id in next_order if node_id in changed],
                "dependency_invalidated_nodes": [
                    node_id
                    for node_id in next_order
                    if node_id in dependency_invalidated
                ],
                "reset_nodes": [
                    node_id for node_id in next_order if node_id in reset_existing
                ],
            },
        }

    def _select_wave_nodes(
        self,
        graph_run_id: str,
        candidates: tuple[GraphNode, ...],
    ) -> list[tuple[GraphNode, RetryPlan | None]]:
        """Choose a deterministic conflict-free wave from currently runnable nodes.

        Pending retries sort ahead of fresh work. Until repo-local worktree isolation exists,
        only one node per repository may enter a wave. Exact RESUME sessions are also unique
        wave resources, preventing two nodes from racing the same native conversation.
        """

        retry_plans = self._pending_retry_plans(graph_run_id)
        retry_nodes = [
            node for node in candidates if node.node_id in retry_plans
        ]
        fresh_nodes = [
            node for node in candidates if node.node_id not in retry_plans
        ]
        selected: list[tuple[GraphNode, RetryPlan | None]] = []
        repositories: set[str] = set()
        sessions: set[str] = set()

        for node in [*retry_nodes, *fresh_nodes]:
            retry_plan = retry_plans.get(node.node_id)
            repository_identity = self.repository_key(node.repository)
            if repository_identity in repositories:
                continue
            if (
                retry_plan is not None
                and retry_plan.resume_session
                and retry_plan.session_id is not None
                and retry_plan.session_id in sessions
            ):
                continue
            execution_node = (
                replace(node, task_packet=retry_plan.task_packet)
                if retry_plan is not None
                else node
            )
            selected.append((execution_node, retry_plan))
            repositories.add(repository_identity)
            if (
                retry_plan is not None
                and retry_plan.resume_session
                and retry_plan.session_id is not None
            ):
                sessions.add(retry_plan.session_id)

        return selected

    async def _execute_claimed_attempt(
        self,
        wave_attempt: WaveAttempt,
        *,
        executor: RepoTaskExecutor,
        resume_executor: ResumeRepoTaskExecutor | None,
    ) -> dict[str, Any]:
        retry_plan = wave_attempt.retry_plan
        try:
            # Persist this dispatch boundary before calling the executor.
            # Prepared attempts retain their retry plans if the coordinator
            # crashes before scheduling or starting a coroutine.
            self.store.mark_attempt_dispatched(wave_attempt.attempt_id)
            if retry_plan is not None and retry_plan.resume_session:
                assert retry_plan.session_id is not None
                assert resume_executor is not None
                raw_result = await resume_executor(
                    wave_attempt.node,
                    retry_plan.session_id,
                )
            else:
                raw_result = await executor(wave_attempt.node)
            result = _validated_execution_result(raw_result)
        except asyncio.CancelledError:
            self.store.finish_attempt(
                wave_attempt.attempt_id,
                runtime_state="failed",
                result={
                    "state": "failed",
                    "agent_response": None,
                    "failure_type": "execution_cancelled",
                },
            )
            raise
        except RecoverableRepoTaskExecutionError as exc:
            self.store.finish_attempt(
                wave_attempt.attempt_id,
                runtime_state="failed",
                result={
                    "state": "failed",
                    "agent_response": None,
                    "failure_type": "executor_exception",
                    "failure_detail": str(exc)[-2400:],
                },
                session_id=exc.session_id,
            )
            raise
        except Exception as exc:
            self.store.finish_attempt(
                wave_attempt.attempt_id,
                runtime_state="failed",
                result={
                    "state": "failed",
                    "agent_response": None,
                    "failure_type": "executor_exception",
                    # The Lead may need the precise operator command after restart.
                    # For startup blockers, preserve it beyond generic tail truncation.
                    "failure_detail": (
                        exc.actionable_detail()
                        if isinstance(exc, AgentStartupBlocked)
                        else str(exc)[-2400:]
                    ),
                },
            )
            raise

        self.store.finish_attempt(
            wave_attempt.attempt_id,
            runtime_state=_persisted_runtime_state(result["state"]),
            result=result,
            session_id=result["session_id"],
            turn_id=result["turn_id"],
        )
        return result

    def _terminalize_running_wave_attempts(
        self,
        attempts: list[WaveAttempt],
        *,
        failure_type: str,
    ) -> None:
        """Fail-safe cleanup for tasks cancelled before their coroutine body runs."""

        for item in attempts:
            persisted = self.store.get_attempt(item.attempt_id)
            if persisted is None or persisted.get("runtime_state") != "running":
                continue
            self.store.finish_attempt(
                item.attempt_id,
                runtime_state="failed",
                result={
                    "state": "failed",
                    "agent_response": None,
                    "failure_type": failure_type,
                },
            )

    async def delegate_next(
        self,
        graph_run_id: str,
        *,
        executor: RepoTaskExecutor,
        resume_executor: ResumeRepoTaskExecutor | None = None,
    ) -> dict[str, Any]:
        """Execute one deterministic conflict-free runnable wave and return to QiQi.

        Independent repositories execute concurrently. Retry nodes are ordered before fresh
        work and retain the Phase-8 fresh TaskPacket / exact-session RESUME semantics. The
        wave is closed only after every claimed attempt reaches a terminal runtime state.
        """

        _, snapshot, revision = self._snapshot(graph_run_id)
        graph_state = derive_graph_state(snapshot)
        if graph_state != "ready":
            raise RuntimeError(
                f"graph is not ready for delegation: state={graph_state!r}"
            )
        candidates = runnable_nodes(snapshot)
        if not candidates:
            raise RuntimeError("graph is ready but has no runnable node")
        if not callable(executor):
            raise ValueError("repo-task executor must be callable")

        selected = self._select_wave_nodes(graph_run_id, candidates)
        if not selected:
            raise RuntimeError("graph is ready but no conflict-free runnable wave exists")
        for execution_node, retry_plan in selected:
            if execution_node.route is None:
                raise RuntimeError(
                    f"runnable node {execution_node.node_id!r} has no route for repository execution"
                )
            if (
                retry_plan is not None
                and retry_plan.resume_session
                and not callable(resume_executor)
            ):
                raise RuntimeError(
                    "retry requested RESUME but no resume executor is available"
                )

        wave_id = new_wave_id()
        # One SQLite transaction validates the authored revision and claims the
        # entire wave. If a later node cannot be claimed, the whole wave rolls
        # back: no artificial failed attempts, lost feedback, or dispatched Peer.
        attempt_ids = self.store.start_wave(
            graph_run_id,
            wave_id,
            expected_revision=revision,
            attempts=tuple(
                {
                    "node_id": node.node_id,
                    "resume_session": (
                        retry_plan.resume_session if retry_plan is not None else False
                    ),
                    "session_id": retry_plan.session_id if retry_plan else None,
                    "retry_plan": retry_plan.as_dict() if retry_plan else None,
                }
                for node, retry_plan in selected
            ),
        )
        claimed = [
            WaveAttempt(node=node, retry_plan=plan, attempt_id=attempt_id)
            for (node, plan), attempt_id in zip(selected, attempt_ids, strict=True)
        ]

        # Until each coroutine enters the dispatch boundary, retry intent
        # remains durable. A crash with prepared attempts is fail-closed:
        # never automatically assume an external worker is absent.

        tasks = [
            asyncio.create_task(
                self._execute_claimed_attempt(
                    item,
                    executor=executor,
                    resume_executor=resume_executor,
                )
            )
            for item in claimed
        ]
        try:
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._terminalize_running_wave_attempts(
                claimed,
                failure_type="execution_cancelled",
            )
            self.store.close_wave(graph_run_id, wave_id)
            raise

        self._terminalize_running_wave_attempts(
            claimed,
            failure_type="executor_exception",
        )
        self.store.close_wave(graph_run_id, wave_id)

        # Preserve the pre-Phase-9 public behavior for executor failures: runtime state is
        # terminal and inspectable, but the execution exception still propagates. We wait
        # for every sibling first so no wave can be stranded by a fast-failing child.
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome

        current = self.get_graph(graph_run_id)
        results: list[dict[str, Any]] = []
        for item, outcome in zip(claimed, outcomes, strict=True):
            assert isinstance(outcome, dict)
            results.append(
                {
                    "node_id": item.node.node_id,
                    "attempt_id": item.attempt_id,
                    "runtime_state": outcome["state"],
                    "session_id": outcome["session_id"],
                    "turn_id": outcome["turn_id"],
                    "resume_session": bool(
                        item.retry_plan is not None
                        and item.retry_plan.resume_session
                    ),
                    **(
                        {"blocker_type": outcome["blocker_type"]}
                        if "blocker_type" in outcome
                        else {}
                    ),
                    **(
                        {
                            "candidate_count": outcome["candidate_count"],
                        }
                        if outcome.get("state") == "capture_ambiguous"
                        else {}
                    ),
                }
            )
        return {
            **current,
            "wave_id": wave_id,
            "results": results,
        }

    def submit_decisions(
        self,
        graph_run_id: str,
        decisions: tuple[ReviewDecision, ...],
        *,
        expected_revision: int,
        lead_dispositions: tuple[dict[str, Any], ...] = (),
    ) -> dict[str, Any]:
        graph, snapshot, revision = self._snapshot(graph_run_id)
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise ValueError("expected_revision must be a non-negative integer")
        if expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        if revision != expected_revision:
            raise RuntimeError(
                "stale graph snapshot revision: "
                f"expected {expected_revision}, current {revision}"
            )

        graph_state = derive_graph_state(snapshot)
        if graph_state != "awaiting_review":
            raise RuntimeError(
                "graph decisions are only accepted while graph_state='awaiting_review'; "
                f"current state={graph_state!r}"
            )

        # Scheduler maps capture_ambiguous onto a reviewable settled state.
        # An ambiguous capture still has no authoritative Peer response and
        # must never be promoted to semantic ACCEPT through this lower layer.
        for decision in decisions:
            if decision.action != "accept":
                continue
            persisted = self.store.get_node(graph_run_id, decision.node_id)
            if persisted is None:
                raise ValueError(f"unknown node: {decision.node_id}")
            attempt_id = persisted.get("current_attempt_id")
            attempt = self.store.get_attempt(attempt_id) if attempt_id else None
            result = attempt.get("result") if isinstance(attempt, dict) else None
            if isinstance(result, dict) and result.get("state") == "capture_ambiguous":
                raise ValueError(
                    f"ACCEPT requires unambiguous captured Peer evidence for node "
                    f"{decision.node_id!r}; retry, replan or block instead"
                )
            # Enforce this at the runtime boundary, not just the MCP wrapper:
            # the Python API must never ACCEPT a failed/blocked/ambiguous Peer.
            if (
                not isinstance(attempt, dict)
                or attempt.get("runtime_state") != "settled"
                or not isinstance(result, dict)
                or result.get("state") != "settled"
                or not isinstance(result.get("agent_response"), str)
                or not result["agent_response"].strip()
                or not attempt.get("turn_id")
            ):
                raise ValueError(
                    f"ACCEPT requires an exact settled attempt with captured "
                    f"Peer evidence for node {decision.node_id!r}; "
                    "retry, replan or block instead"
                )

        scheduler_decisions = tuple(
            NodeDecision(node_id=decision.node_id, action=decision.action)
            for decision in decisions
        )
        updated = apply_decisions(snapshot, scheduler_decisions)

        graph_nodes = {node.node_id: node for node in graph.nodes}
        retry_plans: dict[tuple[str, str], RetryPlan] = {}
        for decision in decisions:
            if decision.action != "retry":
                continue
            persisted = self.store.get_node(graph_run_id, decision.node_id)
            if persisted is None:
                raise RuntimeError(
                    f"persisted graph run is missing node state for {decision.node_id!r}"
                )
            session_id = persisted.get("session_id") if decision.resume_session else None
            if decision.resume_session and (
                not isinstance(session_id, str) or not session_id.strip()
            ):
                raise RuntimeError(
                    f"retry requested RESUME for node {decision.node_id!r} "
                    "but no previous native session is available"
                )
            authored = graph_nodes[decision.node_id]
            retry_plans[(graph_run_id, decision.node_id)] = RetryPlan(
                task_packet=_retry_task_packet(authored.task_packet, decision.feedback),
                resume_session=decision.resume_session,
                session_id=session_id,
                feedback=decision.feedback,
            )

        # Store a record for every semantic decision, even if the caller is
        # the Python API rather than the public MCP wrapper and no Peer turn
        # was captured. The caller may supply a validated captured turn ID.
        provided = {d["node_id"]: d for d in lead_dispositions}
        if len(provided) != len(lead_dispositions):
            raise ValueError("duplicate Lead decision disposition for node")
        if set(provided) - {d.node_id for d in decisions}:
            raise ValueError("Lead dispositions include nodes without decisions")
        complete_dispositions = []
        for decision in decisions:
            current_node = self.store.get_node(graph_run_id, decision.node_id)
            if current_node is None:
                raise RuntimeError("reviewed node state disappeared")
            supplied = provided.get(decision.node_id, {})
            reason = supplied.get("reason") or f"Lead decision: {decision.action}"
            complete_dispositions.append({
                "node_id": decision.node_id,
                "attempt_id": current_node.get("current_attempt_id"),
                "turn_id": current_node.get("turn_id"),
                "action": decision.action,
                "reason": reason,
                "owner": (
                    decision.owner if decision.action in {"replan", "block"} else None
                ),
                "return_checkpoint": (
                    decision.return_checkpoint
                    if decision.action in {"replan", "block"} else None
                ),
            })

        self.store.save_snapshot_with_dispositions(
            graph_run_id,
            updated,
            expected_revision=expected_revision,
            dispositions=tuple(complete_dispositions),
            decision_node_ids=tuple(decision.node_id for decision in decisions),
            retry_plans={
                node_id: plan.as_dict()
                for (run_id, node_id), plan in retry_plans.items()
                if run_id == graph_run_id
            },
        )

        updated_states = {state.node_id: state for state in updated.node_states}
        current = self.get_graph(graph_run_id)
        outcomes: list[dict[str, Any]] = []
        for decision in decisions:
            outcome: dict[str, Any] = {
                "node_id": decision.node_id,
                "action": decision.action,
                "semantic_state": updated_states[decision.node_id].semantic_state,
            }
            if decision.action == "retry":
                plan = retry_plans[(graph_run_id, decision.node_id)]
                outcome["retry_execution"] = {
                    "resume_session": plan.resume_session,
                    "session_id": plan.session_id,
                    "feedback": list(plan.feedback),
                }
            if decision.action in {"replan", "block"}:
                outcome["defer"] = {
                    "owner": decision.owner,
                    "return_checkpoint": decision.return_checkpoint,
                }
            outcomes.append(outcome)

        return {
            **current,
            "decision_outcomes": outcomes,
            "replan_required_nodes": [
                decision.node_id
                for decision in decisions
                if decision.action == "replan"
            ],
        }
