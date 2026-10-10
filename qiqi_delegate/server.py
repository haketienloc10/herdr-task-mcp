"""Standalone QiQi MCP tools: delegation and TaskGraph; no SLP/Supervisor."""
from __future__ import annotations

import os
from functools import wraps
from typing import Any, Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from qiqi_delegate.core import build_task_packet
from qiqi_delegate.runtime import AgentStartupBlocked, DelegateRuntime, workspace_root
from qiqi_delegate.task_graph_runtime import (
    GraphRuntime, task_graph_from_payload, decisions_from_payload,
)
from qiqi_delegate.task_graph_store import GraphRuntimeStore
from qiqi_delegate.task_graph import GraphNode
from qiqi_delegate.task_request import TaskRequestStore

class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fact: str
    source: str

class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim: str
    source: str

class Context(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trusted_facts: list[Fact] = Field(default_factory=list)
    claims_to_investigate: list[Claim] = Field(default_factory=list)

class Packet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objective: str
    scope: list[str]
    acceptance_criteria: list[str]
    out_of_scope: list[str] = Field(default_factory=list)
    context: Context | None = None
    constraints: list[str] = Field(default_factory=list)
    known_unknowns: list[str] = Field(default_factory=list)

class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str
    repository: str
    route: str
    task_packet: Packet
    depends_on: list[str] = Field(default_factory=list)
    kind: Literal["repo_task"] = "repo_task"

class Graph(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nodes: list[Node] = Field(min_length=1)

class RequestSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["inline", "repo_file", "workspace_file", "peer_turn"]
    text: str | None = None
    label: str | None = None
    repository: str | None = None
    path: str | None = None
    turn_id: str | None = None

class RequirementInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    text: str
    evidence_refs: list[str]

class ReadinessAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirements: list[RequirementInput]
    blocking_unknowns: list[str]
    decision: Literal["direct", "targeted_discovery", "full_discovery", "blocked"]
    rationale: str

class ReviewLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str
    attempt_id: str

class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str
    action: Literal["accept", "retry", "replan", "block"]
    resume_session: bool = False
    feedback: list[str] = Field(
        default_factory=list,
        description="Only valid for action='retry'; not allowed for block/replan/accept.",
    )
    owner: str | None = Field(
        default=None,
        description="Required with action='block' or 'replan' to assign recovery owner.",
    )
    return_checkpoint: str | None = Field(
        default=None,
        description="Required with action='block' or 'replan' to define next review point.",
    )

runtime = DelegateRuntime(workspace_root())
task_requests = TaskRequestStore(runtime.db, runtime.root, runtime.repos)
graph_runtime = GraphRuntime(
    GraphRuntimeStore(runtime.db),
    repository_key=lambda name: str(runtime.repos()[name]),
    readiness_guard=task_requests.assert_graph_ready,
)
mcp = MCPServer(
    "QiQi Delegate (standalone)",
    instructions=(
        "Delegate repository-local tasks using a self-sufficient TaskPacket. "
        "Pass the exact repository name from workspace repos.yaml. "
        "Lead owns technical acceptance. Native result hooks are the only answer source. "
        "No terminal scraping, Supervisor, or sibling repository reads. "
        "Call workspace_info first for registered repositories and route names. "
        "For new work, register the user request and optional sources using "
        "prepare_task_request; assess readiness before implementation. "
        "Use direct delegation when requirements are actionable, and "
        "targeted/full Discovery only for blocking unknowns. "
        "A document or handoff is never required. "
        "For multiple nodes: start_graph, delegate_next, get_node_reviews, submit_decisions. "
        "A settled Peer response does not imply ACCEPT; Lead must explicitly accept it. "
        "For discovery, analysis or review, author acceptance criteria requiring relevant "
        "file:line evidence, implementation/data flow and limitations, not just a summary. "
        "Before ACCEPT, read the exact captured Peer response via get_node_reviews, "
        "check evidence against the criteria and RETRY with targeted feedback if shallow. "
        "In the final answer, preserve concrete findings and citations across Peer reports; "
        "do not replace them with a generic high-level paraphrase. "
        "If a Peer cannot start because Herdr is unavailable, review the runtime error "
        "and do not inspect or implement in the target repository directly. "
        "For block/replan decisions provide owner and return_checkpoint; "
        "feedback and resume_session are retry-only."
    ),
)

def _public_tool_errors(func):
    """Expose actionable input/runtime failures instead of 'Error executing tool'."""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except ToolError:
            raise
        except (ValueError, RuntimeError) as exc:
            detail = str(exc).strip() or type(exc).__name__
            lowered = detail.lower()
            if "agent_not_ready" in lowered:
                code = "agent_startup_blocked"
                action = (
                    "inspect the preserved Herdr startup pane using the exact "
                    "agent name and workspace ID in the error; handle any "
                    "interactive trust/auth prompt manually. After closing "
                    "the workspace and confirming the worker stopped, arrange "
                    "operator-side claim cleanup before retrying."
                )
            elif "repos.yaml" in lowered or "repository" in lowered:
                code = "repository_registry_invalid"
                action = (
                    "check repos.yaml paths against existing exact Git roots; "
                    "paths inside the workspace or to registered sibling repos "
                    "under the workspace parent are supported"
                )
            elif "route" in lowered or "agent-routing.yaml" in lowered:
                code = "routing_invalid"
                action = "check agent-routing.yaml for an exact supported route and CLI arguments"
            elif "herdr" in lowered or "agent" in lowered:
                code = "worker_runtime_failed"
                action = "check the Herdr CLI, integration status, agent pane and runtime logs"
            else:
                code = "delegation_invalid"
                action = "inspect the reported input, task state or runtime stage and retry"
            # Startup recovery may contain a long, shell-quoted workspace path.
            # Clip only diagnostic evidence, never the exact operator command.
            public_detail = (
                exc.actionable_detail()
                if isinstance(exc, AgentStartupBlocked)
                else detail[:1200]
            )
            raise ToolError(
                f"code={code}; {public_detail}; action={action}"
            ) from exc
    return wrapper


def _to_packet(value: Packet):
    return build_task_packet(**value.model_dump(exclude_none=True))


def _check_graph_routes(authored) -> None:
    """Fail before writing any TaskGraph snapshot if a route does not exist."""
    for node in authored.nodes:
        if not node.route:
            raise ValueError(f"node {node.node_id!r} has no route")
        runtime.route(node.route)


@mcp.tool()
@_public_tool_errors
async def workspace_info() -> dict[str, Any]:
    """List registered repository names, agent route names and selected Herdr session.

    Call this before start_graph or delegate_repo_task. Pass exact route_names keys, never agent kinds.
    """
    repositories = runtime.repos()
    cfg = runtime._load_yaml("agent-routing.yaml")
    routes = cfg.get("routes", {})
    if not isinstance(routes, dict):
        raise ValueError("agent-routing.yaml routes must be an object")
    summary = {}
    for name in routes:
        agent, _ = runtime.route(name)
        summary[name] = agent
    return {
        "repositories": sorted(repositories),
        "routes": summary,
        "route_names": sorted(summary),
        "route_usage": (
            "Use a key from route_names as the route argument, "
            "not an agent kind value from routes (for example, codex)."
        ),
        "herdr_session": runtime.herdr_session or "inherited/default",
        "herdr_socket_env_present": bool(os.environ.get("HERDR_SOCKET_PATH")),
    }

@mcp.tool()
@_public_tool_errors
async def prepare_task_request(
    user_request: str,
    sources: list[RequestSource] | None = None,
) -> dict[str, Any]:
    """Preserve raw user intent and optional context sources without Discovery."""
    return task_requests.create(
        user_request, [source.model_dump(exclude_none=True) for source in (sources or [])]
    )


@mcp.tool()
@_public_tool_errors
async def get_task_request(request_id: str) -> dict[str, Any]:
    """Read intent, source snapshots, assessment and current staleness."""
    return task_requests.get(request_id)


@mcp.tool()
@_public_tool_errors
async def add_task_source(
    request_id: str, expected_revision: int, source: RequestSource,
) -> dict[str, Any]:
    """Append an explicit source; invalidate previous readiness assessment."""
    return task_requests.append(
        request_id, expected_revision, source.model_dump(exclude_none=True),
    )


@mcp.tool()
@_public_tool_errors
async def submit_context_assessment(
    request_id: str, expected_revision: int, assessment: ReadinessAssessment,
) -> dict[str, Any]:
    """Record evidence-linked readiness, not LLM self-confidence."""
    return task_requests.assess(
        request_id, expected_revision, assessment.model_dump(),
    )


@mcp.tool()
@_public_tool_errors
async def delegate_discovery(
    request_id: str,
    repository_names: list[str],
    route: str,
    questions: list[str],
    mode: Literal["targeted_discovery", "full_discovery"] = "targeted_discovery",
) -> dict[str, Any]:
    """One prompt-only no-write Discovery Peer across registered roots.

    No enforced read-only sandbox. Codex --yolo remains supported.
    """
    current = task_requests.get(request_id)
    assessment = current["assessment"]
    if (assessment is None or assessment["decision"] != mode or
            not assessment["blocking_unknowns"]):
        raise ValueError("Discovery requires a matching readiness assessment with blockers")
    if not repository_names or len(set(repository_names)) != len(repository_names):
        raise ValueError("Discovery needs a unique nonempty repository list")
    roots = runtime.repos()
    if any(name not in roots for name in repository_names):
        raise ValueError("Discovery repository must be registered in repos.yaml")
    if not questions or any(not isinstance(q, str) or not q.strip() for q in questions):
        raise ValueError("Discovery questions must be nonempty")
    context_lines = [
        f"User request (verbatim): {current['user_request']}",
        *[
            f"Source {src['id']} ({src['kind']}; {src['verification']}): "
            + src["content"][:6000]
            for src in current["sources"][:8]
        ],
    ]
    packet = build_task_packet(
        objective="Investigate unanswered questions for the user request; do not implement.",
        scope=[f"Registered repository: {name}" for name in repository_names],
        acceptance_criteria=[
            "Answer discovery questions with evidence and repository-relative path:line.",
            "Separate verified code findings, reported claims and unresolved unknowns.",
            "List implementation tasks as proposals, not instructions to implement.",
            "Report whether any files changed unexpectedly.",
        ],
        constraints=["No writing, file edits, commits, installs, test runs that write files, "
                     "or other side effects.", *context_lines],
        known_unknowns=questions,
    )
    response = await runtime.delegate(
        repository=repository_names[0], route=route, packet=packet,
        discovery_repositories=tuple(repository_names),
    )
    updated = None
    if response.get("state") == "settled" and response.get("turn_id"):
        updated = task_requests.append(
            request_id, current["revision"],
            {"kind": "peer_turn", "turn_id": response["turn_id"]},
        )
    return {
        "result": response, "task_request": updated,
        "next_step": "Review evidence and submit a new readiness assessment",
    }


@mcp.tool()
@_public_tool_errors
async def delegate_repo_task(
    repository: Annotated[str, Field(description="Exact repos.yaml name")],
    route: str,
    objective: str,
    scope: list[str],
    acceptance_criteria: list[str],
    out_of_scope: list[str] | None = None,
    context: Context | None = None,
    constraints: list[str] | None = None,
    known_unknowns: list[str] | None = None,
    session_id: str | None = None,
    task_request_id: str | None = None,
    task_request_revision: int | None = None,
    requirement_refs: list[str] | None = None,
) -> dict[str, Any]:
    """START/RESUME Peer; no request_id means explicit legacy unassessed behavior."""
    if task_request_id is not None:
        if task_request_revision is None:
            raise ValueError("task_request_revision is required with task_request_id")
        current = task_requests.assert_ready(task_request_id, task_request_revision)
        required = {r["id"] for r in current["assessment"]["requirements"]}
        if not requirement_refs or any(r not in required for r in requirement_refs):
            raise ValueError("delegate_repo_task needs valid requirement_refs")
        selected_requirements = [
            r for r in current["assessment"]["requirements"]
            if r["id"] in requirement_refs
        ]
        constraints = list(constraints or []) + [
            "Original user request (verbatim): " + current["user_request"],
            "Resolved requirements: " + " | ".join(r["text"] for r in selected_requirements),
        ]
        for source in current["sources"][:8]:
            if any(source["id"] in r["evidence_refs"] for r in selected_requirements):
                constraints.append(
                    f"Source {source['id']} [{source['kind']} / "
                    f"{source['verification']}]: " + source["content"][:6000]
                )
    packet = build_task_packet(
        objective=objective, scope=scope, acceptance_criteria=acceptance_criteria,
        out_of_scope=out_of_scope, constraints=constraints, known_unknowns=known_unknowns,
        context=context.model_dump() if context else None,
    )
    return await runtime.delegate(repository=repository, route=route,
                                  packet=packet, session_id=session_id)

@mcp.tool()
@_public_tool_errors
async def start_graph(
    graph: Graph,
    task_request_id: str | None = None,
    task_request_revision: int | None = None,
    requirement_map: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Validate DAG; bound runs require direct readiness and requirement mapping."""
    authored = task_graph_from_payload(graph.model_dump(exclude_none=True))
    _check_graph_routes(authored)
    if task_request_id is not None:
        if task_request_revision is None:
            raise ValueError("task_request_revision is required with task_request_id")
        current = task_requests.assert_ready(task_request_id, task_request_revision)
        if requirement_map is None:
            raise ValueError("bound graph requires requirement_map")
        task_requests._check_map(
            current, [node.node_id for node in authored.nodes], requirement_map,
        )
    elif task_request_revision is not None or requirement_map is not None:
        raise ValueError("task_request_id is required for context-bound graph")
    result = graph_runtime.start_graph(
        authored, repository_names=runtime.repos().keys(),
    )
    if task_request_id is not None:
        task_requests.bind_graph(
            result["graph_run_id"], task_request_id, current["revision"],
            [node.node_id for node in authored.nodes], requirement_map,
        )
        result["task_request_binding"] = task_requests.graph_binding(result["graph_run_id"])
    else:
        result["task_readiness_policy"] = "legacy_unassessed"
    return result

@mcp.tool()
@_public_tool_errors
async def get_graph(graph_run_id: str) -> dict[str, Any]:
    """Return compact TaskGraph state and exact review locators."""
    result = graph_runtime.get_graph(graph_run_id)
    result["task_request_binding"] = task_requests.graph_binding(graph_run_id)
    if result["task_request_binding"] is None:
        result["task_readiness_policy"] = "legacy_unassessed"
    return result

@mcp.tool()
@_public_tool_errors
async def get_node_reviews(graph_run_id: str,
                           reviews: Annotated[list[ReviewLocator], Field(min_length=1, max_length=8)],
                           expected_revision: int | None = None) -> dict[str, Any]:
    """Load at most eight exact review locators in a single bounded call."""
    locators = [(item.node_id, item.attempt_id) for item in reviews]
    return graph_runtime.get_node_reviews(
        graph_run_id, locators, expected_revision=expected_revision
    )

async def _graph_execute(
    graph_run_id: str, node: GraphNode, session_id: str | None = None,
) -> dict:
    if not node.route:
        raise ValueError(f"node {node.node_id} has no route")
    packet = node.task_packet
    binding = task_requests.graph_binding(graph_run_id)
    if binding is not None:
        current = task_requests.assert_ready(
            binding["request_id"], binding["request_revision"],
        )
        requirement_ids = binding["requirement_map"].get(node.node_id, [])
        requirements = [
            r for r in current["assessment"]["requirements"]
            if r["id"] in requirement_ids
        ]
        if not requirements:
            raise ValueError("bound node has no current mapped requirements")
        # A TaskPacket must remain self-sufficient even in a fresh Peer session.
        # Data below is provenance-bearing context, not authority to inspect other roots.
        additions = [
            "Original user request (verbatim): " + current["user_request"],
            "Requirements for this node: " + " | ".join(r["text"] for r in requirements),
        ]
        for source in current["sources"][:8]:
            referenced = any(
                source["id"] in req["evidence_refs"] for req in requirements
            )
            if referenced:
                additions.append(
                    f"Source {source['id']} [{source['kind']} / "
                    f"{source['verification']}]: " + source["content"][:6000]
                )
        for parent in node.depends_on:
            previous = graph_runtime.store.get_node(graph_run_id, parent)
            if not previous or previous.get("semantic_state") != "satisfied":
                raise RuntimeError("upstream dependency is not semantically accepted")
            attempt = graph_runtime.store.get_attempt(
                previous["current_attempt_id"],
            )
            response = (attempt or {}).get("result", {}).get("agent_response")
            if not isinstance(response, str) or not response.strip():
                raise RuntimeError("accepted upstream evidence is missing")
            additions.append(
                f"Accepted upstream Peer report ({parent}; captured evidence): "
                + response[:6000]
            )
        payload = packet.as_dict()
        payload["constraints"] = list(payload.get("constraints", [])) + additions
        packet = build_task_packet(**payload)
    return await runtime.delegate(
        repository=node.repository, route=node.route,
        packet=packet, session_id=session_id,
    )

@mcp.tool()
@_public_tool_errors
async def delegate_next(graph_run_id: str) -> dict[str, Any]:
    """Execute one conflict-free wave; dependent nodes require ACCEPT."""
    async def start(node: GraphNode):
        return await _graph_execute(graph_run_id, node)
    async def resume(node: GraphNode, session_id: str):
        return await _graph_execute(graph_run_id, node, session_id)
    return await graph_runtime.delegate_next(graph_run_id, executor=start, resume_executor=resume)

@mcp.tool()
@_public_tool_errors
async def submit_decisions(graph_run_id: str, decisions: list[Decision],
                           expected_revision: int) -> dict[str, Any]:
    """Record Lead decisions after semantic review of exact captured Peer reports.

    For action='block' or 'replan', set owner and return_checkpoint.
    Only action='retry' permits feedback or resume_session.
    Never ACCEPT a failed runtime attempt without captured Peer evidence.
    """
    parsed = decisions_from_payload([
        d.model_dump(exclude_none=True, exclude_defaults=True) for d in decisions
    ])
    dispositions = []
    for decision in parsed:
        persisted = graph_runtime.store.get_node(graph_run_id, decision.node_id)
        if not persisted:
            raise ValueError(f"unknown node: {decision.node_id}")
        turn_id = persisted.get("turn_id")
        if decision.action == "accept":
            if not turn_id:
                raise ValueError("ACCEPT requires a captured Peer turn")
            with runtime._connect() as db:
                turn = db.execute("SELECT state FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
            if not turn or turn["state"] != "settled":
                raise ValueError("ACCEPT requires an exact successful captured Peer response")
        reason = f"Lead decision: {decision.action}"
        if decision.feedback:
            reason += "; feedback=" + " | ".join(decision.feedback)
        if decision.action in {"replan", "block"}:
            reason += f"; owner={decision.owner}; return_checkpoint={decision.return_checkpoint}"
        dispositions.append({
            "turn_id": turn_id, "action": decision.action, "reason": reason,
            "node_id": decision.node_id,
            "attempt_id": persisted.get("current_attempt_id"),
            "owner": decision.owner if decision.action in {"replan", "block"} else None,
            "return_checkpoint": (
                decision.return_checkpoint
                if decision.action in {"replan", "block"} else None
            ),
        })
    return graph_runtime.submit_decisions(
        graph_run_id, parsed, expected_revision=expected_revision,
        lead_dispositions=tuple(dispositions),
    )

@mcp.tool()
@_public_tool_errors
async def reconcile_graph(
    graph_run_id: str, graph: Graph, expected_revision: int,
    requirement_map: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Replace DAG while preserving request provenance and requirement mappings."""
    authored = task_graph_from_payload(graph.model_dump(exclude_none=True))
    _check_graph_routes(authored)
    binding = task_requests.graph_binding(graph_run_id)
    if binding is not None:
        if requirement_map is None:
            raise ValueError("reconcile_graph requires refreshed requirement_map")
        current = task_requests.assert_ready(
            binding["request_id"], binding["request_revision"],
        )
        task_requests._check_map(
            current, [node.node_id for node in authored.nodes], requirement_map,
        )
        # Changing only the requirement mapping must not silently preserve
        # an already-satisfied node whose authored task was never revised.
        previous_nodes = {
            node.node_id: node for node in graph_runtime._graph_for_run(graph_run_id).nodes
        }
        for node in authored.nodes:
            if (node.node_id in previous_nodes and
                    binding["requirement_map"].get(node.node_id) != requirement_map[node.node_id] and
                    previous_nodes[node.node_id] == node):
                raise ValueError(
                    f"changed requirements for {node.node_id!r} require a revised task packet"
                )
    elif requirement_map is not None:
        raise ValueError("cannot attach requirement_map to legacy unassessed graph")
    result = graph_runtime.reconcile_graph(
        graph_run_id, authored, repository_names=runtime.repos().keys(),
        expected_revision=expected_revision,
    )
    if binding is not None:
        task_requests.bind_graph(
            graph_run_id, binding["request_id"], current["revision"],
            [node.node_id for node in authored.nodes], requirement_map, replace=True,
        )
    return result

def main():
    mcp.run()

if __name__ == "__main__":
    main()
